from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError

from .db import (
    ApiCredential,
    ApiEndpoint,
    ApiProvider,
    BusinessRoute,
    BusinessRouteOverride,
    LlmCall,
    LogicalModel,
    ModelBinding,
    ModelReasoningTier,
    ProviderTest,
    dumps,
    loads,
    resolve_default,
    session_scope,
    utcnow,
)
from .provider_management import (
    ConfigurationError,
    PROVIDER_LIMITER,
    endpoint_test_hash,
    get_model_tiers,
    latest_model_test,
    model_records,
    provider_endpoint,
    resolve_business_route,
    resolve_model_config,
    route_issues,
)
from .providers import ProviderError, validate_endpoint
from .secrets import delete_secret, read_secret, save_secret


router = APIRouter(prefix="/api/v1/settings/llm", tags=["provider-management"])


def api_error(code: str, message: str, status: int = 409):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def handle_configuration_error(error: ConfigurationError):
    api_error(error.code, str(error), error.status_code)


class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai_responses"] = "openai_responses"
    base_url: str = Field(min_length=8, max_length=500)
    auth_mode: Literal["bearer", "none"] = "bearer"
    api_key: str | None = Field(default=None, max_length=500)
    allow_private_http: bool = False
    max_concurrency: int = Field(default=3, ge=1, le=32)
    enabled: bool = True

    @field_validator("name", "base_url")
    @classmethod
    def trim_fields(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value

    @model_validator(mode="after")
    def validate_auth(self):
        self.api_key = (
            self.api_key.strip() or None if self.api_key is not None else None
        )
        if self.auth_mode == "none" and self.api_key:
            raise ValueError("无认证模式不能同时提交 API Key")
        return self


class ProviderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai_responses"]
    base_url: str = Field(min_length=8, max_length=500)
    auth_mode: Literal["bearer", "none"]
    allow_private_http: bool
    max_concurrency: int = Field(ge=1, le=32)
    enabled: bool

    @field_validator("name", "base_url")
    @classmethod
    def trim_fields(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class CredentialPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=500)
    expected_revision: int = Field(ge=1)

    @field_validator("api_key")
    @classmethod
    def trim_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("API Key 不能为空")
        return value


class ModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remote_model_id: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)
    context_window_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    # Accepted for older clients; admission is controlled only by the Provider.
    max_concurrency: int | None = Field(
        default=None, ge=1, le=32, json_schema_extra={"deprecated": True}
    )
    metadata_source: Literal["provider_api", "catalog", "manual", "unknown"] = "manual"

    @field_validator("remote_model_id", "display_name")
    @classmethod
    def trim_values(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value

    @model_validator(mode="after")
    def require_remote_id(self):
        if not self.remote_model_id.strip():
            raise ValueError("Model ID 不能为空")
        return self


class ModelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    remote_model_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    context_window_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_concurrency: int | None = Field(
        default=None, ge=1, le=32, json_schema_extra={"deprecated": True}
    )
    enabled: bool

    @field_validator("remote_model_id", "display_name")
    @classmethod
    def trim_values(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class TierInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=2, max_length=40)
    label: str = Field(min_length=1, max_length=80)
    api_value: str | None = Field(default=None, max_length=80)
    enabled: bool = True
    sort_order: int = Field(ge=0, le=1000)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", value):
            raise ValueError("档位 code 只能使用小写字母、数字、下划线和连字符")
        return value

    @field_validator("label")
    @classmethod
    def trim_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("显示名称不能为空")
        return value

    @model_validator(mode="after")
    def validate_off(self):
        if self.code == "off":
            self.api_value = None
            self.enabled = True
        else:
            self.api_value = self.api_value.strip() if self.api_value else None
            if self.enabled and not self.api_value:
                raise ValueError("启用的思考档位必须填写 API 值")
        return self


class TierUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    tiers: list[TierInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def require_off_and_unique(self):
        codes = [tier.code for tier in self.tiers]
        if "off" not in codes or len(codes) != len(set(codes)):
            raise ValueError("档位必须包含 off 且 code 不可重复")
        return self


class RouteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    model_id: str | None = Field(max_length=36)
    reasoning_tier: str | None = Field(max_length=40)


def _tier_dict(tier: ModelReasoningTier) -> dict:
    return {
        "code": tier.code,
        "label": tier.label,
        "api_value": tier.api_value,
        "enabled": tier.enabled,
        "sort_order": tier.sort_order,
        "revision": tier.revision,
    }


def _model_payload(
    db, owner: ApiProvider, endpoint: ApiEndpoint, model: LogicalModel
) -> dict:
    binding = db.scalar(select(ModelBinding).where(ModelBinding.model_id == model.id))
    if not binding:
        return {}
    return {
        "id": model.id,
        "provider_id": owner.id,
        "remote_model_id": binding.remote_model_id,
        "display_name": model.name,
        "context_window_tokens": model.context_window_tokens,
        "max_output_tokens": model.max_output_tokens,
        "provider_concurrency": owner.max_concurrency,
        "capabilities": loads(model.capabilities_json),
        "capability_source": model.capability_source,
        "metadata_source": model.metadata_source,
        "enabled": model.enabled and binding.enabled,
        "revision": model.revision,
        "binding_revision": binding.revision,
        "reasoning_tiers": [_tier_dict(tier) for tier in get_model_tiers(db, model.id)],
        "last_test": latest_model_test(db, model.id),
    }


def _provider_payload(
    db, owner: ApiProvider, endpoint: ApiEndpoint, include_models: bool = True
) -> dict:
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
    credential_configured = False
    credential_store_available = True
    if credential:
        try:
            credential_configured = bool(read_secret(credential.secret_ref))
        except Exception:
            credential_store_available = False
    models = []
    if include_models:
        rows = db.scalars(
            select(LogicalModel)
            .join(ModelBinding, ModelBinding.model_id == LogicalModel.id)
            .where(
                ModelBinding.endpoint_id == endpoint.id,
                LogicalModel.archived_at.is_(None),
            )
            .order_by(LogicalModel.name)
        ).all()
        models = [_model_payload(db, owner, endpoint, model) for model in rows]
    return {
        "id": owner.id,
        "name": owner.name,
        "protocol": endpoint.protocol,
        "base_url": endpoint.base_url,
        "auth_mode": endpoint.auth_mode,
        "credential_configured": (
            credential_configured if endpoint.auth_mode == "bearer" else False
        ),
        "credential_version": credential.version if credential else None,
        "credential_store_available": credential_store_available,
        "allow_private_http": endpoint.allow_private_http,
        "max_concurrency": owner.max_concurrency,
        "enabled": owner.enabled and endpoint.enabled,
        "revision": owner.revision,
        "endpoint_revision": endpoint.revision,
        "archived_at": owner.archived_at.isoformat() if owner.archived_at else None,
        "models": models,
    }


def _find_route_users(db, model_ids: list[str]) -> list[str]:
    if not model_ids:
        return []
    users = []
    default = db.get(BusinessRoute, "default")
    if default and default.model_id in model_ids:
        users.append("default")
    for route in db.scalars(
        select(BusinessRouteOverride).where(
            BusinessRouteOverride.model_id.in_(model_ids)
        )
    ).all():
        users.append(route.code)
    return users


def _validate_route_model(
    db, model_id: str | None, tier_code: str | None, business_code: str
) -> dict | None:
    if model_id is None:
        return None
    try:
        config = resolve_model_config(db, model_id, tier_code or "high", business_code)
    except ConfigurationError as exc:
        handle_configuration_error(exc)
    if (
        not config["provider_enabled"]
        or not config["endpoint_enabled"]
        or not config["model_enabled"]
        or not config["binding_enabled"]
    ):
        api_error("MODEL_NOT_READY", "停用的模型不能用于业务路由", 422)
    if config["auth_mode"] == "bearer":
        try:
            if not config.get("credential_ref") or not read_secret(
                config["credential_ref"]
            ):
                api_error(
                    "CREDENTIAL_NOT_CONFIGURED", "请先为 Provider 配置 API Key", 422
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "CREDENTIAL_STORE_UNAVAILABLE",
                    "message": "系统凭据存储暂不可用",
                },
            ) from exc
    if route_issues(db, config):
        api_error(
            "MODEL_CAPABILITY_NOT_TESTED", "请先运行模型测试并通过该业务所需检查", 422
        )
    return config


def _check_revision(current: int, expected: int):
    if current != expected:
        api_error("CONFIG_REVISION_CONFLICT", "配置已被其他操作更新，请刷新后重试", 409)


@router.get("/protocols")
def get_protocols():
    return {
        "items": [
            {"code": "openai_responses", "label": "OpenAI Responses", "enabled": True}
        ]
    }


@router.get("/providers")
def list_providers():
    with session_scope() as db:
        owners = db.scalars(
            select(ApiProvider)
            .where(ApiProvider.archived_at.is_(None))
            .order_by(ApiProvider.name)
        ).all()
        items = []
        for owner in owners:
            endpoint = db.scalar(
                select(ApiEndpoint).where(ApiEndpoint.provider_id == owner.id)
            )
            if endpoint:
                items.append(_provider_payload(db, owner, endpoint))
        return {"items": items}


@router.post("/providers", status_code=201)
def create_provider(body: ProviderCreate):
    try:
        url = validate_endpoint(
            body.base_url, allow_private_http=body.allow_private_http
        )
    except ProviderError as exc:
        api_error(exc.code, str(exc), 422)
    if body.auth_mode == "none" and body.api_key:
        api_error("INVALID_CREDENTIAL", "无认证模式不能同时提交 API Key", 422)
    secret_ref: str | None = None
    try:
        with session_scope() as db:
            owner = ApiProvider(
                code=f"provider-{uuid.uuid4()}",
                name=body.name,
                enabled=body.enabled,
                max_concurrency=body.max_concurrency,
            )
            db.add(owner)
            db.flush()
            endpoint = ApiEndpoint(
                provider_id=owner.id,
                name=body.name,
                protocol=body.protocol,
                base_url=url,
                auth_mode=body.auth_mode,
                allow_private_http=body.allow_private_http,
                enabled=body.enabled,
            )
            db.add(endpoint)
            db.flush()
            if body.api_key:
                secret_ref = f"provider:{owner.id}:credential:{uuid.uuid4()}"
                save_secret(secret_ref, body.api_key)
                db.add(
                    ApiCredential(
                        endpoint_id=endpoint.id,
                        version=1,
                        secret_ref=secret_ref,
                        status="active",
                    )
                )
                endpoint.credential_ref = secret_ref
            db.flush()
            return _provider_payload(db, owner, endpoint)
    except (HTTPException, StaleDataError):
        if secret_ref:
            delete_secret(secret_ref)
        raise
    except Exception as exc:
        if secret_ref:
            delete_secret(secret_ref)
        if body.api_key:
            api_error(
                "CREDENTIAL_STORE_UNAVAILABLE",
                "Provider 保存失败，请检查系统凭据存储",
                503,
            )
        raise exc


@router.get("/providers/{provider_id}")
def get_provider(provider_id: str):
    with session_scope() as db:
        try:
            owner, endpoint = provider_endpoint(db, provider_id)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        return _provider_payload(db, owner, endpoint)


@router.patch("/providers/{provider_id}")
def patch_provider(provider_id: str, body: ProviderPatch):
    try:
        url = validate_endpoint(
            body.base_url, allow_private_http=body.allow_private_http
        )
    except ProviderError as exc:
        api_error(exc.code, str(exc), 422)
    revoked_refs: list[str] = []
    with session_scope() as db:
        try:
            owner, endpoint = provider_endpoint(db, provider_id)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        _check_revision(owner.revision, body.expected_revision)
        if body.auth_mode == "none" and endpoint.credential_ref:
            credentials = db.scalars(
                select(ApiCredential).where(
                    ApiCredential.endpoint_id == endpoint.id,
                    ApiCredential.status.in_(["active", "retired"]),
                )
            ).all()
            for credential in credentials:
                credential.status, credential.revoked_at = "revoked", utcnow()
                revoked_refs.append(credential.secret_ref)
            endpoint.credential_ref = None
        owner.name = endpoint.name = body.name
        owner.enabled = endpoint.enabled = body.enabled
        owner.max_concurrency = body.max_concurrency
        endpoint.protocol = body.protocol
        endpoint.base_url = url
        endpoint.auth_mode = body.auth_mode
        endpoint.allow_private_http = body.allow_private_http
        owner.revision += 1
        endpoint.revision += 1
        db.flush()
        result = _provider_payload(db, owner, endpoint)
    PROVIDER_LIMITER.provider_changed(provider_id)
    for ref in revoked_refs:
        delete_secret(ref)
    return result


@router.delete("/providers/{provider_id}")
def archive_provider(provider_id: str, expected_revision: int = Query(ge=1)):
    with session_scope() as db:
        try:
            owner, endpoint = provider_endpoint(db, provider_id)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        _check_revision(owner.revision, expected_revision)
        model_ids = list(
            db.scalars(
                select(LogicalModel.id)
                .join(ModelBinding, ModelBinding.model_id == LogicalModel.id)
                .where(ModelBinding.endpoint_id == endpoint.id)
            ).all()
        )
        users = _find_route_users(db, model_ids)
        if users:
            api_error(
                "CONFIG_IN_USE",
                f"Provider 仍被业务路由使用：{', '.join(sorted(set(users)))}",
            )
        owner.enabled, endpoint.enabled = False, False
        owner.archived_at = utcnow()
        owner.revision += 1
        endpoint.revision += 1
        result = {"id": owner.id, "archived": True, "revision": owner.revision}
    PROVIDER_LIMITER.provider_changed(provider_id)
    return result


@router.put("/providers/{provider_id}/credential")
def replace_credential(provider_id: str, body: CredentialPut):
    new_ref: str | None = None
    try:
        with session_scope() as db:
            try:
                owner, endpoint = provider_endpoint(db, provider_id)
            except ConfigurationError as exc:
                handle_configuration_error(exc)
            _check_revision(owner.revision, body.expected_revision)
            if endpoint.auth_mode != "bearer":
                api_error("INVALID_AUTH_MODE", "请先选择 Bearer API Key 认证", 409)
            old = (
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
            version = (
                db.scalar(
                    select(func.max(ApiCredential.version)).where(
                        ApiCredential.endpoint_id == endpoint.id
                    )
                )
                or 0
            ) + 1
            new_ref = f"provider:{owner.id}:credential:{uuid.uuid4()}"
            save_secret(new_ref, body.api_key)
            if old:
                old.status, old.retired_at = "retired", utcnow()
            db.add(
                ApiCredential(
                    endpoint_id=endpoint.id,
                    version=version,
                    secret_ref=new_ref,
                    status="active",
                )
            )
            endpoint.credential_ref = new_ref
            endpoint.revision += 1
            owner.revision += 1
            db.flush()
            result = {
                "provider_id": owner.id,
                "credential_configured": True,
                "credential_version": version,
                "revision": owner.revision,
            }
        return result
    except (HTTPException, StaleDataError):
        if new_ref:
            delete_secret(new_ref)
        raise
    except Exception as exc:
        if new_ref:
            delete_secret(new_ref)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CREDENTIAL_STORE_UNAVAILABLE",
                "message": "凭据替换失败，请检查系统凭据存储",
            },
        ) from exc


