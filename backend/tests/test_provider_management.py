import asyncio
import json
import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app import db as database, main, provider_api, providers, runtime
from backend.app.db import (
    ApiCredential,
    Base,
    BusinessRoute,
    BusinessRouteOverride,
    GenerationSettingsRecord,
    LlmCall,
    Presentation,
    dumps,
    loads,
    session_scope,
    utcnow,
)
from backend.app.provider_management import ProviderLimiter, resolve_business_route
from backend.tests.test_core import document, valid_html

PREFIX = "/api/v1/settings/llm"


@pytest.fixture
def managed_client(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite:///" + (tmp_path / "providers.db").as_posix(),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(bind=engine, expire_on_commit=False, autoflush=False),
    )
    secrets = {}
    for module in (provider_api, main, runtime):
        monkeypatch.setattr(module, "read_secret", lambda ref: secrets.get(ref))
    for module in (provider_api,):
        monkeypatch.setattr(
            module, "save_secret", lambda ref, value: secrets.__setitem__(ref, value)
        )
    monkeypatch.setattr(
        provider_api, "delete_secret", lambda ref: secrets.pop(ref, None) is not None
    )
    monkeypatch.setattr(main, "init_db", lambda: None)

    async def idle_scheduler(stop):
        await stop.wait()

    monkeypatch.setattr(main, "scheduler", idle_scheduler)

    async def fake_stream(**kwargs):
        if (kwargs.get("json_schema") or {}).get("properties", {}).get("ok"):
            text = '{"ok": true}'
        elif kwargs.get("json_schema"):
            text = json.dumps(document())
        elif kwargs.get("max_output_tokens"):
            text = "OK"
        else:
            text = valid_html()
        yield providers.ProviderEvent("text_delta", text)
        yield providers.ProviderEvent(
            "completed",
            {
                "text": text,
                "response_id": "response-test",
                "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            },
        )

    monkeypatch.setattr(runtime, "stream_responses", fake_stream)
    with session_scope() as db:
        db.add(GenerationSettingsRecord(id="default"))
        db.add_all(
            [
                BusinessRouteOverride(code="ppt_outline"),
                BusinessRouteOverride(code="ppt_html"),
            ]
        )
    with TestClient(main.app) as client:
        client.secrets = secrets
        yield client
    engine.dispose()


def create_provider(client, **overrides):
    body = {
        "name": "Team Provider",
        "base_url": "https://example.com/v1",
        "auth_mode": "none",
        **overrides,
    }
    response = client.post(PREFIX + "/providers", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def create_model(client, provider, remote="model-a"):
    response = client.post(
        PREFIX + f"/providers/{provider['id']}/models",
        json={
            "remote_model_id": remote,
            "display_name": remote.upper(),
            "context_window_tokens": 128000,
            "max_concurrency": 2,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def seed_default(model):
    with session_scope() as db:
        db.add(
            BusinessRoute(code="default", model_id=model["id"], reasoning_tier="high")
        )


def provider_patch(provider, **updates):
    return (
        {
            key: provider[key]
            for key in (
                "name",
                "protocol",
                "base_url",
                "auth_mode",
                "allow_private_http",
                "max_concurrency",
                "enabled",
            )
        }
        | {"expected_revision": provider["revision"]}
        | updates
    )


def model_patch(model, **updates):
    return (
        {
            key: model[key]
            for key in (
                "remote_model_id",
                "display_name",
                "context_window_tokens",
                "max_output_tokens",
                "enabled",
            )
        }
        | {"expected_revision": model["revision"]}
        | updates
    )


def test_provider_model_crud_validation_and_revision(managed_client):
    client = managed_client
    provider = create_provider(client)
    assert not provider["credential_configured"]
    assert "credential_ref" not in provider
    response = client.patch(
        PREFIX + f"/providers/{provider['id']}",
        json=provider_patch(provider, name="Renamed", max_concurrency=6),
    )
    assert response.status_code == 200
    assert (
        client.patch(
            PREFIX + f"/providers/{provider['id']}", json=provider_patch(provider)
        ).status_code
        == 409
    )
    model = create_model(client, provider)
    assert [tier["code"] for tier in model["reasoning_tiers"]] == [
        "off",
        "low",
        "medium",
        "high",
    ]
    assert model["capability_source"] == "assumed"
    assert (
        client.post(
            PREFIX + f"/providers/{provider['id']}/models",
            json={"remote_model_id": "model-a"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            PREFIX + f"/providers/{provider['id']}/models",
            json={"remote_model_id": "   "},
        ).status_code
        == 422
    )
    updated = client.patch(
        PREFIX + f"/models/{model['id']}",
        json=model_patch(model, context_window_tokens=64000),
    ).json()
    assert updated["context_window_tokens"] == 64000
    assert updated["metadata_source"] == "manual"
    assert (
        client.patch(
            PREFIX + f"/models/{model['id']}", json=model_patch(model)
        ).status_code
        == 409
    )
    assert (
        client.delete(
            PREFIX + f"/models/{model['id']}?expected_revision={updated['revision']}"
        ).status_code
        == 200
    )
    current = client.get(PREFIX + f"/providers/{provider['id']}").json()
    assert current["models"] == []
    replacement = create_model(client, provider)
    assert replacement["id"] != model["id"]
    assert (
        client.delete(
            PREFIX
            + f"/models/{replacement['id']}?expected_revision={replacement['revision']}"
        ).status_code
        == 200
    )
    current = client.get(PREFIX + f"/providers/{provider['id']}").json()
    assert (
        client.delete(
            PREFIX
            + f"/providers/{provider['id']}?expected_revision={current['revision']}"
        ).status_code
        == 200
    )
    assert client.get(PREFIX + f"/providers/{provider['id']}").status_code == 404


@pytest.mark.parametrize(
    "url,allowed,status",
    [
        ("http://192.168.1.2:8000/v1", False, 422),
        ("http://192.168.1.2:8000/v1", True, 201),
        ("http://8.8.8.8/v1", True, 422),
        ("http://169.254.169.254/v1", True, 422),
        ("https://user:secret@example.com/v1", False, 422),
        ("https://example.com/v1?key=x", False, 422),
        ("https://example.com:invalid/v1", False, 422),
        ("https://example.com/v1/responses", False, 422),
    ],
)
def test_provider_endpoint_validation(managed_client, url, allowed, status):
    response = managed_client.post(
        PREFIX + "/providers",
        json={
            "name": "Test",
            "base_url": url,
            "auth_mode": "none",
            "allow_private_http": allowed,
        },
    )
    assert response.status_code == status


def test_credentials_are_write_only_rotated_and_revoked(managed_client):
    client = managed_client
    provider = create_provider(client, auth_mode="bearer", api_key="old-secret")
    model = create_model(client, provider)
    seed_default(model)
    with session_scope() as db:
        old_config = resolve_business_route(db, "default")
    provider = client.get(PREFIX + f"/providers/{provider['id']}").json()
    response = client.put(
        PREFIX + f"/providers/{provider['id']}/credential",
        json={"expected_revision": provider["revision"], "api_key": "new-secret"},
    )
    assert response.status_code == 200
    assert response.json()["credential_version"] == 2
    assert "secret" not in response.text
    with session_scope() as db:
        assert db.get(ApiCredential, old_config["credential_id"]).status == "retired"
    assert client.secrets[old_config["credential_ref"]] == "old-secret"
    provider = client.get(PREFIX + f"/providers/{provider['id']}").json()
    assert (
        client.delete(
            PREFIX
            + f"/providers/{provider['id']}/credential?expected_revision={provider['revision']}"
        ).status_code
        == 200
    )
    assert client.secrets == {}
    with session_scope() as db:
        assert {row.status for row in db.scalars(select(ApiCredential)).all()} == {
            "revoked"
        }
    response = client.post(
        PREFIX + "/providers",
        json={
            "name": "Invalid",
            "base_url": "https://example.com",
            "api_key": "sensitive-value",
            "max_concurrency": 0,
        },
    )
    assert response.status_code == 422 and "sensitive-value" not in response.text


def test_model_test_hash_routes_inheritance_and_snapshots(managed_client):
    client = managed_client
    provider = create_provider(client)
    model = create_model(client, provider)
    second = create_model(client, provider, "model-b")
    seed_default(model)
    for candidate in (model, second):
        assert (
            client.post(PREFIX + f"/models/{candidate['id']}/test").json()["status"]
            == "passed"
        )
    routes = {
        item["code"]: item
        for item in client.get(PREFIX + "/business-routes").json()["items"]
    }
    override = client.put(
        PREFIX + "/business-routes/ppt_outline",
        json={
            "expected_revision": routes["ppt_outline"]["revision"],
            "model_id": second["id"],
            "reasoning_tier": None,
        },
    )
    assert override.status_code == 200, override.text
    response = client.post(
        "/api/v1/presentations", json={"user_request": "Provider snapshot test"}
    )
    assert response.status_code == 202, response.text
    pid = response.json()["presentation_id"]
    current_route = client.get(PREFIX + "/business-routes").json()["items"][0]
    assert (
        client.put(
            PREFIX + "/business-routes/default",
            json={
                "expected_revision": current_route["revision"],
                "model_id": model["id"],
                "reasoning_tier": "low",
            },
        ).status_code
        == 200
    )
    with session_scope() as db:
        snapshot = loads(db.get(Presentation, pid).config_snapshot_json)
        assert snapshot["resolved_routes"]["ppt_outline"]["model_id"] == second["id"]
        assert snapshot["resolved_routes"]["ppt_outline"]["reasoning_tier"] == "high"
        assert resolve_business_route(db, "ppt_outline")["reasoning_tier"] == "low"
    # Unrelated model additions must not invalidate an existing connection test.
    create_model(client, provider, "model-c")
    models = client.get(PREFIX + f"/providers/{provider['id']}/models").json()["items"]
    assert not next(item for item in models if item["id"] == model["id"])["last_test"][
        "stale"
    ]
    assert (
        client.delete(
            PREFIX + f"/models/{model['id']}?expected_revision={model['revision']}"
        ).status_code
        == 409
    )
    updated = client.patch(
        PREFIX + f"/models/{model['id']}",
        json=model_patch(model, remote_model_id="changed-model"),
    ).json()
    assert updated["last_test"]["stale"]
    assert client.get(PREFIX + "/business-routes").json()["items"][0]["ready"] is False


def test_reasoning_tiers_custom_values_and_inherited_protection(managed_client):
    client = managed_client
    provider = create_provider(client)
    model = create_model(client, provider)
    seed_default(model)
    tiers = [
        {
            key: item[key]
            for key in ("code", "label", "api_value", "enabled", "sort_order")
        }
        for item in model["reasoning_tiers"]
    ]
    tiers[-1]["enabled"] = False
    response = client.put(
        PREFIX + f"/models/{model['id']}/reasoning-tiers",
        json={"expected_revision": model["revision"], "tiers": tiers},
    )
    assert response.status_code == 409
    tiers[-1]["enabled"] = True
    tiers.append(
        {
            "code": "deep",
            "label": "Deep",
            "api_value": "xhigh",
            "enabled": True,
            "sort_order": 4,
        }
    )
    response = client.put(
        PREFIX + f"/models/{model['id']}/reasoning-tiers",
        json={"expected_revision": model["revision"], "tiers": tiers},
    )
    assert response.status_code == 200, response.text
    with session_scope() as db:
        assert (
            len(
                db.scalars(
                    select(database.ModelReasoningTier).where(
                        database.ModelReasoningTier.model_id == model["id"]
                    )
                ).all()
            )
            == 5
        )


def test_discovery_import_metadata_and_failure_preserves_models(
    managed_client, monkeypatch
):
    client = managed_client
    provider = create_provider(client)
    model = create_model(client, provider)
    provider = client.get(PREFIX + f"/providers/{provider['id']}").json()
    client_type = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "model-a"},
                    {"id": "new-model", "context_length": 64000},
                    {"id": "unknown"},
                    {"id": "unknown"},
                ]
            },
        )

    monkeypatch.setattr(
        provider_api.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    response = client.post(
        PREFIX
        + f"/providers/{provider['id']}/discover-models?expected_revision={provider['revision']}"
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 3 and items[0]["already_added"]
    assert items[1]["context_window_tokens"] == 64000
    assert items[2]["context_window_tokens"] is None
    assert "authorization" not in requests[0].headers
    assert (
        client.get(PREFIX + f"/providers/{provider['id']}/models").json()["items"][0][
            "id"
        ]
        == model["id"]
    )
    monkeypatch.setattr(
        provider_api.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda request: httpx.Response(404)), **kwargs
        ),
    )
    assert (
        client.post(
            PREFIX
            + f"/providers/{provider['id']}/discover-models?expected_revision={provider['revision']}"
        ).status_code
        == 422
    )
    assert (
        len(client.get(PREFIX + f"/providers/{provider['id']}/models").json()["items"])
        == 1
    )


@pytest.mark.asyncio
async def test_shared_limiter_provider_and_cancellation():
    limiter = ProviderLimiter(lambda provider_id: 1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def occupy():
        async with limiter.acquire("provider"):
            started.set()
            await release.wait()

    first = asyncio.create_task(occupy())
    await started.wait()
    waiting = asyncio.create_task(occupy())
    await asyncio.sleep(0)
    assert limiter.active_count == 1 and not waiting.done()
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert limiter.active_count == 0 and not limiter._capacities
    async with limiter.acquire("provider"):
        assert limiter.active_count == 1


def test_call_cursor_is_chronological_and_detail_is_sanitized(managed_client):
    with session_scope() as db:
        now = utcnow()
        for index in range(4):
            db.add(
                LlmCall(
                    id=str(4 - index),
                    purpose="provider_test",
                    model="test",
                    status="SUCCEEDED",
                    started_at=now + timedelta(seconds=index),
                    config_snapshot_json=dumps(
                        {"credential_ref": "private-ref", "model": "test"}
                    ),
                )
            )
    first = managed_client.get(PREFIX + "/calls?limit=2").json()
    second = managed_client.get(
        PREFIX + "/calls?limit=2&cursor=" + first["next_cursor"]
    ).json()
    assert [item["id"] for item in first["items"] + second["items"]] == [
        "1",
        "2",
        "3",
        "4",
    ]
    assert managed_client.get(PREFIX + "/calls/1").json()["config_snapshot"] == {
        "model": "test"
    }


@pytest.mark.asyncio
async def test_gateway_uses_frozen_business_models_and_retired_key(
    managed_client, monkeypatch
):
    client = managed_client
    provider = create_provider(client, auth_mode="bearer", api_key="original-key")
    outline = create_model(client, provider, "outline-model")
    html = create_model(client, provider, "html-model")
    seed_default(outline)
    for model in (outline, html):
        assert (
            client.post(PREFIX + f"/models/{model['id']}/test").json()["status"]
            == "passed"
        )
    assert (
        client.put(
            PREFIX + "/business-routes/ppt_html",
            json={
                "expected_revision": 1,
                "model_id": html["id"],
                "reasoning_tier": "low",
            },
        ).status_code
        == 200
    )
    response = client.post(
        "/api/v1/presentations", json={"user_request": "Frozen routing test"}
    )
    assert response.status_code == 202
    pid = response.json()["presentation_id"]
    current = client.get(PREFIX + f"/providers/{provider['id']}").json()
    assert (
        client.put(
            PREFIX + f"/providers/{provider['id']}/credential",
            json={"expected_revision": current["revision"], "api_key": "rotated-key"},
        ).status_code
        == 200
    )
    requests = []

    async def stream(**kwargs):
        requests.append(kwargs)
        text = json.dumps(document()) if kwargs.get("json_schema") else valid_html()
        yield providers.ProviderEvent(
            "completed", {"text": text, "usage": {"input_tokens": 1}}
        )

    monkeypatch.setattr(runtime, "stream_responses", stream)
    gateway = runtime.ProviderGateway()
    await gateway.complete_json("test", pid, "director")
    await gateway._stream("test", pid, "page_composer", business_code="ppt_html")
    assert [
        (request["model"], request["reasoning_effort"], request["api_key"])
        for request in requests
    ] == [
        ("outline-model", "high", "original-key"),
        ("html-model", "low", "original-key"),
    ]
    assert all("max_output_tokens" not in request for request in requests)
    current = client.get(PREFIX + f"/providers/{provider['id']}").json()
    assert (
        client.delete(
            PREFIX
            + f"/providers/{provider['id']}/credential?expected_revision={current['revision']}"
        ).status_code
        == 200
    )
    with pytest.raises(providers.ProviderError) as error:
        await gateway.complete_json("test", pid, "director")
    assert error.value.code == "CREDENTIAL_REVOKED"


@pytest.mark.asyncio
async def test_gateway_cancellation_records_audit_and_releases_limit(
    managed_client, monkeypatch
):
    provider = create_provider(managed_client)
    model = create_model(managed_client, provider)
    seed_default(model)
    limiter = ProviderLimiter()
    monkeypatch.setattr(runtime, "PROVIDER_LIMITER", limiter)
    started = asyncio.Event()

    async def blocking_stream(**kwargs):
        started.set()
        await asyncio.Event().wait()
        yield providers.ProviderEvent("completed", {})

    monkeypatch.setattr(runtime, "stream_responses", blocking_stream)
    task = asyncio.create_task(
        runtime.ProviderGateway()._stream("test", None, "provider_test")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert limiter.active_count == 0
    call = managed_client.get(PREFIX + "/calls").json()["items"][0]
    assert call["status"] == "CANCELLED" and call["error_code"] == "CALL_CANCELLED"


def test_model_test_failure_and_keychain_unavailable_are_visible(
    managed_client, monkeypatch
):
    client = managed_client
    provider = create_provider(client, auth_mode="bearer", api_key="stored-key")
    model = create_model(client, provider)
    seed_default(model)

    async def refused(**kwargs):
        yield providers.ProviderEvent("refused", {"usage": {}})

    monkeypatch.setattr(runtime, "stream_responses", refused)
    result = client.post(PREFIX + f"/models/{model['id']}/test").json()
    assert result["status"] == "failed"
    assert all(check["error_code"] == "MODEL_REFUSED" for check in result["checks"])

    def keychain_error(ref):
        raise RuntimeError("should never echo this keychain error")

    monkeypatch.setattr(provider_api, "read_secret", keychain_error)
    result = client.get(PREFIX + "/providers")
    assert result.status_code == 200
    assert result.json()["items"][0]["credential_store_available"] is False
    assert "should never echo" not in result.text
    assert client.post(PREFIX + f"/models/{model['id']}/test").status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "disable"])
async def test_queued_call_revalidates_before_sending(
    managed_client, monkeypatch, change
):
    client = managed_client
    provider = create_provider(client, auth_mode="bearer", api_key="queued-key")
    model = create_model(client, provider)
    seed_default(model)
    limiter = ProviderLimiter(lambda provider_id: 1)
    monkeypatch.setattr(runtime, "PROVIDER_LIMITER", limiter)
    sent = []

    async def stream(**kwargs):
        sent.append(kwargs)
        yield providers.ProviderEvent("completed", {"text": "OK"})

    monkeypatch.setattr(runtime, "stream_responses", stream)
    async with limiter.acquire(provider["id"]):
        task = asyncio.create_task(
            runtime.ProviderGateway()._stream("test", None, "provider_test")
        )
        await asyncio.sleep(0)
        assert not task.done()
        current = client.get(PREFIX + f"/providers/{provider['id']}").json()
        if change == "revoke":
            result = client.delete(
                PREFIX
                + f"/providers/{provider['id']}/credential?expected_revision={current['revision']}"
            )
        else:
            result = client.patch(
                PREFIX + f"/providers/{provider['id']}",
                json=provider_patch(current, enabled=False),
            )
        assert result.status_code == 200
    with pytest.raises(providers.ProviderError) as error:
        await task
    assert error.value.code == (
        "CREDENTIAL_REVOKED" if change == "revoke" else "CONFIG_NOT_READY"
    )
    assert sent == [] and limiter.active_count == 0
    call = client.get(PREFIX + "/calls").json()["items"][0]
    assert call["status"] == "FAILED" and call["error_code"] == error.value.code


def test_default_route_rejects_incompatible_inherited_budget(managed_client):
    client = managed_client
    provider = create_provider(client)
    model = create_model(client, provider)
    second = create_model(client, provider, "second")
    seed_default(model)
    for item in (model, second):
        assert (
            client.post(PREFIX + f"/models/{item['id']}/test").json()["status"]
            == "passed"
        )
    assert (
        client.put(
            PREFIX + "/business-routes/ppt_html",
            json={
                "expected_revision": 1,
                "model_id": second["id"],
                "reasoning_tier": None,
            },
        ).status_code
        == 200
    )
    tiers = [
        {
            key: item[key]
            for key in ("code", "label", "api_value", "enabled", "sort_order")
        }
        for item in model["reasoning_tiers"]
    ]
    tiers.append(
        {
            "code": "deep",
            "label": "Deep",
            "api_value": "xhigh",
            "enabled": True,
            "sort_order": 4,
        }
    )
    assert (
        client.put(
            PREFIX + f"/models/{model['id']}/reasoning-tiers",
            json={"expected_revision": model["revision"], "tiers": tiers},
        ).status_code
        == 200
    )
    assert (
        client.post(PREFIX + f"/models/{model['id']}/test").json()["status"] == "passed"
    )
    response = client.put(
        PREFIX + "/business-routes/default",
        json={
            "expected_revision": 1,
            "model_id": model["id"],
            "reasoning_tier": "deep",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "REASONING_TIER_UNAVAILABLE"
    with session_scope() as db:
        assert db.get(BusinessRoute, "default").reasoning_tier == "high"


def test_new_presentation_requires_current_capability_tests(managed_client):
    client = managed_client
    provider = create_provider(client)
    model = create_model(client, provider)
    seed_default(model)
    response = client.post(
        "/api/v1/presentations", json={"user_request": "Validate readiness"}
    )
    assert response.status_code == 409
    assert (
        client.post(PREFIX + f"/models/{model['id']}/test").json()["status"] == "passed"
    )
    assert (
        client.post(
            "/api/v1/presentations", json={"user_request": "Validated"}
        ).status_code
        == 202
    )
    response = client.patch(
        PREFIX + f"/models/{model['id']}",
        json=model_patch(model, remote_model_id="changed"),
    )
    assert response.status_code == 200
    assert (
        client.post(
            "/api/v1/presentations", json={"user_request": "Stale test"}
        ).status_code
        == 409
    )


def test_structured_check_rejects_numeric_boolean(managed_client, monkeypatch):
    provider = create_provider(managed_client)
    model = create_model(managed_client, provider)

    async def invalid_json(**kwargs):
        yield providers.ProviderEvent(
            "completed", {"text": '{"ok":1}' if kwargs.get("json_schema") else "OK"}
        )

    monkeypatch.setattr(runtime, "stream_responses", invalid_json)
    result = managed_client.post(PREFIX + f"/models/{model['id']}/test").json()
    assert result["status"] == "failed"
    assert result["checks"][0]["status"] == "passed"
    assert result["checks"][1]["status"] == "failed"
