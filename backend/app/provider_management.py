from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
    session_scope,
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


def current_provider_limit(provider_id: str) -> int:
    with session_scope() as db:
        provider = db.get(ApiProvider, provider_id)
        if not provider or not provider.enabled or provider.archived_at:
            raise ConfigurationError("CONFIG_NOT_READY", "Provider 已停用或不存在")
        return provider.max_concurrency


@dataclass
class _ProviderCapacity:
    loop: asyncio.AbstractEventLoop
    active: int = 0
    waiters: deque[object] = field(default_factory=deque)
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class ProviderLimiter:
    """FIFO request admission per Provider, shared by all models in one worker."""

    def __init__(self, limit_reader: Callable[[str], int] = current_provider_limit):
        self._limit_reader = limit_reader
        self._capacities: dict[str, _ProviderCapacity] = {}

    @property
    def active_count(self) -> int:
        return sum(capacity.active for capacity in self._capacities.values())

    def provider_changed(self, provider_id: str) -> None:
        """Wake queued calls after commit; FastAPI updates may run in a thread."""
        capacity = self._capacities.get(provider_id)
        if capacity and not capacity.loop.is_closed():
            capacity.loop.call_soon_threadsafe(capacity.changed.set)

    @asynccontextmanager
    async def acquire(self, provider_id: str) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        capacity = self._capacities.setdefault(provider_id, _ProviderCapacity(loop))
        if capacity.loop is not loop:
            raise RuntimeError("ProviderLimiter requires a single event loop")
        ticket = object()
        capacity.waiters.append(ticket)
        admitted = False
        try:
            while True:
                capacity.changed.clear()
                if capacity.waiters[0] is ticket:
                    # Read current committed policy, never a frozen request snapshot.
                    limit = self._limit_reader(provider_id)
                    if limit < 1:
                        raise ConfigurationError(
                            "CONFIG_NOT_READY", "Provider 并发必须大于零"
                        )
                    if capacity.active < limit:
                        capacity.waiters.popleft()
                        capacity.active += 1
                        admitted = True
                        capacity.changed.set()
                        break
                await capacity.changed.wait()
            yield
        finally:
            # No await during release: cancellation cannot leak a slot or ticket.
            if admitted:
                capacity.active -= 1
            else:
                capacity.waiters.remove(ticket)
            capacity.changed.set()
            if not capacity.active and not capacity.waiters:
                self._capacities.pop(provider_id, None)


PROVIDER_LIMITER = ProviderLimiter()
