from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings, _read_key_file
from .secrets import save_secret


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    version: Mapped[str] = mapped_column(String(80), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


PROVIDER_MIGRATION = "20261106_provider_management_v1"
MODEL_CONCURRENCY_MIGRATION = "20261107_model_concurrency_v1"


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    protocol: Mapped[str] = mapped_column(String(80), default="openai_responses")
    base_url: Mapped[str] = mapped_column(String(500))
    model: Mapped[str] = mapped_column(String(160))
    api_key_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    credential_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ApiProvider(Base):
    __tablename__ = "api_providers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}


class ApiEndpoint(Base):
    __tablename__ = "api_endpoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id: Mapped[str] = mapped_column(ForeignKey("api_providers.id"))
    name: Mapped[str] = mapped_column(String(160))
    protocol: Mapped[str] = mapped_column(String(80), default="openai_responses")
    base_url: Mapped[str] = mapped_column(String(500))
    credential_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    auth_mode: Mapped[str] = mapped_column(String(20), default="bearer")
    allow_private_http: Mapped[bool] = mapped_column(Boolean, default=False)


class LogicalModel(Base):
    __tablename__ = "logical_models"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    context_window_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    capabilities_json: Mapped[str] = mapped_column(Text, default='{"text_input":true,"multimodal_input":true,"tool_calling":true}')
    capability_source: Mapped[str] = mapped_column(String(20), default="assumed")
    metadata_source: Mapped[str] = mapped_column(String(30), default="unknown")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}


class ModelBinding(Base):
    __tablename__ = "model_bindings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    model_id: Mapped[str] = mapped_column(ForeignKey("logical_models.id"), unique=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("api_endpoints.id"))
    remote_model_id: Mapped[str] = mapped_column(String(200))
    structured_output_mode: Mapped[str] = mapped_column(String(40), default="native_json_schema")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class BusinessRoute(Base):
    __tablename__ = "business_routes"
    code: Mapped[str] = mapped_column(String(80), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("logical_models.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    reasoning_tier: Mapped[str] = mapped_column(String(40), default="high")
    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}


class BusinessRouteOverride(Base):
    __tablename__ = "business_route_overrides"
    code: Mapped[str] = mapped_column(String(80), primary_key=True)
    model_id: Mapped[str | None] = mapped_column(ForeignKey("logical_models.id"), nullable=True)
    reasoning_tier: Mapped[str | None] = mapped_column(String(40), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}


class ModelReasoningTier(Base):
    __tablename__ = "model_reasoning_tiers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    model_id: Mapped[str] = mapped_column(ForeignKey("logical_models.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(80))
    api_value: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("model_id", "code", name="uq_model_reasoning_tier"),)


class ApiCredential(Base):
    __tablename__ = "api_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("api_endpoints.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    secret_ref: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("endpoint_id", "version", name="uq_endpoint_credential_version"),)


