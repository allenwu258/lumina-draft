from backend.app.provider_protocols import (
    PROTOCOL_REGISTRY,
    ProviderRequest,
    get_protocol_adapter,
    supports_protocol,
)


def test_responses_protocol_is_registered_once():
    assert supports_protocol("openai_responses")
    assert supports_protocol("openai_chat_completions")
    adapter = get_protocol_adapter("openai_responses")
    assert adapter.protocol == "openai_responses"
    assert [item.code for item in PROTOCOL_REGISTRY.descriptors()] == [
        "openai_responses",
        "openai_chat_completions",
    ]


def test_request_context_preserves_legacy_boundary_without_optional_limit():
    request = ProviderRequest(
        protocol="openai_responses",
        base_url="https://example.com/v1",
        api_key="secret",
        model="model",
        instructions="system",
        prompt="hello",
    )
    values = request.as_legacy_kwargs()
    assert values["protocol"] == "openai_responses"
    assert values["json_schema"] is None
    assert "max_output_tokens" not in values
    assert "secret" not in repr(request)
