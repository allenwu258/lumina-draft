from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from .base import ProviderAdapter, ProviderError, ProviderEvent, ProviderRequest
from .transport import MAX_EVENT_BYTES, bounded_lines, fetch_json, validate_endpoint


ANTHROPIC_VERSION = "2026-10-21"
DEFAULT_MAX_OUTPUT_TOKENS = 8192
MAX_MODEL_PAGES = 100


class AnthropicMessagesAdapter(ProviderAdapter):
    """Anthropic Claude Messages API streaming protocol adapter."""

    protocol = "anthropic_messages"
    label = "Anthropic Claude · Messages API"
    auth_modes = ("bearer", "api_key_header")
    default_auth_mode = "bearer"
    default_base_url = "https://api.anthropic.com/v1"

    @staticmethod
    def build_body(request: ProviderRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            # The Messages API requires max_tokens. 8192 is the fallback;
            # explicit budgets and the catalog's model ceiling both apply.
            "max_tokens": _effective_max_tokens(request),
            "messages": [{"role": "user", "content": request.prompt}],
            "stream": True,
        }
        if request.instructions:
            body["system"] = request.instructions
        if request.json_schema is not None:
            body["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": compile_anthropic_schema(request.json_schema),
                }
            }
        reasoning = _parse_reasoning_value(request.reasoning_effort)
        if reasoning is not None:
            kind, value = reasoning
            if kind == "effort":
                body["output_config"] = body.get("output_config", {})
                body["output_config"]["effort"] = value
                body["thinking"] = {"type": "adaptive"}
            elif kind == "adaptive":
                body["thinking"] = {"type": "adaptive"}
            elif kind == "budget":
                budget = int(value)
                max_tokens = body["max_tokens"]
                if budget >= max_tokens:
                    raise ProviderError(
                        "INVALID_REASONING_BUDGET",
                        "Anthropic Thinking Token Budget 必须小于最大输出 Token 数",
                    )
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # `disabled` is an explicit API value for model configurations
            # that need to override a provider-side default.
            elif kind == "disabled":
                body["thinking"] = {"type": "disabled"}
        return body

    @staticmethod
    def _headers(request: ProviderRequest) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if request.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {request.api_key}"
        elif request.auth_mode == "api_key_header":
            headers["x-api-key"] = request.api_key
        return headers

    @staticmethod
    def _validate_auth(request: ProviderRequest) -> None:
        if request.auth_mode not in {"bearer", "api_key_header"}:
            raise ProviderError("INVALID_AUTH_MODE", "不支持的 Anthropic 认证方式")
        if not request.api_key:
            raise ProviderError(
                "CREDENTIAL_NOT_CONFIGURED", "请先配置 Anthropic API Key"
            )

    @staticmethod
    def _http_error(status: int) -> ProviderError:
        if status == 400:
            code, message = (
                "PROVIDER_INVALID_REQUEST",
                "Anthropic 请求参数或模型配置无效",
            )
        elif status == 401:
            code, message = "PROVIDER_UNAUTHORIZED", "Anthropic API Key 无效"
        elif status == 403:
            code, message = "PROVIDER_FORBIDDEN", "Anthropic API Key 没有调用权限"
        elif status == 404:
            code, message = "PROVIDER_NOT_FOUND", "Anthropic 模型或 Messages 接口不存在"
        elif status == 413:
            code, message = "CONTEXT_WINDOW_EXCEEDED", "Anthropic 请求超过上下文限制"
        elif status == 429:
            code, message = "PROVIDER_RATE_LIMITED", "Anthropic 限流或额度不足"
        elif status == 529:
            code, message = "PROVIDER_OVERLOADED", "Anthropic 服务当前过载"
        else:
            code, message = "PROVIDER_HTTP_ERROR", "Anthropic API 请求失败"
        return ProviderError(code, message, status)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        base_url = validate_endpoint(
            request.base_url,
            allow_private_http=request.allow_private_http,
        )
        self._validate_auth(request)
        if not request.model.strip() or any(ord(char) < 32 for char in request.model):
            raise ProviderError("INVALID_MODEL_ID", "Anthropic Model ID 格式无效")
        body = self.build_body(request)
        state = _AnthropicStreamState()
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
                        f"{base_url}/messages",
                        headers=self._headers(request),
                        json=body,
                    ) as response:
                        if response.status_code >= 300:
                            raise self._http_error(response.status_code)

                        yield ProviderEvent("started", {"model": request.model})
                        event_name = "message"
                        data_lines: list[str] = []
                        event_bytes = 0
                        async for line in bounded_lines(response):
                            if not line:
                                if not data_lines:
                                    event_name, event_bytes = "message", 0
                                    continue
                                payload = self._decode_event(data_lines)
                                events, terminal = self._consume_event(
                                    event_name, payload, state, started
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
                                        "Anthropic 流事件超过大小限制",
                                    )
                                data_lines.append(line[5:].lstrip())

                        if data_lines:
                            payload = self._decode_event(data_lines)
                            events, terminal = self._consume_event(
                                event_name, payload, state, started
                            )
                            for event in events:
                                yield event
                            if terminal:
                                return
                        raise ProviderError(
                            "PROVIDER_STREAM_INTERRUPTED",
                            "Anthropic 流在 message_stop 终态前中断",
                        )
        except ProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ProviderError("PROVIDER_TIMEOUT", "Anthropic 请求超时") from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                "PROVIDER_CONNECTION_ERROR", "无法连接 Anthropic API"
            ) from exc
        except (UnicodeDecodeError, ValueError, TypeError, AttributeError) as exc:
            raise ProviderError(
                "INVALID_PROVIDER_EVENT", "Anthropic 返回了无效的流事件"
            ) from exc

    @staticmethod
    def _decode_event(data_lines: list[str]) -> dict[str, Any]:
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "INVALID_PROVIDER_EVENT", "Anthropic 返回了无效的流事件"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "INVALID_PROVIDER_EVENT", "Anthropic 返回了无效的流事件"
            )
        return payload

    @classmethod
    def _consume_event(
        cls,
        event_name: str,
        payload: dict[str, Any],
        state: _AnthropicStreamState,
        started: float,
    ) -> tuple[list[ProviderEvent], bool]:
        kind = payload.get("type") or event_name
        if kind == "ping":
            return [], False
        if kind == "error":
            raise ProviderError("PROVIDER_STREAM_ERROR", "Anthropic 返回失败事件")
        if kind == "message_start":
            message = payload.get("message")
            if not isinstance(message, dict):
                raise ProviderError("INVALID_PROVIDER_EVENT", "Anthropic 消息结构无效")
            state.response_id = _optional_string(message.get("id"))
            usage = message.get("usage")
            if isinstance(usage, dict):
                state.merge_usage(usage)
            return [], False
        if kind == "content_block_start":
            index = _event_index(payload)
            block = payload.get("content_block")
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise ProviderError(
                    "INVALID_PROVIDER_EVENT", "Anthropic 内容块结构无效"
                )
            block_type = block["type"]
            state.block_types[index] = block_type
            if block_type == "tool_use":
                raise ProviderError(
                    "UNSUPPORTED_TOOL_CALL", "当前模型调用不支持 Anthropic 工具调用响应"
                )
            if block_type not in {"text", "thinking", "redacted_thinking"}:
                raise ProviderError(
                    "INVALID_PROVIDER_EVENT", "Anthropic 返回了不支持的内容块"
                )
            return [], False
        if kind == "content_block_delta":
            index = _event_index(payload)
            delta = payload.get("delta")
            if not isinstance(delta, dict) or not isinstance(delta.get("type"), str):
                raise ProviderError("INVALID_PROVIDER_EVENT", "Anthropic 内容增量无效")
            delta_type = delta["type"]
            if delta_type == "text_delta":
                text = delta.get("text")
                if not isinstance(text, str):
                    raise ProviderError(
                        "INVALID_PROVIDER_EVENT", "Anthropic 文本增量无效"
                    )
                if state.block_types.get(index) not in {None, "text"}:
                    raise ProviderError(
                        "INVALID_PROVIDER_EVENT", "Anthropic 内容块类型不匹配"
                    )
                if text:
                    state.content.append(text)
                    return [ProviderEvent("text_delta", text)], False
            elif delta_type == "thinking_delta":
                thinking = delta.get("thinking")
                if not isinstance(thinking, str):
                    raise ProviderError(
                        "INVALID_PROVIDER_EVENT", "Anthropic Thinking 增量无效"
                    )
                if state.block_types.get(index) not in {None, "thinking"}:
                    raise ProviderError(
                        "INVALID_PROVIDER_EVENT", "Anthropic 内容块类型不匹配"
                    )
                if thinking:
                    return [ProviderEvent("reasoning_delta", thinking)], False
            elif delta_type in {"signature_delta", "citations_delta"}:
                return [], False
            elif delta_type == "input_json_delta":
                raise ProviderError(
                    "UNSUPPORTED_TOOL_CALL", "当前模型调用不支持 Anthropic 工具调用响应"
                )
            else:
                raise ProviderError(
                    "INVALID_PROVIDER_EVENT", "Anthropic 返回了不支持的内容增量"
                )
            return [], False
        if kind == "content_block_stop":
            _event_index(payload)
            return [], False
        if kind == "message_delta":
            delta = payload.get("delta")
            if not isinstance(delta, dict):
                raise ProviderError("INVALID_PROVIDER_EVENT", "Anthropic 终态增量无效")
            stop_reason = delta.get("stop_reason")
            if stop_reason is not None:
                if not isinstance(stop_reason, str):
                    raise ProviderError(
                        "INVALID_PROVIDER_EVENT", "Anthropic 结束原因无效"
                    )
                state.stop_reason = stop_reason
            usage = payload.get("usage")
            if isinstance(usage, dict):
                state.merge_usage(usage)
            return [], False
        if kind == "message_stop":
            return [cls._terminal_event(state, started)], True
        raise ProviderError("INVALID_PROVIDER_EVENT", "Anthropic 返回了未知的消息事件")

    @staticmethod
    def _terminal_event(state: _AnthropicStreamState, started: float) -> ProviderEvent:
        usage = state.usage_snapshot()
        stop_reason = state.stop_reason
        if stop_reason in {"end_turn", "stop_sequence"}:
            return ProviderEvent(
                "completed",
                {
                    "text": "".join(state.content),
                    "response_id": state.response_id,
                    "usage": usage,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
        if stop_reason == "max_tokens":
            return ProviderEvent(
                "incomplete",
                {
                    "text": "".join(state.content),
                    "reason": "max_output_tokens",
                    "usage": usage,
                },
            )
        if stop_reason == "refusal":
            return ProviderEvent("refused", {"usage": usage})
        if stop_reason == "tool_use":
            return ProviderEvent(
                "failed",
                {"message": "模型尝试调用当前不支持的工具", "usage": usage},
            )
        if stop_reason in {"pause_turn", "model_context_window_exceeded"}:
            return ProviderEvent(
                "incomplete",
                {
                    "text": "".join(state.content),
                    "reason": (
                        "context_window"
                        if stop_reason == "model_context_window_exceeded"
                        else stop_reason
                    ),
                    "usage": usage,
                },
            )
        return ProviderEvent(
            "failed",
            {
                "message": "Anthropic 未提供受支持的完成原因",
                "reason": stop_reason or "missing_stop_reason",
                "usage": usage,
            },
        )

    async def discover_models(
        self,
        *,
        base_url: str,
        api_key: str,
        auth_mode: str,
        allow_private_http: bool = False,
    ) -> list[dict[str, Any]]:
        if auth_mode not in self.auth_modes:
            raise ProviderError("INVALID_AUTH_MODE", "不支持的 Anthropic 认证方式")
        if not api_key:
            raise ProviderError(
                "CREDENTIAL_NOT_CONFIGURED", "请先配置 Anthropic API Key"
            )
        root = validate_endpoint(base_url, allow_private_http=allow_private_http)
        headers = self._discovery_headers(api_key, auth_mode)
        after_id: str | None = None
        seen_cursors: set[str] = set()
        results: list[dict[str, Any]] = []
        for _ in range(MAX_MODEL_PAGES):
            query = "limit=100"
            if after_id:
                query += "&after_id=" + quote(after_id, safe="")
            payload = await fetch_json(
                f"{root}/models?{query}",
                headers=headers,
                base_url=root,
                allow_private_http=allow_private_http,
            )
            if not isinstance(payload, dict) or not isinstance(
                payload.get("data"), list
            ):
                raise ProviderError("INVALID_MODEL_LIST", "Anthropic 模型目录格式无效")
            for item in payload["data"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                model_id = item["id"].strip()
                if (
                    not model_id
                    or len(model_id) > 200
                    or any(ord(c) < 32 for c in model_id)
                ):
                    continue
                lifecycle = item.get("lifecycle")
                lifecycle_state = (
                    lifecycle.get("status")
                    if isinstance(lifecycle, dict)
                    else lifecycle
                )
                if lifecycle_state == "retired":
                    continue
                capabilities = item.get("capabilities")
                result: dict[str, Any] = {
                    "id": model_id,
                    "name": str(item.get("display_name") or model_id)[:200],
                    "context_window_tokens": _positive_int(
                        item.get("max_input_tokens")
                    ),
                    "max_output_tokens": _positive_int(item.get("max_tokens")),
                }
                if isinstance(capabilities, dict):
                    result["capabilities"] = capabilities
                if lifecycle_state:
                    result["lifecycle"] = lifecycle_state
                results.append(result)
            has_more = payload.get("has_more", False)
            if type(has_more) is not bool:
                raise ProviderError("INVALID_MODEL_LIST", "Anthropic 模型分页状态无效")
            if not has_more:
                break
            next_cursor = payload.get("last_id")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ProviderError(
                    "INVALID_MODEL_LIST", "Anthropic 模型分页缺少 last_id"
                )
            if next_cursor in seen_cursors or next_cursor == after_id:
                raise ProviderError("INVALID_MODEL_LIST", "Anthropic 模型分页游标重复")
            seen_cursors.add(next_cursor)
            after_id = next_cursor
        else:
            raise ProviderError(
                "MODEL_LIST_TOO_LARGE", "Anthropic 模型目录分页超过 100 页"
            )
        return results

    @staticmethod
    def _discovery_headers(api_key: str, auth_mode: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        elif auth_mode == "api_key_header":
            headers["x-api-key"] = api_key
        return headers

    def default_reasoning_tiers(
        self, model_id: str, capabilities: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        tiers = [{"code": "off", "label": "Off", "api_value": None, "sort_order": 0}]
        if not isinstance(capabilities, dict):
            return tiers
        effort_values = _capability_values(
            capabilities.get("effort"), {"low", "medium", "high", "max", "xhigh"}
        )
        thinking_details = capabilities.get("thinking")
        thinking_types = _capability_values(
            (
                thinking_details.get("types")
                if isinstance(thinking_details, dict)
                else None
            ),
            {"adaptive"},
        )
        if effort_values:
            labels = {
                "low": "Low",
                "medium": "Medium",
                "high": "High",
                "max": "Max",
                "xhigh": "Extra High",
            }
            tiers.extend(
                {
                    "code": value,
                    "label": labels[value],
                    "api_value": value,
                    "sort_order": order,
                }
                for order, value in enumerate(
                    ("low", "medium", "high", "max", "xhigh"), start=1
                )
                if value in effort_values
            )
        elif "adaptive" in thinking_types:
            tiers.append(
                {
                    "code": "adaptive",
                    "label": "Adaptive",
                    "api_value": "adaptive",
                    "sort_order": 1,
                }
            )
        return tiers


@dataclass(slots=True)
class _AnthropicStreamState:
    content: list[str] = field(default_factory=list)
    response_id: str | None = None
    stop_reason: str | None = None
    block_types: dict[int, str] = field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    def merge_usage(self, usage: dict[str, Any]) -> None:
        for key, attribute in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_creation_input_tokens", "cache_creation_input_tokens"),
            ("cache_read_input_tokens", "cache_read_input_tokens"),
        ):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                setattr(self, attribute, value)

    def usage_snapshot(self) -> dict[str, int | None]:
        total = (
            self.input_tokens + self.output_tokens
            if self.input_tokens is not None and self.output_tokens is not None
            else None
        )
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": total,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


def compile_anthropic_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local references and compile Pydantic JSON Schema to a safe subset.

    Shape and type constraints are preserved for provider-side constrained
    decoding. Validation-only numeric, length, and regex constraints are
    removed because Anthropic's grammar compiler does not support the complete
    JSON Schema vocabulary. The application still validates final data against
    the original Pydantic schema.
    """

    if not isinstance(schema, dict):
        raise ProviderError(
            "UNSUPPORTED_JSON_SCHEMA", "Anthropic JSON Schema 必须是对象"
        )
    definitions: dict[str, Any] = {}
    for key in ("$defs", "definitions"):
        value = schema.get(key)
        if isinstance(value, dict):
            definitions.update(value)
    validation_only = {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "title",
        "examples",
        "default",
    }
    allowed = {
        "$ref",
        "$defs",
        "definitions",
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "const",
        "default",
        "format",
        "anyOf",
        "description",
        "additionalProperties",
    } | validation_only

    def visit(node: Any, stack: tuple[str, ...] = (), depth: int = 0) -> dict[str, Any]:
        if depth > 64 or not isinstance(node, dict):
            raise ProviderError(
                "UNSUPPORTED_JSON_SCHEMA", "Anthropic JSON Schema 无效或过深"
            )
        unknown = set(node) - allowed
        if unknown:
            raise ProviderError(
                "UNSUPPORTED_JSON_SCHEMA",
                "Anthropic JSON Schema 不支持约束：" + ", ".join(sorted(unknown)),
            )
        if "$ref" in node:
            ref = node["$ref"]
            prefix = (
                "#/$defs/"
                if isinstance(ref, str) and ref.startswith("#/$defs/")
                else "#/definitions/"
            )
            if not isinstance(ref, str) or not ref.startswith(prefix):
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA", "Anthropic 仅支持本地 JSON Schema 引用"
                )
            name = ref[len(prefix) :].replace("~1", "/").replace("~0", "~")
            if name in stack:
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA", "Anthropic 不支持递归 JSON Schema"
                )
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA", "JSON Schema 引用了不存在的定义"
                )
            overlay = {key: value for key, value in node.items() if key != "$ref"}
            return visit({**target, **overlay}, (*stack, name), depth + 1)

        result: dict[str, Any] = {}
        type_value = node.get("type")
        if isinstance(type_value, list):
            non_null = [value for value in type_value if value != "null"]
            if len(non_null) != 1:
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA", "Anthropic 只支持单一可空类型"
                )
            type_value = non_null[0]
            result["anyOf"] = [
                {"type": type_value},
                {"type": "null"},
            ]
        elif type_value is not None:
            if type_value not in {
                "string",
                "integer",
                "number",
                "boolean",
                "object",
                "array",
                "null",
            }:
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA",
                    f"Anthropic 不支持 JSON Schema 类型：{type_value}",
                )
            result["type"] = type_value
        notes = _constraint_notes(node)
        description = node.get("description", "")
        if not isinstance(description, str):
            raise ProviderError("UNSUPPORTED_JSON_SCHEMA", "description 必须是字符串")
        if description or notes:
            result["description"] = " ".join([description.strip(), *notes]).strip()
        schema_format = node.get("format")
        if "format" in node and not isinstance(schema_format, str):
            raise ProviderError("UNSUPPORTED_JSON_SCHEMA", "format 必须是字符串")
        if isinstance(schema_format, str):
            if schema_format in _SUPPORTED_STRING_FORMATS:
                result["format"] = schema_format
            else:
                format_notes = _constraint_notes({"format": schema_format})
                result["description"] = " ".join(
                    filter(None, [result.get("description", ""), *format_notes])
                )
        for key in ("enum", "const"):
            if key in node:
                value = node[key]
                values = value if key == "enum" else [value]
                if key == "enum" and not isinstance(value, list):
                    raise ProviderError("UNSUPPORTED_JSON_SCHEMA", "enum 必须是数组")
                if any(isinstance(item, (dict, list)) for item in values):
                    raise ProviderError(
                        "UNSUPPORTED_JSON_SCHEMA",
                        "Anthropic 不支持对象或数组形式的 enum/const",
                    )
                result[key] = value
        if "anyOf" in node:
            branches = node["anyOf"]
            if not isinstance(branches, list) or not branches:
                raise ProviderError("UNSUPPORTED_JSON_SCHEMA", "anyOf 必须是非空数组")
            compiled_branches = [visit(branch, stack, depth + 1) for branch in branches]
            if "anyOf" in result:
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA", "不支持嵌套的可空类型联合"
                )
            result["anyOf"] = compiled_branches
            result.pop("type", None)
        if "properties" in node:
            properties = node["properties"]
            if not isinstance(properties, dict):
                raise ProviderError("UNSUPPORTED_JSON_SCHEMA", "properties 必须是对象")
            result["type"] = "object"
            result["properties"] = {
                name: visit(child, stack, depth + 1)
                for name, child in properties.items()
            }
            # Anthropic constrained output requires a closed, fully specified
            # object. Optional Pydantic fields stay nullable only when the
            # source schema declares null; all keys are still emitted.
            result["required"] = list(properties)
            result["additionalProperties"] = False
        elif node.get("type") == "object":
            result["properties"] = {}
            result["required"] = []
            result["additionalProperties"] = False
        if "items" in node:
            result["type"] = "array"
            result["items"] = visit(node["items"], stack, depth + 1)
        if type(node.get("minItems")) is int and node["minItems"] in {0, 1}:
            result["minItems"] = node["minItems"]
        for key in node:
            if key in validation_only:
                continue
            if key in {
                "$defs",
                "definitions",
                "$ref",
                "type",
                "properties",
                "required",
                "items",
                "enum",
                "const",
                "anyOf",
                "description",
                "additionalProperties",
            }:
                continue
            # Recognized optional metadata is intentionally removed.
        return result

    return visit(schema)


def _parse_reasoning_value(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized or normalized == "off":
        return None
    if normalized in {"low", "medium", "high", "max", "xhigh"}:
        return "effort", normalized
    if normalized.startswith("effort:"):
        effort = normalized.split(":", 1)[1]
        if effort in {"low", "medium", "high", "max", "xhigh"}:
            return "effort", effort
    if normalized in {"adaptive", "thinking:adaptive"}:
        return "adaptive", "adaptive"
    if normalized in {"disabled", "thinking:disabled"}:
        return "disabled", "disabled"
    if normalized.startswith(("budget:", "thinking:budget:")):
        raw = normalized.rsplit(":", 1)[1]
        if raw.isdecimal() and int(raw) >= 1024:
            return "budget", raw
    raise ProviderError(
        "UNSUPPORTED_REASONING_TIER",
        "Anthropic 思考档位需使用 low/medium/high/max/xhigh、adaptive 或有效 token budget",
    )


def _event_index(payload: dict[str, Any]) -> int:
    value = payload.get("index")
    if type(value) is not int or value < 0:
        raise ProviderError("INVALID_PROVIDER_EVENT", "Anthropic 内容块索引无效")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _capability_values(value: Any, defaults: set[str]) -> set[str]:
    if value is True:
        return defaults
    if isinstance(value, str):
        return {value.lower()}
    if isinstance(value, list):
        return {item.lower() for item in value if isinstance(item, str)}
    if not isinstance(value, dict):
        return set()
    if value.get("supported") is False or value.get("enabled") is False:
        return set()
    for key in ("values", "levels", "types", "supported_values"):
        items = value.get(key)
        if isinstance(items, list):
            return {item.lower() for item in items if isinstance(item, str)}
        if isinstance(items, dict):
            return {
                name.lower()
                for name, details in items.items()
                if isinstance(name, str)
                and (
                    details is True
                    or isinstance(details, dict)
                    and details.get("supported") is True
                )
            }
    named_capabilities = {
        name.lower()
        for name, details in value.items()
        if name not in {"supported", "enabled"}
        and isinstance(name, str)
        and (
            details is True
            or isinstance(details, dict)
            and details.get("supported") is True
        )
    }
    if named_capabilities:
        return named_capabilities
    if value.get("supported") is True or value.get("enabled") is True:
        return defaults
    return set()


def _constraint_notes(node: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if "default" in node:
        default = json.dumps(node["default"], ensure_ascii=False, separators=(",", ":"))
        notes.append(f"Default value: {default}.")
    for key, phrase in (
        ("minimum", "Minimum value"),
        ("maximum", "Maximum value"),
        ("exclusiveMinimum", "Value must be greater than"),
        ("exclusiveMaximum", "Value must be less than"),
        ("multipleOf", "Value must be a multiple of"),
        ("minLength", "Minimum length"),
        ("maxLength", "Maximum length"),
        ("maxItems", "Maximum item count"),
    ):
        if key in node:
            notes.append(f"{phrase}: {node[key]}.")
    if node.get("uniqueItems") is True:
        notes.append("Items must be unique.")
    if "minItems" in node and node["minItems"] not in {0, 1}:
        notes.append(f"Minimum item count: {node['minItems']}.")
    if isinstance(node.get("pattern"), str):
        notes.append(f"Must match this regular expression: {node['pattern']}.")
    if (
        isinstance(node.get("format"), str)
        and node["format"] not in _SUPPORTED_STRING_FORMATS
    ):
        notes.append(f"Expected format: {node['format']}.")
    return notes


_SUPPORTED_STRING_FORMATS = {
    "date-time",
    "time",
    "date",
    "duration",
    "email",
    "hostname",
    "uri",
    "ipv4",
    "ipv6",
    "uuid",
}


def _effective_max_tokens(request: ProviderRequest) -> int:
    requested = request.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
    if request.model_max_output_tokens is None:
        return requested
    return min(requested, request.model_max_output_tokens)
