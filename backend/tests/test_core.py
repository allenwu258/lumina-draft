import json
import os
import re
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

_tmp = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = "sqlite:///" + (Path(_tmp.name) / "test.db").as_posix()
os.environ["DS_API_FILE"] = str(Path(_tmp.name) / "missing.txt")
os.environ["SCENE_DATA_DIR"] = str(Path(_tmp.name) / "scenes")
os.environ.pop("DS_API_KEY", None)

from backend.app import main, provider_api, runtime  # noqa: E402
from backend.app.providers import ProviderEvent  # noqa: E402
from backend.app.rendering import normalize_html, validate_html  # noqa: E402
from backend.app.font_map import normalize_font_stack  # noqa: E402
from backend.app.prompts import COMPOSER_PROMPT  # noqa: E402
from backend.app.tailwind_profile import COMPOSER_UTILITIES, TAILWIND_CLASSES, TAILWIND_PROFILE, css_for_html, preview_html  # noqa: E402
from backend.app.db import Conversation, ConversationMessage, engine, Job, LlmCall, Presentation, loads, session_scope  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def close_database():
    yield
    engine.dispose()
    _tmp.cleanup()


def valid_html():
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>#ppt-page{{width:800px;height:450px;box-sizing:border-box;overflow:hidden;--ppt-background:#F8FAFC;--ppt-surface:#FFFFFF;--ppt-primary:#2563EB;--ppt-text:#1F2937;--ppt-muted:#64748B;--ppt-title-size:32px;--ppt-body-size:18px;--ppt-caption-size:12px;--ppt-font-family:Arial,sans-serif}}</style></head><body><main id="ppt-page" data-css-profile="{TAILWIND_PROFILE}" class="bg-ppt-background p-8"><div class="flex items-center gap-4 bg-ppt-surface p-4"><h1 id="tailwind-proof" class="font-ppt text-ppt-title font-semibold text-ppt-text">Hello</h1><p class="text-ppt-body text-ppt-muted">Utility rendering</p></div></main></body></html>'''


def legacy_html():
    return '<!doctype html><html><head><style>#ppt-page{width:800px;height:450px}</style></head><body><main id="ppt-page">Legacy page</main></body></html>'


def test_html_contract_rejects_active_content():
    assert validate_html(valid_html())[0]
    assert not validate_html(valid_html().replace("Hello", "<script>alert(1)</script>"))[0]
    assert not validate_html(valid_html().replace("Hello", '<div style="background:url(https://example.com/a)">x</div>'))[0]
    assert not validate_html(valid_html().replace("Hello", '<svg><a xlink:href="https://example.com"/></svg>'))[0]
    assert not validate_html(valid_html().replace("450px", "449px"))[0]
    assert not validate_html('<main id="ppt-page">partial</main>')[0]
    assert validate_html(legacy_html()) == (True, "ok")


def test_html_normalization_is_bounded_and_auditable():
    raw = '<!doctype html><html><body><script src="https://cdn.tailwindcss.com"></script><main id="ppt-page" style="width:800px;height:450px">safe</main></body></html>'
    normalized, actions = normalize_html(raw)
    assert actions == ["KNOWN_TAILWIND_CDN_REMOVED_OFFLINE", "MISSING_EMPTY_HEAD_ADDED"]
    assert "cdn.tailwindcss.com" not in normalized
    assert validate_html(normalized)[0]
    unknown = raw.replace("https://cdn.tailwindcss.com", "https://example.com/a.js")
    unchanged, actions = normalize_html(unknown)
    assert actions == ["MISSING_EMPTY_HEAD_ADDED"] and unchanged != unknown
    assert not validate_html(unchanged)[0]


def test_windows_font_map_prefers_exact_names_then_aliases_and_defaults():
    mapped = normalize_font_stack('"PingFang SC", Arial, sans-serif', {"Arial"})
    assert mapped.startswith("Arial, \"Microsoft YaHei\"")
    assert "Segoe UI" in mapped
    assert normalize_font_stack('"Unregistered Typeface"', set()) == 'Arial, "Microsoft YaHei"'


