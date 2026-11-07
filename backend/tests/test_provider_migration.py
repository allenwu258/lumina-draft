"""Upgrade pre-management databases without reading or copying secret values."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from backend.app import db as database
from backend.app.db import (
    ApiCredential,
    ApiProvider,
    Base,
    BusinessRoute,
    BusinessRouteOverride,
    Job,
    ModelReasoningTier,
    Presentation,
    Run,
    SchemaMigration,
    dumps,
    loads,
    resolve_default,
)
from backend.app.provider_management import resolve_business_route


LEGACY_TABLES = (
    """CREATE TABLE providers (
        id VARCHAR(36) PRIMARY KEY, code VARCHAR(80) UNIQUE, name VARCHAR(160),
        protocol VARCHAR(80), base_url VARCHAR(500), model VARCHAR(160),
        api_key_configured BOOLEAN, credential_ref VARCHAR(80), enabled BOOLEAN,
        revision INTEGER, created_at DATETIME, updated_at DATETIME)""",
    "CREATE TABLE generation_settings (id VARCHAR(40) PRIMARY KEY, reasoning_effort VARCHAR(20), revision INTEGER)",
    "CREATE TABLE api_providers (id VARCHAR(36) PRIMARY KEY, code VARCHAR(80) UNIQUE, name VARCHAR(160), enabled BOOLEAN, revision INTEGER)",
    "CREATE TABLE logical_models (id VARCHAR(36) PRIMARY KEY, code VARCHAR(80) UNIQUE, name VARCHAR(200), enabled BOOLEAN, revision INTEGER)",
    """CREATE TABLE api_endpoints (id VARCHAR(36) PRIMARY KEY, provider_id VARCHAR(36),
        name VARCHAR(160), protocol VARCHAR(80), base_url VARCHAR(500), credential_ref VARCHAR(120),
        enabled BOOLEAN, revision INTEGER)""",
    """CREATE TABLE model_bindings (id VARCHAR(36) PRIMARY KEY, model_id VARCHAR(36) UNIQUE,
        endpoint_id VARCHAR(36), remote_model_id VARCHAR(200), structured_output_mode VARCHAR(40),
        enabled BOOLEAN, revision INTEGER)""",
    "CREATE TABLE business_routes (code VARCHAR(80) PRIMARY KEY, model_id VARCHAR(36), revision INTEGER)",
)


@pytest.mark.parametrize("source", ["legacy_only", "normalized", "interrupted"])
@pytest.mark.parametrize(
    "effort,expected", [("auto", None), ("none", "none"), ("max", "max")]
)
def test_upgrade_preserves_routes_credentials_and_snapshots(
    tmp_path, monkeypatch, source, effort, expected
):
    engine = create_engine("sqlite:///" + (tmp_path / "old.db").as_posix())
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(
        database,
        "settings",
        SimpleNamespace(
            db_url=str(engine.url),
            api_base_url="https://unused.example",
            api_model="unused",
        ),
    )

    def unexpected_secret_write(*args):
        pytest.fail("Migration must preserve references without copying a secret")

    monkeypatch.setattr(database, "save_secret", unexpected_secret_write)
    try:
        with engine.begin() as connection:
            for statement in LEGACY_TABLES:
                connection.execute(text(statement))
            connection.execute(
                text(
                    "INSERT INTO providers VALUES ('legacy', 'default', 'Original', 'openai_responses', 'https://example.com/v1', 'original-model', 1, 'original-secret-ref', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                text("INSERT INTO generation_settings VALUES ('default', :effort, 7)"),
                {"effort": effort},
            )
            if source != "legacy_only":
                connection.execute(
                    text(
                        "INSERT INTO api_providers VALUES ('owner', 'default-provider', 'Existing', 1, 4)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO logical_models VALUES ('model', 'default-model', 'Existing Model', 1, 3)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO api_endpoints VALUES ('endpoint', 'owner', 'Existing', 'openai_responses', 'https://example.com/v2', 'normalized-secret-ref', 1, 2)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO model_bindings VALUES ('binding', 'model', 'endpoint', 'existing-model', 'native_json_schema', 1, 6)"
                    )
                )
                connection.execute(
                    text("INSERT INTO business_routes VALUES ('default', 'model', 8)")
                )
        Base.metadata.create_all(engine)
        original_snapshot = dumps(
            {
                "model_id": "model",
                "model": "frozen-model",
                "reasoning_effort": effort,
                "endpoint_id": "endpoint",
                "credential_ref": "frozen-previous-key-ref",
            }
        )
        with sessions.begin() as db:
            presentation = Presentation(
                id="old-presentation",
                user_request="existing",
                config_snapshot_json=original_snapshot,
            )
            db.add(presentation)
            db.flush()
            run = Run(
                id="old-run",
                presentation_id=presentation.id,
                kind="DIRECTOR",
                status="QUEUED",
            )
            db.add(run)
            db.flush()
            db.add(
                Job(
                    id="old-job",
                    run_id=run.id,
                    presentation_id=presentation.id,
                    kind="DIRECTOR",
                    status="QUEUED",
                )
            )
        if source == "interrupted":
            database._upgrade_existing_schema()
            # Simulate restart after schema changes but before data migration commits.
        database.init_db()
        with sessions.begin() as db:
            route, model, binding, endpoint, owner = resolve_default(db)
            assert resolve_business_route(db, "default")["reasoning_effort"] == expected
            assert binding.remote_model_id == (
                "original-model" if source == "legacy_only" else "existing-model"
            )
            assert endpoint.credential_ref == (
                "original-secret-ref"
                if source == "legacy_only"
                else "normalized-secret-ref"
            )
            credential = db.scalar(select(ApiCredential))
            assert (
                credential.secret_ref == endpoint.credential_ref
                and credential.version == 1
            )
            assert credential.status == "active"
            if source != "legacy_only":
                previous = db.scalar(
                    select(ApiCredential).where(
                        ApiCredential.secret_ref == "frozen-previous-key-ref"
                    )
                )
                assert previous.status == "retired" and previous.version == 2
            assert (
                db.get(Presentation, "old-presentation").config_snapshot_json
                == original_snapshot
            )
            assert db.get(Job, "old-job").model_id == "model"
            assert len(db.scalars(select(BusinessRouteOverride)).all()) == 2
            assert len(db.scalars(select(SchemaMigration)).all()) == 2
            assert len(db.scalars(select(ApiProvider)).all()) == 1
            if source != "legacy_only":
                assert (owner.id, endpoint.id, model.id, binding.id) == (
                    "owner",
                    "endpoint",
                    "model",
                    "binding",
                )
                assert (owner.revision, route.revision) == (4, 8)
            assert {tier.code for tier in db.scalars(select(ModelReasoningTier))} >= {
                "off",
                "low",
                "medium",
                "high",
            }
            route.reasoning_tier = "low"
        database.init_db()
        with sessions() as db:
            assert resolve_business_route(db, "default")["reasoning_effort"] == "low"
            assert len(db.scalars(select(ApiCredential)).all()) == (
                1 if source == "legacy_only" else 2
            )
            assert (
                loads(db.get(Presentation, "old-presentation").config_snapshot_json)[
                    "reasoning_effort"
                ]
                == effort
            )
        assert "total_duration_ms" in {
            column["name"] for column in inspect(engine).get_columns("llm_calls")
        }
    finally:
        engine.dispose()


def test_provider_concurrency_is_migrated_once_to_each_model(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "provider-capacity.db").as_posix())
    monkeypatch.setattr(database, "engine", engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE schema_migrations (version VARCHAR(80) PRIMARY KEY, applied_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE api_providers (id VARCHAR(36) PRIMARY KEY, max_concurrency INTEGER NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE api_endpoints (id VARCHAR(36) PRIMARY KEY, provider_id VARCHAR(36))"
        )
        connection.exec_driver_sql(
            "CREATE TABLE logical_models (id VARCHAR(36) PRIMARY KEY, max_concurrency INTEGER NOT NULL DEFAULT 1)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE model_bindings (model_id VARCHAR(36) PRIMARY KEY, endpoint_id VARCHAR(36))"
        )
        connection.exec_driver_sql("INSERT INTO api_providers VALUES ('local', 1), ('cloud', 7)")
        connection.exec_driver_sql("INSERT INTO api_endpoints VALUES ('ep-local', 'local'), ('ep-cloud', 'cloud')")
        connection.exec_driver_sql("INSERT INTO logical_models (id) VALUES ('local-a'), ('local-b'), ('cloud-a')")
        connection.exec_driver_sql("INSERT INTO model_bindings VALUES ('local-a', 'ep-local'), ('local-b', 'ep-local'), ('cloud-a', 'ep-cloud')")

    database._migrate_model_concurrency()
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT id, max_concurrency FROM logical_models ORDER BY id"
        ).all() == [("cloud-a", 7), ("local-a", 1), ("local-b", 1)]
        assert "max_concurrency" not in {
            column["name"] for column in inspect(connection).get_columns("api_providers")
        }
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE logical_models SET max_concurrency = 11 WHERE id = 'local-a'"
        )
    database._migrate_model_concurrency()
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT max_concurrency FROM logical_models WHERE id = 'local-a'"
        ).scalar_one() == 11
    engine.dispose()
