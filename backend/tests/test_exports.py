"""Real PPTX transport plus persistent dependency/recovery/race tests; no LLM calls."""

import asyncio
import json
from pathlib import Path
import struct
import zipfile
import zlib

import pytest
from sqlalchemy import func, select

from backend.app import export_service, scene_runtime, scene_service
from backend.app.db import (
    LlmCall,
    Outline,
    PageScene,
    PageTask,
    PptxExport,
    SceneConversionJob,
    dumps,
    session_scope,
)
from backend.tests.test_core import document, legacy_html
from backend.tests.test_scenes import (
    scene_queue,
    seed_pages,
    minimal_scene,
    wait_until,
)  # noqa: F401
from backend.tests.test_provider_management import managed_client  # noqa: F401

RealWorker = scene_runtime.SceneWorker


def add_outline(pid, pages):
    data = document()
    data["brief"]["title"] = "中文导出测试"
    data["slides"] = [
        {**data["slides"][0], "slide_id": p.slide_id, "page_number": p.page_number}
        for p in pages
    ]
    with session_scope() as db:
        db.add(
            Outline(
                presentation_id=pid,
                revision=1,
                confirmed=True,
                content_hash=scene_service.digest(dumps(data)),
                document_json=dumps(data),
            )
        )


def save_scene(page, image_color=None):
    with session_scope() as db:
        page = db.get(PageTask, page.id)
        job = scene_service.enqueue_tx(db, page)
        job.status, job.attempt_token = "RUNNING", "seed"
        scene = minimal_scene(job)
        folder = scene_service.SCENE_ROOT / job.id
        folder.mkdir(parents=True, exist_ok=True)
        if image_color:

            def chunk(kind, data):
                return (
                    struct.pack(">I", len(data))
                    + kind
                    + data
                    + struct.pack(">I", zlib.crc32(kind + data))
                )

            raw = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\0" + bytes(image_color)))
                + chunk(b"IEND", b"")
            )
            (folder / "asset.png").write_bytes(raw)
            scene["nodes"] = [
                {
                    "id": "same-image-node",
                    "sourcePath": "main/img",
                    "kind": "image",
                    "frame": {"x": 20, "y": 20, "width": 50, "height": 50},
                    "assetId": "same-asset-id",
                }
            ]
            scene["paintOps"] = [{"op": "draw", "nodeId": "same-image-node"}]
            scene["assets"] = [
                {
                    "id": "same-asset-id",
                    "mediaType": "image/png",
                    "sha256": scene_service.digest(raw),
                    "byteLength": len(raw),
                    "width": 1,
                    "height": 1,
                    "file": "asset.png",
                    "coveredNodeIds": ["same-image-node"],
                    "bakedEffects": [],
                }
            ]
        encoded = scene_service.validate_scene(scene, job, folder)
        assert scene_service.publish_tx(
            db,
            job.id,
            "seed",
            scene,
            encoded,
            folder.relative_to(scene_service.SCENE_ROOT).as_posix(),
        )
        db.flush()
        return page.current_scene_id


def request_export(client, pid, retry=False):
    snapshot = client.get(f"/api/v1/presentations/{pid}").json()
    assert snapshot["export_source"]["can_export"], snapshot
    response = client.post(
        f"/api/v1/presentations/{pid}/exports",
        json={
            "expected_source_hash": snapshot["export_source"]["source_hash"],
            "retry": retry,
        },
    )
    assert response.status_code in {200, 202}, response.text
    return response.json()


def export_state(client, eid):
    return client.get(f"/api/v1/exports/{eid}").json()


@pytest.fixture
def export_queue(scene_queue, monkeypatch, tmp_path):
    client, worker, start = scene_queue
    monkeypatch.setattr(export_service, "SCENE_ROOT", scene_service.SCENE_ROOT)
    monkeypatch.setattr(export_service, "EXPORT_ROOT", tmp_path / "exports")
    worker.exports = []
    worker.hold_export = False
    worker.export_failure = None

    async def export_pptx(self, record, token, work, staged, cancelled):
        cls = type(self)
        cls.active += 1
        cls.peak = max(cls.peak, cls.active)
        cls.exports.append((record.id, token))
        transport = RealWorker()
        try:
            while cls.hold_export:
                await asyncio.sleep(0.01)
            if cls.export_failure:
                code, cls.export_failure = cls.export_failure, None
                raise scene_runtime.WorkerError(code, "injected export failure")
            return await transport.export_pptx(record, token, work, staged, cancelled)
        finally:
            await transport.close()
            cls.active -= 1

    monkeypatch.setattr(worker, "export_pptx", export_pptx, raising=False)
    yield client, worker, start
    worker.hold_export = False