def test_windows_font_map_rewrites_font_family_and_shorthand_in_html(monkeypatch):
    from backend.app import font_map

    monkeypatch.setattr(
        font_map,
        "installed_win32_families",
        lambda: frozenset({"arial", "microsoft yahei"}),
    )
    raw = '<!doctype html><html><head><style>#ppt-page{width:800px;height:450px;font-family:PingFang SC,Arial}h1{font:italic 20px/1.2 Inter}</style></head><body><main id="ppt-page" style="font-family:Noto Sans SC"><h1>Font</h1></main></body></html>'
    normalized, actions = normalize_html(raw)
    assert "WINDOWS_FONT_FAMILY_NORMALIZED" in actions
    assert "font-family:Arial, \"Microsoft YaHei\"" in normalized
    assert 'font:italic 20px/1.2 Arial, "Microsoft YaHei"' in normalized
    assert 'font-family:"Microsoft YaHei", Arial' in normalized


def test_composer_does_not_enforce_a_finite_tailwind_profile():
    assert validate_html(valid_html()) == (True, "ok")
    assert f'data-css-profile="{TAILWIND_PROFILE}"' not in COMPOSER_PROMPT
    assert "class 白名单" in COMPOSER_PROMPT
    assert len(TAILWIND_CLASSES) == len(set(TAILWIND_CLASSES))
    assert validate_html(valid_html().replace("items-center", "items-unregistered"))[0]
    assert validate_html(valid_html().replace(f'data-css-profile="{TAILWIND_PROFILE}"', 'data-css-profile="unknown"'))[0]
    assert validate_html(valid_html().replace("class=\"flex items-center gap-4 bg-ppt-surface p-4\"", 'style="display:flex"'))[0]
    rendered = preview_html(valid_html())
    assert 'data-tailwind-profile="tailwind-ppt-v1"' in rendered
    assert ".flex{" in rendered
    assert preview_html(legacy_html()) == legacy_html()


def test_composer_is_single_paragraph_without_an_injected_utility_catalog():
    assert len(COMPOSER_PROMPT.splitlines()) == 1
    assert '\\' not in COMPOSER_PROMPT
    assert f'data-css-profile="{TAILWIND_PROFILE}"' not in COMPOSER_PROMPT
    assert COMPOSER_UTILITIES not in COMPOSER_PROMPT


def test_expanded_composer_utilities_pass_the_html_contract():
    body = '<section class="flex-1 min-h-0 min-w-0 grid grid-cols-12 gap-6"><div class="col-span-7 bg-linear-to-br from-indigo-900 via-violet-700 to-rose-500 rounded-3xl p-6"><h1 class="text-[30px] leading-tight text-white">专业演示</h1></div><aside class="col-span-5 bg-ppt-primary/10"><div class="size-16 rotate-3 bg-amber-300 shadow-lg"></div></aside></section>'
    html = valid_html().replace(
        '<div class="flex items-center gap-4 bg-ppt-surface p-4"><h1 id="tailwind-proof" class="font-ppt text-ppt-title font-semibold text-ppt-text">Hello</h1><p class="text-ppt-body text-ppt-muted">Utility rendering</p></div>',
        body,
    )
    assert body in html
    assert validate_html(html) == (True, "ok")
    assert validate_html(html.replace("text-[30px]", "text-[301px]"))[0]


@pytest.mark.parametrize("utility", [
    "mt-auto", "mb-auto", "border-l-2", "border-r-4", "border-y-2",
    "text-[17px]", "size-7", "items-baseline", "w-5", "left-40", "left-72", "top-32",
])
def test_real_failed_output_utility_families_are_admitted(utility):
    html = valid_html().replace("items-center", f"items-center {utility}")
    assert validate_html(html) == (True, "ok")


