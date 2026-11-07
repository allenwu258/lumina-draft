import asyncio
import json

import httpx
import pytest

from backend.app import providers


class GatedSSEStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk: bytes, final_chunk: bytes):
        self.first_chunk = first_chunk
        self.final_chunk = final_chunk
        self.first_chunk_sent = asyncio.Event()
        self.release_final_chunk = asyncio.Event()

    async def __aiter__(self):
        self.first_chunk_sent.set()
        yield self.first_chunk
        await self.release_final_chunk.wait()
        yield self.final_chunk

    async def aclose(self):
        self.release_final_chunk.set()


@pytest.mark.asyncio
async def test_responses_stream_requires_terminal_event(monkeypatch):
    payload = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hello","output_index":0,"content_index":0}\n\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"}))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    with pytest.raises(providers.ProviderError, match="终态事件前中断"):
        async for _ in providers.stream_responses(base_url="https://example.com", api_key="secret", model="model", instructions="system", prompt="hello", max_output_tokens=32):
            pass


@pytest.mark.asyncio
async def test_responses_stream_reports_usage(monkeypatch):
    events = [
        {"type": "response.output_text.delta", "delta": "hello", "output_index": 0, "content_index": 0},
        {"type": "response.output_text.done", "text": "hello"},
        {"type": "response.completed", "response": {"id": "r1", "status": "completed", "usage": {"input_tokens": 2, "output_tokens": 1}}},
    ]
    wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"}))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    result = [event async for event in providers.stream_responses(base_url="https://example.com/v1", api_key="secret", model="model", instructions="system", prompt="hello", max_output_tokens=32)]
    assert [e.kind for e in result] == ["started", "text_delta", "completed"]
    assert result[-1].data["text"] == "hello"
    assert result[-1].data["usage"]["output_tokens"] == 1


@pytest.mark.asyncio
async def test_responses_stream_yields_small_delta_before_response_finishes(monkeypatch):
    delta = {
        "type": "response.output_text.delta",
        "delta": "first token",
        "output_index": 0,
        "content_index": 0,
    }
    completed = {
        "type": "response.completed",
        "response": {"status": "completed", "usage": {}},
    }
    stream = GatedSSEStream(
        f"event: {delta['type']}\ndata: {json.dumps(delta)}\n\n".encode(),
        f"event: {completed['type']}\ndata: {json.dumps(completed)}\n\n".encode(),
    )
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        providers.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    stream=stream,
                    headers={"content-type": "text/event-stream"},
                )
            ),
            **kwargs,
        ),
    )

    events = providers.stream_responses(
        base_url="https://example.com/v1",
        api_key="secret",
        model="model",
        instructions="system",
        prompt="hello",
    )
    assert (await anext(events)).kind == "started"
    next_event = asyncio.create_task(anext(events))
    try:
        await asyncio.wait_for(stream.first_chunk_sent.wait(), timeout=1)
        done, _ = await asyncio.wait({next_event}, timeout=0.1)
        assert next_event in done, "small SSE deltas must not wait for later response bytes"
        event = await next_event
        assert (event.kind, event.data) == ("text_delta", "first token")
    finally:
        stream.release_final_chunk.set()
        if not next_event.done():
            await asyncio.gather(next_event, return_exceptions=True)

    result = [event async for event in events]
    assert result[-1].kind == "completed"


@pytest.mark.asyncio
async def test_responses_streams_reasoning_and_final_text_separately(monkeypatch):
    events = [
        {"type": "response.reasoning_text.delta", "delta": "先分析结构。", "output_index": 0},
        {"type": "response.output_text.delta", "delta": "{\"title\":", "output_index": 1, "content_index": 0},
        {"type": "response.completed", "response": {"status": "completed", "usage": {}}},
    ]
    wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events).encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"}))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))

    result = [event async for event in providers.stream_responses(base_url="https://example.com", api_key="secret", model="model", instructions="system", prompt="hello")]

    assert [(event.kind, event.data) for event in result if event.kind.endswith("delta")] == [
        ("reasoning_delta", "先分析结构。"),
        ("text_delta", "{\"title\":"),
    ]


