import json
import shutil

import pytest
from sqlalchemy import Column, MetaData, String, Table, create_engine, insert, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from backend.app import db as database, export_service, scene_service, works_service
from backend.app.db import Base, Outline, PageScene, PageTask, PptxExport, Run, Work, WorkPage, session_scope, utcnow
from backend.tests.test_exports import add_outline, export_queue, save_scene
from backend.tests.test_provider_management import managed_client
from backend.tests.test_scenes import scene_queue, seed_pages, wait_until


def source_hash(presentation_id, batch_id=None):
    with session_scope() as db:
        return export_service.source_summary(db, presentation_id, batch_id)["source_hash"]


def create_work(client, presentation_id, key, batch_id=None):
    response = client.post(
        f"/api/v1/presentations/{presentation_id}/works",
        headers={"Idempotency-Key": key},
        json={
            "expected_source_hash": source_hash(presentation_id, batch_id),
            **({"batch_id": batch_id} if batch_id else {}),
        },
    )
    return response


def prepare_work(client, presentation_id, batch_id=None):
    response = create_work(client, presentation_id, "create-work-test-key-0001", batch_id)
    assert response.status_code == 201, response.text
    return response.json()["work"]["id"]


def test_create_work_is_idempotent_revisioned_and_asset_independent(
    export_queue, monkeypatch, tmp_path
):
    client, _, _ = export_queue
    monkeypatch.setattr(works_service, "OBJECT_ROOT", tmp_path / "objects")
    monkeypatch.setattr(works_service, "SCENE_ROOT", scene_service.SCENE_ROOT)
    presentation_id, _, pages = seed_pages(2)
    add_outline(presentation_id, pages)
    save_scene(pages[0], image_color=(30, 140, 90, 255))
    save_scene(pages[1])

    first = create_work(client, presentation_id, "create-work-test-key-0001")
    assert first.status_code == 201, first.text
    first_id = first.json()["work"]["id"]
    assert create_work(client, presentation_id, "create-work-test-key-0001").json()["work"]["id"] == first_id
    second = create_work(client, presentation_id, "create-work-test-key-0002")
    assert second.status_code == 201 and second.json()["work"]["id"] != first_id

    work = client.get(f"/api/v1/works/{first_id}").json()
    assert [page["position"] for page in work["pages"]] == [1, 2]
    first_page = work["pages"][0]
    page_response = client.get(
        f"/api/v1/works/{first_id}/pages/{first_page['id']}"
    )
    assert page_response.status_code == 200
    assert len(page_response.headers["etag"].strip('"')) == 64
    page_document = page_response.json()
    assert page_document["id"] == first_page["id"]
    assert page_document["position"] == first_page["position"]
    assert page_document["title"] == first_page["title"]
    scene = page_document["scene"]
    asset = scene["assets"][0]
    with session_scope() as db:
        source_scene = db.scalar(select(PageScene).where(PageScene.page_task_id == pages[0].id))
        source_folder = scene_service.SCENE_ROOT / source_scene.asset_dir
    shutil.rmtree(source_folder)
    saved_asset = client.get(
        f"/api/v1/works/{first_id}/pages/{first_page['id']}/assets/{asset['id']}"
    )
    assert saved_asset.status_code == 200 and saved_asset.content[:8] == b"\x89PNG\r\n\x1a\n"

    saved = client.patch(
        f"/api/v1/works/{first_id}", json={"name": "独立作品", "base_revision": 1}
    )
    assert saved.status_code == 200 and saved.json()["revision"] == 2
    conflict = client.patch(
        f"/api/v1/works/{first_id}", json={"name": "旧标签页", "base_revision": 1}
    )
    assert conflict.status_code == 409
    assert client.get("/api/v1/works").status_code == 200


def test_agent_batch_work_creation_uses_the_selected_batch(export_queue, monkeypatch, tmp_path):
    client, _, _ = export_queue
    monkeypatch.setattr(works_service, "OBJECT_ROOT", tmp_path / "objects")
    monkeypatch.setattr(works_service, "SCENE_ROOT", scene_service.SCENE_ROOT)
    presentation_id, run_id, pages = seed_pages(1)
    add_outline(presentation_id, pages)
    with session_scope() as db:
        outline = db.scalar(select(Outline).where(Outline.presentation_id == presentation_id))
        run = db.get(Run, run_id)
        run.batch_id, run.outline_id = run.id, outline.id
        db.get(PageTask, pages[0].id).batch_id = run.id
        batch_id = run.id
    save_scene(pages[0])

    response = create_work(client, presentation_id, "create-agent-batch-key-0001", batch_id)
    assert response.status_code == 201, response.text
    with session_scope() as db:
        work = db.get(Work, response.json()["work"]["id"])
        source = json.loads(work.source_json)
        assert source["batch_id"] == batch_id
        stored_pages = db.scalars(select(WorkPage).where(WorkPage.work_id == work.id)).all()
        assert len(stored_pages) == 1