@router.delete("/providers/{provider_id}/credential")
def remove_credential(provider_id: str, expected_revision: int = Query(ge=1)):
    refs: list[str] = []
    with session_scope() as db:
        try:
            owner, endpoint = provider_endpoint(db, provider_id)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        _check_revision(owner.revision, expected_revision)
        credentials = db.scalars(
            select(ApiCredential).where(
                ApiCredential.endpoint_id == endpoint.id,
                ApiCredential.status.in_(["active", "retired"]),
            )
        ).all()
        for credential in credentials:
            credential.status, credential.revoked_at = "revoked", utcnow()
            refs.append(credential.secret_ref)
        endpoint.credential_ref = None
        endpoint.revision += 1
        owner.revision += 1
        result = {
            "provider_id": owner.id,
            "credential_configured": False,
            "revision": owner.revision,
        }
    for secret_ref in refs:
        delete_secret(secret_ref)
    return result


@router.get("/providers/{provider_id}/models")
def list_provider_models(provider_id: str):
    with session_scope() as db:
        try:
            owner, endpoint = provider_endpoint(db, provider_id)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        models = db.scalars(
            select(LogicalModel)
            .join(ModelBinding, ModelBinding.model_id == LogicalModel.id)
            .where(
                ModelBinding.endpoint_id == endpoint.id,
                LogicalModel.archived_at.is_(None),
            )
            .order_by(LogicalModel.name)
        ).all()
        return {
            "items": [_model_payload(db, owner, endpoint, model) for model in models]
        }


