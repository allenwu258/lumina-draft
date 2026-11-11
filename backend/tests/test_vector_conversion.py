"""Version transitions keep old display usable without treating it as a new export dependency."""

import copy
import json
import struct
import zlib

import pytest
from sqlalchemy import func, select

from backend.app import scene_service, scene_runtime, export_service
from backend.app.db import (
    PageScene,
    PageTask,
    SceneConversionJob,
    PptxExport,
    session_scope,
)
from backend.tests.test_scenes import (
    scene_queue,
    seed_pages,
    enqueue,
    get_summary,
    wait_until,
    minimal_scene,
)  # noqa: F401
from backend.tests.test_exports import (
    export_queue,
    save_scene,
    add_outline,
    request_export,
    export_state,
)  # noqa: F401
from backend.tests.test_provider_management import managed_client  # noqa: F401


def legacy_scene(page):
    sid = save_scene(page)
    with session_scope() as db:
        row = db.get(PageScene, sid)
        job = db.get(SceneConversionJob, row.job_id)
        job.converter_version = "dom-canvas-0.3.0"
        job.input_hash = scene_service.digest("legacy:" + page.id)
        scene = json.loads(row.scene_json)
        scene["schemaVersion"] = row.schema_version = "0.3.0"
        scene["source"]["converterVersion"] = job.converter_version
        row.scene_json = scene_service.canonical(scene)
        row.scene_hash = scene_service.digest(row.scene_json)
    return sid


def test_upgrade_retains_old_display_freezes_css_and_requires_explicit_retry(
    export_queue, monkeypatch
):
    client, worker, start = export_queue
    pid, _, pages = seed_pages()
    old_scene = legacy_scene(pages[0])
    monkeypatch.setattr(
        scene_service,
        "compiled_tailwind_css",
        lambda: "new css must not replace old css",
    )
    initial = get_summary(client, pid)
    assert initial["scene_id"] == old_scene and not initial["up_to_date"]
    assert initial["status"] == "NOT_REQUESTED"
    with session_scope() as db:
        assert (
            db.scalar(select(func.count()).select_from(SceneConversionJob)) == 1
        )  # GET never enqueues.
    queued = enqueue(client, pages[0])
    assert queued["scene_id"] == old_scene and queued["status"] == "QUEUED"
    assert not queued["up_to_date"] and export_service.exportable_ref(queued) is None
    assert enqueue(client, pages[0])["job_id"] == queued["job_id"]
    with session_scope() as db:
        assert (
            db.get(SceneConversionJob, queued["job_id"]).css
            == db.get(SceneConversionJob, db.get(PageScene, old_scene).job_id).css
        )
    worker.failures = ["CONVERSION_CONTENT_ERROR"]
    start()
    failed = wait_until(
        lambda: (s if (s := get_summary(client, pid))["status"] == "FAILED" else None)
    )
    assert (
        failed["scene_id"] == old_scene and failed["conversion"]["status"] == "FAILED"
    )
    assert enqueue(client, pages[0])["status"] == "FAILED" and len(worker.entered) == 1
    enqueue(client, pages[0], retry=True)
    result = wait_until(
        lambda: (s if (s := get_summary(client, pid))["up_to_date"] else None)
    )
    assert result["scene_id"] != old_scene
    with session_scope() as db:
        assert db.get(PageScene, old_scene).schema_version == "0.3.0"
        assert db.scalar(select(func.count()).select_from(PageScene)) == 2


def test_missing_html_uses_legacy_scene_with_warning_and_changed_html_cannot_display_it(
    export_queue,
):
    client, _, _ = export_queue
    pid, _, pages = seed_pages()
    old = legacy_scene(pages[0])
    with session_scope() as db:
        page = db.get(PageTask, pages[0].id)
        page.html = page.html_hash = None
    result = enqueue(client, pages[0])
    assert result["scene_id"] == old and not result["up_to_date"]
    assert result["error_code"] == "SOURCE_HTML_MISSING"
    assert export_service.exportable_ref(result)["scene_id"] == old
    with session_scope() as db:
        page = db.get(PageTask, pages[0].id)
        page.html = pages[0].html.replace("Page 1", "New content")
        page.html_hash = scene_service.digest(page.html)
        assert scene_service.summary(db, page)["scene_id"] is None


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING"])
def test_restart_replaces_old_version_job_and_fences_old_token(scene_queue, state):
    client, worker, start = scene_queue
    pid, _, pages = seed_pages()
    old_id = enqueue(client, pages[0])["job_id"]
    with session_scope() as db:
        old = db.get(SceneConversionJob, old_id)
        old.status, old.converter_version = state, "dom-canvas-0.3.0"
        old.css, old.css_hash = "/* frozen old profile */", scene_service.digest(
            "/* frozen old profile */"
        )
        old.input_hash, old.attempt_token = "b" * 64, "old-token"
    start()
    result = wait_until(
        lambda: (s if (s := get_summary(client, pid))["up_to_date"] else None)
    )
    assert result["job_id"] != old_id and old_id not in [j for j, _ in worker.entered]
    with session_scope() as db:
        old = db.get(SceneConversionJob, old_id)
        replacement = db.get(SceneConversionJob, result["job_id"])
        assert (
            old.status == "CANCELLED" and old.error_code == "CONVERTER_VERSION_CHANGED"
        )
        assert replacement.css == old.css and replacement.source_html == old.source_html
        scene = minimal_scene(old)
        assert not scene_service.publish_tx(
            db, old_id, "old-token", scene, scene_service.canonical(scene), "unused"
        )


