from __future__ import annotations

import copy
import ipaddress
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from .base import ProviderError


MAX_EVENT_BYTES = 512 * 1024
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
)


def validate_endpoint(base_url: str, allow_private_http: bool = False) -> str:
    """Validate and normalize a provider API root URL."""

    try:
        parts = urlsplit(base_url.strip().rstrip("/"))
        if (
            parts.port == 0
            or any(char.isspace() for char in base_url)
            or "\\" in base_url
        ):
            raise ValueError("invalid URL")
    except ValueError as exc:
        raise ProviderError("INVALID_ENDPOINT", "接入地址格式无效") from exc
    if (
        not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ProviderError("INVALID_ENDPOINT", "接入地址格式无效")
    if parts.path.rstrip("/").endswith(("/responses", "/models")):
        raise ProviderError(
            "INVALID_ENDPOINT", "请填写 API 根地址，不包含 /responses 或 /models"
        )
    if parts.scheme == "https":
        pass
    elif parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}:
        pass
    elif parts.scheme == "http" and allow_private_http:
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError as exc:
            raise ProviderError(
                "INVALID_ENDPOINT", "HTTP 仅允许明确配置的私有 IP 地址"
            ) from exc
        if not any(address in network for network in PRIVATE_NETWORKS):
            raise ProviderError("INVALID_ENDPOINT", "HTTP 地址必须是私有局域网 IP")
    else:
        raise ProviderError(
            "INVALID_ENDPOINT",
            "接入地址必须使用 HTTPS（本机回环地址或显式允许的私有 IP 可使用 HTTP）",
        )
    return base_url.strip().rstrip("/")


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make every object property required without mutating the caller's schema."""

    result = copy.deepcopy(schema)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object" or "properties" in value:
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    return result


async def bounded_lines(response: Any) -> AsyncIterator[str]:
    """Yield UTF-8 SSE lines while bounding a single line/event in memory."""

    pending = bytearray()
    async for chunk in response.aiter_bytes():
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            if len(line) > MAX_EVENT_BYTES:
                raise ProviderError(
                    "PROVIDER_EVENT_TOO_LARGE", "供应商流事件超过大小限制"
                )
            yield line.rstrip(b"\r").decode("utf-8")
        if len(pending) > MAX_EVENT_BYTES:
            raise ProviderError("PROVIDER_EVENT_TOO_LARGE", "供应商流事件超过大小限制")
    if pending:
        yield bytes(pending).rstrip(b"\r").decode("utf-8")
    # Dispatch a final event even when the server omits the trailing blank line.
    yield ""
