import json

import httpx
import pytest

from backend.app import providers


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