class ProviderTest(Base):
    __tablename__ = "provider_tests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id: Mapped[str] = mapped_column(ForeignKey("api_providers.id"), index=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("logical_models.id"), index=True)
    config_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    checks_json: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GenerationSettingsRecord(Base):
    __tablename__ = "generation_settings"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default="default")
    reasoning_effort: Mapped[str] = mapped_column(String(20), default="high")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Presentation(Base):
    __tablename__ = "presentations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_request: Mapped[str] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(40), default="PLANNING", index=True)
    config_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    presentation_id: Mapped[str] = mapped_column(ForeignKey("presentations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Outline(Base):
    __tablename__ = "outlines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    presentation_id: Mapped[str] = mapped_column(ForeignKey("presentations.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    document_json: Mapped[str] = mapped_column(Text)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageTask(Base):
    __tablename__ = "page_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    presentation_id: Mapped[str] = mapped_column(ForeignKey("presentations.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    slide_id: Mapped[str] = mapped_column(String(36))
    page_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    brief_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    html: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    presentation_id: Mapped[str] = mapped_column(ForeignKey("presentations.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(80), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("presentation_id", "seq", name="uq_event_presentation_seq"),)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True, index=True)
    presentation_id: Mapped[str] = mapped_column(ForeignKey("presentations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    queue_reason: Mapped[str | None] = mapped_column(String(40), default="SCHEDULER", nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queue_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),)


class LlmCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    presentation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    purpose: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30))
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    business_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_response_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    config_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Path("data").mkdir(exist_ok=True)
engine = create_engine(settings.db_url, connect_args={"check_same_thread": False} if settings.db_url.startswith("sqlite") else {})
if settings.db_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


DEFAULT_REASONING_TIERS = (("off", None), ("low", "low"), ("medium", "medium"), ("high", "high"))


def legacy_reasoning_tier(db, model_id: str, effort: str) -> str:
    """Preserve existing wire values; only auto means omission of reasoning."""
    code = "off" if effort == "auto" else effort
    if not db.scalar(select(ModelReasoningTier.id).where(
        ModelReasoningTier.model_id == model_id, ModelReasoningTier.code == code,
    )):
        db.add(ModelReasoningTier(
            model_id=model_id, code=code, label=code.title(),
            api_value=None if code == "off" else effort, sort_order=10,
        ))
        db.flush()
    return code


def init_db() -> None:
    Base.metadata.create_all(engine)
    migrated_reasoning = _upgrade_existing_schema()
    with SessionLocal() as db:
        if db.get(GenerationSettingsRecord, "default") is None:
            db.add(GenerationSettingsRecord(id="default", reasoning_effort="high"))
            db.commit()
        default_route = db.get(BusinessRoute, "default")
        provider = db.scalar(select(Provider).where(Provider.code == "default"))
        if provider is None and default_route is None:
            key = os.getenv("DS_API_KEY") or _read_key_file().get("api_key", "")
            ref = str(uuid.uuid4()) if key else None
            if ref:
                save_secret(ref, key)
            db.add(Provider(code="default", name="Default DeepSeek", protocol="openai_responses", base_url=settings.api_base_url, model=settings.api_model, api_key_configured=bool(key), credential_ref=ref))
            db.commit()
            provider = db.scalar(select(Provider).where(Provider.code == "default"))
        imported_default = default_route is None
        if imported_default:
            # Import the initial one-row configuration into the normalized routing model.
            owner = ApiProvider(code="default-provider", name=provider.name, enabled=provider.enabled)
            model = LogicalModel(code="default-model", name=provider.name, enabled=provider.enabled)
            db.add_all([owner, model])
            db.flush()
            endpoint = ApiEndpoint(provider_id=owner.id, name=provider.name, protocol=provider.protocol, base_url=provider.base_url, credential_ref=provider.credential_ref, enabled=provider.enabled)
            db.add(endpoint)
            db.flush()
            db.add_all([ModelBinding(model_id=model.id, endpoint_id=endpoint.id, remote_model_id=provider.model, enabled=provider.enabled), BusinessRoute(code="default", model_id=model.id)])
            db.commit()
        default_route = db.get(BusinessRoute, "default")
        generation = db.get(GenerationSettingsRecord, "default")
        for route_code in ("ppt_outline", "ppt_html"):
            if db.get(BusinessRouteOverride, route_code) is None:
                db.add(BusinessRouteOverride(code=route_code))
        for model in db.scalars(select(LogicalModel)).all():
            if not db.scalar(select(ModelReasoningTier.id).where(ModelReasoningTier.model_id == model.id).limit(1)):
                for order, (code, value) in enumerate(DEFAULT_REASONING_TIERS):
                    db.add(ModelReasoningTier(model_id=model.id, code=code, label=code.title(), api_value=value, sort_order=order))
        db.flush()
        if (migrated_reasoning or imported_default) and default_route and generation:
            default_route.reasoning_tier = legacy_reasoning_tier(db, default_route.model_id, generation.reasoning_effort)
        for endpoint in db.scalars(select(ApiEndpoint).where(ApiEndpoint.credential_ref.is_not(None))).all():
            if not db.scalar(select(ApiCredential.id).where(ApiCredential.endpoint_id == endpoint.id).limit(1)):
                db.add(ApiCredential(endpoint_id=endpoint.id, version=1, secret_ref=endpoint.credential_ref, status="active"))
        db.flush()
        if migrated_reasoning:
            # Previous releases rotated keys without credential history. Preserve refs
            # frozen by existing works without reading the secret values themselves.
            for presentation in db.scalars(select(Presentation)).all():
                snapshot = loads(presentation.config_snapshot_json)
                endpoint_id, ref = snapshot.get("endpoint_id"), snapshot.get("credential_ref")
                if not endpoint_id or not ref or not db.get(ApiEndpoint, endpoint_id):
                    continue
                if db.scalar(select(ApiCredential.id).where(ApiCredential.secret_ref == ref)):
                    continue
                version = (db.scalar(select(ApiCredential.version).where(
                    ApiCredential.endpoint_id == endpoint_id,
                ).order_by(ApiCredential.version.desc()).limit(1)) or 0) + 1
                db.add(ApiCredential(endpoint_id=endpoint_id, version=version, secret_ref=ref, status="retired"))
                db.flush()
        if not db.get(SchemaMigration, PROVIDER_MIGRATION):
            db.add(SchemaMigration(version=PROVIDER_MIGRATION))
        db.commit()
    _migrate_model_concurrency()


def _upgrade_existing_schema() -> bool:
    if not settings.db_url.startswith("sqlite"):
        return False
    additions = {
        "api_providers": {
            "archived_at": "DATETIME NULL",
            "created_at": "DATETIME NULL",
            "updated_at": "DATETIME NULL",
        },
        "api_endpoints": {
            "auth_mode": "VARCHAR(20) NOT NULL DEFAULT 'bearer'",
            "allow_private_http": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "logical_models": {
            "context_window_tokens": "INTEGER NULL",
            "max_output_tokens": "INTEGER NULL",
            "max_concurrency": "INTEGER NOT NULL DEFAULT 1",
            "capabilities_json": "TEXT NOT NULL DEFAULT '{\"text_input\":true,\"multimodal_input\":true,\"tool_calling\":true}'",
            "capability_source": "VARCHAR(20) NOT NULL DEFAULT 'assumed'",
            "metadata_source": "VARCHAR(30) NOT NULL DEFAULT 'unknown'",
            "archived_at": "DATETIME NULL",
        },
        "business_routes": {"reasoning_tier": "VARCHAR(40) NOT NULL DEFAULT 'high'"},
        "page_tasks": {"error_code": "VARCHAR(80) NULL"},
        "jobs": {
            "error_code": "VARCHAR(80) NULL",
            "error": "TEXT NULL",
            "queue_reason": "VARCHAR(40) NULL",
            "started_at": "DATETIME NULL",
            "queued_at": "DATETIME NULL",
            "finished_at": "DATETIME NULL",
            "queue_duration_ms": "INTEGER NULL",
        },
        "runs": {"error_code": "VARCHAR(80) NULL"},
        "llm_calls": {
            "error_code": "VARCHAR(80) NULL",
            "provider_id": "VARCHAR(36) NULL",
            "model_id": "VARCHAR(36) NULL",
            "business_code": "VARCHAR(40) NULL",
            "provider_response_id": "VARCHAR(200) NULL",
            "config_snapshot_json": "TEXT NULL",
            "queue_duration_ms": "INTEGER NULL",
            "queue_reason": "VARCHAR(40) NULL",
            "admitted_at": "DATETIME NULL",
            "time_to_first_token_ms": "INTEGER NULL",
            "total_duration_ms": "INTEGER NULL",
        },
    }
    with SessionLocal() as db:
        needs_data_migration = db.get(SchemaMigration, PROVIDER_MIGRATION) is None
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {column["name"] for column in inspect(connection).get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    # Static DDL contains JSON colons, which text() treats as bind parameters.
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.execute(text("UPDATE api_providers SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
        connection.execute(text("UPDATE api_providers SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"))
        connection.execute(text("UPDATE jobs SET queued_at = created_at WHERE queued_at IS NULL"))
    return needs_data_migration


def _migrate_model_concurrency() -> None:
    with engine.begin() as connection:
        applied = connection.execute(
            text("SELECT 1 FROM schema_migrations WHERE version = :version"),
            {"version": MODEL_CONCURRENCY_MIGRATION},
        ).first()
        if applied:
            return
        provider_columns = {
            column["name"] for column in inspect(connection).get_columns("api_providers")
        }
        if "max_concurrency" in provider_columns:
            connection.execute(
                text(
                    """UPDATE logical_models
                    SET max_concurrency = (
                        SELECT api_providers.max_concurrency
                        FROM model_bindings
                        JOIN api_endpoints ON api_endpoints.id = model_bindings.endpoint_id
                        JOIN api_providers ON api_providers.id = api_endpoints.provider_id
                        WHERE model_bindings.model_id = logical_models.id
                    )
                    WHERE EXISTS (
                        SELECT 1 FROM model_bindings
                        WHERE model_bindings.model_id = logical_models.id
                    )"""
                )
            )
            connection.exec_driver_sql(
                "ALTER TABLE api_providers DROP COLUMN max_concurrency"
            )
        connection.execute(
            text(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES (:version, CURRENT_TIMESTAMP)"
            ),
            {"version": MODEL_CONCURRENCY_MIGRATION},
        )


def resolve_default(db):
    route = db.get(BusinessRoute, "default")
    model = db.get(LogicalModel, route.model_id) if route else None
    binding = db.scalar(select(ModelBinding).where(ModelBinding.model_id == model.id)) if model else None
    endpoint = db.get(ApiEndpoint, binding.endpoint_id) if binding else None
    owner = db.get(ApiProvider, endpoint.provider_id) if endpoint else None
    return route, model, binding, endpoint, owner


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str) -> object:
    return json.loads(value)
