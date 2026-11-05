from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings, _read_key_file
from .secrets import save_secret


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


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


class ApiEndpoint(Base):
    __tablename__ = "api_endpoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id: Mapped[str] = mapped_column(ForeignKey("api_providers.id"))
    name: Mapped[str] = mapped_column(String(160))
    protocol: Mapped[str] = mapped_column(String(80), default="openai_responses")
    base_url: Mapped[str] = mapped_column(String(500))
    credential_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class LogicalModel(Base):
    __tablename__ = "logical_models"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ModelBinding(Base):
    __tablename__ = "model_bindings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    model_id: Mapped[str] = mapped_column(ForeignKey("logical_models.id"), unique=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("api_endpoints.id"))
    remote_model_id: Mapped[str] = mapped_column(String(160))
    structured_output_mode: Mapped[str] = mapped_column(String(40), default="native_json_schema")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class BusinessRoute(Base):
    __tablename__ = "business_routes"
    code: Mapped[str] = mapped_column(String(80), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("logical_models.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)


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


def init_db() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.get(GenerationSettingsRecord, "default") is None:
            db.add(GenerationSettingsRecord(id="default", reasoning_effort="high"))
            db.commit()
        provider = db.scalar(select(Provider).where(Provider.code == "default"))
        if provider is None:
            key = os.getenv("DS_API_KEY") or _read_key_file().get("api_key", "")
            ref = str(uuid.uuid4()) if key else None
            if ref:
                save_secret(ref, key)
            db.add(Provider(code="default", name="Default DeepSeek", protocol="openai_responses", base_url=settings.api_base_url, model=settings.api_model, api_key_configured=bool(key), credential_ref=ref))
            db.commit()
            provider = db.scalar(select(Provider).where(Provider.code == "default"))
        if not db.get(BusinessRoute, "default"):
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
