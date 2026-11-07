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
        "model_concurrency": model.max_concurrency,
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


def current_model_limit(model_id: str) -> int:
    with session_scope() as db:
        owner, endpoint, model, binding = model_records(db, model_id)
        if (
            not owner.enabled
            or owner.archived_at
            or not endpoint.enabled
            or not model.enabled
            or model.archived_at
            or not binding.enabled
        ):
            raise ConfigurationError("CONFIG_NOT_READY", "Provider 或模型已停用")
        return model.max_concurrency


@dataclass
class _ModelCapacity:
    loop: asyncio.AbstractEventLoop
    active: int = 0
    users: int = 0
    waiters: dict[str, deque[object]] = field(default_factory=dict)
    run_order: deque[str] = field(default_factory=deque)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


class ModelLimiter:
    """Fair, process-local request admission per model for one event loop."""

    def __init__(self, limit_reader: Callable[[str], int] = current_model_limit):
        self._limit_reader = limit_reader
        self._capacities: dict[str, _ModelCapacity] = {}

    @property
    def active_count(self) -> int:
        return sum(capacity.active for capacity in self._capacities.values())

    def model_changed(self, model_id: str) -> None:
        """Wake queued calls after a committed model or provider state change."""
        capacity = self._capacities.get(model_id)
        if capacity and not capacity.loop.is_closed():
            capacity.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._notify(capacity))
            )

    @staticmethod
    async def _notify(capacity: _ModelCapacity) -> None:
        async with capacity.condition:
            capacity.condition.notify_all()

    def provider_changed(self, provider_id: str) -> None:
        with session_scope() as db:
            model_ids = db.scalars(
                select(LogicalModel.id)
                .join(ModelBinding, ModelBinding.model_id == LogicalModel.id)
                .join(ApiEndpoint, ApiEndpoint.id == ModelBinding.endpoint_id)
                .where(ApiEndpoint.provider_id == provider_id)
            ).all()
        for model_id in model_ids:
            self.model_changed(model_id)

    @asynccontextmanager
    async def acquire(
        self,
        model_id: str,
        run_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        capacity = self._capacities.setdefault(model_id, _ModelCapacity(loop))
        if capacity.loop is not loop:
            raise RuntimeError("ModelLimiter requires a single event loop")
        capacity.users += 1
        queue_key = run_id or "__unscoped__"
        ticket = object()
        registered = False
        admitted = False
        try:
            async with capacity.condition:
                capacity.waiters.setdefault(queue_key, deque()).append(ticket)
                if queue_key not in capacity.run_order:
                    capacity.run_order.append(queue_key)
                registered = True

            async def wait_for_admission() -> None:
                nonlocal admitted
                while True:
                    async with capacity.condition:
                        selected = None
                        for key in capacity.run_order:
                            queue = capacity.waiters.get(key)
                            if queue:
                                selected = queue[0]
                                break
                        if selected is ticket:
                            limit = self._limit_reader(model_id)
                            if limit < 1:
                                raise ConfigurationError(
                                    "CONFIG_NOT_READY", "模型并发必须大于零"
                                )
                            if capacity.active < limit:
                                queue = capacity.waiters[queue_key]
                                queue.popleft()
                                if queue:
                                    capacity.run_order.rotate(-1)
                                else:
                                    del capacity.waiters[queue_key]
                                    capacity.run_order.remove(queue_key)
                                capacity.active += 1
                                admitted = True
                                capacity.condition.notify_all()
                                return
                        await capacity.condition.wait()

            if timeout_seconds is None:
                await wait_for_admission()
            else:
                await asyncio.wait_for(wait_for_admission(), timeout=timeout_seconds)
            yield
        finally:
            # Always release the admission slot and waiter registration on exit.
            async with capacity.condition:
                if admitted:
                    capacity.active -= 1
                elif registered:
                    queue = capacity.waiters.get(queue_key)
                    if queue and ticket in queue:
                        queue.remove(ticket)
                        if not queue:
                            del capacity.waiters[queue_key]
                            if queue_key in capacity.run_order:
                                capacity.run_order.remove(queue_key)
                capacity.condition.notify_all()
            capacity.users -= 1
            if not capacity.active and not capacity.waiters and not capacity.users:
                if self._capacities.get(model_id) is capacity:
                    self._capacities.pop(model_id, None)


MODEL_LIMITER = ModelLimiter()