@pytest.mark.asyncio
async def test_responses_stream_accepts_reasoning_summary_text_delta(monkeypatch):
    events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "计划摘要。", "output_index": 0, "summary_index": 0},
        {"type": "response.completed", "response": {"status": "completed", "usage": {}}},
    ]
    wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events).encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"}))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))

    result = [event async for event in providers.stream_responses(base_url="https://example.com/v1", api_key="secret", model="model", instructions="system", prompt="hello")]

    assert [(event.kind, event.data) for event in result if event.kind.endswith("delta")] == [("reasoning_delta", "计划摘要。")]


@pytest.mark.asyncio
@pytest.mark.parametrize("token_limit", [None, 32])
async def test_responses_omits_unset_output_limit(monkeypatch, token_limit):
    requests = []
    event = {"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1}}}
    wire = f"event: response.completed\ndata: {json.dumps(event)}\n\n".encode()
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})
    transport = httpx.MockTransport(respond)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    async for _ in providers.stream_responses(base_url="https://example.com", api_key="secret", model="model", instructions="system", prompt="hello", max_output_tokens=token_limit):
        pass
    if token_limit is None:
        assert "max_output_tokens" not in requests[0]
    else:
        assert requests[0]["max_output_tokens"] == token_limit


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "none", "low", "high", "max"])
async def test_responses_reasoning_effort(monkeypatch, effort):
    requests = []
    event = {"type": "response.completed", "response": {"status": "completed", "usage": {}}}
    wire = f"event: response.completed\ndata: {json.dumps(event)}\n\n".encode()

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(respond)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    async for _ in providers.stream_responses(base_url="https://example.com", api_key="secret", model="model", instructions="system", prompt="hello", reasoning_effort=effort):
        pass
    if effort is None:
        assert "reasoning" not in requests[0]
    else:
        assert requests[0]["reasoning"] == {"effort": effort}


@pytest.mark.asyncio
async def test_responses_no_auth_and_strict_nested_schema(monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string", "default": "x"}}}}}}
    result = [event async for event in providers.stream_responses(base_url="https://example.com/v1", api_key="", model="m", instructions="i", prompt="p", auth_mode="none", json_schema=schema)]
    assert result[-1].kind == "completed"
    assert "authorization" not in requests[0].headers
    payload = json.loads(requests[0].content)
    strict = payload["text"]["format"]["schema"]
    assert payload["text"]["format"]["strict"] is True
    assert strict["required"] == ["items"] and strict["additionalProperties"] is False
    assert strict["properties"]["items"]["items"]["required"] == ["name"]
    assert "default" not in strict["properties"]["items"]["items"]["properties"]["name"]
    assert "required" not in schema


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [
    [{"type": "response.refusal.done", "refusal": "No"}, {"type": "response.completed", "response": {"status": "completed"}}],
    [{"type": "response.completed", "response": {"status": "completed", "output": [{"content": [{"type": "refusal", "refusal": "No"}]}]}}],
])
async def test_refusal_is_not_a_success(monkeypatch, events):
    wire = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=wire)), **kwargs))
    result = [event async for event in providers.stream_responses(base_url="https://example.com", api_key="k", model="m", instructions="i", prompt="p")]
    assert result[-1].kind == "refused"


@pytest.mark.asyncio
async def test_sse_line_size_is_bounded(monkeypatch):
    wire = b"data: " + b"a" * (providers.MAX_EVENT_BYTES + 1)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=wire)), **kwargs))
    with pytest.raises(providers.ProviderError) as error:
        async for _ in providers.stream_responses(base_url="https://example.com", api_key="k", model="m", instructions="i", prompt="p"):
            pass
    assert error.value.code == "PROVIDER_EVENT_TOO_LARGE"


@pytest.mark.asyncio
async def test_final_event_without_blank_line_preserves_refusal(monkeypatch):
    event = {"type": "response.completed", "response": {"status": "completed", "output": [{"content": [{"type": "refusal", "refusal": "No"}]}]}}
    client_type = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=("data: " + json.dumps(event)).encode())), **kwargs))
    result = [event async for event in providers.stream_responses(base_url="https://example.com", api_key="k", model="m", instructions="i", prompt="p")]
    assert result[-1].kind == "refused"
