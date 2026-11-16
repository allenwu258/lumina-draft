from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from .base import ProviderAdapter, ProviderError, ProviderEvent, ProviderRequest
from .transport import (
    MAX_EVENT_BYTES,
    bounded_lines,
    strict_json_schema,
    validate_endpoint,
)


class OpenAIResponsesAdapter(ProviderAdapter):
    """OpenAI Responses streaming protocol adapter."""

    protocol = "openai_responses"
    label = "OpenAI Responses"

    @staticmethod
    def build_body(request: ProviderRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "instructions": request.instructions,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": request.prompt}],
                }
            ],
            "stream": True,
            "store": False,
        }
        if request.reasoning_effort is not None:
            body["reasoning"] = {"effort": request.reasoning_effort}
        if request.max_output_tokens is not None:
            body["max_output_tokens"] = request.max_output_tokens
        if request.json_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "ppt_generation_spec",
                    "strict": True,
                    "schema": strict_json_schema(request.json_schema),
                }
            }
        return body

    @staticmethod
    def _headers(request: ProviderRequest) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if request.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {request.api_key}"
        return headers

    @staticmethod
    def _http_error(status: int) -> ProviderError:
        message = {
            401: "API 凭证无效",
            403: "API 凭证没有调用权限",
            404: "模型或 Responses 接口不存在",
            429: "供应商限流或额度不足",
        }.get(status, "供应商请求失败")
        return ProviderError("PROVIDER_HTTP_ERROR", message, status)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        base_url = validate_endpoint(
            request.base_url,
            allow_private_http=request.allow_private_http,
        )
        if request.auth_mode not in {"bearer", "none"}:
            raise ProviderError("INVALID_AUTH_MODE", "不支持的认证方式")
        if request.auth_mode == "bearer" and not request.api_key:
            raise ProviderError("CREDENTIAL_NOT_CONFIGURED", "请先配置 API 凭证")

        body = self.build_body(request)
        headers = self._headers(request)
        started = time.monotonic()
        terminal = False
        content: dict[tuple[int, int], list[str]] = {}

        try:
            timeout = httpx.Timeout(900.0, connect=15.0, read=900.0, write=15.0)
            async with asyncio.timeout(900):
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        f"{base_url}/responses",
                        headers=headers,
                        json=body,
                    ) as response:
                        if response.status_code >= 300:
                            # Do not retain or return arbitrary provider response
                            # bodies: they can echo user data.
                            raise self._http_error(response.status_code)

                        event_name = "message"
                        data_lines: list[str] = []
                        event_bytes = 0
                        refused = False
                        yield ProviderEvent("started", {"model": request.model})

                        async for line in bounded_lines(response):
                            if not line:
                                if not data_lines:
                                    event_name = "message"
                                    continue
                                payload = self._decode_event(data_lines)
                                kind = payload.get("type") or event_name
                                handled = self._handle_event(
                                    kind=kind,
                                    payload=payload,
                                    content=content,
                                    refused=refused,
                                    started=started,
                                )
                                # _handle_event returns a state object to keep
                                # terminal and refusal handling in one place.
                                refused = handled.refused
                                for event in handled.events:
                                    yield event
                                data_lines, event_name, event_bytes = [], "message", 0
                                terminal = handled.terminal
                                if terminal:
                                    return
                                continue
                            if line.startswith("event:"):
                                event_name = line[6:].strip()
                            elif line.startswith("data:"):
                                event_bytes += len(line.encode("utf-8"))
                                if event_bytes > MAX_EVENT_BYTES:
                                    raise ProviderError(
                                        "PROVIDER_EVENT_TOO_LARGE",
                                        "供应商流事件超过大小限制",
                                    )
                                data_lines.append(line[5:].lstrip())
                        if not terminal:
                            raise ProviderError(
                                "PROVIDER_STREAM_INTERRUPTED",
                                "供应商流在终态事件前中断",
                            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ProviderError("PROVIDER_TIMEOUT", "供应商请求超时") from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                "PROVIDER_CONNECTION_ERROR", "无法连接模型供应商"
            ) from exc
        except (UnicodeDecodeError, ValueError, TypeError, AttributeError) as exc:
            raise ProviderError(
                "INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件"
            ) from exc

    @staticmethod
    def _decode_event(data_lines: list[str]) -> dict[str, Any]:
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError("INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件")
        return payload

    @staticmethod
    def _handle_event(
        *,
        kind: str,
        payload: dict[str, Any],
        content: dict[tuple[int, int], list[str]],
        refused: bool,
        started: float,
    ) -> "_EventHandlingResult":
        """Normalize one Responses event.

        The small result object keeps mutable parser state outside the event
        mapping logic and makes the adapter straightforward to extend.
        """

        events: list[ProviderEvent] = []
        terminal = False
        if kind == "response.output_text.delta":
            key = (
                int(payload.get("output_index", 0)),
                int(payload.get("content_index", 0)),
            )
            delta = payload.get("delta", "")
            if not isinstance(delta, str):
                raise ProviderError(
                    "INVALID_PROVIDER_EVENT", "供应商返回了无效的文本增量"
                )
            content.setdefault(key, []).append(delta)
            if delta:
                events.append(ProviderEvent("text_delta", delta))
        elif kind == "response.output_text.done":
            key = (
                int(payload.get("output_index", 0)),
                int(payload.get("content_index", 0)),
            )
            text = payload.get("text", "")
            if key not in content and isinstance(text, str):
                content[key] = [text]
                if text:
                    events.append(ProviderEvent("text_delta", text))
        elif kind in {
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
        }:
            delta = payload.get("delta", "")
            if isinstance(delta, str) and delta:
                events.append(ProviderEvent("reasoning_delta", delta))
        elif kind == "response.refusal.delta":
            refused = True
            events.append(ProviderEvent("refusal_delta", payload.get("delta", "")))
        elif kind == "response.refusal.done":
            refused = True
        elif kind in {"response.completed", "response.incomplete", "response.failed"}:
            terminal = True
            response_data = payload.get("response", {})
            status = response_data.get("status", kind.split(".")[-1])
            usage = response_data.get("usage") or {}
            output = "".join("".join(value) for _, value in sorted(content.items()))
            refused = refused or any(
                isinstance(part, dict) and part.get("type") == "refusal"
                for item in response_data.get("output", [])
                if isinstance(item, dict)
                for part in item.get("content", [])
            )
            if refused:
                events.append(ProviderEvent("refused", {"usage": usage}))
            elif kind == "response.completed" and status == "completed":
                events.append(
                    ProviderEvent(
                        "completed",
                        {
                            "text": output,
                            "response_id": response_data.get("id"),
                            "usage": usage,
                            "duration_ms": int((time.monotonic() - started) * 1000),
                        },
                    )
                )
            elif kind == "response.incomplete" or status == "incomplete":
                events.append(
                    ProviderEvent(
                        "incomplete",
                        {
                            "text": output,
                            "reason": (
                                response_data.get("incomplete_details") or {}
                            ).get("reason"),
                            "usage": usage,
                        },
                    )
                )
            else:
                events.append(
                    ProviderEvent(
                        "failed", {"message": "模型返回失败终态", "usage": usage}
                    )
                )
        elif kind == "error":
            terminal = True
            events.append(ProviderEvent("failed", {"message": "供应商返回失败终态"}))

        return _EventHandlingResult(events=events, terminal=terminal, refused=refused)


@dataclass(frozen=True, slots=True)
class _EventHandlingResult:
    events: list[ProviderEvent]
    terminal: bool
    refused: bool
