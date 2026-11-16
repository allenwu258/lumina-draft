"""Backward-compatible provider streaming façade.

The orchestration code historically imported ``stream_responses`` directly
from this module.  The actual protocol implementation now lives in
``provider_protocols``; this module keeps the stable public surface while
dispatching through the protocol registry.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from .provider_protocols import (
    PROTOCOL_REGISTRY,
    ProviderError,
    ProviderEvent,
    ProviderRequest,
    get_protocol_adapter,
)
from .provider_protocols.transport import (
    MAX_EVENT_BYTES,
    bounded_lines,
    strict_json_schema,
    validate_endpoint,
)


async def stream_provider(request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
    """Dispatch a normalized request to its registered protocol adapter."""

    async for event in PROTOCOL_REGISTRY.stream(request):
        yield event


async def stream_responses(
    *,
    base_url: str,
    api_key: str,
    model: str,
    instructions: str,
    prompt: str,
    max_output_tokens: int | None = None,
    json_schema: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
    auth_mode: str = "bearer",
    allow_private_http: bool = False,
    protocol: str = "openai_responses",
    structured_output_mode: str = "native_json_schema",
) -> AsyncIterator[ProviderEvent]:
    """Compatibility entry point for the Responses-only migration stage.

    New code should create :class:`ProviderRequest` and use
    :func:`stream_provider`; the keyword API remains intentionally available
    for existing call sites and test seams.
    """

    request = ProviderRequest(
        protocol=protocol,
        base_url=base_url,
        api_key=api_key,
        model=model,
        instructions=instructions,
        prompt=prompt,
        max_output_tokens=max_output_tokens,
        json_schema=json_schema,
        reasoning_effort=reasoning_effort,
        auth_mode=auth_mode,
        allow_private_http=allow_private_http,
        structured_output_mode=structured_output_mode,
    )
    async for event in stream_provider(request):
        yield event


__all__ = [
    "MAX_EVENT_BYTES",
    "ProviderError",
    "ProviderEvent",
    "ProviderRequest",
    "bounded_lines",
    "get_protocol_adapter",
    "httpx",
    "stream_provider",
    "stream_responses",
    "strict_json_schema",
    "validate_endpoint",
]
