"""Queue fault tests use a controllable converter; real Chromium/Canvas is tested separately."""

import asyncio
import copy
from concurrent.futures import CancelledError
import json
import time

import pytest
from sqlalchemy import func, select

from backend.app import main, runtime, scene_api, scene_runtime, scene_service
from backend.app.db import (
    Event,
    LlmCall,
    PageScene,
    PageTask,
    Presentation,
    Run,
    SceneConversionJob,
    session_scope,
)
from backend.tests.test_core import valid_html
from backend.tests.test_provider_management import managed_client  # noqa: F401


def seed_pages(count=1):
    with session_scope() as db:
        presentation = Presentation(user_request="scene queue test", status="READY")
        db.add(presentation)
        db.flush()
        run = Run(
            presentation_id=presentation.id, kind="GENERATION", status="SUCCEEDED"
        )
        db.add(run)
        db.flush()
        pages = []
        for i in range(count):
            html = valid_html().replace("Hello", f"Page {i + 1}")
            page = PageTask(
                presentation_id=presentation.id,
                run_id=run.id,
                slide_id=f"slide-{i}",
                page_number=i + 1,
                title=f"Page {i + 1}",
                brief_json="{}",
                status="SUCCEEDED",
                html=html,
                html_hash=scene_service.digest(html),
            )
            db.add(page)
            db.flush()
            pages.append(page)
        return presentation.id, run.id, pages


def minimal_scene(job):
    return {
        "schemaVersion": "0.3.0",
        "source": {
            "htmlHash": job.html_hash,
            "cssHash": job.css_hash,
            "converterVersion": job.converter_version,
        },
        "environment": {
            "chromium": "test",
            "platform": "test",
            "locale": "zh-CN",
            "timezone": "Asia/Shanghai",
            "dpr": 1,
            "fontSignature": "a" * 64,
        },
        "page": {"width": 800, "height": 450, "background": "white"},
        "nodes": [],
        "paintOps": [],
        "assets": [],
        "textInventory": [],
        "diagnostics": [],
    }


def wait_until(predicate, timeout=8):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


def get_summary(client, presentation, index=0):
    return client.get(f"/api/v1/presentations/{presentation}").json()["pages"][index][
        "scene"
    ]


def enqueue(client, page, retry=False):
    response = client.post(
        f"/api/v1/page-tasks/{page.id}/scene-conversions",
        json={"expected_html_hash": page.html_hash, "retry": retry},
    )
    assert response.status_code in {200, 202}, response.text
    return response.json()


@pytest.fixture
def scene_queue(managed_client, monkeypatch, tmp_path):
    for module in (scene_api, scene_runtime, scene_service):
        monkeypatch.setattr(module, "SCENE_ROOT", tmp_path / "scenes")

    class ControlledWorker:
        active = peak = 0
        entered = []
        held = False
        failures = []
        starts = closes = 0

        async def start(self):
            type(self).starts += 1

        async def close(self):
            type(self).closes += 1

        async def execute(self, job, token, work, cancelled):
            cls = type(self)
            cls.active += 1
            cls.peak = max(cls.peak, cls.active)
            cls.entered.append((job.id, token))
            try:
                while cls.held:
                    if cancelled():
                        raise scene_runtime.WorkerError("CONVERSION_CANCELLED")
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.04)
                if cls.failures:
                    raise scene_runtime.WorkerError(
                        cls.failures.pop(0), "injected conversion failure"
                    )
                out = work / "out"
                out.mkdir(parents=True)
                (out / "scene.json").write_text(
                    json.dumps(minimal_scene(job)), encoding="utf-8"
                )
                return out
            finally:
                cls.active -= 1

    monkeypatch.setattr(scene_runtime, "SceneWorker", ControlledWorker)
    client = managed_client
    running = []

    def start():
        stop = client.portal.call(asyncio.Event)
        future = client.portal.start_task_soon(scene_runtime.scene_scheduler, stop)
        running.append((stop, future))
        return future

    yield client, ControlledWorker, start
    ControlledWorker.held = False
    for stop, future in running:
        client.portal.call(stop.set)
        try:
            future.result(timeout=5)
        except CancelledError:
            pass


