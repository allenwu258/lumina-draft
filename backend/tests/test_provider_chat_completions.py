import json

import httpx
import pytest

from backend.app.provider_protocols import ProviderError, ProviderRequest
from backend.app.provider_protocols.openai_chat_completions import (
    OpenAIChatCompletionsAdapter,
)


def request(**overrides):
    values = {
        "protocol": "openai_chat_completions",
        "base_url": "https://example.com/v1",
        "api_key": "secret",
        "model": "model",
        "instructions": "system instructions",
        "prompt": "user prompt",
    }
    values.update(overrides)
    return ProviderRequest(**values)


def sse(*events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode() + b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_chat_completions_builds_native_request_and_normalizes_usage(monkeypatch):
    requests = []
    wire = sse(
        {"id": "chatcmpl-1", "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
    )

    def respond(http_request):
        requests.append(http_request)
        return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.provider_protocols.openai_chat_completions.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string", "default": "example"}},
    }
    result = [
        event
        async for event in OpenAIChatCompletionsAdapter().stream(
            request(
                reasoning_effort="high",
                max_output_tokens=1200,
                json_schema=schema,
            )
        )
    ]

    sent = requests[0]
    body = json.loads(sent.content)
    assert sent.url.path == "/v1/chat/completions"
    assert sent.headers["Authorization"] == "Bearer secret"
    assert body["messages"] == [
        {"role": "system", "content": "system instructions"},
        {"role": "user", "content": "user prompt"},
    ]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["reasoning_effort"] == "high"
    assert body["max_completion_tokens"] == 1200
    assert "max_tokens" not in body
    assert body["response_format"]["type"] == "json_schema"
    strict = body["response_format"]["json_schema"]
    assert strict["strict"] is True
    assert strict["schema"]["required"] == ["title"]
    assert "default" not in strict["schema"]["properties"]["title"]
    assert "required" not in schema
    assert [event.kind for event in result] == ["started", "text_delta", "text_delta", "completed"]
    assert result[-1].data["text"] == "Hello world"
    assert result[-1].data["response_id"] == "chatcmpl-1"
    assert result[-1].data["usage"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
    }


@pytest.mark.asyncio
async def test_chat_completions_without_auth_and_output_limit_omits_optional_fields(monkeypatch):
    requests = []

    def respond(http_request):
        requests.append(http_request)
        return httpx.Response(200, content=sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.provider_protocols.openai_chat_completions.httpx.AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    result = [
        event
        async for event in OpenAIChatCompletionsAdapter().stream(
            request(api_key="", auth_mode="none")
        )
    ]
    body = json.loads(requests[0].content)
    assert "authorization" not in requests[0].headers
    assert "reasoning_effort" not in body
    assert "max_completion_tokens" not in body
    assert result[-1].kind == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_reason", "expected_kind", "expected_reason"),
    [
        ("length", "incomplete", "max_output_tokens"),
        ("content_filter", "incomplete", "content_filter"),
    ],
)
async def test_chat_completions_maps_non_success_finish_reasons(
    monkeypatch, finish_reason, expected_kind, expected_reason
):
    wire = sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": finish_reason}]})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.provider_protocols.openai_chat_completions.httpx.AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=wire)
            ),
            **kwargs,
        ),
    )
    result = [event async for event in OpenAIChatCompletionsAdapter().stream(request())]
    assert result[-1].kind == expected_kind
    assert result[-1].data["reason"] == expected_reason


@pytest.mark.asyncio
async def test_chat_completions_maps_refusal_and_rejects_tool_calls(monkeypatch):
    refusal_wire = sse({"choices": [{"delta": {"refusal": "I cannot help."}, "finish_reason": "stop"}]})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.provider_protocols.openai_chat_completions.httpx.AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=refusal_wire)
            ),
            **kwargs,
        ),
    )
    result = [event async for event in OpenAIChatCompletionsAdapter().stream(request())]
    assert [event.kind for event in result] == ["started", "refusal_delta", "refused"]

    tool_wire = sse({"choices": [{"delta": {"tool_calls": [{"index": 0}]}, "finish_reason": None}]})
    monkeypatch.setattr(
        "backend.app.provider_protocols.openai_chat_completions.httpx.AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=tool_wire)
            ),
            **kwargs,
        ),
    )
    with pytest.raises(ProviderError) as exc:
        async for _ in OpenAIChatCompletionsAdapter().stream(request()):
            pass
    assert exc.value.code == "UNSUPPORTED_TOOL_CALL"


@pytest.mark.asyncio
async def test_chat_completions_requires_done_marker(monkeypatch):
    wire = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"stop"}]}\n\n'
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.provider_protocols.openai_chat_completions.httpx.AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=wire)
            ),
            **kwargs,
        ),
    )
    with pytest.raises(ProviderError) as exc:
        async for _ in OpenAIChatCompletionsAdapter().stream(request()):
            pass
    assert exc.value.code == "PROVIDER_STREAM_INTERRUPTED"


@pytest.mark.asyncio
async def test_chat_completions_does_not_treat_missing_finish_reason_as_success(monkeypatch):
    wire = sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.provider_protocols.openai_chat_completions.httpx.AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=wire)
            ),
            **kwargs,
        ),
    )
    result = [event async for event in OpenAIChatCompletionsAdapter().stream(request())]
    assert result[-1].kind == "failed"
