from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import uuid
from types import SimpleNamespace

from sqlalchemy import select, update

from .db import IdempotencyRecord, PageScene, PageTask, SceneConversionJob, Work, WorkPage, dumps, loads, utcnow
from .export_service import ExportError, manifest_source_hash, source_manifest
from .scene_service import SCENE_ROOT, canonical, digest, ensure_page_scene, validate_scene

ROOT = Path(__file__).resolve().parents[2]
OBJECT_ROOT = Path(os.getenv("WORK_OBJECTS_DIR", ROOT / "data" / "objects")).resolve()
CREATE_SCOPE = "works.create"


def object_path(asset: dict) -> Path:
    extension = "svg" if asset["mediaType"] == "image/svg+xml" else "png"
    return OBJECT_ROOT / asset["sha256"][:2] / f'{asset["sha256"]}.{extension}'


def install_object(source: Path, asset: dict) -> None:
    target = object_path(asset)
    if target.is_symlink():
        raise ValueError("OBJECT_STORE_CORRUPT")
    if target.is_file():
        if target.stat().st_size == asset["byteLength"] and digest(target.read_bytes()) == asset["sha256"]:
            return
        raise ValueError("OBJECT_STORE_CORRUPT")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        shutil.copyfile(source, temporary)
        if digest(temporary.read_bytes()) != asset["sha256"]:
            raise ValueError("SCENE_ASSET_HASH")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def work_summary(work: Work, pages: list[WorkPage]) -> dict:
    return {
        "id": work.id,
        "name": work.name,
        "type": work.type,
        "revision": work.revision,
        "page_count": len(pages),
        "created_at": work.created_at.isoformat(),
        "updated_at": work.updated_at.isoformat(),
        "pages": [
            {"id": page.id, "position": page.position, "title": page.title}
            for page in pages
        ],
    }


def prepare_tx(
    db,
    presentation_id: str,
    expected_hash: str,
    idempotency_key: str,
    *,
    batch_id: str | None = None,
    retry: bool = False,
) -> dict:
    request_hash = digest(canonical([presentation_id, batch_id, expected_hash]))
    prior = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == CREATE_SCOPE,
            IdempotencyRecord.key == idempotency_key,
        )
    )
    if prior:
        if prior.request_hash != request_hash:
            raise ExportError("IDEMPOTENCY_CONFLICT", "幂等键已用于其他来源")
        response = loads(prior.response_json)
        response["replayed"] = True
        return response
    manifest = source_manifest(db, presentation_id, batch_id)
    source_hash = manifest_source_hash(manifest)
    if source_hash != expected_hash:
        raise ExportError("SOURCE_CHANGED", "生成结果已更新，请刷新后重试")

    selected = []
    states = []
    for item in manifest["pages"]:
        page = db.get(PageTask, item["page_task_id"])
        try:
            result = ensure_page_scene(db, page, item["source_html_hash"], retry=retry)
        except ValueError as exc:
            raise ExportError(
                str(exc), f'第 {item["page_number"]} 页场景无法创建：{exc}', item["page_number"]
            ) from exc
        ready = bool(result["ref"]) and (
            result["up_to_date"] or result["error_code"] == "SOURCE_HTML_MISSING"
        )
        states.append(
            {
                "position": item["page_number"],
                "title": page.title,
                "status": "READY" if ready else result["status"],
                "error": result["error"] if not ready else None,
            }
        )
        if ready and result["ref"]:
            scene = db.get(PageScene, result["scene_id"])
            if (
                not scene
                or scene.scene_hash != result["scene_hash"]
                or digest(scene.scene_json) != scene.scene_hash
            ):
                raise ExportError("SCENE_CORRUPT", f'第 {item["page_number"]} 页场景校验失败', item["page_number"])
            job = db.get(SceneConversionJob, scene.job_id)
            if not job:
                raise ExportError("SCENE_SOURCE_MISSING", f'第 {item["page_number"]} 页缺少场景来源', item["page_number"])
            source_folder = SCENE_ROOT.resolve() / scene.asset_dir
            folder = source_folder.resolve()
            if source_folder.is_symlink() or not folder.is_relative_to(SCENE_ROOT.resolve()):
                raise ExportError("SCENE_ASSET_PATH", f'第 {item["page_number"]} 页场景资源目录无效', item["page_number"])
            selected.append(
                {
                    "position": item["page_number"],
                    "title": page.title[:200],
                    "scene_id": scene.id,
                    "scene_hash": scene.scene_hash,
                    "scene_json": scene.scene_json,
                    "asset_dir": source_folder.as_posix(),
                }
            )

    if len(selected) != len(manifest["pages"]):
        retry_needed = any(page["status"] in {"FAILED", "CANCELLED"} for page in states)
        return {
            "status": "NEEDS_RETRY" if retry_needed else "PREPARING",
            "ready_pages": len(selected),
            "total_pages": len(manifest["pages"]),
            "pages": states,
        }

    return {
        "status": "READY_TO_COMMIT",
        "request_hash": request_hash,
        "presentation_id": presentation_id,
        "batch_id": batch_id,
        "source_hash": source_hash,
        "outline_id": manifest["outline_id"],
        "name": manifest["title"][:200] or "未命名演示文稿",
        "pages": selected,
    }


