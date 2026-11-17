"""Protocol adapters for model providers.

The application talks to providers through a small, protocol-neutral contract.
Concrete wire formats live in this package so the orchestration layer does not
need to know how a provider transports messages or streams events.
"""

from .base import ProviderAdapter, ProviderError, ProviderEvent, ProviderRequest
from .anthropic_messages import AnthropicMessagesAdapter
from .openai_chat_completions import OpenAIChatCompletionsAdapter
from .openai_responses import OpenAIResponsesAdapter
from .gemini_generate_content import GeminiGenerateContentAdapter
from .registry import ProviderProtocolRegistry


PROTOCOL_REGISTRY = ProviderProtocolRegistry()
PROTOCOL_REGISTRY.register(OpenAIResponsesAdapter())
PROTOCOL_REGISTRY.register(OpenAIChatCompletionsAdapter())
PROTOCOL_REGISTRY.register(GeminiGenerateContentAdapter())
PROTOCOL_REGISTRY.register(AnthropicMessagesAdapter())


def get_protocol_adapter(protocol: str) -> ProviderAdapter:
    """Return the registered adapter for ``protocol``."""

    return PROTOCOL_REGISTRY.get(protocol)


def supports_protocol(protocol: str) -> bool:
    """Return whether a protocol has a registered adapter."""

    return PROTOCOL_REGISTRY.supports(protocol)


__all__ = [
    "OpenAIResponsesAdapter",
    "OpenAIChatCompletionsAdapter",
    "GeminiGenerateContentAdapter",
    "AnthropicMessagesAdapter",
    "PROTOCOL_REGISTRY",
    "ProviderAdapter",
    "ProviderError",
    "ProviderEvent",
    "ProviderProtocolRegistry",
    "ProviderRequest",
    "get_protocol_adapter",
    "supports_protocol",
]
