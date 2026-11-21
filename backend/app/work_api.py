from __future__ import annotations

import asyncio

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import export_service, runtime, works_service
from .db import PptxExport, Work, WorkPage, dumps, loads, session_scope
from .export_service import ExportError
from .scene_service import WAKE, digest

router = APIRouter(prefix="/api/v1", tags=["works"])


class CreateWorkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    batch_id: str | None = None
    retry: bool = False


class RenameWorkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    base_revision: int = Field(ge=1)


class WorkExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    retry: bool = False


class SaveWorkPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision: int = Field(ge=1)
    scene: dict
    editor: dict = Field(default_factory=lambda: {"version": 1, "text": {}})


def raise_work_error(error: ExportError):
    raise HTTPException(
        404 if error.code == "NOT_FOUND" else 409,
        detail={"code": error.code, "message": str(error), "page_number": error.page},
    ) from error


@router.post("/presentations/{presentation_id}/works")
async def create_work(
    presentation_id: str,
    body: CreateWorkRequest,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=200),
):
    try:
        async with runtime.EVENT_WRITE_LOCK:
            with session_scope() as db:
                prepared = works_service.prepare_tx(
                    db,
                    presentation_id,
                    body.expected_source_hash,
                    idempotency_key,
                    batch_id=body.batch_id,
                    retry=body.retry,
                )
        if prepared["status"] == "SUCCEEDED":
            response.status_code = 200 if prepared.get("replayed") else 201
            return prepared
        if prepared["status"] != "READY_TO_COMMIT":
            response.status_code = 202
            WAKE.set()
            return prepared

        await asyncio.to_thread(works_service.install_objects, prepared)
        async with runtime.EVENT_WRITE_LOCK:
            with session_scope() as db:
                result = works_service.commit_tx(db, prepared, idempotency_key)
    except ExportError as exc:
        raise_work_error(exc)
    response.status_code = 201
    return result


@router.get("/works")
def list_works():
    with session_scope() as db:
        works = db.scalars(select(Work).order_by(Work.updated_at.desc())).all()
        return [
            works_service.work_summary(
                work,
                db.scalars(
                    select(WorkPage)
                    .where(WorkPage.work_id == work.id)
                    .order_by(WorkPage.position)
                ).all(),
            )
            for work in works
        ]


@router.get("/works/{work_id}")
def get_work(work_id: str):
    with session_scope() as db:
        try:
            work, pages = works_service.get_work(db, work_id)
        except ExportError as exc:
            raise_work_error(exc)
        latest = db.scalar(
            select(PptxExport)
            .where(PptxExport.work_id == work_id)
            .order_by(PptxExport.created_at.desc(), PptxExport.id.desc())
            .limit(1)
        )
        return {
            **works_service.work_summary(work, pages),
            "latest_export": (
                export_service.export_dict(latest)
                if latest and loads(latest.input_manifest_json).get("work_layout_version")
                == export_service.WORK_LAYOUT_VERSION
                else None
            ),
        }


@router.patch("/works/{work_id}")
def rename_work(work_id: str, body: RenameWorkRequest):
    with session_scope() as db:
        try:
            return works_service.rename_tx(db, work_id, body.name, body.base_revision)
        except ExportError as exc:
            raise_work_error(exc)


@router.get("/works/{work_id}/pages/{page_id}")
def get_work_page(work_id: str, page_id: str, request: Request):
    with session_scope() as db:
        page = db.scalar(
            select(WorkPage).where(WorkPage.id == page_id, WorkPage.work_id == work_id)
        )
        if not page:
            raise HTTPException(404, detail={"code": "PAGE_NOT_FOUND", "message": "页面不存在"})
        try:
            scene, _, scene_hash, editor = works_service.work_page_snapshot(page)
        except ExportError as exc:
            raise_work_error(exc)
        etag = '"' + digest(scene_hash + page.editor_json) + '"'
        headers = {
            "ETag": etag,
            "Cache-Control": "private, no-cache",
            "X-Content-Type-Options": "nosniff",
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(
            content=dumps(
                {
                    "id": page.id,
                    "position": page.position,
                    "title": page.title,
                    "scene_hash": scene_hash,
                    "scene": scene,
                    "editor": editor,
                }
            ),
            media_type="application/json",
            headers=headers,
        )


@router.put("/works/{work_id}/pages/{page_id}")
def save_work_page(work_id: str, page_id: str, body: SaveWorkPageRequest):
    with session_scope() as db:
        try:
            return works_service.save_page_tx(
                db, work_id, page_id, body.scene, body.editor, body.base_revision
            )
        except ExportError as exc:
            raise_work_error(exc)


@router.get("/works/{work_id}/pages/{page_id}/assets/{asset_id}")
def get_work_asset(work_id: str, page_id: str, asset_id: str):
    with session_scope() as db:
        page = db.scalar(
            select(WorkPage).where(WorkPage.id == page_id, WorkPage.work_id == work_id)
        )
        if not page:
            raise HTTPException(404, detail="Page not found")
        try:
            scene = works_service.stored_scene(page.scene_json, page.scene_hash)
        except ExportError as exc:
            raise_work_error(exc)
        asset = next((item for item in scene["assets"] if item["id"] == asset_id), None)
        if not asset:
            raise HTTPException(404, detail="Asset not declared")
        root = works_service.OBJECT_ROOT.resolve()
        source_path = works_service.object_path(asset)
        path = source_path.resolve()
        if (
            source_path.is_symlink()
            or not path.is_relative_to(root)
            or not path.is_file()
            or path.stat().st_size != asset["byteLength"]
            or digest(path.read_bytes()) != asset["sha256"]
        ):
            raise HTTPException(404, detail="Asset unavailable")
        return FileResponse(
            path,
            media_type=asset["mediaType"],
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "ETag": '"' + asset["sha256"] + '"',
                "X-Content-Type-Options": "nosniff",
            },
        )


@router.post("/works/{work_id}/exports", status_code=202)
async def create_work_export(work_id: str, body: WorkExportRequest, response: Response):
    async with runtime.EVENT_WRITE_LOCK:
        with session_scope() as db:
            try:
                record = export_service.create_work_tx(
                    db, work_id, body.expected_revision, body.retry
                )
                result = export_service.export_dict(record)
            except ExportError as exc:
                raise_work_error(exc)
    response.status_code = 200 if result["status"] in {"SUCCEEDED", "FAILED"} else 202
    WAKE.set()
    return result
