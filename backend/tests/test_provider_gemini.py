import json

import httpx
import pytest

from backend.app.provider_protocols import ProviderError, ProviderRequest
from backend.app.provider_protocols.gemini_generate_content import (
    GeminiGenerateContentAdapter,
    compile_gemini_schema,
)
from backend.app.schemas import OutlineDocument


def request(**overrides):
    values = {
        "protocol": "gemini_generate_content",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": "secret",
        "model": "gemini-2.5-flash",
        "instructions": "system instructions",
        "prompt": "user prompt",
        "auth_mode": "api_key_header",
    }
    values.update(overrides)
    return ProviderRequest(**values)


def sse(*payloads):
    return b"".join(
        b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_gemini_stream_uses_header_auth_and_normalizes_output_and_usage(
    monkeypatch,
):
    requests = []
    wire = sse(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "internal thought", "thought": True},
                            {"text": "Hello"},
                        ]
                    }
                }
            ]
        },
        {
            "responseId": "gemini-response-1",
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 2,
                "totalTokenCount": 5,
            },
            "candidates": [
                {"content": {"parts": [{"text": " world"}]}, "finishReason": "STOP"}
            ],
        },
    )
    client_type = httpx.AsyncClient

    def respond(http_request):
        requests.append(http_request)
        return httpx.Response(
            200, content=wire, headers={"content-type": "text/event-stream"}
        )

    monkeypatch.setattr(
        "backend.app.provider_protocols.gemini_generate_content.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = [event async for event in GeminiGenerateContentAdapter().stream(request())]

    sent = requests[0]
    body = json.loads(sent.content)
    assert sent.url.path == "/v1beta/models/gemini-2.5-flash:streamGenerateContent"
    assert sent.url.params["alt"] == "sse"
    assert sent.headers["x-goog-api-key"] == "secret"
    assert "authorization" not in sent.headers
    assert body["systemInstruction"] == {"parts": [{"text": "system instructions"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "user prompt"}]}]
    assert [event.kind for event in result] == [
        "started",
        "text_delta",
        "text_delta",
        "completed",
    ]
    assert "internal thought" not in "".join(
        str(event.data) for event in result if event.kind == "text_delta"
    )
    assert result[-1].data["text"] == "Hello world"
    assert result[-1].data["response_id"] == "gemini-response-1"
    assert result[-1].data["usage"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
    }


@pytest.mark.asyncio
async def test_gemini_maps_max_tokens_to_incomplete(monkeypatch):
    wire = sse(
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": "partial"}]},
                    "finishReason": "MAX_TOKENS",
                }
            ]
        }
    )
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.provider_protocols.gemini_generate_content.httpx.AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=wire)),
            **kwargs,
        ),
    )
    result = [event async for event in GeminiGenerateContentAdapter().stream(request())]
    assert result[-1].kind == "incomplete"
    assert result[-1].data["reason"] == "max_output_tokens"
    assert result[-1].data["text"] == "partial"


def test_gemini_builds_structured_output_schema_and_supported_reasoning_settings():
    schema = OutlineDocument.model_json_schema()
    body = GeminiGenerateContentAdapter.build_body(
        request(
            json_schema=schema,
            reasoning_effort="16384",
            max_output_tokens=4096,
        )
    )
    compiled = body["generationConfig"]["responseSchema"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["maxOutputTokens"] == 4096
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 16384}
    assert compiled["type"] == "OBJECT"
    assert compiled["properties"]["slides"]["type"] == "ARRAY"
    assert compiled["properties"]["slides"]["items"]["properties"]["page_type"]["enum"]
    assert "default" not in json.dumps(compiled)

    gemini3 = GeminiGenerateContentAdapter.build_body(
        request(model="gemini-3-pro-preview", reasoning_effort="HIGH")
    )
    assert gemini3["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "HIGH"}


def test_gemini_rejects_unsupported_reasoning_and_unsafe_model_paths():
    adapter = GeminiGenerateContentAdapter()
    assert [
        tier["code"] for tier in adapter.default_reasoning_tiers("gemini-1.5-pro")
    ] == ["off"]
    assert [
        tier["api_value"]
        for tier in adapter.default_reasoning_tiers("models/gemini-3-pro-preview")
    ] == [None, "LOW", "HIGH"]
    with pytest.raises(ProviderError, match="Token Budget"):
        adapter.build_body(
            request(model="gemini-3-pro-preview", reasoning_effort="8192")
        )
    with pytest.raises(ProviderError):
        adapter.build_body(request(model="gemini-1.5-pro", reasoning_effort="high"))
    with pytest.raises(ProviderError) as exc:
        adapter._model_resource("models/gemini-pro/other")
    assert exc.value.code == "INVALID_MODEL_ID"


def test_gemini_schema_rejects_unresolved_refs_and_unsupported_keywords():
    with pytest.raises(ProviderError) as unresolved:
        compile_gemini_schema({"$ref": "#/$defs/Missing", "$defs": {}})
    assert unresolved.value.code == "UNSUPPORTED_JSON_SCHEMA"
    with pytest.raises(ProviderError) as unsupported:
        compile_gemini_schema({"type": "object", "unevaluatedProperties": False})
    assert unsupported.value.code == "UNSUPPORTED_JSON_SCHEMA"


@pytest.mark.asyncio
async def test_gemini_discovery_pages_and_filters_models_not_supporting_generate_content(
    monkeypatch,
):
    requests = []

    async def fetch_json(url, *, headers, base_url, allow_private_http=False):
        requests.append((url, headers, base_url, allow_private_http))
        if "pageToken=next%2Bpage" in url:
            return {
                "models": [
                    {
                        "name": "models/gemini-2.5-flash",
                        "displayName": "Gemini Flash",
                        "inputTokenLimit": 1048576,
                        "outputTokenLimit": 65536,
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-invalid/nested",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            }
        return {
            "models": [
                {
                    "name": "models/embedding-001",
                    "supportedGenerationMethods": ["embedContent"],
                }
            ],
            "nextPageToken": "next+page",
        }

    monkeypatch.setattr(
        "backend.app.provider_protocols.gemini_generate_content.fetch_json", fetch_json
    )
    result = await GeminiGenerateContentAdapter().discover_models(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="secret",
        auth_mode="api_key_header",
    )
    assert result == [
        {
            "id": "gemini-2.5-flash",
            "name": "Gemini Flash",
            "context_window_tokens": 1048576,
            "max_output_tokens": 65536,
        }
    ]
    assert len(requests) == 2
    assert all(item[1]["x-goog-api-key"] == "secret" for item in requests)


def test_provider_create_uses_protocol_default_auth_mode():
    from backend.app.provider_api import ProviderCreate

    provider = ProviderCreate(
        name="Gemini",
        protocol="gemini_generate_content",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key=" test-key ",
    )
    assert provider.auth_mode == "api_key_header"
    assert provider.api_key == "test-key"
