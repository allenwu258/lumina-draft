from __future__ import annotations

from typing import Literal
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from . import export_service as service, runtime
from .db import PptxExport, session_scope
from .scene_service import WAKE, digest

router = APIRouter(prefix="/api/v1", tags=["exports"])


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["pptx"] = "pptx"
    expected_source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    retry: bool = False


@router.post("/presentations/{presentation_id}/exports", status_code=202)
async def create_export(presentation_id: str, body: ExportRequest, response: Response):
    async with runtime.EVENT_WRITE_LOCK:
        with session_scope() as db:
            try:
                record = service.create_tx(
                    db, presentation_id, body.expected_source_hash, body.retry
                )
                result = service.export_dict(record)
            except service.ExportError as exc:
                raise HTTPException(
                    404 if exc.code == "NOT_FOUND" else 409,
                    detail={
                        "code": exc.code,
                        "message": str(exc),
                        "page_number": exc.page,
                    },
                ) from exc
    response.status_code = 200 if result["status"] in {"SUCCEEDED", "FAILED"} else 202
    WAKE.set()
    await runtime.EVENT_BUS.notify(presentation_id)
    return result


@router.get("/exports/{export_id}")
def get_export(export_id: str):
    with session_scope() as db:
        record = db.get(PptxExport, export_id)
        if not record:
            raise HTTPException(
                404, detail={"code": "NOT_FOUND", "message": "导出任务不存在"}
            )
        return service.export_dict(record)


@router.get("/exports/{export_id}/file")
def download_export(export_id: str):
    with session_scope() as db:
        record = db.get(PptxExport, export_id)
        if not record:
            raise HTTPException(
                404, detail={"code": "NOT_FOUND", "message": "导出任务不存在"}
            )
        if record.status != "SUCCEEDED" or not record.output_relative_path:
            raise HTTPException(
                409, detail={"code": "EXPORT_NOT_READY", "message": "导出文件尚未就绪"}
            )
        path = (service.EXPORT_ROOT / record.output_relative_path).resolve()
        if (
            not path.is_relative_to(service.EXPORT_ROOT)
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != record.byte_length
            or digest(path.read_bytes()) != record.file_sha256
        ):
            raise HTTPException(
                410,
                detail={
                    "code": "EXPORT_FILE_UNAVAILABLE",
                    "message": "导出文件丢失或校验失败",
                },
            )
        return FileResponse(
            path,
            media_type=service.MIME,
            filename=record.download_filename,
            headers={
                "ETag": '"' + record.file_sha256 + '"',
                "Cache-Control": "private, no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
