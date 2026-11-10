from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .db import PageTask, PageScene, loads, session_scope
from . import runtime
from .scene_service import SCENE_ROOT, WAKE, enqueue_tx, summary

router = APIRouter(prefix="/api/v1", tags=["scenes"])


class ConvertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_html_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    retry: bool = False


@router.post("/page-tasks/{task_id}/scene-conversions", status_code=202)
async def ensure_scene(task_id: str, body: ConvertRequest):
    async with runtime.EVENT_WRITE_LOCK:
        with session_scope() as db:
            page = db.get(PageTask, task_id)
            if not page:
                raise HTTPException(
                    404, detail={"code": "NOT_FOUND", "message": "页面不存在"}
                )
            if page.status != "SUCCEEDED" or not page.html:
                raise HTTPException(
                    409, detail={"code": "PAGE_NOT_READY", "message": "HTML 尚未完成"}
                )
            if body.expected_html_hash != page.html_hash:
                raise HTTPException(
                    409,
                    detail={
                        "code": "HTML_REVISION_CONFLICT",
                        "message": "页面已更新，请刷新后重试",
                    },
                )
            enqueue_tx(db, page, retry=body.retry)
            result = summary(db, page)
            presentation_id = page.presentation_id
    WAKE.set()
    await runtime.EVENT_BUS.notify(presentation_id)
    return result


@router.get("/scenes/{scene_id}")
def get_scene(scene_id: str, request: Request):
    with session_scope() as db:
        scene = db.get(PageScene, scene_id)
        if not scene:
            raise HTTPException(
                404, detail={"code": "SCENE_NOT_FOUND", "message": "场景不存在"}
            )
        etag = '"' + scene.scene_hash + '"'
        headers = {
            "ETag": etag,
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(
            content=scene.scene_json, media_type="application/json", headers=headers
        )


@router.get("/scenes/{scene_id}/assets/{asset_id}")
def get_asset(scene_id: str, asset_id: str):
    with session_scope() as db:
        scene = db.get(PageScene, scene_id)
        if not scene:
            raise HTTPException(404, detail="Scene not found")
        asset = next(
            (a for a in loads(scene.scene_json)["assets"] if a["id"] == asset_id), None
        )
        if not asset:
            raise HTTPException(404, detail="Asset not declared")
        folder = (SCENE_ROOT / scene.asset_dir).resolve()
        path = (folder / asset["file"]).resolve()
        if (
            not folder.is_relative_to(SCENE_ROOT)
            or path.parent != folder
            or not path.is_file()
            or path.is_symlink()
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