@router.post("/providers/{provider_id}/models", status_code=201)
def create_model(provider_id: str, body: ModelCreate):
    with session_scope() as db:
        try:
            owner, endpoint = provider_endpoint(db, provider_id)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        if db.scalar(
            select(ModelBinding.id)
            .join(LogicalModel, LogicalModel.id == ModelBinding.model_id)
            .where(
                ModelBinding.endpoint_id == endpoint.id,
                ModelBinding.remote_model_id == body.remote_model_id,
                LogicalModel.archived_at.is_(None),
            )
        ):
            api_error("MODEL_ALREADY_EXISTS", "该 Provider 下已存在此 Model ID", 409)
        model = LogicalModel(
            code=f"model-{uuid.uuid4()}",
            name=body.display_name or body.remote_model_id,
            enabled=True,
            context_window_tokens=body.context_window_tokens,
            max_output_tokens=body.max_output_tokens,
            capabilities_json=dumps(
                {"text_input": True, "multimodal_input": True, "tool_calling": True}
            ),
            capability_source="assumed",
            metadata_source=body.metadata_source,
        )
        db.add(model)
        db.flush()
        binding = ModelBinding(
            model_id=model.id,
            endpoint_id=endpoint.id,
            remote_model_id=body.remote_model_id,
            structured_output_mode="native_json_schema",
        )
        db.add(binding)
        for order, (code, label, api_value) in enumerate(
            (
                ("off", "Off", None),
                ("low", "Low", "low"),
                ("medium", "Medium", "medium"),
                ("high", "High", "high"),
            )
        ):
            db.add(
                ModelReasoningTier(
                    model_id=model.id,
                    code=code,
                    label=label,
                    api_value=api_value,
                    sort_order=order,
                )
            )
        db.flush()
        owner.revision += 1
        return _model_payload(db, owner, endpoint, model)


