from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import select

from .db import (
    ApiCredential,
    ApiEndpoint,
    ApiProvider,
    BusinessRouteOverride,
    LogicalModel,
    ModelBinding,
    ModelReasoningTier,
    ProviderTest,
    loads,
    resolve_default,
)


class ConfigurationError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def configuration_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(
        config, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def provider_endpoint(db, provider_id: str) -> tuple[ApiProvider, ApiEndpoint]:
    owner = db.get(ApiProvider, provider_id)
    if not owner or owner.archived_at:
        raise ConfigurationError("PROVIDER_NOT_FOUND", "Provider 不存在", 404)
    endpoint = db.scalar(select(ApiEndpoint).where(ApiEndpoint.provider_id == owner.id))
    if not endpoint:
        raise ConfigurationError("ENDPOINT_NOT_FOUND", "Provider 接入地址不存在", 404)
    return owner, endpoint


def model_records(db, model_id: str):
    model = db.get(LogicalModel, model_id)
    if not model or model.archived_at:
        raise ConfigurationError("MODEL_NOT_FOUND", "模型不存在", 404)
    binding = db.scalar(select(ModelBinding).where(ModelBinding.model_id == model.id))
    if not binding:
        raise ConfigurationError("MODEL_BINDING_NOT_FOUND", "模型没有接入绑定")
    endpoint = db.get(ApiEndpoint, binding.endpoint_id)
    owner = db.get(ApiProvider, endpoint.provider_id) if endpoint else None
    if not endpoint or not owner or owner.archived_at:
        raise ConfigurationError("PROVIDER_NOT_FOUND", "模型所属 Provider 不存在", 404)
    return owner, endpoint, model, binding


def get_model_tiers(db, model_id: str) -> list[ModelReasoningTier]:
    return list(
        db.scalars(
            select(ModelReasoningTier)
            .where(ModelReasoningTier.model_id == model_id)
            .order_by(ModelReasoningTier.sort_order, ModelReasoningTier.code)
        ).all()
    )


def resolve_model_config(
    db, model_id: str, reasoning_tier: str, business_code: str
) -> dict[str, Any]:
    owner, endpoint, model, binding = model_records(db, model_id)
    tier = db.scalar(
        select(ModelReasoningTier).where(
            ModelReasoningTier.model_id == model.id,
            ModelReasoningTier.code == reasoning_tier,
            ModelReasoningTier.enabled.is_(True),
        )
    )
    if not tier:
        raise ConfigurationError(
            "REASONING_TIER_UNAVAILABLE", "所选模型不支持该思考档位", 422
        )
    config = {
        "business_code": business_code,
        "provider_id": owner.id,
        "provider_name": owner.name,
        "provider_revision": owner.revision,
        "provider_enabled": owner.enabled,
        "provider_concurrency": owner.max_concurrency,
        "endpoint_id": endpoint.id,
        "endpoint_revision": endpoint.revision,
        "endpoint_enabled": endpoint.enabled,
        "protocol": endpoint.protocol,
        "base_url": endpoint.base_url,
        "auth_mode": endpoint.auth_mode,
        "allow_private_http": endpoint.allow_private_http,
        "credential_ref": endpoint.credential_ref,
        "credential_id": None,
        "credential_version": None,
        "model_id": model.id,
        "model_name": model.name,
        "model_revision": model.revision,
        "model_enabled": model.enabled,
        "model_concurrency": model.max_concurrency,
        "binding_id": binding.id,
        "binding_revision": binding.revision,
        "binding_enabled": binding.enabled,
        "remote_model_id": binding.remote_model_id,
        "model": binding.remote_model_id,
        "structured_output_mode": binding.structured_output_mode,
        "reasoning_tier": tier.code,
        "reasoning_effort": tier.api_value,
        "max_output_tokens": model.max_output_tokens,
        "context_window_tokens": model.context_window_tokens,
    }
    credential = (
        db.scalar(
            select(ApiCredential).where(
                ApiCredential.endpoint_id == endpoint.id,
                ApiCredential.secret_ref == endpoint.credential_ref,
                ApiCredential.status.in_(["active", "retired"]),
            )
        )
        if endpoint.credential_ref
        else None
    )
    if credential:
        config["credential_id"] = credential.id
        config["credential_version"] = credential.version
    config["config_hash"] = configuration_hash(config)
    return config


def resolve_business_route(db, business_code: str) -> dict[str, Any]:
    default_route, default_model, _, _, _ = resolve_default(db)
    if not default_route or not default_model:
        raise ConfigurationError("DEFAULT_ROUTE_NOT_READY", "尚未设置默认模型")
    override = (
        db.get(BusinessRouteOverride, business_code)
        if business_code != "default"
        else None
    )
    selected_model_id = (
        override.model_id if override and override.model_id else default_route.model_id
    )
    default_tier = default_route.reasoning_tier or "high"
    selected_tier = (
        override.reasoning_tier
        if override and override.reasoning_tier
        else default_tier
    )
    result = resolve_model_config(db, selected_model_id, selected_tier, business_code)
    result["route_revision"] = override.revision if override else default_route.revision
    result["default_route_revision"] = default_route.revision
    result["config_hash"] = configuration_hash(
        {key: value for key, value in result.items() if key != "config_hash"}
    )
    return result


def resolve_all_routes(db) -> dict[str, dict[str, Any]]:
    return {
        code: resolve_business_route(db, code)
        for code in ("default", "ppt_outline", "ppt_html")
    }


def endpoint_test_hash(db, model_id: str) -> str:
    owner, endpoint, model, binding = model_records(db, model_id)
    credential = (
        db.scalar(
            select(ApiCredential).where(
                ApiCredential.endpoint_id == endpoint.id,
                ApiCredential.secret_ref == endpoint.credential_ref,
                ApiCredential.status == "active",
            )
        )
        if endpoint.credential_ref
        else None
    )
    tiers = [
        {"code": row.code, "api_value": row.api_value, "enabled": row.enabled}
        for row in get_model_tiers(db, model.id)
    ]
    return configuration_hash(
        {
            "provider_id": owner.id,
            "base_url": endpoint.base_url,
            "protocol": endpoint.protocol,
            "auth_mode": endpoint.auth_mode,
            "allow_private_http": endpoint.allow_private_http,
            "remote_model_id": binding.remote_model_id,
            "structured_output_mode": binding.structured_output_mode,
            "credential": credential.id if credential else None,
            "tiers": tiers,
        }
    )


def latest_model_test(db, model_id: str) -> dict[str, Any] | None:
    test = db.scalar(
        select(ProviderTest)
        .where(ProviderTest.model_id == model_id)
        .order_by(
            ProviderTest.started_at.desc(),
            ProviderTest.id.desc(),
        )
        .limit(1)
    )
    if not test:
        return None
    return {
        "id": test.id,
        "status": test.status,
        "stale": test.config_hash != endpoint_test_hash(db, model_id),
        "checks": loads(test.checks_json),
        "started_at": test.started_at.isoformat(),
        "finished_at": test.finished_at.isoformat() if test.finished_at else None,
    }


def route_issues(db, config: dict[str, Any], *, require_test: bool = True) -> list[str]:
    issues = []
    if not all(
        config[key]
        for key in (
            "provider_enabled",
            "endpoint_enabled",
            "model_enabled",
            "binding_enabled",
        )
    ):
        issues.append("Provider 或模型已停用")
    if config["protocol"] != "openai_responses":
        issues.append("当前仅支持 Responses 协议")
    if config["auth_mode"] == "bearer" and not config.get("credential_id"):
        issues.append("未配置有效 API Key")
    if require_test:
        test = latest_model_test(db, config["model_id"])
        checks = {
            item["name"]: item["status"] for item in (test or {}).get("checks", [])
        }
        required = (
            ["streaming_text", "structured_output"]
            if config["business_code"] == "ppt_outline"
            else ["streaming_text"]
        )
        if (
            not test
            or test["stale"]
            or any(checks.get(check) != "passed" for check in required)
        ):
            issues.append("请先通过当前配置的模型测试")
    return issues


class ProviderModelLimiter:
    """Process-local dual limit for the app's single-worker deployment."""

    def __init__(self, system_limit: int = 3):
        self._condition = asyncio.Condition()
        self._system_limit = system_limit
        self._active = 0
        self._providers: dict[str, int] = {}
        self._models: dict[str, int] = {}

    @asynccontextmanager
    async def acquire(
        self,
        provider_id: str,
        provider_limit: int,
        model_id: str | None = None,
        model_limit: int | None = None,
    ) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(
                lambda: (
                    self._active < self._system_limit
                    and self._providers.get(provider_id, 0) < max(1, provider_limit)
                    and (
                        model_id is None
                        or self._models.get(model_id, 0) < max(1, model_limit or 1)
                    )
                )
            )
            self._providers[provider_id] = self._providers.get(provider_id, 0) + 1
            self._active += 1
            if model_id:
                self._models[model_id] = self._models.get(model_id, 0) + 1
        try:
            yield
        finally:
            async with self._condition:
                self._providers[provider_id] -= 1
                self._active -= 1
                if not self._providers[provider_id]:
                    del self._providers[provider_id]
                if model_id:
                    self._models[model_id] -= 1
                    if not self._models[model_id]:
                        del self._models[model_id]
                self._condition.notify_all()


MODEL_LIMITER = ProviderModelLimiter()
