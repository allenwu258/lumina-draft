from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, ClassVar


@dataclass
class ProviderEvent:
    """Protocol-neutral event emitted by a provider adapter."""

    kind: str
    data: Any = None


class ProviderError(Exception):
    """Stable, user-safe error raised while talking to a provider."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Normalized request context shared by every protocol adapter.

    The orchestration layer creates this object once.  Adapters are responsible
    only for translating it into their wire format and normalizing the response
    back to :class:`ProviderEvent` values.
    """

    protocol: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    instructions: str
    prompt: str
    json_schema: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    auth_mode: str = "bearer"
    allow_private_http: bool = False
    structured_output_mode: str = "native_json_schema"

    def as_legacy_kwargs(self) -> dict[str, Any]:
        """Expose the old keyword contract during the migration.

        Existing callers and test seams can continue to replace
        ``stream_responses`` while the internal request context is already
        normalized.  This boundary can be removed once those callers migrate.
        """

        return {
            "protocol": self.protocol,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "instructions": self.instructions,
            "prompt": self.prompt,
            "json_schema": self.json_schema,
            "reasoning_effort": self.reasoning_effort,
            "auth_mode": self.auth_mode,
            "allow_private_http": self.allow_private_http,
            "structured_output_mode": self.structured_output_mode,
            **(
                {"max_output_tokens": self.max_output_tokens}
                if self.max_output_tokens is not None
                else {}
            ),
        }


class ProviderAdapter(ABC):
    """Adapter contract implemented by one provider wire protocol."""

    protocol: ClassVar[str]
    label: ClassVar[str]
    auth_modes: ClassVar[tuple[str, ...]] = ("bearer", "none")
    default_auth_mode: ClassVar[str] = "bearer"
    default_base_url: ClassVar[str | None] = None

    def default_reasoning_tiers(self, model_id: str) -> list[dict[str, Any]]:
        return [
            {"code": "off", "label": "Off", "api_value": None, "sort_order": 0},
            {"code": "low", "label": "Low", "api_value": "low", "sort_order": 1},
            {"code": "medium", "label": "Medium", "api_value": "medium", "sort_order": 2},
            {"code": "high", "label": "High", "api_value": "high", "sort_order": 3},
        ]

    @abstractmethod
    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        """Stream a normalized provider response."""

        raise NotImplementedError

    async def discover_models(
        self,
        *,
        base_url: str,
        api_key: str,
        auth_mode: str,
        allow_private_http: bool = False,
    ) -> list[dict[str, Any]]:
        """Return provider model records normalized to the management API shape."""

        raise ProviderError(
            "MODEL_DISCOVERY_UNSUPPORTED", "当前 Provider 协议不支持模型发现"
        )