@router.patch("/models/{model_id}")
def patch_model(model_id: str, body: ModelPatch):
    with session_scope() as db:
        try:
            owner, endpoint, model, binding = model_records(db, model_id)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        _check_revision(model.revision, body.expected_revision)
        duplicate = db.scalar(
            select(ModelBinding.id)
            .join(LogicalModel, LogicalModel.id == ModelBinding.model_id)
            .where(
                ModelBinding.endpoint_id == endpoint.id,
                ModelBinding.remote_model_id == body.remote_model_id,
                LogicalModel.id != model.id,
                LogicalModel.archived_at.is_(None),
            )
        )
        if duplicate:
            api_error("MODEL_ALREADY_EXISTS", "该 Provider 下已存在此 Model ID", 409)
        model.name = body.display_name
        if (
            model.context_window_tokens != body.context_window_tokens
            or model.max_output_tokens != body.max_output_tokens
        ):
            model.metadata_source = "manual"
        model.context_window_tokens = body.context_window_tokens
        model.max_output_tokens = body.max_output_tokens
        # Preserve the legacy column for old snapshots; it no longer limits calls.
        model.enabled = body.enabled
        binding.remote_model_id = body.remote_model_id
        model.revision += 1
        binding.revision += 1
        owner.revision += 1
        db.flush()
        return _model_payload(db, owner, endpoint, model)