def test_unregistered_utility_tokens_are_admitted_for_later_compiler_diagnostics():
    html = valid_html().replace("items-center", "items-center unknown-z unknown-a unknown-z")
    valid, reason = validate_html(html)
    assert valid and reason == "ok"
    many = " ".join(f"unregistered-{n}-" + "x" * 80 for n in range(100))
    valid, reason = validate_html(valid_html().replace("items-center", many))
    assert valid and reason == "ok"


def test_runtime_tailwind_compilation_handles_arbitrary_composer_utilities():
    html = legacy_html().replace(
        "Legacy page",
        '<div class="grid grid-cols-[1fr_205px] bg-[#102B3D] text-[17px] font-sans">自由布局</div>',
    )
    css = css_for_html(html)
    assert ".grid" in css
    assert "grid-template-columns" in css
    assert "Segoe UI" in css
    assert "ui-sans-serif" not in css
    assert "#102B3D" in css
    rendered = preview_html(html, css=css)
    assert 'data-tailwind-profile="tailwind-ppt-v1"' in rendered
    assert "grid-template-columns" in rendered
    early_preview = preview_html(html)
    assert 'data-tailwind-runtime="true"' in early_preview
    assert "grid-template-columns" in early_preview


def document():
    return {"schema_version": "1.0", "brief": {"title": "Test deck", "purpose": "Explain a topic", "audience": "Staff", "language": "en", "tone": "Clear", "assumptions": []}, "style_spec": {"description": "Simple", "colors": {"background": "#FFFFFF", "surface": "#FFFFFF", "primary": "#123456", "text": "#111111", "muted_text": "#777777"}, "typography": {"font_family": "Arial", "title_size_px": 32, "body_size_px": 18, "caption_size_px": 12}, "layout_rules": ["Readable"]}, "slides": [{"page_number": n, "page_type": "content", "title": f"Slide {n}", "key_message": f"Message {n}", "content_outline": ["Useful content"], "design_notes": "Simple", "summary": f"Summary {n}"} for n in [1, 2]]}


