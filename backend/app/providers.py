from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import httpx

MAX_EVENT_BYTES = 512 * 1024
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))


@dataclass
class ProviderEvent:
    kind: str
    data: Any = None


class ProviderError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def validate_endpoint(base_url: str, allow_private_http: bool = False) -> str:
    try:
        parts = urlsplit(base_url.strip().rstrip("/"))
        if parts.port == 0 or any(char.isspace() for char in base_url) or "\\" in base_url:
            raise ValueError("invalid URL")
    except ValueError as exc:
        raise ProviderError("INVALID_ENDPOINT", "接入地址格式无效") from exc
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ProviderError("INVALID_ENDPOINT", "接入地址格式无效")
    if parts.path.rstrip("/").endswith(("/responses", "/models")):
        raise ProviderError("INVALID_ENDPOINT", "请填写 API 根地址，不包含 /responses 或 /models")
    if parts.scheme == "https":
        pass
    elif parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}:
        pass
    elif parts.scheme == "http" and allow_private_http:
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError as exc:
            raise ProviderError("INVALID_ENDPOINT", "HTTP 仅允许明确配置的私有 IP 地址") from exc
        if not any(address in network for network in PRIVATE_NETWORKS):
            raise ProviderError("INVALID_ENDPOINT", "HTTP 地址必须是私有局域网 IP")
    else:
        raise ProviderError("INVALID_ENDPOINT", "接入地址必须使用 HTTPS（本机回环地址或显式允许的私有 IP 可使用 HTTP）")
    return base_url.strip().rstrip("/")


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make every object property required, preserving nullable property types."""
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


async def bounded_lines(response: httpx.Response) -> AsyncIterator[str]:
    pending = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=8192):
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            if len(line) > MAX_EVENT_BYTES:
                raise ProviderError("PROVIDER_EVENT_TOO_LARGE", "供应商流事件超过大小限制")
            yield line.rstrip(b"\r").decode("utf-8")
        if len(pending) > MAX_EVENT_BYTES:
            raise ProviderError("PROVIDER_EVENT_TOO_LARGE", "供应商流事件超过大小限制")
    if pending:
        yield bytes(pending).rstrip(b"\r").decode("utf-8")
    # Dispatch a final event even when the server omits the trailing blank line.
    yield ""


async def stream_responses(
    *, base_url: str, api_key: str, model: str, instructions: str, prompt: str,
    max_output_tokens: int | None = None, json_schema: dict | None = None,
    reasoning_effort: str | None = None, auth_mode: str = "bearer", allow_private_http: bool = False,
) -> AsyncIterator[ProviderEvent]:
    base_url = validate_endpoint(base_url, allow_private_http=allow_private_http)
    if auth_mode not in {"bearer", "none"}:
        raise ProviderError("INVALID_AUTH_MODE", "不支持的认证方式")
    if auth_mode == "bearer" and not api_key:
        raise ProviderError("CREDENTIAL_NOT_CONFIGURED", "请先配置 API 凭证")
    body: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "stream": True,
        "store": False,
    }
    if reasoning_effort is not None:
        body["reasoning"] = {"effort": reasoning_effort}
    if max_output_tokens is not None:
        body["max_output_tokens"] = max_output_tokens
    if json_schema is not None:
        body["text"] = {"format": {"type": "json_schema", "name": "ppt_generation_spec", "strict": True, "schema": strict_json_schema(json_schema)}}
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    started = time.monotonic()
    terminal = False
    content: dict[tuple[int, int], list[str]] = {}
    try:
        timeout = httpx.Timeout(900.0, connect=15.0, read=900.0, write=15.0)
        async with asyncio.timeout(900):
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream("POST", f"{base_url}/responses", headers=headers, json=body) as response:
                    if response.status_code >= 300:
                        # Do not retain or return arbitrary provider response bodies: they can echo user data.
                        message = {401: "API 凭证无效", 403: "API 凭证没有调用权限", 404: "模型或 Responses 接口不存在", 429: "供应商限流或额度不足"}.get(response.status_code, "供应商请求失败")
                        raise ProviderError("PROVIDER_HTTP_ERROR", message, response.status_code)
                    event_name = "message"
                    data_lines: list[str] = []
                    event_bytes = 0
                    refused = False
                    yield ProviderEvent("started", {"model": model})
                    async for line in bounded_lines(response):
                        if not line:
                            if not data_lines:
                                event_name = "message"
                                continue
                            try:
                                payload = json.loads("\n".join(data_lines))
                            except json.JSONDecodeError as exc:
                                raise ProviderError("INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件") from exc
                            if not isinstance(payload, dict):
                                raise ProviderError("INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件")
                            kind = payload.get("type") or event_name
                            if kind == "response.output_text.delta":
                                key = (int(payload.get("output_index", 0)), int(payload.get("content_index", 0)))
                                delta = payload.get("delta", "")
                                if not isinstance(delta, str):
                                    raise ProviderError("INVALID_PROVIDER_EVENT", "供应商返回了无效的文本增量")
                                content.setdefault(key, []).append(delta)
                                if delta:
                                    yield ProviderEvent("text_delta", delta)
                            elif kind == "response.output_text.done":
                                key = (int(payload.get("output_index", 0)), int(payload.get("content_index", 0)))
                                text = payload.get("text", "")
                                if key not in content and isinstance(text, str):
                                    content[key] = [text]
                                    if text:
                                        yield ProviderEvent("text_delta", text)
                            elif kind in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
                                # Normalize supported Responses reasoning text and optional summaries.
                                delta = payload.get("delta", "")
                                if isinstance(delta, str) and delta:
                                    yield ProviderEvent("reasoning_delta", delta)
                            elif kind == "response.refusal.delta":
                                refused = True
                                yield ProviderEvent("refusal_delta", payload.get("delta", ""))
                            elif kind == "response.refusal.done":
                                refused = True
                            elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                                terminal = True
                                response_data = payload.get("response", {})
                                status = response_data.get("status", kind.split(".")[-1])
                                usage = response_data.get("usage") or {}
                                output = "".join("".join(v) for _, v in sorted(content.items()))
                                refused = refused or any(
                                    isinstance(part, dict) and part.get("type") == "refusal"
                                    for item in response_data.get("output", []) if isinstance(item, dict)
                                    for part in item.get("content", [])
                                )
                                if refused:
                                    yield ProviderEvent("refused", {"usage": usage})
                                elif kind == "response.completed" and status == "completed":
                                    yield ProviderEvent("completed", {"text": output, "response_id": response_data.get("id"), "usage": usage, "duration_ms": int((time.monotonic()-started)*1000)})
                                elif kind == "response.incomplete" or status == "incomplete":
                                    yield ProviderEvent("incomplete", {"text": output, "reason": (response_data.get("incomplete_details") or {}).get("reason"), "usage": usage})
                                else:
                                    yield ProviderEvent("failed", {"message": "模型返回失败终态", "usage": usage})
                            elif kind == "error":
                                terminal = True
                                yield ProviderEvent("failed", {"message": "供应商返回失败终态"})
                            data_lines, event_name, event_bytes = [], "message", 0
                            if terminal:
                                return
                            continue
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                        elif line.startswith("data:"):
                            event_bytes += len(line.encode("utf-8"))
                            if event_bytes > MAX_EVENT_BYTES:
                                raise ProviderError("PROVIDER_EVENT_TOO_LARGE", "供应商流事件超过大小限制")
                            data_lines.append(line[5:].lstrip())
                    if not terminal:
                        raise ProviderError("PROVIDER_STREAM_INTERRUPTED", "供应商流在终态事件前中断")
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ProviderError("PROVIDER_TIMEOUT", "供应商请求超时") from exc
    except httpx.RequestError as exc:
        raise ProviderError("PROVIDER_CONNECTION_ERROR", "无法连接模型供应商") from exc
    except (UnicodeDecodeError, ValueError, TypeError, AttributeError) as exc:
        raise ProviderError("INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件") from exc