def test_mixed_old_and_cached_pages_deduplicate_and_download(export_queue):
    client, worker, start = export_queue
    pid, _, pages = seed_pages(2)
    add_outline(pid, pages)
    cached = save_scene(pages[0])
    with session_scope() as db:
        page = db.get(PageTask, pages[0].id)
        page.html = page.html_hash = None
        page = db.get(PageTask, pages[1].id)
        page.html, page.html_hash = legacy_html(), None
    initial = request_export(client, pid)
    assert initial["status"] == "PREPARING" and initial["ready_pages"] == 1
    assert request_export(client, pid)["id"] == initial["id"]
    start()
    result = wait_until(
        lambda: (
            v
            if (v := export_state(client, initial["id"]))["status"]
            in {"SUCCEEDED", "FAILED"}
            else None
        ),
        15,
    )
    assert result["status"] == "SUCCEEDED", result
    assert worker.peak == 1 and len(worker.entered) == 1 and len(worker.exports) == 1
    assert request_export(client, pid)["id"] == initial["id"]
    assert len(worker.exports) == 1
    response = client.get(result["download_url"])
    assert response.status_code == 200 and response.content[:2] == b"PK"
    assert scene_service.digest(response.content) == result["file_sha256"]
    assert client.get(result["download_url"]).content == response.content
    assert "filename*=utf-8''" in response.headers["content-disposition"]
    with session_scope() as db:
        assert db.get(PageTask, pages[0].id).current_scene_id == cached
        assert db.get(PageTask, pages[1].id).html_hash == scene_service.digest(
            legacy_html()
        )
        assert db.scalar(select(func.count()).select_from(LlmCall)) == 0
        assert db.scalar(select(func.count()).select_from(PptxExport)) == 1


def test_scene_hit_ignores_new_environment_and_repairs_pointer(
    export_queue, monkeypatch
):
    client, _, _ = export_queue
    _, _, pages = seed_pages()
    saved = save_scene(pages[0])
    with session_scope() as db:
        db.get(PageTask, pages[0].id).current_scene_id = None
    monkeypatch.setattr(
        scene_service,
        "compiled_tailwind_css",
        lambda: pytest.fail("must not compile CSS on a Scene hit"),
    )
    monkeypatch.setattr(
        scene_service,
        "environment_key",
        lambda: pytest.fail("must not recapture a Scene hit"),
    )
    result = client.post(
        f"/api/v1/page-tasks/{pages[0].id}/scene-conversions",
        json={"expected_html_hash": pages[0].html_hash},
    )
    assert result.status_code == 200 and result.json()["scene_id"] == saved
    with session_scope() as db:
        assert db.get(PageTask, pages[0].id).current_scene_id == saved


def test_incomplete_and_stale_source_are_rejected(export_queue):
    client, _, _ = export_queue
    pid, _, pages = seed_pages(2)
    add_outline(pid, pages)
    assert (
        client.post(
            f"/api/v1/presentations/{pid}/exports",
            json={"expected_source_hash": "a" * 64},
        ).status_code
        == 409
    )
    with session_scope() as db:
        db.get(PageTask, pages[1].id).status = "FAILED"
    snapshot = client.get(f"/api/v1/presentations/{pid}").json()
    assert not snapshot["export_source"]["can_export"]
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(PptxExport)) == 0


def test_conversion_failure_and_explicit_retry_reuse_export(export_queue):
    client, worker, start = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    result = request_export(client, pid)
    worker.failures = ["CONVERSION_CONTENT_ERROR"]
    start()
    failed = wait_until(
        lambda: (
            v
            if (v := export_state(client, result["id"]))["status"] == "FAILED"
            else None
        )
    )
    assert failed["failed_page"] == 1 and not worker.exports
    assert request_export(client, pid)["status"] == "FAILED"
    retried = request_export(client, pid, retry=True)
    assert retried["id"] == result["id"]
    wait_until(lambda: export_state(client, result["id"])["status"] == "SUCCEEDED", 15)
    assert len(worker.entered) == 2 and len(worker.exports) == 1