def test_work_export_freezes_the_requested_revision(export_queue, monkeypatch, tmp_path):
    client, worker, start = export_queue
    monkeypatch.setattr(works_service, "OBJECT_ROOT", tmp_path / "objects")
    monkeypatch.setattr(works_service, "SCENE_ROOT", scene_service.SCENE_ROOT)
    presentation_id, _, pages = seed_pages(1)
    add_outline(presentation_id, pages)
    save_scene(pages[0])
    work_id = prepare_work(client, presentation_id)

    worker.hold_export = True
    first = client.post(
        f"/api/v1/works/{work_id}/exports", json={"expected_revision": 1}
    )
    assert first.status_code == 202, first.text
    first_record = first.json()
    start()
    wait_until(lambda: worker.exports)

    saved = client.patch(
        f"/api/v1/works/{work_id}", json={"name": "修订后的作品", "base_revision": 1}
    )
    assert saved.status_code == 200 and saved.json()["revision"] == 2
    second = client.post(
        f"/api/v1/works/{work_id}/exports", json={"expected_revision": 2}
    )
    assert second.status_code == 202, second.text

    with session_scope() as db:
        first_export = db.get(export_service.PptxExport, first_record["id"])
        first_manifest = json.loads(first_export.input_manifest_json)
        second_export = db.get(export_service.PptxExport, second.json()["id"])
        second_manifest = json.loads(second_export.input_manifest_json)
    assert first_manifest["title"] != second_manifest["title"]
    assert first_manifest["pages"][0]["scene_hash"] == second_manifest["pages"][0]["scene_hash"]

    worker.hold_export = False
    finished = wait_until(
        lambda: (
            states
            if all(
                (states := [
                    client.get(f"/api/v1/exports/{record_id}").json()["status"]
                    for record_id in (first_record["id"], second.json()["id"])
                ])
            ) and all(state in {"SUCCEEDED", "FAILED"} for state in states)
            else None
        ),
        timeout=25,
    )
    assert finished == ["SUCCEEDED", "SUCCEEDED"]
    assert client.get(f"/api/v1/exports/{first_record['id']}/file").status_code == 200
    assert client.get(f"/api/v1/exports/{second.json()['id']}/file").status_code == 200


def test_existing_export_table_migrates_without_losing_files(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "legacy.db").as_posix())
    Base.metadata.create_all(engine)
    metadata = MetaData()
    Table("presentations", metadata, Column("id", String(36), primary_key=True))
    legacy_columns = [
        column._copy()
        for column in PptxExport.__table__.columns
        if column.name != "work_id"
    ]
    legacy = Table("pptx_exports_legacy", metadata, *legacy_columns)
    with engine.begin() as connection:
        connection.execute(CreateTable(legacy))
        values = {}
        for column in legacy.columns:
            if column.name == "id":
                values[column.name] = "legacy-export"
            elif column.name == "presentation_id":
                values[column.name] = "legacy-presentation"
            elif column.name in {"request_hash", "source_hash"}:
                values[column.name] = "a" * 64
            elif column.name == "input_manifest_json":
                values[column.name] = '{"pages": []}'
            elif column.name == "status":
                values[column.name] = "SUCCEEDED"
            elif column.name == "attempt":
                values[column.name] = 1
            elif column.name == "download_filename":
                values[column.name] = "existing.pptx"
            elif column.name == "report_json":
                values[column.name] = "{}"
            elif column.type.python_type is str:
                values[column.name] = "legacy"
            elif column.type.python_type is int:
                values[column.name] = 1
            elif column.type.python_type.__name__ == "datetime":
                values[column.name] = utcnow()
        connection.execute(insert(legacy).values(**values))
        connection.exec_driver_sql("DROP TABLE pptx_exports")
        connection.exec_driver_sql("ALTER TABLE pptx_exports_legacy RENAME TO pptx_exports")

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    database._upgrade_existing_schema()

    columns = {column["name"]: column for column in inspect(engine).get_columns("pptx_exports")}
    assert columns["presentation_id"]["nullable"] is True
    assert columns["work_id"]["nullable"] is True
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT presentation_id, output_relative_path, download_filename FROM pptx_exports WHERE id='legacy-export'"
        ).one()
    assert row == ("legacy-presentation", "legacy", "existing.pptx")
    engine.dispose()
