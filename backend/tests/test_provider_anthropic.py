import json

import httpx
import pytest

from backend.app.provider_protocols import ProviderError, ProviderRequest
from backend.app.provider_protocols.anthropic_messages import (
    ANTHROPIC_VERSION,
    AnthropicMessagesAdapter,
    compile_anthropic_schema,
)

_ASYNC_CLIENT = httpx.AsyncClient


def request(**overrides):
    values = {
        "protocol": "anthropic_messages",
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "secret",
        "model": "claude-sonnet-4-5",
        "instructions": "system instructions",
        "prompt": "user prompt",
        "auth_mode": AnthropicMessagesAdapter.default_auth_mode,
    }
    values.update(overrides)
    return ProviderRequest(**values)


def stream(*events):
    return "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
    ).encode()


def message_events(
    text="Hello",
    *,
    stop_reason="end_turn",
    input_tokens=3,
    output_tokens=2,
):
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_123",
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        (
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return stream(*events)


def install_stream_transport(monkeypatch, wire, status=200):
    requests = []

    def respond(http_request):
        requests.append(http_request)
        return httpx.Response(
            status,
            content=wire,
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        "backend.app.provider_protocols.anthropic_messages.httpx.AsyncClient",
        lambda **kwargs: _ASYNC_CLIENT(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth_mode", "expected_header"),
    [("bearer", "authorization"), ("api_key_header", "x-api-key")],
)
async def test_messages_api_builds_request_and_normalizes_stream(
    monkeypatch, auth_mode, expected_header
):
    requests = install_stream_transport(monkeypatch, message_events("Hello world"))
    schema = {
        "$defs": {
            "Brief": {
                "type": "object",
                "properties": {"title": {"type": "string", "minLength": 1}},
            }
        },
        "type": "object",
        "properties": {"brief": {"$ref": "#/$defs/Brief"}},
    }
    result = [
        event
        async for event in AnthropicMessagesAdapter().stream(
            request(
                auth_mode=auth_mode,
                reasoning_effort="high",
                max_output_tokens=12000,
                json_schema=schema,
            )
        )
    ]

    sent = requests[0]
    body = json.loads(sent.content)
    assert sent.url.path == "/v1/messages"
    assert sent.headers[expected_header] == (
        "Bearer secret" if auth_mode == "bearer" else "secret"
    )
    assert (
        "x-api-key" if auth_mode == "bearer" else "authorization"
    ) not in sent.headers
    assert sent.headers["anthropic-version"] == ANTHROPIC_VERSION
    assert body["model"] == "claude-sonnet-4-5"
    assert body["max_tokens"] == 12000
    assert body["system"] == "system instructions"
    assert body["messages"] == [{"role": "user", "content": "user prompt"}]
    assert body["stream"] is True
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "high"
    compiled = body["output_config"]["format"]["schema"]
    assert compiled["required"] == ["brief"]
    assert compiled["properties"]["brief"]["required"] == ["title"]
    assert "minLength" not in compiled["properties"]["brief"]["properties"]["title"]
    assert (
        compiled["properties"]["brief"]["properties"]["title"]["description"]
        == "Minimum length: 1."
    )
    assert [event.kind for event in result] == [
        "started",
        "text_delta",
        "completed",
    ]
    assert result[-1].data["text"] == "Hello world"
    assert result[-1].data["response_id"] == "msg_123"
    assert result[-1].data["usage"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
    }
    assert "$defs" in schema


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_limit", "model_limit", "expected_limit"),
    [
        (None, None, None),
        (12000, None, 12000),
        (None, 2048, 2048),
        (None, 32768, 32768),
        (12000, 8192, 8192),
        (4096, 8192, 4096),
    ],
)
async def test_messages_api_respects_optional_output_limits_and_omits_empty_system(
    monkeypatch,
    call_limit,
    model_limit,
    expected_limit,
):
    requests = install_stream_transport(monkeypatch, message_events("OK"))
    result = [
        event
        async for event in AnthropicMessagesAdapter().stream(
            request(
                instructions="",
                max_output_tokens=call_limit,
                model_max_output_tokens=model_limit,
            )
        )
    ]
    body = json.loads(requests[0].content)
    if expected_limit is None:
        assert "max_tokens" not in body
    else:
        assert body["max_tokens"] == expected_limit
    assert "system" not in body
    assert requests[0].headers["x-api-key"] == "secret"
    assert "authorization" not in requests[0].headers
    assert result[-1].kind == "completed"


@pytest.mark.asyncio
async def test_anthropic_thinking_blocks_are_not_mixed_into_text(monkeypatch):
    wire = stream(
        (
            "message_start",
            {"type": "message_start", "message": {"id": "msg_1", "usage": {}}},
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Let me reason"},
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "Answer"},
            },
        ),
        (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    install_stream_transport(monkeypatch, wire)
    result = [event async for event in AnthropicMessagesAdapter().stream(request())]
    assert [event.kind for event in result] == [
        "started",
        "reasoning_delta",
        "text_delta",
        "completed",
    ]
    assert result[1].data == "Let me reason"
    assert result[-1].data["text"] == "Answer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "expected_kind", "expected_reason"),
    [
        ("max_tokens", "incomplete", "max_output_tokens"),
        ("model_context_window_exceeded", "incomplete", "context_window"),
        ("pause_turn", "incomplete", "pause_turn"),
        ("refusal", "refused", None),
        ("tool_use", "failed", None),
    ],
)
async def test_messages_api_maps_terminal_reasons(
    monkeypatch, stop_reason, expected_kind, expected_reason
):
    install_stream_transport(
        monkeypatch,
        message_events("partial", stop_reason=stop_reason),
    )
    result = [event async for event in AnthropicMessagesAdapter().stream(request())]
    assert result[-1].kind == expected_kind
    if expected_reason:
        assert result[-1].data["reason"] == expected_reason


@pytest.mark.asyncio
async def test_messages_api_rejects_tool_blocks_and_incomplete_streams(monkeypatch):
    tool_wire = stream(
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "x"},
            },
        ),
    )
    install_stream_transport(monkeypatch, tool_wire)
    with pytest.raises(ProviderError) as exc:
        async for _ in AnthropicMessagesAdapter().stream(request()):
            pass
    assert exc.value.code == "UNSUPPORTED_TOOL_CALL"

    interrupted_wire = stream(
        (
            "message_start",
            {"type": "message_start", "message": {"id": "msg_1", "usage": {}}},
        ),
        (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        ),
    )
    install_stream_transport(monkeypatch, interrupted_wire)
    with pytest.raises(ProviderError) as exc:
        async for _ in AnthropicMessagesAdapter().stream(request()):
            pass
    assert exc.value.code == "PROVIDER_STREAM_INTERRUPTED"


@pytest.mark.asyncio
async def test_anthropic_stream_error_does_not_expose_provider_payload(monkeypatch):
    install_stream_transport(
        monkeypatch,
        stream(("error", {"type": "error", "error": {"message": "private detail"}})),
    )
    with pytest.raises(ProviderError) as exc:
        async for _ in AnthropicMessagesAdapter().stream(request()):
            pass
    assert exc.value.code == "PROVIDER_STREAM_ERROR"
    assert "private detail" not in str(exc.value)


@pytest.mark.asyncio
async def test_anthropic_http_errors_are_safe_and_status_aware(monkeypatch):
    requests = install_stream_transport(monkeypatch, b"secret body", status=529)
    del requests
    with pytest.raises(ProviderError) as exc:
        async for _ in AnthropicMessagesAdapter().stream(request()):
            pass
    assert exc.value.code == "PROVIDER_OVERLOADED"
    assert exc.value.status == 529
    assert "secret body" not in str(exc.value)


@pytest.mark.asyncio
async def test_anthropic_models_api_paginates_and_filters_retired(monkeypatch):
    requests = []
    responses = [
        {
            "data": [
                {
                    "id": "claude-1",
                    "display_name": "Claude One",
                    "max_input_tokens": 200000,
                    "max_tokens": 8192,
                    "capabilities": {"thinking": {"supported": True}},
                    "lifecycle": {"status": "active"},
                }
            ],
            "has_more": True,
            "last_id": "claude-1",
        },
        {
            "data": [
                {
                    "id": "claude-retired",
                    "lifecycle": {"status": "retired"},
                },
                {"id": "claude-2", "display_name": "Claude Two", "max_tokens": 4096},
            ],
            "has_more": False,
            "last_id": "claude-2",
        },
    ]

    def respond(http_request):
        requests.append(http_request)
        return httpx.Response(200, json=responses[len(requests) - 1])

    monkeypatch.setattr(
        "backend.app.provider_protocols.anthropic_messages.httpx.AsyncClient",
        lambda **kwargs: _ASYNC_CLIENT(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    adapter = AnthropicMessagesAdapter()
    models = await adapter.discover_models(
        base_url="https://api.anthropic.com/v1",
        api_key="secret",
        auth_mode="api_key_header",
    )
    assert [model["id"] for model in models] == ["claude-1", "claude-2"]
    assert models[0]["context_window_tokens"] == 200000
    assert models[0]["max_output_tokens"] == 8192
    assert models[0]["capabilities"]["thinking"]["supported"] is True
    assert "after_id=claude-1" in str(requests[1].url)
    assert all(request.headers["x-api-key"] == "secret" for request in requests)
    assert all(
        request.headers["anthropic-version"] == ANTHROPIC_VERSION
        for request in requests
    )


@pytest.mark.asyncio
async def test_anthropic_models_api_rejects_repeating_cursor(monkeypatch):
    response = {"data": [{"id": "claude-1"}], "has_more": True, "last_id": "claude-1"}
    monkeypatch.setattr(
        "backend.app.provider_protocols.anthropic_messages.httpx.AsyncClient",
        lambda **kwargs: _ASYNC_CLIENT(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
            **kwargs,
        ),
    )
    with pytest.raises(ProviderError) as exc:
        await AnthropicMessagesAdapter().discover_models(
            base_url="https://api.anthropic.com/v1",
            api_key="secret",
            auth_mode="bearer",
        )
    assert exc.value.code == "INVALID_MODEL_LIST"


def test_anthropic_schema_compiler_inlines_refs_and_preserves_nullable_union():
    original = {
        "$defs": {"Child": {"type": "object", "properties": {"x": {"type": "string"}}}},
        "type": "object",
        "properties": {
            "child": {"$ref": "#/$defs/Child"},
            "maybe": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        },
    }
    compiled = compile_anthropic_schema(original)
    assert compiled["required"] == ["child", "maybe"]
    assert compiled["properties"]["child"]["required"] == ["x"]
    assert "anyOf" in compiled["properties"]["maybe"]
    assert "$defs" in original


def test_anthropic_schema_compiler_moves_default_metadata_into_description():
    compiled = compile_anthropic_schema(
        {
            "type": "object",
            "properties": {"title": {"type": "string", "default": "Untitled"}},
        }
    )
    title = compiled["properties"]["title"]
    assert "default" not in title
    assert 'Default value: "Untitled".' in title["description"]


def test_anthropic_schema_compiler_rejects_external_and_recursive_refs():
    with pytest.raises(ProviderError) as exc:
        compile_anthropic_schema({"$ref": "https://example.com/schema.json"})
    assert exc.value.code == "UNSUPPORTED_JSON_SCHEMA"

    recursive = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"next": {"$ref": "#/$defs/Node"}},
            }
        },
        "$ref": "#/$defs/Node",
    }
    with pytest.raises(ProviderError) as exc:
        compile_anthropic_schema(recursive)
    assert exc.value.code == "UNSUPPORTED_JSON_SCHEMA"


def test_anthropic_reasoning_tiers_are_model_family_specific():
    adapter = AnthropicMessagesAdapter()
    assert [
        tier["code"] for tier in adapter.default_reasoning_tiers("claude-3-haiku")
    ] == ["off"]
    assert [
        tier["api_value"]
        for tier in adapter.default_reasoning_tiers(
            "claude-sonnet-4-5",
            {
                "effort": {
                    "supported": True,
                    "low": {"supported": True},
                    "medium": {"supported": True},
                    "high": {"supported": True},
                    "max": {"supported": True},
                    "xhigh": {"supported": True},
                }
            },
        )
    ] == [
        None,
        "low",
        "medium",
        "high",
        "max",
        "xhigh",
    ]
    assert adapter.build_body(request(reasoning_effort="thinking:adaptive"))[
        "thinking"
    ] == {"type": "adaptive"}
    with pytest.raises(ProviderError) as exc:
        adapter.build_body(
            request(reasoning_effort="thinking:budget:8192", max_output_tokens=8192)
        )
    assert exc.value.code == "INVALID_REASONING_BUDGET"


def test_anthropic_thinking_budget_does_not_introduce_an_output_limit():
    body = AnthropicMessagesAdapter.build_body(
        request(reasoning_effort="thinking:budget:16384")
    )
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert "max_tokens" not in body


def test_anthropic_thinking_budget_respects_model_output_limit():
    with pytest.raises(ProviderError) as exc:
        AnthropicMessagesAdapter.build_body(
            request(
                reasoning_effort="thinking:budget:2048", model_max_output_tokens=2048
            )
        )
    assert exc.value.code == "INVALID_REASONING_BUDGET"