def test_source_change_before_scene_fails_without_mixed_pages(export_queue):
    client, worker, start = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    result = request_export(client, pid)
    worker.held = True
    start()
    wait_until(lambda: worker.entered)
    with session_scope() as db:
        page = db.get(PageTask, pages[0].id)
        page.html = page.html.replace("Page 1", "Updated")
        page.html_hash = scene_service.digest(page.html)
    worker.held = False
    failed = wait_until(
        lambda: (
            v
            if (v := export_state(client, result["id"]))["status"] == "FAILED"
            else None
        )
    )
    assert failed["error_code"] == "SOURCE_CHANGED" and not worker.exports


def test_frozen_scene_survives_page_edits_and_missing_download_fails(export_queue):
    client, worker, start = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    save_scene(pages[0])
    result = request_export(client, pid)
    worker.hold_export = True
    start()
    wait_until(lambda: worker.exports)
    with session_scope() as db:
        page = db.get(PageTask, pages[0].id)
        page.html_hash = "c" * 64
        page.current_scene_id = None
    worker.hold_export = False
    done = wait_until(
        lambda: (
            v
            if (v := export_state(client, result["id"]))["status"]
            in {"SUCCEEDED", "FAILED"}
            else None
        ),
        15,
    )
    assert done["status"] == "SUCCEEDED", done
    with session_scope() as db:
        record = db.get(PptxExport, result["id"])
        (export_service.EXPORT_ROOT / record.output_relative_path).unlink()
    assert client.get(done["download_url"]).status_code == 410
    assert len(worker.exports) == 1


def test_interrupted_export_recovers_and_old_token_is_fenced(export_queue):
    client, worker, start = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    save_scene(pages[0])
    result = request_export(client, pid)
    worker.hold_export = True
    future = start()
    wait_until(lambda: worker.exports)
    old_token = worker.exports[0][1]
    future.cancel()
    wait_until(lambda: export_state(client, result["id"])["status"] == "QUEUED")
    with session_scope() as db:
        record = db.get(PptxExport, result["id"])
        assert not export_service.publish_tx(
            db, record.id, old_token, record.input_hash, {}
        )
    worker.hold_export = False
    start()
    wait_until(lambda: export_state(client, result["id"])["status"] == "SUCCEEDED", 15)
    assert worker.exports[1][1] != old_token


def test_writer_failure_can_retry_without_reconversion(export_queue):
    client, worker, start = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    save_scene(pages[0])
    result = request_export(client, pid)
    worker.export_failure = "EXPORT_CONTENT_ERROR"
    start()
    wait_until(lambda: export_state(client, result["id"])["status"] == "FAILED")
    assert not worker.entered
    assert request_export(client, pid, retry=True)["id"] == result["id"]
    wait_until(lambda: export_state(client, result["id"])["status"] == "SUCCEEDED", 15)
    assert len(worker.exports) == 2 and not worker.entered


def test_corrupt_scene_is_rejected_and_not_reconverted(export_queue):
    client, worker, _ = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    sid = save_scene(pages[0])
    with session_scope() as db:
        db.get(PageScene, sid).scene_json = "{}"
    result = request_export(client, pid)
    assert result["status"] == "FAILED" and result["failed_page"] == 1
    assert result["error_code"] == "SCENE_CORRUPT"
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(SceneConversionJob)) == 1
    assert not worker.entered and not worker.exports


def test_file_publication_failure_keeps_task_failed(export_queue, monkeypatch):
    client, _, start = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    save_scene(pages[0])

    def no_disk(*args):
        raise OSError("injected destination disk failure")

    monkeypatch.setattr(export_service, "install_result", no_disk)
    record = request_export(client, pid)
    start()
    failed = wait_until(
        lambda: (
            v
            if (v := export_state(client, record["id"]))["status"] == "FAILED"
            else None
        ),
        15,
    )
    assert not failed["download_url"]
    assert client.get(f"/api/v1/exports/{record['id']}/file").status_code == 409