def test_fifo_single_slot_idempotency_and_immutable_scene(scene_queue):
    client, worker, start = scene_queue
    pid, _, pages = seed_pages(3)
    queued = [enqueue(client, p) for p in pages]
    assert enqueue(client, pages[0])["job_id"] == queued[0]["job_id"]
    start()
    wait_until(lambda: get_summary(client, pid, 2)["status"] == "SUCCEEDED")
    assert [job for job, _ in worker.entered] == [s["job_id"] for s in queued]
    assert worker.peak == 1 and worker.active == 0
    summary = get_summary(client, pid)
    result = client.get(f"/api/v1/scenes/{summary['scene_id']}")
    assert result.status_code == 200 and result.json()["schemaVersion"] == "0.3.0"
    assert scene_service.digest(result.text) == summary["scene_hash"]
    assert (
        client.get(
            f"/api/v1/scenes/{summary['scene_id']}",
            headers={"If-None-Match": result.headers["etag"]},
        ).status_code
        == 304
    )
    assert (
        client.get(
            f"/api/v1/scenes/{summary['scene_id']}/assets/not-declared"
        ).status_code
        == 404
    )
    assert enqueue(client, pages[0])["scene_id"] == summary["scene_id"]
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(LlmCall)) == 0
        events = db.scalars(
            select(Event).where(Event.presentation_id == pid).order_by(Event.seq)
        ).all()
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert (
        client.post(
            f"/api/v1/page-tasks/{pages[0].id}/scene-conversions",
            json={"expected_html_hash": "f" * 64},
        ).status_code
        == 409
    )


def test_content_failure_requires_retry_without_model_calls(scene_queue):
    client, worker, start = scene_queue
    pid, _, pages = seed_pages()
    job_id = enqueue(client, pages[0])["job_id"]
    worker.failures = ["CONVERSION_CONTENT_ERROR"]
    start()
    wait_until(lambda: get_summary(client, pid)["status"] == "FAILED")
    assert len(worker.entered) == 1
    assert enqueue(client, pages[0])["status"] == "FAILED"
    assert enqueue(client, pages[0], retry=True)["job_id"] == job_id
    wait_until(lambda: get_summary(client, pid)["status"] == "SUCCEEDED")
    assert len(worker.entered) == 2 and worker.entered[0][1] != worker.entered[1][1]
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(LlmCall)) == 0


def test_browser_failure_retries_once_and_next_page_still_runs(scene_queue):
    client, worker, start = scene_queue
    pid, _, pages = seed_pages(2)
    jobs = [enqueue(client, p)["job_id"] for p in pages]
    worker.failures = ["BROWSER_CRASHED", "BROWSER_CRASHED"]
    start()
    wait_until(lambda: get_summary(client, pid, 1)["status"] == "SUCCEEDED")
    assert get_summary(client, pid)["status"] == "FAILED"
    assert [x[0] for x in worker.entered] == [jobs[0], jobs[0], jobs[1]]


def test_interrupted_job_recovers_and_stale_attempt_cannot_publish(scene_queue):
    client, worker, start = scene_queue
    pid, _, pages = seed_pages()
    queued = enqueue(client, pages[0])
    worker.held = True
    future = start()
    wait_until(lambda: len(worker.entered) == 1)
    old_token = worker.entered[0][1]
    future.cancel()
    wait_until(lambda: get_summary(client, pid)["status"] == "QUEUED")
    with session_scope() as db:
        job = db.get(SceneConversionJob, queued["job_id"])
        assert job.attempt_token is None
        scene = minimal_scene(job)
        assert not scene_service.publish_tx(
            db, job.id, old_token, scene, scene_service.canonical(scene), "unused"
        )
    worker.held = False
    start()
    wait_until(lambda: get_summary(client, pid)["status"] == "SUCCEEDED")
    assert worker.entered[1][1] != old_token
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(PageScene)) == 1