@router.delete("/models/{model_id}")
def archive_model(model_id: str, expected_revision: int = Query(ge=1)):
    with session_scope() as db:
        try:
            owner, _, model, _ = model_records(db, model_id)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        _check_revision(model.revision, expected_revision)
        users = _find_route_users(db, [model.id])
        if users:
            api_error(
                "CONFIG_IN_USE",
                f"模型仍被业务路由使用：{', '.join(sorted(set(users)))}",
            )
        model.enabled = False
        model.archived_at = utcnow()
        model.revision += 1
        owner.revision += 1
        return {"id": model.id, "archived": True, "revision": model.revision}


@router.put("/models/{model_id}/reasoning-tiers")
def update_reasoning_tiers(model_id: str, body: TierUpdate):
    with session_scope() as db:
        try:
            owner, endpoint, model, _ = model_records(db, model_id)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        _check_revision(model.revision, body.expected_revision)
        incoming = {tier.code: tier for tier in body.tiers}
        existing = {tier.code: tier for tier in get_model_tiers(db, model.id)}
        for code in ("default", "ppt_outline", "ppt_html"):
            try:
                config = resolve_business_route(db, code)
            except ConfigurationError:
                continue
            if config["model_id"] == model.id:
                selected = incoming.get(config["reasoning_tier"])
                if not selected or not selected.enabled:
                    api_error(
                        "REASONING_TIER_IN_USE",
                        "不能删除或停用正被业务路由使用的档位",
                        409,
                    )
        for code, tier in existing.items():
            if code not in incoming:
                in_default = db.get(BusinessRoute, "default")
                overrides = db.scalars(
                    select(BusinessRouteOverride).where(
                        BusinessRouteOverride.reasoning_tier == code
                    )
                ).all()
                if (
                    in_default
                    and in_default.model_id == model.id
                    and in_default.reasoning_tier == code
                ) or any(item.model_id == model.id for item in overrides):
                    api_error(
                        "REASONING_TIER_IN_USE",
                        f"档位 {tier.label} 正被业务路由使用",
                        409,
                    )
                db.delete(tier)
        for item in body.tiers:
            row = existing.get(item.code)
            if row:
                row.label, row.api_value, row.enabled, row.sort_order = (
                    item.label,
                    item.api_value,
                    item.enabled,
                    item.sort_order,
                )
                row.revision += 1
            else:
                db.add(
                    ModelReasoningTier(
                        model_id=model.id,
                        code=item.code,
                        label=item.label,
                        api_value=item.api_value,
                        enabled=item.enabled,
                        sort_order=item.sort_order,
                    )
                )
        model.revision += 1
        owner.revision += 1
        db.flush()
        return {
            "model_id": model.id,
            "revision": model.revision,
            "tiers": [_tier_dict(tier) for tier in get_model_tiers(db, model.id)],
        }


@router.get("/business-routes")
def list_business_routes():
    with session_scope() as db:
        routes = []
        for code, label in (
            ("default", "全局默认"),
            ("ppt_outline", "PPT 大纲生成"),
            ("ppt_html", "PPT 内容 HTML 生成"),
        ):
            if code == "default":
                route, _, _, _, _ = resolve_default(db)
                routes.append(
                    {
                        "code": code,
                        "label": label,
                        "model_id": route.model_id if route else None,
                        "reasoning_tier": route.reasoning_tier if route else "high",
                        "revision": route.revision if route else 1,
                    }
                )
            else:
                route = db.get(BusinessRouteOverride, code)
                routes.append(
                    {
                        "code": code,
                        "label": label,
                        "model_id": route.model_id if route else None,
                        "reasoning_tier": route.reasoning_tier if route else None,
                        "revision": route.revision if route else 1,
                    }
                )
        for route in routes:
            try:
                config = resolve_business_route(db, route["code"])
            except ConfigurationError as exc:
                config = None
                route["issues"] = [str(exc)]
            if config:
                route["resolved"] = {
                    key: config[key]
                    for key in (
                        "provider_id",
                        "provider_name",
                        "model_id",
                        "model_name",
                        "remote_model_id",
                        "reasoning_tier",
                        "reasoning_effort",
                        "provider_concurrency",
                        "config_hash",
                    )
                }
                route["issues"] = route_issues(db, config)
                route["ready"] = not route["issues"]
            else:
                route["resolved"], route["ready"] = None, False
        return {"items": routes}


