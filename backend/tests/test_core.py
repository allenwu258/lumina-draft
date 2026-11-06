import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = "sqlite:///" + (Path(_tmp.name) / "test.db").as_posix()
os.environ["DS_API_FILE"] = str(Path(_tmp.name) / "missing.txt")
os.environ.pop("DS_API_KEY", None)

from backend.app import main, runtime  # noqa: E402
from backend.app.providers import ProviderEvent  # noqa: E402
from backend.app.rendering import validate_html  # noqa: E402
from backend.app.db import engine, LlmCall, Presentation, loads, session_scope  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def close_database():
    yield
    engine.dispose()
    _tmp.cleanup()


def valid_html():
    return '<!doctype html><html><head><style>#ppt-page{width:800px;height:450px}</style></head><body><main id="ppt-page">Hello</main></body></html>'


def test_html_contract_rejects_active_content():
    assert validate_html(valid_html())[0]
    assert not validate_html(valid_html().replace("Hello", "<script>alert(1)</script>"))[0]
    assert not validate_html(valid_html().replace("Hello", '<div style="background:url(https://example.com/a)">x</div>'))[0]
    assert not validate_html(valid_html().replace("Hello", '<svg><a xlink:href="https://example.com"/></svg>'))[0]
    assert not validate_html(valid_html().replace("450px", "449px"))[0]
    assert not validate_html('<main id="ppt-page">partial</main>')[0]


def document():
    return {"schema_version": "1.0", "brief": {"title": "Test deck", "purpose": "Explain a topic", "audience": "Staff", "language": "en", "tone": "Clear", "assumptions": []}, "style_spec": {"description": "Simple", "colors": {"background": "#FFFFFF", "surface": "#FFFFFF", "primary": "#123456", "text": "#111111", "muted_text": "#777777"}, "typography": {"font_family": "Arial", "title_size_px": 32, "body_size_px": 18, "caption_size_px": 12}, "layout_rules": ["Readable"]}, "slides": [{"page_number": n, "page_type": "content", "title": f"Slide {n}", "key_message": f"Message {n}", "content_outline": ["Useful content"], "design_notes": "Simple", "summary": f"Summary {n}"} for n in [1, 2]]}