def test_cancel_and_superseded_html_fence_late_results(scene_queue):
    client, worker, start = scene_queue
    pid, rid, pages = seed_pages()
    enqueue(client, pages[0])
    worker.held = True
    start()
    wait_until(lambda: bool(worker.entered))
    client.portal.call(scene_service.cancel_conversions, rid)
    wait_until(lambda: get_summary(client, pid)["status"] == "CANCELLED")
    worker.held = False
    wait_until(lambda: worker.active == 0)
    assert get_summary(client, pid)["scene_id"] is None
    enqueue(client, pages[0], retry=True)
    wait_until(lambda: get_summary(client, pid)["status"] == "SUCCEEDED")
    with session_scope() as db:
        job = db.get(SceneConversionJob, worker.entered[0][0])
        page = db.get(PageTask, pages[0].id)
        old_scene = page.current_scene_id
        page.html_hash = "0" * 64
        job.status, job.attempt_token = "RUNNING", "late"
        scene = minimal_scene(job)
        assert not scene_service.publish_tx(
            db, job.id, "late", scene, scene_service.canonical(scene), "unused"
        )
        assert page.current_scene_id == old_scene


def test_scene_validation_rejects_bad_references_and_oversized_sources(
    scene_queue, tmp_path
):
    client, _, _ = scene_queue
    _, _, pages = seed_pages()
    queued = enqueue(client, pages[0])
    with session_scope() as db:
        job = db.get(SceneConversionJob, queued["job_id"])
        scene = minimal_scene(job)
        assert scene_service.validate_scene(scene, job, tmp_path)
        bad = copy.deepcopy(scene)
        bad["paintOps"] = [{"op": "draw", "nodeId": "absent"}]
        with pytest.raises(KeyError):
            scene_service.validate_scene(bad, job, tmp_path)
        bad = copy.deepcopy(scene)
        bad["source"]["htmlHash"] = "b" * 64
        with pytest.raises(ValueError, match="SCENE_SOURCE_MISMATCH"):
            scene_service.validate_scene(bad, job, tmp_path)


def test_html_comparison_ticket_pins_frozen_source_and_css(scene_queue, monkeypatch):
    client, _, start = scene_queue
    pid, _, pages = seed_pages()
    enqueue(client, pages[0])
    start()
    wait_until(lambda: get_summary(client, pid)["status"] == "SUCCEEDED")
    scene_id = get_summary(client, pid)["scene_id"]
    with session_scope() as db:
        db.get(PageTask, pages[0].id).html = valid_html().replace(
            "Hello", "mutated source"
        )
    ticket = client.post(
        f"/api/v1/page-tasks/{pages[0].id}/preview-tickets?scene_id={scene_id}"
    ).json()
    preview = client.get(ticket["preview_url"])
    assert (
        preview.status_code == 200
        and "Page 1" in preview.text
        and "mutated source" not in preview.text
    )
    assert 'data-tailwind-profile="tailwind-ppt-v1"' in preview.text


@pytest.mark.asyncio
async def test_real_worker_transport_restart_and_owned_process_cleanup(
    managed_client, monkeypatch, tmp_path
):
    """Exercise Python JSONL transport and actual Chromium, including dead Node recovery."""
    root = tmp_path / "real-scenes"
    monkeypatch.setattr(scene_runtime, "SCENE_ROOT", root)
    pid, _, pages = seed_pages()
    queued = enqueue(managed_client, pages[0])
    with session_scope() as db:
        job = db.get(SceneConversionJob, queued["job_id"])
    worker = scene_runtime.SceneWorker()
    processes = []
    try:
        for attempt in range(2):
            await worker.start()
            process = worker.process
            processes.append(process)
            work = root / ".work" / job.id / str(attempt)
            work.mkdir(parents=True)
            output = await worker.execute(job, str(attempt), work, lambda: False)
            scene = json.loads((output / "scene.json").read_text(encoding="utf-8"))
            assert scene_service.validate_scene(scene, job, output)
            assert any(
                n["kind"] == "text" and n["text"]["sourceText"] == "Page 1"
                for n in scene["nodes"]
            )
            if attempt == 0:
                process.kill()
                await asyncio.to_thread(process.wait, 3)
        assert processes[0].pid != processes[1].pid
    finally:
        await worker.close()
    assert all(p.poll() is not None for p in processes)
    assert worker.job is None and worker.process is None
