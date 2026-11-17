from __future__ import annotations

import copy
import ipaddress
import json
import asyncio
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import httpx

from .base import ProviderError


MAX_EVENT_BYTES = 512 * 1024
MAX_MODEL_LIST_BYTES = 2 * 1024 * 1024
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
    if parts.path.rstrip("/").endswith(
        ("/responses", "/chat/completions", "/models")
    ):
        raise ProviderError(
            "INVALID_ENDPOINT",
            "请填写 API 根地址，不包含 /responses、/chat/completions 或 /models",
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


async def fetch_json(
    url: str,
    *,
    headers: dict[str, str],
    base_url: str,
    allow_private_http: bool = False,
) -> Any:
    """Fetch a bounded JSON document from a validated provider endpoint."""

    validate_endpoint(base_url, allow_private_http=allow_private_http)
    try:
        timeout = httpx.Timeout(25.0, connect=10.0, read=20.0, write=10.0)
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
            ) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code >= 300:
                        code = {
                            401: "PROVIDER_UNAUTHORIZED",
                            403: "PROVIDER_FORBIDDEN",
                            404: "MODEL_DISCOVERY_UNSUPPORTED",
                            429: "PROVIDER_RATE_LIMITED",
                        }.get(response.status_code, "MODEL_DISCOVERY_FAILED")
                        message = {
                            401: "API 凭证无效",
                            403: "API 凭证没有读取模型目录的权限",
                            404: "Provider 未提供模型目录接口",
                            429: "Provider 限流或额度不足",
                        }.get(response.status_code, "Provider 未能返回模型目录")
                        raise ProviderError(code, message, response.status_code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_MODEL_LIST_BYTES:
                            raise ProviderError(
                                "MODEL_LIST_TOO_LARGE", "模型目录响应超过 2 MiB"
                            )
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(
                "INVALID_MODEL_LIST", "Provider 返回的模型目录格式无效"
            ) from exc
    except ProviderError:
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ProviderError("MODEL_DISCOVERY_TIMEOUT", "获取模型目录超时") from exc
    except httpx.RequestError as exc:
        raise ProviderError(
            "MODEL_DISCOVERY_CONNECTION_FAILED", "无法连接 Provider"
        ) from exc