def png(width, height):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0\xff" * width) * height))
        + chunk(b"IEND", b"")
    )


def test_svg_fallback_hash_ownership_and_text_targets_are_validated(
    scene_queue, tmp_path
):
    client, _, _ = scene_queue
    _, _, pages = seed_pages()
    queued = enqueue(client, pages[0])
    with session_scope() as db:
        job = db.get(SceneConversionJob, queued["job_id"])
        scene = minimal_scene(job)
        scene["nodes"] = [
            {
                "id": "diagram",
                "kind": "image",
                "sourcePath": "main/1",
                "assetId": "svg",
                "frame": {"x": 20, "y": 20, "width": 2, "height": 1},
            }
        ]
        scene["paintOps"] = [{"op": "draw", "nodeId": "diagram"}]
        scene["textInventory"] = [
            {
                "sourcePath": "main/1/0/0",
                "sourceText": "Hello",
                "nodeId": "diagram",
                "reason": "svg-asset",
                "target": {"kind": "asset", "nodeId": "diagram", "assetId": "svg"},
            }
        ]
        scene["assets"] = []
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="1"><text>Hello</text></svg>'
        for aid, mime, raw in [
            ("svg", "image/svg+xml", svg),
            ("png", "image/png", png(2, 1)),
        ]:
            filename = aid + (".svg" if aid == "svg" else ".png")
            (tmp_path / filename).write_bytes(raw)
            scene["assets"].append(
                {
                    "id": aid,
                    "mediaType": mime,
                    "file": filename,
                    "sha256": scene_service.digest(raw),
                    "byteLength": len(raw),
                    "width": 2,
                    "height": 1,
                    "coveredNodeIds": ["diagram"],
                    "bakedEffects": [],
                    **({"fallbackAssetId": "png"} if aid == "svg" else {}),
                }
            )
        assert scene_service.validate_scene(scene, job, tmp_path)
        for mutate, code in [
            (
                lambda s: s["assets"][0].update(fallbackAssetId="svg"),
                "SCENE_ASSET_FALLBACK",
            ),
            (
                lambda s: s["textInventory"][0]["target"].update(assetId="png"),
                "SCENE_INVENTORY_TARGET",
            ),
            (
                lambda s: s["assets"][0].update(coveredNodeIds=[]),
                "SCENE_ASSET_FALLBACK",
            ),
        ]:
            bad = copy.deepcopy(scene)
            mutate(bad)
            with pytest.raises(ValueError, match=code):
                scene_service.validate_scene(bad, job, tmp_path)
        (tmp_path / "svg.svg").write_bytes(b"corrupt primary; do not use fallback")
        with pytest.raises(ValueError, match="SCENE_ASSET_HASH"):
            scene_service.validate_scene(scene, job, tmp_path)


def test_export_target_creates_new_identity_and_hides_old_latest(export_queue):
    client, _, _ = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    save_scene(pages[0])
    old = request_export(client, pid)
    with session_scope() as db:
        record = db.get(PptxExport, old["id"])
        record.export_version, record.request_hash = "scene-pptx-0.1.0", "c" * 64
    snapshot = client.get(f"/api/v1/presentations/{pid}").json()
    assert snapshot["latest_export"] is None
    new = request_export(client, pid)
    assert new["id"] != old["id"]
    assert request_export(client, pid)["id"] == new["id"]


def test_failed_upgrade_cannot_silently_export_old_scene(export_queue):
    client, worker, start = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    old = legacy_scene(pages[0])
    worker.failures = ["CONVERSION_CONTENT_ERROR"]
    record = request_export(client, pid)
    assert record["status"] == "PREPARING" and record["ready_pages"] == 0
    start()
    result = wait_until(
        lambda: (
            r
            if (r := export_state(client, record["id"]))["status"] == "FAILED"
            else None
        )
    )
    assert result["error_code"] == "SCENE_PREPARATION_FAILED" and worker.exports == []
    assert get_summary(client, pid)["scene_id"] == old


def test_source_less_legacy_export_keeps_compatibility_warning(export_queue):
    client, worker, start = export_queue
    pid, _, pages = seed_pages()
    add_outline(pid, pages)
    old = legacy_scene(pages[0])
    with session_scope() as db:
        page = db.get(PageTask, pages[0].id)
        page.html = page.html_hash = None
    record = request_export(client, pid)
    assert record["status"] == "QUEUED" and record["ready_pages"] == 1
    start()
    result = wait_until(
        lambda: (
            r
            if (r := export_state(client, record["id"]))["status"]
            in {"SUCCEEDED", "FAILED"}
            else None
        ),
        15,
    )
    assert result["status"] == "SUCCEEDED", result
    assert any("来源 HTML 缺失" in warning for warning in result["warnings"])
    assert worker.entered == [] and get_summary(client, pid)["scene_id"] == old