def install_objects(prepared: dict) -> None:
    for page in prepared["pages"]:
        try:
            scene = json.loads(page["scene_json"])
            source_folder = Path(page["asset_dir"])
            folder = source_folder.resolve()
            source = scene["source"]
            job = SimpleNamespace(
                html_hash=source["htmlHash"],
                css_hash=source["cssHash"],
                converter_version=source["converterVersion"],
                canvas_width=scene["page"]["width"],
                canvas_height=scene["page"]["height"],
            )
            if (
                source_folder.is_symlink()
                or not folder.is_relative_to(SCENE_ROOT.resolve())
                or not folder.is_dir()
            ):
                raise ValueError("SCENE_ASSET_PATH")
            validate_scene(scene, job, folder)
            for asset in scene["assets"]:
                install_object(folder / asset["file"], asset)
        except (OSError, ValueError) as exc:
            raise ExportError(
                "SCENE_INPUT_INVALID", f'第 {page["position"]} 页场景或资源无效：{exc}', page["position"]
            ) from exc


def commit_tx(db, prepared: dict, idempotency_key: str) -> dict:
    prior = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == CREATE_SCOPE,
            IdempotencyRecord.key == idempotency_key,
        )
    )
    if prior:
        if prior.request_hash != prepared["request_hash"]:
            raise ExportError("IDEMPOTENCY_CONFLICT", "幂等键已用于其他来源")
        response = loads(prior.response_json)
        response["replayed"] = True
        return response
    work = Work(
        name=prepared["name"],
        type="presentation",
        revision=1,
        source_json=dumps(
            {
                "presentation_id": prepared["presentation_id"],
                "batch_id": prepared["batch_id"],
                "source_hash": prepared["source_hash"],
                "outline_id": prepared["outline_id"],
            }
        ),
    )
    db.add(work)
    db.flush()
    for page in prepared["pages"]:
        db.add(
            WorkPage(
                work_id=work.id,
                position=page["position"],
                title=page["title"],
                scene_hash=page["scene_hash"],
                scene_json=page["scene_json"],
            )
        )
    db.flush()
    pages = db.scalars(
        select(WorkPage).where(WorkPage.work_id == work.id).order_by(WorkPage.position)
    ).all()
    response = {"status": "SUCCEEDED", "replayed": False, "work": work_summary(work, pages)}
    db.add(
        IdempotencyRecord(
            scope=CREATE_SCOPE,
            key=idempotency_key,
            request_hash=prepared["request_hash"],
            response_json=dumps(response),
        )
    )
    return response


def get_work(db, work_id: str) -> tuple[Work, list[WorkPage]]:
    work = db.get(Work, work_id)
    if not work:
        raise ExportError("NOT_FOUND", "作品不存在")
    pages = db.scalars(
        select(WorkPage).where(WorkPage.work_id == work_id).order_by(WorkPage.position)
    ).all()
    return work, pages


def rename_tx(db, work_id: str, name: str, base_revision: int) -> dict:
    work = db.get(Work, work_id)
    if not work:
        raise ExportError("NOT_FOUND", "作品不存在")
    clean_name = name.strip()
    if not clean_name:
        raise ExportError("INVALID_NAME", "作品名称不能为空")
    result = db.execute(
        update(Work)
        .where(Work.id == work_id, Work.revision == base_revision)
        .values(name=clean_name, revision=base_revision + 1, updated_at=utcnow())
    )
    if not result.rowcount:
        raise ExportError("REVISION_CONFLICT", "作品已在其他标签页更新，请刷新后处理")
    work, pages = get_work(db, work_id)
    return work_summary(work, pages)


def stored_scene(scene_json: str, scene_hash: str) -> dict:
    if digest(scene_json) != scene_hash:
        raise ExportError("SCENE_CORRUPT", "作品页面场景校验失败")
    return json.loads(scene_json)
