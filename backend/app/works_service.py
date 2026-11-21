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
from .scene_service import SCENE_ROOT, VALIDATORS, canonical, digest, ensure_page_scene, validate_scene

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
                editor_json='{"version":1,"text":{}}',
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


def _editable_scene_checks(current: dict, candidate: dict) -> None:
    if not isinstance(candidate, dict) or candidate.get("schemaVersion") != current.get("schemaVersion"):
        raise ExportError("SCENE_EDIT_INVALID", "页面场景版本不支持编辑")
    if candidate.get("page") != current.get("page"):
        raise ExportError("SCENE_EDIT_INVALID", "页面尺寸不能在编辑器中修改")
    if candidate.get("source") != current.get("source"):
        raise ExportError("SCENE_EDIT_INVALID", "页面来源信息不能在编辑器中修改")
    if candidate.get("paintOps") != current.get("paintOps"):
        raise ExportError("SCENE_EDIT_INVALID", "页面绘制结构不能在初版编辑器中修改")
    validator = VALIDATORS.get(candidate.get("schemaVersion"))
    schema_error = next(validator.iter_errors(candidate), None) if validator else None
    if validator is None or schema_error:
        raise ExportError("SCENE_EDIT_INVALID", "页面场景不符合 SceneIR 结构")
    current_assets = {asset["id"]: asset for asset in current.get("assets", [])}
    candidate_assets = {asset["id"]: asset for asset in candidate.get("assets", [])}
    if current_assets != candidate_assets:
        raise ExportError("SCENE_EDIT_INVALID", "页面资源不能在初版编辑器中替换")
    current_nodes = {node["id"]: node for node in current.get("nodes", [])}
    candidate_nodes = {node["id"]: node for node in candidate.get("nodes", [])}
    if set(current_nodes) != set(candidate_nodes):
        raise ExportError("SCENE_EDIT_INVALID", "编辑器不能新增或删除页面元素")
    for node_id, node in current_nodes.items():
        candidate_node = candidate_nodes[node_id]
        if node["kind"] != candidate_node.get("kind") or node.get("sourcePath") != candidate_node.get("sourcePath"):
            raise ExportError("SCENE_EDIT_INVALID", "页面元素身份不能在编辑器中修改")


def save_page_tx(db, work_id: str, page_id: str, scene: dict, editor: dict, base_revision: int) -> dict:
    work = db.get(Work, work_id)
    page = db.scalar(select(WorkPage).where(WorkPage.id == page_id, WorkPage.work_id == work_id))
    if not work or not page:
        raise ExportError("NOT_FOUND", "作品页面不存在")
    if work.revision != base_revision:
        raise ExportError("REVISION_CONFLICT", "作品已在其他标签页更新，请刷新后处理")
    current = stored_scene(page.scene_json, page.scene_hash)
    _editable_scene_checks(current, scene)
    if not isinstance(editor, dict) or editor.get("version") != 1 or not isinstance(editor.get("text", {}), dict):
        raise ExportError("EDITOR_STATE_INVALID", "编辑器状态格式无效")
    scene_json = canonical(scene)
    page.scene_json = scene_json
    page.scene_hash = digest(scene_json)
    page.editor_json = canonical(editor)
    work.revision += 1
    work.updated_at = utcnow()
    db.flush()
    return {
        "work_revision": work.revision,
        "page_id": page.id,
        "scene_hash": page.scene_hash,
        "editor": loads(page.editor_json),
        "saved_at": work.updated_at.isoformat(),
    }


def stored_scene(scene_json: str, scene_hash: str) -> dict:
    if digest(scene_json) != scene_hash:
        raise ExportError("SCENE_CORRUPT", "作品页面场景校验失败")
    return json.loads(scene_json)


def work_page_snapshot(page: WorkPage) -> tuple[dict, str, str, dict]:
    scene = stored_scene(page.scene_json, page.scene_hash)
    editor = loads(page.editor_json)
    changed = False
    for node in scene["nodes"]:
        if node["kind"] != "text":
            continue
        text = node["text"]
        run_ids = {
            run["id"] for paragraph in text["paragraphs"] for run in paragraph["runs"]
        }
        if run_ids != {node["id"] + "_editor_run"}:
            continue
        moved = False
        for line in text["lines"]:
            fragments = line["fragments"]
            if not fragments:
                continue
            first = fragments[0]["frame"]
            offset = line["frame"]["x"] - first["x"]
            if abs(offset) < 0.01:
                continue
            for fragment in fragments:
                fragment["frame"]["x"] += offset
            moved = True
        if moved:
            frames = [
                line["frame"] for line in text["lines"] if line["fragments"]
            ]
            left = min(frame["x"] for frame in frames)
            top = min(frame["y"] for frame in frames)
            node["frame"] = {
                "x": left,
                "y": top,
                "width": max(frame["x"] + frame["width"] for frame in frames) - left,
                "height": max(frame["y"] + frame["height"] for frame in frames) - top,
            }
            changed = True
    if not changed:
        return scene, page.scene_json, page.scene_hash, editor
    scene_json = canonical(scene)
    return scene, scene_json, digest(scene_json), editor