@router.get("/business-routes/{code}/resolved")
def get_resolved_route(code: str):
    if code not in {"default", "ppt_outline", "ppt_html"}:
        api_error("UNKNOWN_BUSINESS_ROUTE", "业务路由不存在", 404)
    with session_scope() as db:
        try:
            config = resolve_business_route(db, code)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        issues = route_issues(db, config)
        return {
            "business_code": code,
            "resolved": {
                key: value for key, value in config.items() if key != "credential_ref"
            },
            "ready": not issues,
            "issues": issues,
        }


@router.put("/business-routes/{code}")
def update_business_route(code: str, body: RouteUpdate):
    if code not in {"default", "ppt_outline", "ppt_html"}:
        api_error("UNKNOWN_BUSINESS_ROUTE", "业务路由不存在", 404)
    with session_scope() as db:
        if code == "default":
            route = db.get(BusinessRoute, "default")
            if not route:
                api_error("DEFAULT_ROUTE_NOT_READY", "默认模型路由不存在")
            _check_revision(route.revision, body.expected_revision)
            if not body.model_id or not body.reasoning_tier:
                api_error("INVALID_ROUTE", "全局默认必须选择模型和思考档位", 422)
            _validate_route_model(db, body.model_id, body.reasoning_tier, code)
            route.model_id, route.reasoning_tier = body.model_id, body.reasoning_tier
            route.revision += 1
            db.flush()
            for business_code in ("ppt_outline", "ppt_html"):
                try:
                    resolved = resolve_business_route(db, business_code)
                except ConfigurationError as exc:
                    handle_configuration_error(exc)
                _validate_route_model(
                    db, resolved["model_id"], resolved["reasoning_tier"], business_code
                )
            from .db import GenerationSettingsRecord

            generation = db.get(GenerationSettingsRecord, "default")
            if generation:
                generation.reasoning_effort = body.reasoning_tier
                generation.revision += 1
            return {
                "code": code,
                "model_id": route.model_id,
                "reasoning_tier": route.reasoning_tier,
                "revision": route.revision,
            }
        route = db.get(BusinessRouteOverride, code)
        if not route:
            route = BusinessRouteOverride(code=code)
            db.add(route)
            db.flush()
        _check_revision(route.revision, body.expected_revision)
        default = db.get(BusinessRoute, "default")
        effective_model = body.model_id or (default.model_id if default else None)
        default_tier = default.reasoning_tier if default else "high"
        effective_tier = body.reasoning_tier or default_tier
        if effective_model:
            _validate_route_model(db, effective_model, effective_tier, code)
        route.model_id, route.reasoning_tier = body.model_id, body.reasoning_tier
        route.revision += 1
        db.flush()
        return {
            "code": code,
            "model_id": route.model_id,
            "reasoning_tier": route.reasoning_tier,
            "revision": route.revision,
        }


