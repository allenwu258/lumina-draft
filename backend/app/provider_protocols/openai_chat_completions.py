from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .base import ProviderAdapter, ProviderError, ProviderEvent, ProviderRequest
from .transport import MAX_EVENT_BYTES, bounded_lines, strict_json_schema, validate_endpoint


class OpenAIChatCompletionsAdapter(ProviderAdapter):
    """OpenAI Chat Completions streaming protocol adapter."""

    protocol = "openai_chat_completions"
    label = "OpenAI Chat Completions"

    @staticmethod
    def build_body(request: ProviderRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.instructions},
                {"role": "user", "content": request.prompt},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.reasoning_effort is not None:
            body["reasoning_effort"] = request.reasoning_effort
        if request.max_output_tokens is not None:
            body["max_completion_tokens"] = request.max_output_tokens
        if request.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "ppt_generation_spec",
                    "strict": True,
                    "schema": strict_json_schema(request.json_schema),
                },
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
            404: "模型或 Chat Completions 接口不存在",
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

        state = _ChatStreamState()
        started = time.monotonic()
        try:
            timeout = httpx.Timeout(900.0, connect=15.0, read=900.0, write=15.0)
            async with asyncio.timeout(900):
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        f"{base_url}/chat/completions",
                        headers=self._headers(request),
                        json=self.build_body(request),
                    ) as response:
                        if response.status_code >= 300:
                            # Provider response bodies may contain credentials or
                            # user prompts, so never include them in public errors.
                            raise self._http_error(response.status_code)

                        yield ProviderEvent("started", {"model": request.model})
                        event_name = "message"
                        data_lines: list[str] = []
                        event_bytes = 0
                        terminal = False

                        async for line in bounded_lines(response):
                            if not line:
                                if not data_lines:
                                    event_name, event_bytes = "message", 0
                                    continue
                                payload_text = "\n".join(data_lines)
                                events, terminal = self._consume_sse_data(
                                    payload_text,
                                    event_name=event_name,
                                    state=state,
                                    started=started,
                                )
                                for event in events:
                                    yield event
                                data_lines, event_name, event_bytes = [], "message", 0
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

                        if data_lines:
                            events, terminal = self._consume_sse_data(
                                "\n".join(data_lines),
                                event_name=event_name,
                                state=state,
                                started=started,
                            )
                            for event in events:
                                yield event
                        if not terminal:
                            raise ProviderError(
                                "PROVIDER_STREAM_INTERRUPTED",
                                "供应商流在终态标记前中断",
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

    @classmethod
    def _consume_sse_data(
        cls,
        data: str,
        *,
        event_name: str,
        state: _ChatStreamState,
        started: float,
    ) -> tuple[list[ProviderEvent], bool]:
        if data.strip() == "[DONE]":
            return cls._terminal_events(state, started), True
        if event_name == "error":
            raise ProviderError("PROVIDER_STREAM_ERROR", "供应商返回失败事件")
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError("INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件")
        if payload.get("error") is not None:
            raise ProviderError("PROVIDER_STREAM_ERROR", "供应商返回失败事件")
        if state.response_id is None and isinstance(payload.get("id"), str):
            state.response_id = payload["id"]
        if isinstance(payload.get("usage"), dict):
            state.usage = cls._normalize_usage(payload["usage"])

        choices = payload.get("choices", [])
        if not isinstance(choices, list):
            raise ProviderError("INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件")
        events: list[ProviderEvent] = []
        for choice in choices:
            if not isinstance(choice, dict):
                raise ProviderError(
                    "INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件"
                )
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise ProviderError(
                    "INVALID_PROVIDER_EVENT", "供应商返回了无效的流事件"
                )
            if delta.get("tool_calls") or delta.get("function_call"):
                raise ProviderError(
                    "UNSUPPORTED_TOOL_CALL", "当前模型调用不支持工具调用响应"
                )
            text = cls._extract_text(delta.get("content"))
            if text:
                state.content.append(text)
                events.append(ProviderEvent("text_delta", text))
            refusal = delta.get("refusal")
            if refusal is not None:
                if not isinstance(refusal, str):
                    raise ProviderError(
                        "INVALID_PROVIDER_EVENT", "供应商返回了无效的拒绝事件"
                    )
                state.refused = True
                if refusal:
                    events.append(ProviderEvent("refusal_delta", refusal))
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                if not isinstance(finish_reason, str):
                    raise ProviderError(
                        "INVALID_PROVIDER_EVENT", "供应商返回了无效的终止原因"
                    )
                state.finish_reason = finish_reason
        return events, False

    @staticmethod
    def _extract_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            chunks: list[str] = []
            for item in value:
                if not isinstance(item, dict) or item.get("type") != "text":
                    raise ProviderError(
                        "INVALID_PROVIDER_EVENT", "供应商返回了不支持的内容类型"
                    )
                text = item.get("text")
                if not isinstance(text, str):
                    raise ProviderError(
                        "INVALID_PROVIDER_EVENT", "供应商返回了无效的文本增量"
                    )
                chunks.append(text)
            return "".join(chunks)
        raise ProviderError("INVALID_PROVIDER_EVENT", "供应商返回了无效的文本增量")

    @staticmethod
    def _normalize_usage(usage: dict[str, Any]) -> dict[str, int | None]:
        def token_count(key: str) -> int | None:
            value = usage.get(key)
            return value if type(value) is int and value >= 0 else None

        return {
            "input_tokens": token_count("prompt_tokens"),
            "output_tokens": token_count("completion_tokens"),
            "total_tokens": token_count("total_tokens"),
        }

    @staticmethod
    def _terminal_events(
        state: _ChatStreamState, started: float
    ) -> list[ProviderEvent]:
        if state.finish_reason in {"tool_calls", "function_call"}:
            return [
                ProviderEvent(
                    "failed",
                    {
                        "message": "模型尝试调用当前不支持的工具",
                        "usage": state.usage,
                    },
                )
            ]
        if state.refused:
            return [ProviderEvent("refused", {"usage": state.usage})]
        if state.finish_reason is None:
            return [
                ProviderEvent(
                    "failed",
                    {
                        "message": "模型流未提供完成原因",
                        "usage": state.usage,
                    },
                )
            ]
        if state.finish_reason == "length":
            return [
                ProviderEvent(
                    "incomplete",
                    {
                        "text": "".join(state.content),
                        "reason": "max_output_tokens",
                        "usage": state.usage,
                    },
                )
            ]
        if state.finish_reason == "content_filter":
            return [
                ProviderEvent(
                    "incomplete",
                    {
                        "text": "".join(state.content),
                        "reason": "content_filter",
                        "usage": state.usage,
                    },
                )
            ]
        if state.finish_reason not in {None, "stop"}:
            return [
                ProviderEvent(
                    "failed",
                    {"message": "供应商返回了不支持的终止原因", "usage": state.usage},
                )
            ]
        return [
            ProviderEvent(
                "completed",
                {
                    "text": "".join(state.content),
                    "response_id": state.response_id,
                    "usage": state.usage,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
        ]


@dataclass(slots=True)
class _ChatStreamState:
    content: list[str] = field(default_factory=list)
    response_id: str | None = None
    usage: dict[str, int | None] = field(
        default_factory=lambda: {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }
    )
    finish_reason: str | None = None
    refused: bool = False
