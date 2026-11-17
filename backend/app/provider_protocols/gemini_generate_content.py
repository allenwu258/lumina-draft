from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from .base import ProviderAdapter, ProviderError, ProviderEvent, ProviderRequest
from .transport import (
    MAX_EVENT_BYTES,
    bounded_lines,
    fetch_json,
    validate_endpoint,
)


class GeminiGenerateContentAdapter(ProviderAdapter):
    """Google Gemini Developer API's native Generate Content protocol."""

    protocol = "gemini_generate_content"
    label = "Google Gemini API · Generate Content"
    auth_modes = ("api_key_header",)
    default_auth_mode = "api_key_header"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"

    @staticmethod
    def _model_resource(model: str) -> str:
        value = model.strip()
        if value.startswith("models/"):
            value = value[len("models/") :]
        if (
            not value
            or "/" in value
            or "\\" in value
            or any(ord(char) < 32 for char in value)
        ):
            raise ProviderError("INVALID_MODEL_ID", "Gemini Model ID 格式无效")
        return "models/" + quote(value, safe="-._~")

    @classmethod
    def build_body(cls, request: ProviderRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": request.instructions}]},
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
        }
        generation_config: dict[str, Any] = {}
        if request.max_output_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_output_tokens
        if request.json_schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = compile_gemini_schema(
                request.json_schema
            )
        if request.reasoning_effort is not None:
            thinking_config = cls._thinking_config(
                request.model, request.reasoning_effort
            )
            if thinking_config:
                generation_config["thinkingConfig"] = thinking_config
        if generation_config:
            body["generationConfig"] = generation_config
        return body

    @staticmethod
    def _thinking_config(model: str, effort: str) -> dict[str, Any] | None:
        """Map stored tier values to Gemini's two currently used config shapes.

        Numeric tier values mean an explicitly configured thinking token budget.
        Gemini 2.5 defaults are assigned as budgets at model creation; newer
        Gemini families use the documented level enum. Other model families
        only accept Off unless an operator explicitly configures a supported
        value in the tier editor.
        """

        normalized = effort.strip()
        if not normalized:
            return None
        model_lower = model.lower()
        if model_lower.startswith("models/"):
            model_lower = model_lower[len("models/") :]
        if normalized.isdecimal():
            if "gemini-2.5" not in model_lower:
                raise ProviderError(
                    "UNSUPPORTED_REASONING_TIER",
                    "Gemini Thinking Token Budget 仅适用于 Gemini 2.5 系列",
                )
            return {"thinkingBudget": int(normalized)}
        level = normalized.upper()
        if level not in {"MINIMAL", "LOW", "MEDIUM", "HIGH"}:
            raise ProviderError(
                "UNSUPPORTED_REASONING_TIER",
                "Gemini 思考档位值必须是等级或 token budget",
            )
        if "gemini-2.5" in model_lower:
            budget_by_level = {
                "MINIMAL": 256,
                "LOW": 1024,
                "MEDIUM": 8192,
                "HIGH": 16384,
            }
            return {"thinkingBudget": budget_by_level[level]}
        if model_lower.startswith(("gemini-3", "gemini-4")):
            return {"thinkingLevel": level}
        raise ProviderError(
            "UNSUPPORTED_REASONING_TIER",
            "此 Gemini 模型未识别为支持 Thinking 的系列，请选择 Off 或配置该模型支持的数值 budget",
        )

    @staticmethod
    def _headers(request: ProviderRequest) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if request.auth_mode == "api_key_header":
            headers["x-goog-api-key"] = request.api_key
        return headers

    @staticmethod
    def _validate_auth(request: ProviderRequest) -> None:
        if request.auth_mode != "api_key_header":
            raise ProviderError(
                "INVALID_AUTH_MODE", "Gemini API 需要使用 API Key Header"
            )
        if not request.api_key:
            raise ProviderError("CREDENTIAL_NOT_CONFIGURED", "请先配置 Gemini API Key")

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        base_url = validate_endpoint(
            request.base_url,
            allow_private_http=request.allow_private_http,
        )
        self._validate_auth(request)
        model_resource = self._model_resource(request.model)
        url = f"{base_url}/{model_resource}:streamGenerateContent?alt=sse"
        state = _GeminiStreamState()
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
                        url,
                        headers=self._headers(request),
                        json=self.build_body(request),
                    ) as response:
                        if response.status_code >= 300:
                            raise self._http_error(response.status_code)
                        yield ProviderEvent("started", {"model": request.model})
                        data_lines: list[str] = []
                        event_bytes = 0
                        terminal = False
                        async for line in bounded_lines(response):
                            if not line:
                                if not data_lines:
                                    continue
                                payload = self._decode_event(data_lines)
                                events, payload_terminal = self._consume_payload(
                                    payload, state
                                )
                                for event in events:
                                    yield event
                                data_lines, event_bytes = [], 0
                                terminal = terminal or payload_terminal
                                continue
                            if line.startswith("data:"):
                                event_bytes += len(line.encode("utf-8"))
                                if event_bytes > MAX_EVENT_BYTES:
                                    raise ProviderError(
                                        "PROVIDER_EVENT_TOO_LARGE",
                                        "供应商流事件超过大小限制",
                                    )
                                data_lines.append(line[5:].lstrip())
                        if data_lines:
                            payload = self._decode_event(data_lines)
                            events, payload_terminal = self._consume_payload(
                                payload, state
                            )
                            for event in events:
                                yield event
                            terminal = terminal or payload_terminal
                        if terminal:
                            yield self._terminal_event(state, request.model, started)
                            return
                        raise ProviderError(
                            "PROVIDER_STREAM_INTERRUPTED",
                            "Gemini 流在终态响应前中断",
                        )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ProviderError("PROVIDER_TIMEOUT", "Gemini 请求超时") from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                "PROVIDER_CONNECTION_ERROR", "无法连接 Gemini API"
            ) from exc
        except (UnicodeDecodeError, ValueError, TypeError, AttributeError) as exc:
            raise ProviderError(
                "INVALID_PROVIDER_EVENT", "Gemini 返回了无效的流事件"
            ) from exc

    @staticmethod
    def _http_error(status: int) -> ProviderError:
        message = {
            400: "Gemini 请求参数或模型配置无效",
            401: "Gemini API Key 无效",
            403: "Gemini API Key 没有调用权限",
            404: "Gemini 模型或 Generate Content 接口不存在",
            429: "Gemini 限流或额度不足",
        }.get(status, "Gemini API 请求失败")
        return ProviderError("PROVIDER_HTTP_ERROR", message, status)

    @staticmethod
    def _decode_event(lines: list[str]) -> dict[str, Any]:
        try:
            payload = json.loads("\n".join(lines))
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "INVALID_PROVIDER_EVENT", "Gemini 返回了无效的流事件"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError("INVALID_PROVIDER_EVENT", "Gemini 返回了无效的流事件")
        return payload

    @classmethod
    def _consume_payload(
        cls, payload: dict[str, Any], state: _GeminiStreamState
    ) -> tuple[list[ProviderEvent], bool]:
        usage = payload.get("usageMetadata")
        if isinstance(usage, dict):
            state.usage = cls._normalize_usage(usage)
        prompt_feedback = payload.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            state.refusal_reason = str(prompt_feedback["blockReason"])
            return [], True

        # A final usage-only frame can follow the candidate frame that carries
        # finishReason. Keep reading until the HTTP stream closes so that usage
        # metadata is not lost.
        if state.finish_reason is not None:
            return [], True

        candidates = payload.get("candidates", [])
        if not isinstance(candidates, list):
            raise ProviderError("INVALID_PROVIDER_EVENT", "Gemini 返回了无效的候选内容")
        if not candidates:
            return [], bool(state.refusal_reason)
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ProviderError("INVALID_PROVIDER_EVENT", "Gemini 返回了无效的候选内容")
        content = candidate.get("content") or {}
        if not isinstance(content, dict):
            raise ProviderError("INVALID_PROVIDER_EVENT", "Gemini 返回了无效的内容结构")
        parts = content.get("parts", [])
        if not isinstance(parts, list):
            raise ProviderError("INVALID_PROVIDER_EVENT", "Gemini 返回了无效的内容分片")
        events: list[ProviderEvent] = []
        for part in parts:
            if not isinstance(part, dict):
                raise ProviderError(
                    "INVALID_PROVIDER_EVENT", "Gemini 返回了无效的内容分片"
                )
            if (
                part.get("functionCall") is not None
                or part.get("executableCode") is not None
            ):
                raise ProviderError(
                    "UNSUPPORTED_TOOL_CALL",
                    "当前模型调用不支持 Gemini 工具或代码执行响应",
                )
            if part.get("thought") is True:
                continue
            text = part.get("text")
            if text is not None:
                if not isinstance(text, str):
                    raise ProviderError(
                        "INVALID_PROVIDER_EVENT", "Gemini 返回了无效的文本分片"
                    )
                if text:
                    state.content.append(text)
                    events.append(ProviderEvent("text_delta", text))
        finish_reason = candidate.get("finishReason")
        if finish_reason is not None:
            if not isinstance(finish_reason, str):
                raise ProviderError(
                    "INVALID_PROVIDER_EVENT", "Gemini 返回了无效的结束原因"
                )
            state.finish_reason = finish_reason.upper()
            state.response_id = (
                candidate.get("responseId")
                or payload.get("responseId")
                or state.response_id
            )
            state.refusal_reason = (
                state.finish_reason
                if state.finish_reason in _REFUSAL_REASONS
                else state.refusal_reason
            )
            return events, True
        return events, False

    @staticmethod
    def _normalize_usage(usage: dict[str, Any]) -> dict[str, int | None]:
        def count(key: str) -> int | None:
            value = usage.get(key)
            return value if type(value) is int and value >= 0 else None

        return {
            "input_tokens": count("promptTokenCount"),
            "output_tokens": count("candidatesTokenCount"),
            "total_tokens": count("totalTokenCount"),
        }

    @staticmethod
    def _terminal_event(
        state: _GeminiStreamState, model: str, started: float
    ) -> ProviderEvent:
        if state.refusal_reason:
            return ProviderEvent(
                "refused",
                {"reason": state.refusal_reason, "usage": state.usage},
            )
        if state.finish_reason == "STOP":
            return ProviderEvent(
                "completed",
                {
                    "text": "".join(state.content),
                    "response_id": state.response_id,
                    "usage": state.usage,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
        if state.finish_reason == "MAX_TOKENS":
            return ProviderEvent(
                "incomplete",
                {
                    "text": "".join(state.content),
                    "reason": "max_output_tokens",
                    "usage": state.usage,
                },
            )
        if state.finish_reason in _REFUSAL_REASONS:
            return ProviderEvent(
                "refused",
                {"reason": state.finish_reason, "usage": state.usage},
            )
        reason = state.finish_reason or "missing_finish_reason"
        return ProviderEvent(
            "failed",
            {"message": f"Gemini 返回未支持的结束状态：{reason}", "usage": state.usage},
        )

    async def discover_models(
        self,
        *,
        base_url: str,
        api_key: str,
        auth_mode: str,
        allow_private_http: bool = False,
    ) -> list[dict[str, Any]]:
        if auth_mode != "api_key_header" or not api_key:
            raise ProviderError("CREDENTIAL_NOT_CONFIGURED", "请先配置 Gemini API Key")
        root = validate_endpoint(base_url, allow_private_http=allow_private_http)
        headers = {"Accept": "application/json", "x-goog-api-key": api_key}
        token: str | None = None
        results: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        for _ in range(100):
            query = "pageSize=100"
            if token:
                query += "&pageToken=" + quote(token, safe="")
            payload = await fetch_json(
                f"{root}/models?{query}",
                headers=headers,
                base_url=root,
                allow_private_http=allow_private_http,
            )
            if not isinstance(payload, dict) or not isinstance(
                payload.get("models", []), list
            ):
                raise ProviderError(
                    "INVALID_MODEL_LIST", "Gemini 返回的模型目录格式无效"
                )
            for item in payload.get("models", []):
                if not isinstance(item, dict):
                    continue
                methods = item.get("supportedGenerationMethods", [])
                name = item.get("name")
                if not isinstance(name, str) or not name.startswith("models/"):
                    continue
                if not isinstance(methods, list) or "generateContent" not in methods:
                    continue
                model_id = name[len("models/") :]
                if (
                    not model_id
                    or len(model_id) > 200
                    or "/" in model_id
                    or "\\" in model_id
                    or any(ord(char) < 32 for char in model_id)
                ):
                    continue
                results.append(
                    {
                        "id": model_id,
                        "name": str(item.get("displayName") or model_id)[:200],
                        "context_window_tokens": _positive_int(
                            item.get("inputTokenLimit")
                        ),
                        "max_output_tokens": _positive_int(
                            item.get("outputTokenLimit")
                        ),
                    }
                )
            next_token = payload.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                break
            if next_token in seen_tokens:
                raise ProviderError("INVALID_MODEL_LIST", "Gemini 模型目录分页令牌重复")
            seen_tokens.add(next_token)
            token = next_token
        else:
            raise ProviderError(
                "MODEL_LIST_TOO_LARGE", "Gemini 模型目录分页超过 100 页"
            )
        return results

    def default_reasoning_tiers(self, model_id: str) -> list[dict[str, Any]]:
        model = model_id.lower()
        if model.startswith("models/"):
            model = model[len("models/") :]
        tiers: list[dict[str, Any]] = [
            {"code": "off", "label": "Off", "api_value": None, "sort_order": 0}
        ]
        if "gemini-2.5" in model:
            for order, (code, label, value) in enumerate(
                (
                    ("low", "Low", "1024"),
                    ("medium", "Medium", "8192"),
                    ("high", "High", "16384"),
                ),
                start=1,
            ):
                tiers.append(
                    {
                        "code": code,
                        "label": label,
                        "api_value": value,
                        "sort_order": order,
                    }
                )
        elif model.startswith("gemini-3"):
            for order, (code, label, value) in enumerate(
                (("low", "Low", "LOW"), ("high", "High", "HIGH")),
                start=1,
            ):
                tiers.append(
                    {
                        "code": code,
                        "label": label,
                        "api_value": value,
                        "sort_order": order,
                    }
                )
        return tiers


_SCHEMA_KEYS = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "pattern",
    "anyOf",
    "$ref",
    "$defs",
    "definitions",
    "title",
    "default",
    "additionalProperties",
}
_TYPE_MAP = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "object": "OBJECT",
    "array": "ARRAY",
}
_REFUSAL_REASONS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
}


