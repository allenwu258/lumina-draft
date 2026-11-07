import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.app import provider_api, providers, runtime
from backend.app.db import Job, LlmCall, PageTask, Presentation, Run, dumps, session_scope, utcnow
from backend.app.provider_management import ModelLimiter
from backend.tests.test_core import document, valid_html
from backend.tests.test_provider_management import (
    PREFIX,
    create_model,
    create_provider,
    managed_client,
    model_patch,
    provider_patch,
    seed_default,
)


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_model_queues_are_independent_fifo_and_cancellation_safe():
    limits = {"local": 1, "cloud": 20}
    limiter = ModelLimiter(limits.__getitem__)
    release = asyncio.Event()
    admitted = []

    async def call(provider_id, number):
        async with limiter.acquire(provider_id):
            admitted.append((provider_id, number))
            await release.wait()

    tasks = [asyncio.create_task(call("local", n)) for n in range(3)]
    tasks += [asyncio.create_task(call("cloud", n)) for n in range(20)]
    try:
        await eventually(lambda: len(admitted) == 21)
        assert [n for p, n in admitted if p == "local"] == [0]
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        await eventually(lambda: ("local", 2) in admitted)
        assert limiter.active_count == 21
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not limiter._capacities


@pytest.mark.asyncio
async def test_cancellation_while_registering_waiter_does_not_leak_capacity():
    limiter = ModelLimiter(lambda _: 1)
    release = asyncio.Event()

    async def active_call():
        async with limiter.acquire("model", run_id="active"):
            await release.wait()

    active = asyncio.create_task(active_call())
    await eventually(lambda: limiter.active_count == 1)
    capacity = limiter._capacities["model"]
    await capacity.condition.acquire()

    async def queued_call():
        async with limiter.acquire("model", run_id="queued"):
            pytest.fail("cancelled waiter must not be admitted")

    queued = asyncio.create_task(queued_call())
    await eventually(lambda: capacity.users == 2)
    queued.cancel()
    await asyncio.sleep(0)
    capacity.condition.release()
    with pytest.raises(asyncio.CancelledError):
        await queued

    release.set()
    await active
    assert not limiter._capacities


@pytest.mark.asyncio
async def test_capacity_decrease_drains_existing_calls_before_new_admission():
    limits = {"provider": 3}
    limiter = ModelLimiter(limits.__getitem__)
    release = [asyncio.Event() for _ in range(4)]
    admitted = []

    async def call(number):
        async with limiter.acquire("provider"):
            admitted.append(number)
            await release[number].wait()

    tasks = [asyncio.create_task(call(n)) for n in range(4)]
    try:
        await eventually(lambda: len(admitted) == 3)
        limits["provider"] = 1
        limiter.model_changed("provider")
        for n in range(2):
            release[n].set()
            await tasks[n]
            assert admitted == [0, 1, 2]
        release[2].set()
        await eventually(lambda: len(admitted) == 4)
        assert limiter.active_count == 1
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert limiter.active_count == 0


async def prepare_generation(client, monkeypatch, concurrency, page_count):
    provider = create_provider(client)
    model = create_model(client, provider, concurrency=concurrency)
    seed_default(model)
    assert (
        client.post(PREFIX + f"/models/{model['id']}/test").json()["status"] == "passed"
    )
    doc = document()
    template = doc["slides"][0]
    doc["slides"] = [
        dict(template, page_number=n, title=f"Slide {n}")
        for n in range(1, page_count + 1)
    ]

    async def outline_stream(**kwargs):
        yield providers.ProviderEvent("completed", {"text": json.dumps(doc)})

    monkeypatch.setattr(runtime, "stream_responses", outline_stream)
    created = client.post(
        "/api/v1/presentations", json={"user_request": "concurrency integration"}
    )
    assert created.status_code == 202, created.text
    pid, rid = created.json()["presentation_id"], created.json()["run_id"]
    await runtime.run_director(pid, rid)
    outline = client.get(f"/api/v1/presentations/{pid}").json()["latest_outline"]
    confirmed = client.post(
        f"/api/v1/presentations/{pid}/confirm",
        json={"outline_id": outline["id"], "content_hash": outline["content_hash"]},
    )
    assert confirmed.status_code == 202, confirmed.text
    rid = confirmed.json()["run_id"]
    with session_scope() as db:
        db.get(Run, rid).status = "RUNNING"
    return provider, pid, rid


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 20])
async def test_page_running_state_matches_actual_provider_requests(
    managed_client, monkeypatch, concurrency
):
    limiter = ModelLimiter()
    monkeypatch.setattr(runtime, "MODEL_LIMITER", limiter)
    provider, pid, rid = await prepare_generation(
        managed_client, monkeypatch, concurrency, 20
    )
    release = asyncio.Event()
    active = peak = 0
    entered = []

    async def stream(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.append(kwargs)
        try:
            await release.wait()
            yield providers.ProviderEvent("text_delta", valid_html())
            yield providers.ProviderEvent("completed", {"text": valid_html()})
        finally:
            active -= 1

    monkeypatch.setattr(runtime, "stream_responses", stream)
    task = asyncio.create_task(runtime.run_generation(pid, rid))
    try:
        await eventually(lambda: len(entered) == concurrency)
        snapshot = managed_client.get(f"/api/v1/presentations/{pid}").json()
        assert sum(p["status"] == "RUNNING" for p in snapshot["pages"]) == concurrency
        assert (
            sum(p["status"] == "WAITING_MODEL_CAPACITY" for p in snapshot["pages"])
            == 20 - concurrency
        )
        with session_scope() as db:
            calls = db.scalars(
                select(LlmCall).where(
                    LlmCall.presentation_id == pid, LlmCall.purpose == "page_composer"
                )
            ).all()
            assert sum(c.status == "QUEUED" for c in calls) == 20 - concurrency
    finally:
        release.set()
        await task
    assert peak == concurrency and limiter.active_count == 0
    assert all(
        p["status"] == "SUCCEEDED"
        for p in managed_client.get(f"/api/v1/presentations/{pid}").json()["pages"]
    )


@pytest.mark.asyncio
async def test_saved_model_capacity_wakes_waiting_pages_without_restarting(
    managed_client, monkeypatch
):
    limiter = ModelLimiter()
    for module in (runtime, provider_api):
        monkeypatch.setattr(module, "MODEL_LIMITER", limiter)
    provider, pid, rid = await prepare_generation(managed_client, monkeypatch, 1, 5)
    release = asyncio.Event()
    entered = []

    async def stream(**kwargs):
        entered.append(kwargs)
        await release.wait()
        yield providers.ProviderEvent("completed", {"text": valid_html()})

    monkeypatch.setattr(runtime, "stream_responses", stream)
    task = asyncio.create_task(runtime.run_generation(pid, rid))
    try:
        await eventually(lambda: len(entered) == 1)
        current_model = managed_client.get(
            PREFIX + f"/providers/{provider['id']}/models"
        ).json()["items"][0]
        response = managed_client.patch(
            PREFIX + f"/models/{current_model['id']}",
            json=model_patch(current_model, model_concurrency=5),
        )
        assert response.status_code == 200
        await eventually(lambda: len(entered) == 5)
        assert not task.done()
    finally:
        release.set()
        await task
    assert limiter.active_count == 0


@pytest.mark.asyncio
async def test_generation_cancellation_joins_waiting_and_inflight_pages(
    managed_client, monkeypatch
):
    limiter = ModelLimiter()
    monkeypatch.setattr(runtime, "MODEL_LIMITER", limiter)
    _, pid, rid = await prepare_generation(managed_client, monkeypatch, 1, 5)
    started = asyncio.Event()

    async def stream(**kwargs):
        started.set()
        await asyncio.Event().wait()
        yield providers.ProviderEvent("completed", {})

    monkeypatch.setattr(runtime, "stream_responses", stream)
    task = asyncio.create_task(runtime.run_generation(pid, rid))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert limiter.active_count == 0 and not limiter._capacities
    with session_scope() as db:
        calls = db.scalars(
            select(LlmCall).where(
                LlmCall.presentation_id == pid, LlmCall.purpose == "page_composer"
            )
        ).all()
        assert len(calls) == 5 and all(c.status == "CANCELLED" for c in calls)
        assert all(c.queue_duration_ms is not None for c in calls)


@pytest.mark.asyncio
async def test_same_provider_models_have_independent_capacity(
    managed_client, monkeypatch
):
    provider = create_provider(managed_client)
    models = [
        create_model(managed_client, provider, name, concurrency=limit)
        for name, limit in (("a", 1), ("b", 3))
    ]
    from backend.app.provider_management import resolve_model_config

    with session_scope() as db:
        configs = [
            resolve_model_config(db, model["id"], "off", "provider_test")
            for model in models
        ]
    limiter = ModelLimiter()
    monkeypatch.setattr(runtime, "MODEL_LIMITER", limiter)
    release = asyncio.Event()
    entered = []

    async def stream(**kwargs):
        entered.append(kwargs["model"])
        await release.wait()
        yield providers.ProviderEvent("completed", {"text": "OK"})

    monkeypatch.setattr(runtime, "stream_responses", stream)
    tasks = [
        asyncio.create_task(
            runtime.ProviderGateway()._stream(
                "test", None, "provider_test", resolved_config=configs[0] if n == 0 else configs[1]
            )
        )
        for n in range(4)
    ]
    try:
        await eventually(lambda: len(entered) == 4)
        assert entered.count("a") == 1 and entered.count("b") == 3
        assert limiter.active_count == 4
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert entered.count("a") == 1 and entered.count("b") == 3


@pytest.mark.asyncio
async def test_model_queue_round_robins_runs_and_times_out_waiters():
    limiter = ModelLimiter(lambda _: 1)
    entered = []
    gates = {name: asyncio.Event() for name in ("blocker", "a1", "a2", "b1")}

    async def call(name, run_id):
        async with limiter.acquire("model", run_id=run_id):
            entered.append(name)
            await gates[name].wait()

    blocker = asyncio.create_task(call("blocker", "run-0"))
    await eventually(lambda: entered == ["blocker"])
    queued = [
        asyncio.create_task(call("a1", "run-a")),
        asyncio.create_task(call("a2", "run-a")),
        asyncio.create_task(call("b1", "run-b")),
    ]
    await eventually(
        lambda: sum(len(queue) for queue in limiter._capacities["model"].waiters.values()) == 3
    )
    gates["blocker"].set()
    await eventually(lambda: len(entered) == 2)
    assert entered == ["blocker", "a1"]
    gates["a1"].set()
    await eventually(lambda: len(entered) == 3)
    assert entered == ["blocker", "a1", "b1"]
    gates["b1"].set()
    await eventually(lambda: len(entered) == 4)
    assert entered == ["blocker", "a1", "b1", "a2"]
    gates["a2"].set()
    await asyncio.gather(blocker, *queued)
    assert limiter.active_count == 0 and not limiter._capacities

    limits = {"model": 1}
    limiter = ModelLimiter(limits.__getitem__)
    release = asyncio.Event()

    async def held():
        async with limiter.acquire("model"):
            await release.wait()

    active = asyncio.create_task(held())
    await eventually(lambda: limiter.active_count == 1)
    with pytest.raises(TimeoutError):
        async with limiter.acquire("model", timeout_seconds=0.02):
            pytest.fail("timed out waiter must not be admitted")
    assert limiter.active_count == 1
    release.set()
    await active
    assert limiter.active_count == 0 and not limiter._capacities


@pytest.mark.asyncio
async def test_gateway_model_queue_timeout_is_audited(managed_client, monkeypatch):
    provider = create_provider(managed_client)
    model = create_model(managed_client, provider, concurrency=1)
    from backend.app.provider_management import resolve_model_config

    with session_scope() as db:
        config = resolve_model_config(db, model["id"], "off", "provider_test")
    limiter = ModelLimiter()
    monkeypatch.setattr(runtime, "MODEL_LIMITER", limiter)
    monkeypatch.setattr(runtime, "settings", replace(runtime.settings, model_queue_timeout_seconds=0.02))
    release = asyncio.Event()
    entered = asyncio.Event()

    async def stream(**kwargs):
        entered.set()
        await release.wait()
        yield providers.ProviderEvent("completed", {"text": "OK"})

    monkeypatch.setattr(runtime, "stream_responses", stream)
    gateway = runtime.ProviderGateway()
    active = asyncio.create_task(gateway._stream("test", None, "provider_test", resolved_config=config))
    await asyncio.wait_for(entered.wait(), timeout=2)
    queued = asyncio.create_task(gateway._stream("test", None, "provider_test", resolved_config=config))
    with pytest.raises(providers.ProviderError) as error:
        await queued
    assert error.value.code == "CALL_QUEUE_TIMEOUT"
    with session_scope() as db:
        calls = db.scalars(select(LlmCall).order_by(LlmCall.started_at)).all()
        failed = next(call for call in calls if call.error_code == "CALL_QUEUE_TIMEOUT")
        assert failed.status == "FAILED"
        assert failed.queue_reason == "MODEL_CAPACITY"
        assert failed.queue_duration_ms is not None and failed.admitted_at is None
    release.set()
    assert await active == "OK"
    assert limiter.active_count == 0


@pytest.mark.asyncio
async def test_scheduler_runs_models_concurrently_and_fairly(managed_client, monkeypatch):
    provider = create_provider(managed_client)
    models = [create_model(managed_client, provider, f"scheduler-{name}") for name in ("a", "b")]
    presentations = []
    with session_scope() as db:
        for model_id in (models[0]["id"], models[0]["id"], models[1]["id"]):
            presentation = Presentation(
                user_request="scheduler fairness",
                status="PLANNING",
                config_snapshot_json=dumps({
                    "resolved_routes": {"ppt_outline": {"model_id": model_id}}
                }),
            )
            db.add(presentation)
            db.flush()
            run = Run(presentation_id=presentation.id, kind="DIRECTOR", status="QUEUED")
            db.add(run)
            db.flush()
            job = Job(run_id=run.id, presentation_id=presentation.id, kind="DIRECTOR", status="QUEUED")
            db.add(job)
            db.flush()
            presentations.append((presentation.id, run.id, job.id, model_id))

    monkeypatch.setattr(runtime, "settings", replace(runtime.settings, max_active_runs=3, scheduler_queue_timeout_seconds=60))
    started = []
    release = asyncio.Event()

    async def fake_director(presentation_id, run_id):
        started.append((presentation_id, run_id))
        await release.wait()
        with session_scope() as db:
            db.get(Run, run_id).status = "SUCCEEDED"
            db.get(Presentation, presentation_id).status = "WAITING_CONFIRMATION"

    monkeypatch.setattr(runtime, "run_director", fake_director)
    stop = asyncio.Event()
    scheduler_task = asyncio.create_task(runtime.scheduler(stop))
    try:
        await eventually(lambda: len(started) == 3)
        assert len(started) == 3
        selected_models = {model_id for pid, _, _, model_id in presentations if pid in {row[0] for row in started}}
        assert len(selected_models) == 2
        release.set()
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(scheduler_task, timeout=5)
    with session_scope() as db:
        assert all(db.get(Job, job_id).status == "SUCCEEDED" for _, _, job_id, _ in presentations)


@pytest.mark.asyncio
async def test_scheduler_queue_timeout_marks_run_and_generation_pages(managed_client, monkeypatch):
    with session_scope() as db:
        presentation = Presentation(user_request="queued timeout", status="GENERATING")
        db.add(presentation)
        db.flush()
        run = Run(presentation_id=presentation.id, kind="GENERATION", status="QUEUED")
        db.add(run)
        db.flush()
        page = PageTask(
            presentation_id=presentation.id,
            run_id=run.id,
            slide_id="slide",
            page_number=1,
            title="Queued page",
            brief_json="{}",
            status="PENDING",
        )
        db.add(page)
        job = Job(run_id=run.id, presentation_id=presentation.id, kind="GENERATION", status="QUEUED")
        db.add(job)
        db.flush()
        presentation_id, run_id, job_id, page_id = presentation.id, run.id, job.id, page.id

    monkeypatch.setattr(runtime, "settings", replace(runtime.settings, scheduler_queue_timeout_seconds=0))
    stop = asyncio.Event()
    scheduler_task = asyncio.create_task(runtime.scheduler(stop))
    try:
        await eventually(lambda: _job_status(job_id) == "FAILED")
    finally:
        stop.set()
        await asyncio.wait_for(scheduler_task, timeout=3)
    with session_scope() as db:
        assert db.get(Job, job_id).error_code == "JOB_QUEUE_TIMEOUT"
        assert db.get(Run, run_id).error_code == "JOB_QUEUE_TIMEOUT"
        failed_page = db.get(PageTask, page_id)
        assert failed_page.status == "FAILED" and failed_page.error_code == "JOB_QUEUE_TIMEOUT"
        assert db.get(Presentation, presentation_id).status == "PARTIAL_FAILED"


@pytest.mark.asyncio
async def test_restart_requeue_resets_scheduler_wait_timeout(managed_client, monkeypatch):
    with session_scope() as db:
        presentation = Presentation(user_request="restart timeout", status="PLANNING")
        db.add(presentation)
        db.flush()
        run = Run(presentation_id=presentation.id, kind="DIRECTOR", status="RUNNING")
        db.add(run)
        db.flush()
        job = Job(
            run_id=run.id,
            presentation_id=presentation.id,
            kind="DIRECTOR",
            status="RUNNING",
            created_at=utcnow() - timedelta(hours=1),
        )
        db.add(job)
        db.flush()
        presentation_id, run_id, job_id = presentation.id, run.id, job.id

    monkeypatch.setattr(
        runtime,
        "settings",
        replace(runtime.settings, max_active_runs=1, scheduler_queue_timeout_seconds=1),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_director(current_presentation_id, current_run_id):
        entered.set()
        await release.wait()
        with session_scope() as db:
            db.get(Run, current_run_id).status = "SUCCEEDED"
            db.get(Presentation, current_presentation_id).status = "WAITING_CONFIRMATION"

    monkeypatch.setattr(runtime, "run_director", fake_director)
    stop = asyncio.Event()
    scheduler_task = asyncio.create_task(runtime.scheduler(stop))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        with session_scope() as db:
            job = db.get(Job, job_id)
            assert job.status == "RUNNING"
            assert job.created_at < job.queued_at
            assert db.get(Run, run_id).status == "RUNNING"
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(scheduler_task, timeout=3)
    with session_scope() as db:
        assert db.get(Job, job_id).status == "SUCCEEDED"
        assert db.get(Run, run_id).status == "SUCCEEDED"


def _job_status(job_id: str) -> str | None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        return job.status if job else None


@pytest.mark.asyncio
async def test_html_retry_requeues_and_releases_model_capacity(
    managed_client, monkeypatch
):
    limiter = ModelLimiter()
    monkeypatch.setattr(runtime, "MODEL_LIMITER", limiter)
    _, pid, rid = await prepare_generation(managed_client, monkeypatch, 1, 2)
    calls = 0

    async def stream(**kwargs):
        nonlocal calls
        calls += 1
        yield providers.ProviderEvent(
            "completed", {"text": "invalid" if calls == 1 else valid_html()}
        )

    monkeypatch.setattr(runtime, "stream_responses", stream)
    await runtime.run_generation(pid, rid)
    assert calls == 3 and limiter.active_count == 0
    with session_scope() as db:
        pages = db.scalars(select(PageTask).where(PageTask.run_id == rid)).all()
        assert sorted(page.attempt for page in pages) == [1, 2]
        assert all(page.status == "SUCCEEDED" for page in pages)


@pytest.mark.asyncio
async def test_restart_requeues_all_unfinished_page_states(managed_client, monkeypatch):
    _, pid, rid = await prepare_generation(managed_client, monkeypatch, 1, 4)
    with session_scope() as db:
        pages = db.scalars(
            select(PageTask)
            .where(PageTask.run_id == rid)
            .order_by(PageTask.page_number)
        ).all()
        for page, status in zip(
            pages, ["RUNNING", "WAITING_MODEL_CAPACITY", "VALIDATING", "SUCCEEDED"]
        ):
            page.status = status
        db.scalar(select(Job).where(Job.run_id == rid)).status = "RUNNING"
        for status in ("QUEUED", "RUNNING"):
            db.add(
                LlmCall(
                    presentation_id=pid,
                    purpose="page_composer",
                    model="test",
                    status=status,
                )
            )
    stop = asyncio.Event()
    stop.set()
    await runtime.scheduler(stop)
    with session_scope() as db:
        pages = db.scalars(
            select(PageTask)
            .where(PageTask.run_id == rid)
            .order_by(PageTask.page_number)
        ).all()
        assert [page.status for page in pages] == [
            "PENDING",
            "PENDING",
            "PENDING",
            "SUCCEEDED",
        ]
        assert db.scalar(select(Job).where(Job.run_id == rid)).status == "QUEUED"
        calls = db.scalars(
            select(LlmCall).where(
                LlmCall.presentation_id == pid, LlmCall.error_code == "CALL_INTERRUPTED"
            )
        ).all()
        assert len(calls) == 2 and all(call.finished_at for call in calls)


@pytest.mark.asyncio
async def test_disabling_provider_rejects_waiters_while_inflight_call_finishes(
    managed_client, monkeypatch
):
    limiter = ModelLimiter()
    for module in (runtime, provider_api):
        monkeypatch.setattr(module, "MODEL_LIMITER", limiter)
    provider, pid, rid = await prepare_generation(managed_client, monkeypatch, 1, 3)
    release = asyncio.Event()
    entered = []

    async def stream(**kwargs):
        entered.append(kwargs)
        await release.wait()
        yield providers.ProviderEvent("completed", {"text": valid_html()})

    monkeypatch.setattr(runtime, "stream_responses", stream)
    task = asyncio.create_task(runtime.run_generation(pid, rid))
    try:
        await eventually(lambda: len(entered) == 1)
        current = managed_client.get(PREFIX + f"/providers/{provider['id']}").json()
        assert (
            managed_client.patch(
                PREFIX + f"/providers/{provider['id']}",
                json=provider_patch(current, enabled=False),
            ).status_code
            == 200
        )

        def failed_count():
            with session_scope() as db:
                return (
                    len(
                        db.scalars(
                            select(PageTask).where(
                                PageTask.run_id == rid, PageTask.status == "FAILED"
                            )
                        ).all()
                    )
                    == 2
                )

        await eventually(failed_count)
        assert len(entered) == 1 and not task.done()
    finally:
        release.set()
        await task
    assert not limiter._capacities