@pytest.fixture
def client(monkeypatch):
    secrets = {}
    monkeypatch.setattr(provider_api, "save_secret", lambda key, value: secrets.__setitem__(key, value))
    monkeypatch.setattr(provider_api, "read_secret", lambda key: secrets.get(key))
    monkeypatch.setattr(main, "read_secret", lambda key: secrets.get(key))
    monkeypatch.setattr(runtime, "read_secret", lambda key: secrets.get(key))
    main.app.state.reasoning_efforts = []

    async def fake_stream(**kwargs):
        if kwargs.get("max_output_tokens"):
            assert kwargs["max_output_tokens"] == 64
            assert kwargs["reasoning_effort"] is None
            text = '{"ok":true}' if kwargs.get("json_schema") else "OK"
            yield ProviderEvent("text_delta", text)
            yield ProviderEvent("completed", {"text": text, "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}})
            return
        main.app.state.reasoning_efforts.append(kwargs.get("reasoning_effort"))
        text = json.dumps(document()) if kwargs.get("json_schema") else valid_html()
        if kwargs.get("json_schema"):
            yield ProviderEvent("reasoning_delta", "梳理叙事主线。")
        yield ProviderEvent("text_delta", text)
        yield ProviderEvent("completed", {"text": text, "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}})

    monkeypatch.setattr(runtime, "stream_responses", fake_stream)
    with TestClient(main.app) as c:
        response = c.put("/api/v1/settings/llm/default", json={"name": "Test", "base_url": "https://example.com", "model": "fake", "api_key": "secret", "enabled": True})
        assert response.status_code == 200
        assert "secret" not in response.text
        assert c.put("/api/v1/settings/generation", json={"reasoning_effort": "high"}).status_code == 200
        assert c.post("/api/v1/settings/llm/default/test").json()["status"] == "SUCCEEDED"
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
    with session_scope() as db:
        presentation = db.get(Presentation, pid)
        route_model_id = loads(presentation.config_snapshot_json)["resolved_routes"]["ppt_outline"]["model_id"]
        job = db.scalar(select(Job).where(Job.run_id == first.json()["run_id"]))
        assert job.model_id == route_model_id
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
    with session_scope() as db:
        presentation = db.get(Presentation, pid)
        route_model_id = loads(presentation.config_snapshot_json)["resolved_routes"]["ppt_html"]["model_id"]
        job = db.scalar(select(Job).where(Job.run_id == result.json()["run_id"]))
        assert job.model_id == route_model_id
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
    normalized_html, actions = normalize_html(valid_html())
    assert "WINDOWS_FONT_FAMILY_NORMALIZED" in actions
    assert response.text == normalized_html
    assert "sandbox" in response.headers["content-security-policy"]
    assert client.get("/api/v1/settings/llm/default").json()["credential_configured"]
    assert "secret" not in client.get("/api/v1/settings/llm/default").text


def test_multi_turn_identity_and_cancellation(client, monkeypatch):
    from backend.app.agent import leader

    monkeypatch.setattr(leader, "launch", lambda turn_id: None)
    base = "/api/v1/presentations"
    create = {"entry_mode": "agent", "interaction_mode": "multi_turn", "user_request": "董事会讨论"}
    headers = {"Idempotency-Key": "agent-session-first"}
    first = client.post(base, json=create, headers=headers)
    assert first.status_code == 202, first.text
    result = first.json()
    assert client.post(base, json=create, headers=headers).json() == result
    work = f"{base}/{result['presentation_id']}"
    message = {"text": "核对当前作品", "client_request_id": "follow-up"}
    assert client.post(work + "/turns", json=message).status_code == 409
    assert client.post(work + f"/turns/{result['turn_id']}/cancel").json()["state"] == "CANCELLED"
    second = client.post(work + "/turns", json=message)
    assert second.status_code == 202
    assert client.post(work + "/turns", json=message).json() == second.json()
    assert client.post(work + "/turns", json=dict(message, text="不同内容")).status_code == 409
    snapshot = client.get(work).json()
    assert snapshot["presentation"]["status"] == "DRAFT"
    assert snapshot["latest_run"] is snapshot["latest_outline"] is None
    assert snapshot["conversation"]["active_leader_turn_id"] == second.json()["turn_id"]
    chat = client.get(f"/api/v1/conversations/{result['conversation_id']}").json()
    assert [turn["presentation_id"] for turn in chat["turns"]] == [result["presentation_id"]] * 2
    assert [msg["kind"] for msg in chat["messages"]] == ["text", "cancelled", "text"]
    assert client.post(work + f"/turns/{second.json()['turn_id']}/cancel").status_code == 200


def test_entry_modes_and_persisted_special_messages(client):
    missing_canvas = client.post("/api/v1/presentations", json={"user_request": "Design a poster", "entry_mode": "design"})
    assert missing_canvas.status_code == 422
    unavailable = client.post("/api/v1/presentations", json={"user_request": "Create an image", "entry_mode": "image"})
    assert unavailable.status_code == 409

    created = client.post("/api/v1/presentations", json={"user_request": "Create an agent deck", "entry_mode": "agent"})
    assert created.status_code == 202
    pid = created.json()["presentation_id"]
    snapshot = wait_status(client, pid, {"WAITING_CONFIRMATION", "FAILED"})
    assert snapshot["presentation"]["entry_mode"] == "agent"
    assert snapshot["conversation"]["entry_mode"] == "agent"
    assert [message["kind"] for message in snapshot["conversation"]["messages"]] == ["text", "activity", "outline_card"]
    assert client.get(f"/api/v1/conversations/{snapshot['conversation']['id']}").json()["last_message_seq"] == 3

    outline = snapshot["latest_outline"]
    confirmed = client.post(f"/api/v1/presentations/{pid}/confirm", json={"outline_id": outline["id"], "content_hash": outline["content_hash"]})
    assert confirmed.status_code == 202
    snapshot = wait_status(client, pid, {"READY", "PARTIAL_FAILED"})
    kinds = [message["kind"] for message in snapshot["conversation"]["messages"]]
    assert kinds[-2:] == ["action", "generation_card"]
    assert snapshot["conversation"]["messages"][-1]["status"] == "READY"
    conversation = client.get(f"/api/v1/conversations/{snapshot['conversation']['id']}").json()
    assert conversation["turns"][0]["state"] == "COMPLETED"
    assert conversation["turns"][0]["completed_at"] is not None


def test_agent_card_action_validates_latest_outline_and_replays(client, monkeypatch):
    from backend.app.agent import leader

    async def unexpected_decision(*args, **kwargs):
        pytest.fail("An explicit card action should not make a routing model call")

    monkeypatch.setattr(leader.ProviderGateway, "decide", unexpected_decision)
    created = client.post("/api/v1/presentations", json={"user_request": "Legacy Agent draft", "entry_mode": "agent"}).json()
    work_id = created["presentation_id"]
    path = f"/api/v1/presentations/{work_id}"
    initial = wait_status(client, work_id, {"WAITING_CONFIRMATION"})
    initial_outline = initial["latest_outline"]
    initial_card = initial["conversation"]["messages"][-1]
    edited = initial_outline["document"]
    edited["brief"]["title"] = "Saved Agent version"
    saved = client.put(path + "/outline", json={
        "outline_id": initial_outline["id"], "content_hash": initial_outline["content_hash"], "document": edited,
    })
    assert saved.status_code == 200, saved.text
    current = saved.json()

    def action(request_id, card_id, digest):
        return {"client_request_id": request_id, "text": "确认大纲并生成 PPT", "action": {
            "type": "compose_deck", "artifact_message_id": card_id, "expected_content_hash": digest,
        }}

    def assert_rejected(body, code):
        response = client.post(path + "/turns", json=body)
        assert response.status_code == 202, response.text
        for _ in range(100):
            snapshot = client.get(path).json()
            if snapshot["conversation"]["active_leader_turn_id"] is None:
                break
            time.sleep(.05)
        assert snapshot["conversation"]["messages"][-1]["payload"]["code"] == code
        assert not snapshot["latest_outline"]["confirmed"]
        assert snapshot["batch_id"] is None

    assert_rejected(action("stale", initial_card["id"], initial_outline["content_hash"]), "OUTLINE_VERSION_CONFLICT")
    assert_rejected(action("hash", current["message_id"], "0" * 64), "OUTLINE_VERSION_CONFLICT")
    body = action("compose", current["message_id"], current["content_hash"])
    response = client.post(path + "/turns", json=body)
    assert response.status_code == 202, response.text
    assert client.post(path + "/turns", json=body).json() == response.json()
    assert client.post(path + "/turns", json=dict(body, text="other")).status_code == 409
    snapshot = wait_status(client, work_id, {"READY"})
    assert snapshot["latest_outline"]["confirmed"]
    cards = snapshot["conversation"]["messages"]
    assert [card["id"] for card in cards if card["kind"] == "outline_card"] == [initial_card["id"], current["message_id"]]
    assert len([card for card in cards if card["kind"] == "generation_card"]) == 1
    batch = client.get(path + f"/batches/{snapshot['batch_id']}?compact=true").json()
    assert "conversation" not in batch
    assert batch["pages"] == snapshot["pages"]


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


def test_legacy_connection_test_uses_shared_gateway_and_records_usage(client):
    response = client.post("/api/v1/settings/llm/default/test")
    assert response.json() == {"status": "SUCCEEDED", "message": "连接成功"}
    with session_scope() as db:
        call = db.query(LlmCall).filter_by(purpose="provider_test").order_by(LlmCall.started_at.desc()).first()
        assert (call.input_tokens, call.output_tokens, call.total_tokens) == (2, 1, 3)
        assert call.provider_id and call.config_snapshot_json
    assert all("total_tokens" not in item for item in client.get("/api/v1/settings/llm/calls").json()["items"])