def test_scene_text_and_package_order_are_independently_checked(export_queue, tmp_path):
    client, _, start = export_queue
    pid, _, pages = seed_pages(2)
    add_outline(pid, pages)
    for page in pages:
        save_scene(page)
    result = request_export(client, pid)
    start()
    done = wait_until(
        lambda: (
            v
            if (v := export_state(client, result["id"]))["status"]
            in {"SUCCEEDED", "FAILED"}
            else None
        ),
        15,
    )
    assert done["status"] == "SUCCEEDED", done
    with session_scope() as db:
        record = db.get(PptxExport, result["id"])
    path = export_service.EXPORT_ROOT / record.output_relative_path
    work = scene_service.SCENE_ROOT / ".work" / "verification"
    work.mkdir(parents=True)
    staged = export_service.stage_input(record, work)
    scene_ids = [p["sceneId"] for p in staged["pages"]]
    report = {
        "inputHash": staged["inputHash"],
        "pages": [
            {
                "sceneId": sid,
                "nativeTexts": ["injected text"] if i == 0 else [],
                "coveredNodeIds": [],
                "rasterRegions": 0,
            }
            for i, sid in enumerate(scene_ids)
        ],
    }
    tampered = tmp_path / "tampered.pptx"
    with zipfile.ZipFile(path) as original:
        contents = {name: original.read(name) for name in original.namelist()}
    # Forging both XML text and the writer's report must still fail against SceneIR.
    contents["ppt/slides/slide1.xml"] = contents["ppt/slides/slide1.xml"].replace(
        b"</p:spTree>",
        b"<p:sp><p:txBody><a:p><a:r><a:t>injected text</a:t></a:r></a:p></p:txBody></p:sp></p:spTree>",
    )
    with zipfile.ZipFile(tampered, "w") as out:
        for name, raw in contents.items():
            out.writestr(name, raw)
    with pytest.raises(export_service.ExportError, match="Scene"):
        export_service.verify_pptx(tampered, report, staged)
    with zipfile.ZipFile(path) as original:
        contents = {name: original.read(name) for name in original.namelist()}
    relpath = "ppt/_rels/presentation.xml.rels"
    contents[relpath] = (
        contents[relpath]
        .replace(b"slides/slide1.xml", b"slides/TEMP.xml")
        .replace(b"slides/slide2.xml", b"slides/slide1.xml")
        .replace(b"slides/TEMP.xml", b"slides/slide2.xml")
    )
    with zipfile.ZipFile(tampered, "w") as out:
        for name, raw in contents.items():
            out.writestr(name, raw)
    with pytest.raises(export_service.ExportError):
        export_service.verify_pptx(tampered, report, staged)


def test_asset_ids_are_scoped_to_scene_and_embedded_hashes_match(export_queue):
    client, _, start = export_queue
    pid, _, pages = seed_pages(2)
    add_outline(pid, pages)
    save_scene(pages[0], (255, 0, 0, 255))
    save_scene(pages[1], (0, 0, 255, 255))
    result = request_export(client, pid)
    start()
    done = wait_until(
        lambda: (
            v
            if (v := export_state(client, result["id"]))["status"]
            in {"SUCCEEDED", "FAILED"}
            else None
        ),
        15,
    )
    assert done["status"] == "SUCCEEDED", done
    with session_scope() as db:
        record = db.get(PptxExport, result["id"])
        source_assets = [
            json.loads(db.get(PageScene, p.current_scene_id).scene_json)["assets"][0][
                "sha256"
            ]
            for p in db.scalars(
                select(PageTask)
                .where(PageTask.presentation_id == pid)
                .order_by(PageTask.page_number)
            )
        ]
    with zipfile.ZipFile(
        export_service.EXPORT_ROOT / record.output_relative_path
    ) as archive:
        media = [
            scene_service.digest(archive.read(name))
            for name in archive.namelist()
            if name.startswith("ppt/media/") and not name.endswith("/")
        ]
    assert set(media) == set(source_assets) and len(set(media)) == 2


def test_ready_export_and_unrelated_conversion_share_single_fifo_slot(export_queue):
    client, worker, start = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    record = request_export(client, pid)
    other_pid, _, other_pages = seed_pages()
    from backend.tests.test_scenes import enqueue, get_summary

    enqueue(client, other_pages[0])
    start()
    done = wait_until(
        lambda: (
            v
            if (v := export_state(client, record["id"]))["status"]
            in {"SUCCEEDED", "FAILED"}
            else None
        ),
        15,
    )
    assert done["status"] == "SUCCEEDED", done
    assert get_summary(client, other_pid)["status"] == "SUCCEEDED"
    assert worker.peak == 1 and len(worker.entered) == 2 and len(worker.exports) == 1