@pytest.fixture
def client(monkeypatch):
    secrets = {}
    monkeypatch.setattr(main, "save_secret", lambda key, value: secrets.__setitem__(key, value))
    monkeypatch.setattr(main, "read_secret", lambda key: secrets.get(key))
    monkeypatch.setattr(runtime, "read_secret", lambda key: secrets.get(key))
    main.app.state.reasoning_efforts = []

    async def fake_stream(**kwargs):
        assert "max_output_tokens" not in kwargs
        main.app.state.reasoning_efforts.append(kwargs.get("reasoning_effort"))
        text = json.dumps(document()) if kwargs.get("json_schema") else valid_html()
        if kwargs.get("json_schema"):
            yield ProviderEvent("reasoning_delta", "梳理叙事主线。")
        yield ProviderEvent("text_delta", text)
        yield ProviderEvent("completed", {"text": text, "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}})

    monkeypatch.setattr(runtime, "stream_responses", fake_stream)
    async def fake_connection_stream(**kwargs):
        assert kwargs["max_output_tokens"] == 32
        assert kwargs["reasoning_effort"] == "none"
        yield ProviderEvent("completed", {"text": "OK", "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}})
    monkeypatch.setattr(main, "stream_responses", fake_connection_stream)
    with TestClient(main.app) as c:
        response = c.put("/api/v1/settings/llm/default", json={"name": "Test", "base_url": "https://example.com", "model": "fake", "api_key": "secret", "enabled": True})
        assert response.status_code == 200
        assert "secret" not in response.text
        assert c.put("/api/v1/settings/generation", json={"reasoning_effort": "high"}).status_code == 200
        main.app.state.reasoning_efforts.clear()
        yield c


def wait_status(client, pid, statuses):
    for _ in range(100):
        snapshot = client.get(f"/api/v1/presentations/{pid}").json()
        if snapshot["presentation"]["status"] in statuses:
            return snapshot
        time.sleep(.05)
    pytest.fail("Timed out waiting for state")


def test_end_to_end_and_idempotency(client):
    assert client.put("/api/v1/settings/generation", json={"reasoning_effort": "low"}).status_code == 200
    body = {"user_request": "Create a one-page test deck"}
    headers = {"Idempotency-Key": "test-create"}
    first = client.post("/api/v1/presentations", json=body, headers=headers)
    duplicate = client.post("/api/v1/presentations", json=body, headers=headers)
    assert first.status_code == 202
    assert first.json() == duplicate.json()
    assert client.post("/api/v1/presentations", json={"user_request": "Different"}, headers=headers).status_code == 409
    pid = first.json()["presentation_id"]
    assert client.put("/api/v1/settings/generation", json={"reasoning_effort": "max"}).status_code == 200
    with session_scope() as db:
        presentation = db.get(Presentation, pid)
        assert loads(presentation.config_snapshot_json)["reasoning_effort"] == "low"
    snapshot = wait_status(client, pid, {"WAITING_CONFIRMATION", "FAILED"})
    assert snapshot["presentation"]["status"] == "WAITING_CONFIRMATION", snapshot
    outline = snapshot["latest_outline"]
    assert len(outline["document"]["slides"]) == 2
    assert len({s["slide_id"] for s in outline["document"]["slides"]}) == 2
    confirmation = {"outline_id": outline["id"], "content_hash": outline["content_hash"]}
    result = client.post(f"/api/v1/presentations/{pid}/confirm", json=confirmation)
    assert result.status_code == 202
    assert client.post(f"/api/v1/presentations/{pid}/confirm", json=confirmation).json()["run_id"] == result.json()["run_id"]
    snapshot = wait_status(client, pid, {"READY", "PARTIAL_FAILED"})
    assert snapshot["presentation"]["status"] == "READY", snapshot
    assert main.app.state.reasoning_efforts == ["low", "low", "low"]
    streamed_snapshot = client.get(f"/api/v1/presentations/{pid}").json()
    assert streamed_snapshot["director_stream"]["reasoning"] == "梳理叙事主线。"
    assert "schema_version" in streamed_snapshot["director_stream"]["output"]
    with session_scope() as db:
        director_stream = [loads(event.payload_json) for event in db.query(main.Event).filter_by(presentation_id=pid, type="director.stream.delta").all()]
    assert any(event["channel"] == "reasoning" and event["delta"] == "梳理叙事主线。" for event in director_stream)
    assert any(event["channel"] == "output" and "schema_version" in event["delta"] for event in director_stream)
    assert [p["page_number"] for p in snapshot["pages"]] == [1, 2]
    assert all(p["status"] == "SUCCEEDED" for p in snapshot["pages"])
    usage = client.get(f"/api/v1/presentations/{pid}/usage").json()
    assert (usage["call_count"], usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (3, 30, 60, 90)
    assert usage["total_unreported_calls"] == 0
    with session_scope() as db:
        db.add(LlmCall(presentation_id=pid, purpose="page_composer", model="fake", status="FAILED"))
        db.add(LlmCall(presentation_id=pid, purpose="provider_test", model="fake", status="SUCCEEDED", total_tokens=999))
    usage = client.get(f"/api/v1/presentations/{pid}/usage").json()
    assert usage["call_count"] == 4
    assert usage["total_tokens"] == 90
    assert usage["total_unreported_calls"] == 1
    response = client.get(f"/api/v1/page-tasks/{snapshot['pages'][0]['id']}/html")
    assert response.text == valid_html()
    assert "sandbox" in response.headers["content-security-policy"]
    assert client.get("/api/v1/settings/llm/default").json()["credential_configured"]
    assert "secret" not in client.get("/api/v1/settings/llm/default").text


@pytest.mark.asyncio
async def test_sse_replays_from_last_event_id(client):
    response = client.post("/api/v1/presentations", json={"user_request": "Create a one-page test deck"})
    pid = response.json()["presentation_id"]
    wait_status(client, pid, {"WAITING_CONFIRMATION", "FAILED"})
    with session_scope() as db:
        sequences = [row[0] for row in db.query(main.Event.seq).filter_by(presentation_id=pid).order_by(main.Event.seq).all()]
    assert len(sequences) > 2
    expected_seq = sequences[-2]

    # The native EventSource reconnects with Last-Event-ID. The server honors the
    # newer of that header and the initial query cursor, then replays persisted rows.
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = main.Request({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": f"/api/v1/presentations/{pid}/events", "raw_path": b"/events",
        "query_string": b"", "headers": [], "client": ("test", 1), "server": ("test", 80),
    }, receive)
    streamed = await main.events(pid, request, after=sequences[0], last_event_id=str(expected_seq - 1))
    iterator = streamed.body_iterator.__aiter__()
    first_chunk = await anext(iterator)
    await iterator.aclose()
    first_event_id = int(first_chunk.splitlines()[0].removeprefix("id: "))
    assert first_event_id == expected_seq


def test_stale_outline_is_rejected(client):
    response = client.post("/api/v1/presentations", json={"user_request": "Second deck"})
    pid = response.json()["presentation_id"]
    snapshot = wait_status(client, pid, {"WAITING_CONFIRMATION", "FAILED"})
    outline = snapshot["latest_outline"]
    updated = client.put(f"/api/v1/presentations/{pid}/outline", json={"outline_id": outline["id"], "content_hash": outline["content_hash"], "document": outline["document"]})
    assert updated.status_code == 200
    assert client.post(f"/api/v1/presentations/{pid}/confirm", json={"outline_id": outline["id"], "content_hash": outline["content_hash"]}).status_code == 409


@pytest.mark.parametrize("body", [
    {"user_request": "Create a deck", "options": {"page_count": 8}},
    {"user_request": "Create a deck", "page_count": 8},
])
def test_create_rejects_explicit_page_count(client, body):
    assert client.post("/api/v1/presentations", json=body).status_code == 422


def test_usage_not_found(client):
    assert client.get("/api/v1/presentations/missing/usage").status_code == 404


def test_generation_settings_default_validation_and_persistence(client):
    assert client.get("/api/v1/settings/generation").json()["reasoning_effort"] == "high"
    for effort in ("auto", "none", "low", "high", "max"):
        response = client.put("/api/v1/settings/generation", json={"reasoning_effort": effort})
        assert response.status_code == 200
        assert response.json()["reasoning_effort"] == effort
    assert client.put("/api/v1/settings/generation", json={"reasoning_effort": "medium"}).status_code == 422
    assert client.put("/api/v1/settings/generation", json={"reasoning_effort": "high", "extra": True}).status_code == 422


def test_legacy_reasoning_snapshot_defaults_to_none():
    assert runtime.request_reasoning_effort({}) == "none"
    assert runtime.request_reasoning_effort({"reasoning_effort": "high"}) == "high"
    assert runtime.request_reasoning_effort({"reasoning_effort": "auto"}) is None


def test_connection_test_does_not_expose_or_record_tokens(client):
    response = client.post("/api/v1/settings/llm/default/test")
    assert response.json() == {"status": "SUCCEEDED", "message": "连接成功"}
    with session_scope() as db:
        call = db.query(LlmCall).filter_by(purpose="provider_test").order_by(LlmCall.started_at.desc()).first()
        assert call.input_tokens is None and call.output_tokens is None and call.total_tokens is None
    assert all("total_tokens" not in item for item in client.get("/api/v1/settings/llm/calls").json()["items"])
