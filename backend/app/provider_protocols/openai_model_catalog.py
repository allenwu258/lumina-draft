from __future__ import annotations

from typing import Any

from .base import ProviderError
from .transport import fetch_json, validate_endpoint


async def discover_openai_models(
    *,
    base_url: str,
    api_key: str,
    auth_mode: str,
    allow_private_http: bool = False,
) -> list[dict[str, Any]]:
    if auth_mode not in {"bearer", "none"}:
        raise ProviderError("INVALID_AUTH_MODE", "不支持的 OpenAI 认证方式")
    if auth_mode == "bearer" and not api_key:
        raise ProviderError("CREDENTIAL_NOT_CONFIGURED", "请先配置 API 凭证")
    root = validate_endpoint(base_url, allow_private_http=allow_private_http)
    headers = {"Accept": "application/json"}
    if auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    payload = await fetch_json(
        f"{root}/models",
        headers=headers,
        base_url=root,
        allow_private_http=allow_private_http,
    )
    records = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        raise ProviderError("INVALID_MODEL_LIST", "Provider 返回的模型目录格式无效")
    normalized: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        model_id = item["id"].strip()
        if not model_id:
            continue
        normalized.append(
            {
                "id": model_id,
                "name": str(item.get("name") or model_id),
                "context_window_tokens": _positive_int(
                    item.get("context_length", item.get("context_window_tokens"))
                ),
                "max_output_tokens": _positive_int(item.get("max_output_tokens")),
            }
        )
    return normalized


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None