@router.post("/providers/{provider_id}/discover-models")
async def discover_models(provider_id: str, expected_revision: int = Query(ge=1)):
    with session_scope() as db:
        try:
            owner, endpoint = provider_endpoint(db, provider_id)
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        _check_revision(owner.revision, expected_revision)
        if not owner.enabled or not endpoint.enabled:
            api_error("PROVIDER_DISABLED", "Provider 已停用")
        base_url, auth_mode, secret_ref = (
            endpoint.base_url,
            endpoint.auth_mode,
            endpoint.credential_ref,
        )
        allow_private_http = endpoint.allow_private_http
        try:
            validate_endpoint(base_url, allow_private_http=allow_private_http)
            key = (
                read_secret(secret_ref)
                if auth_mode == "bearer" and secret_ref
                else None
            )
        except ProviderError as exc:
            api_error(exc.code, str(exc), 422)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "CREDENTIAL_STORE_UNAVAILABLE",
                    "message": "系统凭据存储暂不可用",
                },
            ) from exc
        if auth_mode == "bearer" and not key:
            api_error("CREDENTIAL_NOT_CONFIGURED", "请先配置 API Key 或选择无认证模式")
        provider_id_snapshot = owner.id
        endpoint_id_snapshot = endpoint.id
    headers = {"Accept": "application/json"}
    if auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    candidates = []
    try:
        async with asyncio.timeout(30):
            async with PROVIDER_LIMITER.acquire(provider_id_snapshot):
                with session_scope() as db:
                    current_owner, current_endpoint = provider_endpoint(
                        db, provider_id_snapshot
                    )
                    _check_revision(current_owner.revision, expected_revision)
                    if not current_owner.enabled or not current_endpoint.enabled:
                        api_error("PROVIDER_DISABLED", "Provider 已停用")
                    if auth_mode == "bearer" and not db.scalar(
                        select(ApiCredential.id).where(
                            ApiCredential.endpoint_id == endpoint_id_snapshot,
                            ApiCredential.secret_ref == secret_ref,
                            ApiCredential.status == "active",
                        )
                    ):
                        api_error("CREDENTIAL_REVOKED", "API 凭据已撤销")
                timeout = httpx.Timeout(25.0, connect=10.0)
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=False
                ) as client:
                    async with client.stream(
                        "GET", f"{base_url}/models", headers=headers
                    ) as response:
                        if response.status_code >= 300:
                            code = {
                                401: "PROVIDER_UNAUTHORIZED",
                                403: "PROVIDER_FORBIDDEN",
                                404: "MODEL_DISCOVERY_UNSUPPORTED",
                            }.get(response.status_code, "MODEL_DISCOVERY_FAILED")
                            api_error(
                                code,
                                "Provider 未能返回可用模型列表",
                                422 if response.status_code < 500 else 502,
                            )
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 2 * 1024 * 1024:
                                api_error(
                                    "MODEL_LIST_TOO_LARGE",
                                    "模型目录响应超过 2 MiB",
                                    502,
                                )
                        payload = json.loads(body)
        data = (
            payload.get("data", payload.get("models", []))
            if isinstance(payload, dict)
            else []
        )
        if not isinstance(data, list):
            api_error("INVALID_MODEL_LIST", "Provider 返回的模型目录格式无效", 502)
        with session_scope() as db:
            owner, endpoint = provider_endpoint(db, provider_id_snapshot)
            _check_revision(owner.revision, expected_revision)
            existing = set(
                db.scalars(
                    select(ModelBinding.remote_model_id)
                    .join(LogicalModel, LogicalModel.id == ModelBinding.model_id)
                    .where(
                        ModelBinding.endpoint_id == endpoint.id,
                        LogicalModel.archived_at.is_(None),
                    )
                ).all()
            )
        seen = set()
        for item in data:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"].strip()
            ):
                continue
            if item["id"].strip() in seen or len(item["id"].strip()) > 200:
                continue
            seen.add(item["id"].strip())
            context = item.get("context_length", item.get("context_window_tokens"))
            output = item.get("max_output_tokens")
            candidates.append(
                {
                    "remote_model_id": item["id"].strip(),
                    "display_name": str(item.get("name") or item["id"]).strip()[:200]
                    or item["id"].strip(),
                    "context_window_tokens": (
                        context if type(context) is int and context > 0 else None
                    ),
                    "max_output_tokens": (
                        output if type(output) is int and output > 0 else None
                    ),
                    "metadata_source": (
                        "provider_api" if context or output else "unknown"
                    ),
                    "already_added": item["id"].strip() in existing,
                }
            )
        return {"provider_id": provider_id_snapshot, "items": candidates}
    except ConfigurationError as exc:
        handle_configuration_error(exc)
    except HTTPException:
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=504,
            detail={"code": "MODEL_DISCOVERY_TIMEOUT", "message": "获取模型目录超时"},
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "MODEL_DISCOVERY_CONNECTION_FAILED",
                "message": "无法连接 Provider",
            },
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "INVALID_MODEL_LIST",
                "message": "Provider 返回的模型目录格式无效",
            },
        ) from exc


