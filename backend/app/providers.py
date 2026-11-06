from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import httpx

from .config import settings


@dataclass
class ProviderEvent:
    kind: str
    data: Any = None


class ProviderError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def validate_endpoint(base_url: str) -> str:
    parts = urlsplit(base_url.strip().rstrip("/"))
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}):
        raise ProviderError("INVALID_ENDPOINT", "接入地址必须使用 HTTPS（本机回环地址可使用 HTTP）")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ProviderError("INVALID_ENDPOINT", "接入地址格式无效")
    return base_url.strip().rstrip("/")


async def stream_responses(
    *, base_url: str, api_key: str, model: str, instructions: str, prompt: str,
    max_output_tokens: int | None = None, json_schema: dict | None = None,
    reasoning_effort: str | None = None,
) -> AsyncIterator[ProviderEvent]:
    base_url = validate_endpoint(base_url)
    if not api_key:
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
        body["text"] = {"format": {"type": "json_schema", "name": "ppt_generation_spec", "schema": json_schema}}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"}
    started = time.monotonic()
    terminal = False
    content: dict[tuple[int, int], list[str]] = {}
    try:
        timeout = httpx.Timeout(180.0, connect=15.0, read=180.0, write=15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("POST", f"{base_url}/responses", headers=headers, json=body) as response:
                if response.status_code >= 400:
                    # Do not retain or return arbitrary provider response bodies: they can echo user data.
                    await response.aread()
                    message = {401: "API 凭证无效", 403: "API 凭证没有调用权限", 404: "模型或 Responses 接口不存在", 429: "供应商限流或额度不足"}.get(response.status_code, "供应商请求失败")
                    raise ProviderError("PROVIDER_HTTP_ERROR", message, response.status_code)
                event_name = "message"
                data_lines: list[str] = []
                yield ProviderEvent("started", {"model": model})
                async for line in response.aiter_lines():
                    if not line:
                        if not data_lines:
                            event_name = "message"
                            continue
                        try:
                            payload = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError:
                            data_lines, event_name = [], "message"
                            continue
                        kind = payload.get("type") or event_name
                        if kind == "response.output_text.delta":
                            key = (int(payload.get("output_index", 0)), int(payload.get("content_index", 0)))
                            delta = payload.get("delta", "")
                            if not isinstance(delta, str):
                                raise ProviderError("INVALID_PROVIDER_EVENT", "供应商返回了无效的文本增量")
                            content.setdefault(key, []).append(delta)
                            if delta:
                                yield ProviderEvent("text_delta", delta)
                        elif kind in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
                            # Normalize supported Responses reasoning text and optional summaries.
                            delta = payload.get("delta", "")
                            if isinstance(delta, str) and delta:
                                yield ProviderEvent("reasoning_delta", delta)
                        elif kind == "response.refusal.delta":
                            yield ProviderEvent("refusal_delta", payload.get("delta", ""))
                        elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                            terminal = True
                            response_data = payload.get("response", {})
                            status = response_data.get("status", kind.split(".")[-1])
                            usage = response_data.get("usage") or {}
                            output = "".join("".join(v) for _, v in sorted(content.items()))
                            if kind == "response.completed" and status == "completed":
                                yield ProviderEvent("completed", {"text": output, "response_id": response_data.get("id"), "usage": usage, "duration_ms": int((time.monotonic()-started)*1000)})
                            elif kind == "response.incomplete" or status == "incomplete":
                                yield ProviderEvent("incomplete", {"text": output, "reason": (response_data.get("incomplete_details") or {}).get("reason"), "usage": usage})
                            else:
                                yield ProviderEvent("failed", {"message": "模型返回失败终态", "usage": usage})
                        elif kind in {"response.failed", "error"}:
                            terminal = True
                            yield ProviderEvent("failed", {"message": "供应商返回失败终态"})
                        data_lines, event_name = [], "message"
                        if terminal:
                            return
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                if data_lines:
                    try:
                        payload = json.loads("\n".join(data_lines))
                        kind = payload.get("type", event_name)
                        if kind in {"response.completed", "response.incomplete", "response.failed"}:
                            terminal = True
                            response_data = payload.get("response", {})
                            usage = response_data.get("usage") or {}
                            output = "".join("".join(v) for _, v in sorted(content.items()))
                            if kind == "response.completed" and response_data.get("status") == "completed":
                                yield ProviderEvent("completed", {"text": output, "response_id": response_data.get("id"), "usage": usage, "duration_ms": int((time.monotonic()-started)*1000)})
                            else:
                                yield ProviderEvent("incomplete" if kind == "response.incomplete" else "failed", {"text": output, "reason": (response_data.get("incomplete_details") or {}).get("reason"), "usage": usage})
                    except (json.JSONDecodeError, TypeError):
                        pass
                if not terminal:
                    raise ProviderError("PROVIDER_STREAM_INTERRUPTED", "供应商流在终态事件前中断")
    except httpx.TimeoutException as exc:
        raise ProviderError("PROVIDER_TIMEOUT", "供应商请求超时") from exc
    except httpx.RequestError as exc:
        raise ProviderError("PROVIDER_CONNECTION_ERROR", "无法连接模型供应商") from exc