def compile_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local Pydantic refs and translate the supported JSON Schema subset."""

    defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}

    def visit(node: Any, depth: int = 0) -> dict[str, Any]:
        if depth > 64 or not isinstance(node, dict):
            raise ProviderError(
                "UNSUPPORTED_JSON_SCHEMA", "Gemini JSON Schema 结构无效或过深"
            )
        unknown = set(node) - _SCHEMA_KEYS
        if unknown:
            raise ProviderError(
                "UNSUPPORTED_JSON_SCHEMA",
                "Gemini JSON Schema 不支持约束：" + ", ".join(sorted(unknown)),
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
                    "UNSUPPORTED_JSON_SCHEMA", "Gemini 仅支持本地定义引用"
                )
            name = ref[len(prefix) :].replace("~1", "/").replace("~0", "~")
            target = defs.get(name)
            if not isinstance(target, dict):
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA", "JSON Schema 引用了不存在的定义"
                )
            merged = {key: value for key, value in node.items() if key != "$ref"}
            return visit({**target, **merged}, depth + 1)

        source = {
            key: value
            for key, value in node.items()
            if key
            not in {"$defs", "definitions", "title", "default", "additionalProperties"}
        }
        nullable = source.pop("nullable", False)
        any_of = source.pop("anyOf", None)
        if any_of is not None:
            if not isinstance(any_of, list) or len(any_of) != 2:
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA", "Gemini 只支持可空的二分类型聯集"
                )
            non_null = [
                item
                for item in any_of
                if not (isinstance(item, dict) and item.get("type") == "null")
            ]
            if len(non_null) != 1:
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA", "Gemini 仅支持 T | null 形式的 anyOf"
                )
            compiled = visit(non_null[0], depth + 1)
            compiled["nullable"] = True
            for key, value in source.items():
                if key not in {"type"}:
                    compiled[key] = translate_child(key, value, depth)
            return compiled

        type_value = source.pop("type", None)
        if isinstance(type_value, list):
            non_null_types = [item for item in type_value if item != "null"]
            if len(non_null_types) != 1:
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA", "Gemini 仅支持单一可空类型"
                )
            type_value, nullable = non_null_types[0], True
        if type_value == "null":
            raise ProviderError(
                "UNSUPPORTED_JSON_SCHEMA", "Gemini Schema 不支持独立 null 类型"
            )
        result: dict[str, Any] = {}
        if type_value is not None:
            if type_value not in _TYPE_MAP:
                raise ProviderError(
                    "UNSUPPORTED_JSON_SCHEMA",
                    f"Gemini 不支持 JSON Schema 类型：{type_value}",
                )
            result["type"] = _TYPE_MAP[type_value]
        if nullable:
            result["nullable"] = True
        for key, value in source.items():
            result[key] = translate_child(key, value, depth)
        return result

    def translate_child(key: str, value: Any, depth: int) -> Any:
        if key == "properties":
            if not isinstance(value, dict):
                raise ProviderError("UNSUPPORTED_JSON_SCHEMA", "properties 必须是对象")
            return {name: visit(child, depth + 1) for name, child in value.items()}
        if key == "items":
            return visit(value, depth + 1)
        if key in {"required", "enum"}:
            if not isinstance(value, list):
                raise ProviderError("UNSUPPORTED_JSON_SCHEMA", f"{key} 必须是数组")
            return value
        if key in {"minimum", "maximum"}:
            return value
        if key in {
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "format",
            "description",
            "pattern",
        }:
            return value
        raise ProviderError(
            "UNSUPPORTED_JSON_SCHEMA", f"Gemini 不支持 JSON Schema 约束：{key}"
        )

    return visit(schema)


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


@dataclass(slots=True)
class _GeminiStreamState:
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
    refusal_reason: str | None = None
