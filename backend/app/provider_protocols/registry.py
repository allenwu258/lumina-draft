from __future__ import annotations

from dataclasses import dataclass

from collections.abc import AsyncIterator

from .base import ProviderAdapter, ProviderError, ProviderRequest, ProviderEvent


@dataclass(frozen=True, slots=True)
class ProtocolDescriptor:
    code: str
    label: str
    enabled: bool = True


class ProviderProtocolRegistry:
    """Registry that resolves protocol codes without coupling callers to adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        if not adapter.protocol:
            raise ValueError("provider adapter protocol is required")
        if adapter.protocol in self._adapters:
            raise ValueError(
                f"provider protocol already registered: {adapter.protocol}"
            )
        self._adapters[adapter.protocol] = adapter

    def get(self, protocol: str) -> ProviderAdapter:
        adapter = self._adapters.get(protocol)
        if adapter is None:
            raise ProviderError(
                "UNSUPPORTED_PROTOCOL", "当前 Provider 协议未注册适配器"
            )
        return adapter

    def supports(self, protocol: str) -> bool:
        return protocol in self._adapters

    def descriptors(self) -> list[ProtocolDescriptor]:
        return [
            ProtocolDescriptor(code=adapter.protocol, label=adapter.label)
            for adapter in self._adapters.values()
        ]

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        adapter = self.get(request.protocol)
        async for event in adapter.stream(request):
            yield event