@router.post("/models/{model_id}/test")
async def test_model(model_id: str):
    with session_scope() as db:
        try:
            owner, endpoint, model, _ = model_records(db, model_id)
            config = resolve_model_config(db, model.id, "off", "provider_test")
        except ConfigurationError as exc:
            handle_configuration_error(exc)
        if not owner.enabled or not endpoint.enabled or not model.enabled:
            api_error("MODEL_NOT_READY", "Provider 或模型已停用")
        if config["auth_mode"] == "bearer":
            try:
                if not config.get("credential_ref") or not read_secret(
                    config["credential_ref"]
                ):
                    api_error("CREDENTIAL_NOT_CONFIGURED", "请先配置 API Key")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "CREDENTIAL_STORE_UNAVAILABLE",
                        "message": "系统凭据存储暂不可用",
                    },
                ) from exc
        test_hash = endpoint_test_hash(db, model.id)
        test = ProviderTest(
            provider_id=owner.id,
            model_id=model.id,
            config_hash=test_hash,
            status="running",
            checks_json="[]",
        )
        db.add(test)
        db.flush()
        test_id = test.id
        config["reasoning_tier"] = "off"
        config["reasoning_effort"] = None
        config["config_hash"] = test_hash
        provider_id_snapshot, provider_name, model_name = (
            owner.id,
            owner.name,
            model.name,
        )
    from .runtime import ProviderGateway

    gateway = ProviderGateway()
    checks = []
    for check_name, schema, prompt, instructions in (
        (
            "streaming_text",
            None,
            "Reply with the word OK.",
            "Reply with one short word.",
        ),
        (
            "structured_output",
            {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            "Return JSON with ok set to true.",
            "Return the requested JSON.",
        ),
    ):
        started = time.monotonic()
        try:
            async with asyncio.timeout(45):
                result = await gateway._stream(
                    prompt,
                    None,
                    "provider_test",
                    schema=schema,
                    resolved_config=config,
                    instructions=instructions,
                    max_output_tokens=64,
                )
            if schema:
                parsed = json.loads(result)
                if (
                    not isinstance(parsed, dict)
                    or set(parsed) != {"ok"}
                    or parsed["ok"] is not True
                ):
                    raise ValueError(
                        "structured output did not match the test contract"
                    )
            elif not result.strip():
                raise ValueError("empty response")
            checks.append(
                {
                    "name": check_name,
                    "status": "passed",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, (ProviderError, ConfigurationError))
                else (
                    "MODEL_TEST_TIMEOUT"
                    if isinstance(exc, TimeoutError)
                    else "CHECK_FAILED"
                )
            )
            message = (
                str(exc)
                if isinstance(exc, (ProviderError, ConfigurationError))
                else (
                    "模型测试超时"
                    if isinstance(exc, TimeoutError)
                    else "模型未通过该项检查"
                )
            )
            checks.append(
                {
                    "name": check_name,
                    "status": "failed",
                    "error_code": code,
                    "message": message[:180],
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
        except asyncio.CancelledError:
            with session_scope() as db:
                row = db.get(ProviderTest, test_id)
                row.status = "cancelled"
                row.checks_json = dumps(checks)
                row.finished_at = utcnow()
            raise
    outcome = (
        "passed" if all(check["status"] == "passed" for check in checks) else "failed"
    )
    with session_scope() as db:
        row = db.get(ProviderTest, test_id)
        row.status = outcome
        row.checks_json = dumps(checks)
        row.finished_at = utcnow()
    return {
        "test_id": test_id,
        "provider_id": provider_id_snapshot,
        "provider_name": provider_name,
        "model_id": model_id,
        "model_name": model_name,
        "config_hash": test_hash,
        "status": outcome,
        "checks": checks,
    }


@router.get("/calls")
def list_calls(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=36),
):
    with session_scope() as db:
        query = select(LlmCall).order_by(LlmCall.started_at.desc(), LlmCall.id.desc())
        if cursor is not None:
            anchor = db.get(LlmCall, cursor)
            if not anchor:
                api_error("INVALID_CALL_CURSOR", "调用记录游标无效", 422)
            query = query.where(
                (LlmCall.started_at < anchor.started_at)
                | ((LlmCall.started_at == anchor.started_at) & (LlmCall.id < anchor.id))
            )
        rows = db.scalars(query.limit(limit + 1)).all()
        more = len(rows) > limit
        rows = rows[:limit]
        return {
            "items": [
                {
                    "id": row.id,
                    "purpose": row.purpose,
                    "business_code": row.business_code,
                    "provider_id": row.provider_id,
                    "model_id": row.model_id,
                    "model": row.model,
                    "status": row.status,
                    "error_code": row.error_code,
                    "error": row.error,
                    "queue_duration_ms": row.queue_duration_ms,
                    "time_to_first_token_ms": row.time_to_first_token_ms,
                    "total_duration_ms": row.total_duration_ms,
                    "started_at": row.started_at.isoformat(),
                }
                for row in rows
            ],
            "next_cursor": rows[-1].id if more and rows else None,
        }


@router.get("/calls/{call_id}")
def get_call(call_id: str):
    with session_scope() as db:
        row = db.get(LlmCall, call_id)
        if not row:
            api_error("CALL_NOT_FOUND", "模型调用记录不存在", 404)
        return {
            "id": row.id,
            "purpose": row.purpose,
            "business_code": row.business_code,
            "provider_id": row.provider_id,
            "model_id": row.model_id,
            "model": row.model,
            "status": row.status,
            "error_code": row.error_code,
            "error": row.error,
            "usage": {
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "total_tokens": row.total_tokens,
            },
            "queue_duration_ms": row.queue_duration_ms,
            "time_to_first_token_ms": row.time_to_first_token_ms,
            "total_duration_ms": row.total_duration_ms,
            "provider_response_id": row.provider_response_id,
            "config_snapshot": (
                {
                    key: value
                    for key, value in loads(row.config_snapshot_json).items()
                    if key not in {"credential_ref", "api_key"}
                }
                if row.config_snapshot_json
                else None
            ),
            "started_at": row.started_at.isoformat(),
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }
