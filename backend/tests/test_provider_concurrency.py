import asyncio
import json

import pytest
from sqlalchemy import select

from backend.app import provider_api, providers, runtime
from backend.app.db import Job, LlmCall, PageTask, Run, session_scope
from backend.app.provider_management import ProviderLimiter
from backend.tests.test_core import document, valid_html
from backend.tests.test_provider_management import (
    PREFIX,
    create_model,
    create_provider,
    managed_client,
    provider_patch,
    seed_default,
)


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_provider_queues_are_independent_fifo_and_cancellation_safe():
    limits = {"local": 1, "cloud": 20}
    limiter = ProviderLimiter(limits.__getitem__)
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
async def test_capacity_decrease_drains_existing_calls_before_new_admission():
    limits = {"provider": 3}
    limiter = ProviderLimiter(limits.__getitem__)
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
        limiter.provider_changed("provider")
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
    provider = create_provider(client, max_concurrency=concurrency)
    model = create_model(client, provider)
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
    limiter = ProviderLimiter()
    monkeypatch.setattr(runtime, "PROVIDER_LIMITER", limiter)
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
            sum(p["status"] == "WAITING_CAPACITY" for p in snapshot["pages"])
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
async def test_saved_provider_capacity_wakes_waiting_pages_without_restarting(
    managed_client, monkeypatch
):
    limiter = ProviderLimiter()
    for module in (runtime, provider_api):
        monkeypatch.setattr(module, "PROVIDER_LIMITER", limiter)
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
        current = managed_client.get(PREFIX + f"/providers/{provider['id']}").json()
        response = managed_client.patch(
            PREFIX + f"/providers/{provider['id']}",
            json=provider_patch(current, max_concurrency=5),
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
    limiter = ProviderLimiter()
    monkeypatch.setattr(runtime, "PROVIDER_LIMITER", limiter)
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
async def test_same_provider_models_share_capacity_and_legacy_model_limits_are_ignored(
    managed_client, monkeypatch
):
    provider = create_provider(managed_client, max_concurrency=2)
    models = [create_model(managed_client, provider, name) for name in ("a", "b")]
    from backend.app.provider_management import resolve_model_config

    with session_scope() as db:
        configs = [
            resolve_model_config(db, model["id"], "off", "provider_test")
            for model in models
        ]
    limiter = ProviderLimiter()
    monkeypatch.setattr(runtime, "PROVIDER_LIMITER", limiter)
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
                "test", None, "provider_test", resolved_config=configs[n % 2]
            )
        )
        for n in range(4)
    ]
    try:
        await eventually(lambda: len(entered) == 2)
        assert entered == ["a", "b"] and limiter.active_count == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert entered == ["a", "b", "a", "b"]


@pytest.mark.asyncio
async def test_html_retry_requeues_and_releases_provider_capacity(
    managed_client, monkeypatch
):
    limiter = ProviderLimiter()
    monkeypatch.setattr(runtime, "PROVIDER_LIMITER", limiter)
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
            pages, ["RUNNING", "WAITING_CAPACITY", "VALIDATING", "SUCCEEDED"]
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
    limiter = ProviderLimiter()
    for module in (runtime, provider_api):
        monkeypatch.setattr(module, "PROVIDER_LIMITER", limiter)
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
