"""A small persistent export workflow. SceneIR is the only page input model."""

from __future__ import annotations

import json
import os
import posixpath
from pathlib import Path
import re
import shutil
import zipfile
from xml.etree import ElementTree as ET

from sqlalchemy import select

from .db import (
    Outline,
    PageScene,
    PageTask,
    PptxExport,
    Presentation,
    SceneConversionJob,
    loads,
    utcnow,
)
from .scene_service import (
    ROOT,
    SCENE_ROOT,
    canonical,
    digest,
    ensure_page_scene,
    scene_ref,
    source_hash,
    validate_scene,
)

EXPORT_VERSION = "scene-pptx-0.1.0"
EXPORT_ROOT = Path(os.getenv("PPTX_EXPORT_DIR", ROOT / "data" / "exports")).resolve()
MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class ExportError(ValueError):
    def __init__(self, code, message, page=None):
        self.code, self.page = code, page
        super().__init__(message)


def source_manifest(db, presentation_id: str) -> dict:
    presentation = db.get(Presentation, presentation_id)
    if not presentation:
        raise ExportError("NOT_FOUND", "作品不存在")
    outline = db.scalar(
        select(Outline)
        .where(Outline.presentation_id == presentation_id)
        .order_by(Outline.revision.desc())
        .limit(1)
    )
    if not outline or not outline.confirmed:
        raise ExportError("OUTLINE_NOT_CONFIRMED", "请先确认大纲并完成全部页面")
    document = loads(outline.document_json)
    slides = document.get("slides", [])
    pages = db.scalars(
        select(PageTask)
        .where(PageTask.presentation_id == presentation_id)
        .order_by(PageTask.page_number, PageTask.id)
    ).all()
    if not 1 <= len(slides) <= 20 or len(pages) != len(slides):
        raise ExportError("INCOMPLETE_PAGES", "页面数量与已确认大纲不一致")
    ordered = []
    for i, (page, brief) in enumerate(zip(pages, slides), 1):
        if (
            page.page_number != i
            or brief["page_number"] != i
            or page.slide_id != brief["slide_id"]
        ):
            raise ExportError("PAGE_ORDER_MISMATCH", f"第 {i} 页与大纲不一致", i)
        if page.status != "SUCCEEDED":
            raise ExportError("PAGE_NOT_READY", f"第 {i} 页内容尚未完成", i)
        value = source_hash(db, page)
        if not value:
            raise ExportError("PAGE_SOURCE_MISSING", f"第 {i} 页缺少来源", i)
        ordered.append(
            {
                "page_task_id": page.id,
                "slide_id": page.slide_id,
                "page_number": i,
                "source_html_hash": value,
            }
        )
    return {
        "outline_id": outline.id,
        "outline_hash": outline.content_hash,
        "title": document["brief"]["title"],
        "pages": ordered,
    }


def source_summary(db, presentation_id: str) -> dict:
    try:
        manifest = source_manifest(db, presentation_id)
        return {
            "source_hash": digest(canonical(manifest)),
            "can_export": True,
            "reason": None,
        }
    except ExportError as exc:
        return {"source_hash": None, "can_export": False, "reason": str(exc)}


def export_dict(record: PptxExport) -> dict:
    manifest = loads(record.input_manifest_json)
    report = loads(record.report_json)
    return {
        "id": record.id,
        "presentation_id": record.presentation_id,
        "source_hash": record.source_hash,
        "status": record.status,
        "ready_pages": sum(bool(p.get("scene")) for p in manifest["pages"]),
        "total_pages": len(manifest["pages"]),
        "error_code": record.error_code,
        "error": record.error,
        "failed_page": record.failed_page,
        "warnings": report.get("warnings", []),
        "download_url": (
            f"/api/v1/exports/{record.id}/file"
            if record.status == "SUCCEEDED"
            else None
        ),
        "filename": record.download_filename,
        "byte_length": record.byte_length,
        "file_sha256": record.file_sha256,
    }


def event_tx(db, record, state=None):
    from .runtime import append_event_tx

    append_event_tx(
        db,
        record.presentation_id,
        "export." + (state or record.status.lower()),
        export_dict(record),
    )


def fail_tx(db, record, exc):
    record.status, record.attempt_token, record.finished_at = "FAILED", None, utcnow()
    record.error_code = getattr(exc, "code", "EXPORT_FAILED")
    record.error, record.failed_page = str(exc)[:1600], getattr(exc, "page", None)
    event_tx(db, record, "failed")


def create_tx(db, presentation_id, expected_hash, retry=False):
    manifest = source_manifest(db, presentation_id)
    source = digest(canonical(manifest))
    if source != expected_hash:
        raise ExportError("SOURCE_CHANGED", "作品已更新，请刷新后重新导出")
    request_hash = digest(canonical([presentation_id, source, EXPORT_VERSION]))
    record = db.scalar(
        select(PptxExport).where(
            PptxExport.presentation_id == presentation_id,
            PptxExport.request_hash == request_hash,
        )
    )
    if record and (record.status != "FAILED" or not retry):
        return record
    if not record:
        title = (
            re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", manifest["title"]).strip(" .")[:100]
            or "演示文稿"
        )
        record = PptxExport(
            presentation_id=presentation_id,
            request_hash=request_hash,
            source_hash=source,
            export_version=EXPORT_VERSION,
            input_manifest_json=canonical(manifest),
            download_filename=title + ".pptx",
            status="PREPARING",
        )
        db.add(record)
        db.flush()
    elif record.input_hash:
        # A frozen scene manifest survives writer retries and later edits.
        record.status, record.attempt, record.attempt_token = "QUEUED", 0, None
        record.error_code = record.error = record.failed_page = record.finished_at = (
            None
        )
        record.queued_at = utcnow()
        event_tx(db, record, "queued")
        return record
    record.status, record.attempt, record.attempt_token = "PREPARING", 0, None
    record.error_code = record.error = record.failed_page = record.finished_at = None
    try:
        for item in manifest["pages"]:
            page = db.get(PageTask, item["page_task_id"])
            try:
                result = ensure_page_scene(
                    db, page, item["source_html_hash"], retry=retry
                )
            except ValueError as exc:
                raise ExportError(
                    str(exc),
                    f"第 {item['page_number']} 页场景准备失败：{exc}",
                    item["page_number"],
                ) from exc
            item["conversion_job_id"], item["scene"] = result["job_id"], result["ref"]
        record.input_manifest_json = canonical(manifest)
        advance_tx(db, record)
        if record.status == "PREPARING":
            event_tx(db, record, "preparing")
    except ValueError as exc:
        fail_tx(db, record, exc)
    return record


def advance_tx(db, record):
    """Resolve pinned dependencies without occupying the browser execution slot."""
    if record.status != "PREPARING":
        return
    manifest = loads(record.input_manifest_json)
    previous = sum(bool(p.get("scene")) for p in manifest["pages"])
    try:
        for item in manifest["pages"]:
            if item.get("scene"):
                continue
            page = db.get(PageTask, item["page_task_id"])
            if (
                not page
                or page.status != "SUCCEEDED"
                or source_hash(db, page) != item["source_html_hash"]
                or page.current_scene_job_id != item["conversion_job_id"]
            ):
                raise ExportError(
                    "SOURCE_CHANGED",
                    f"第 {item['page_number']} 页在准备期间已更新",
                    item["page_number"],
                )
            job = db.get(SceneConversionJob, item["conversion_job_id"])
            if not job or job.status in {"FAILED", "CANCELLED"}:
                raise ExportError(
                    "SCENE_PREPARATION_FAILED",
                    f"第 {item['page_number']} 页场景转换失败：{job.error if job else '任务不存在'}",
                    item["page_number"],
                )
            if job.status == "SUCCEEDED":
                scene = db.scalar(select(PageScene).where(PageScene.job_id == job.id))
                if not scene:
                    raise ExportError(
                        "SCENE_MISSING", "场景产物缺失", item["page_number"]
                    )
                item["scene"] = scene_ref(scene, item["source_html_hash"])
        record.input_manifest_json = canonical(manifest)
        ready = sum(bool(p.get("scene")) for p in manifest["pages"])
        if ready == len(manifest["pages"]):
            record.input_hash = digest(
                canonical([manifest, record.export_version, "installed-fonts-v1"])
            )
            record.status, record.queued_at = "QUEUED", utcnow()
            event_tx(db, record, "queued")
        elif ready != previous:
            event_tx(db, record, "preparing")
    except ValueError as exc:
        fail_tx(db, record, exc)


def stage_input(record, work: Path) -> dict:
    """Verify frozen Scenes/assets and copy a bounded private input for Node."""
    from .db import session_scope

    manifest = loads(record.input_manifest_json)
    pages = []
    with session_scope() as db:
        for item in manifest["pages"]:
            try:
                ref = item["scene"]
                scene_row = db.get(PageScene, ref["scene_id"])
                if (
                    not scene_row
                    or scene_row.scene_hash != ref["scene_hash"]
                    or digest(scene_row.scene_json) != ref["scene_hash"]
                    or scene_row.page_task_id != item["page_task_id"]
                ):
                    raise ValueError("SCENE_IDENTITY")
                job = db.get(SceneConversionJob, scene_row.job_id)
                scene = json.loads(scene_row.scene_json)
                folder = (SCENE_ROOT / scene_row.asset_dir).resolve()
                if (
                    not folder.is_relative_to(SCENE_ROOT.resolve())
                    or folder.is_symlink()
                    or job.html_hash != item["source_html_hash"]
                ):
                    raise ValueError("SCENE_SOURCE")
                validate_scene(scene, job, folder)
                target = work / "assets" / scene_row.id
                target.mkdir(parents=True, exist_ok=True)
                assets = {}
                for asset in scene["assets"]:
                    destination = target / asset["file"]
                    shutil.copyfile(folder / asset["file"], destination)
                    assets[asset["id"]] = destination.relative_to(SCENE_ROOT).as_posix()
                pages.append(
                    {"scene": scene, "assets": assets, "sceneId": scene_row.id}
                )
            except Exception as exc:
                raise ExportError(
                    "SCENE_INPUT_INVALID",
                    f"第 {item['page_number']} 页场景或资产不可用：{exc}",
                    item["page_number"],
                ) from exc
    return {"title": manifest["title"], "inputHash": record.input_hash, "pages": pages}


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}


def verify_pptx(path: Path, report: dict, staged: dict):
    """Independent package/content checks before any successful publication."""
    if (
        path.stat().st_size > 128 * 1024 * 1024
        or report.get("inputHash") != staged["inputHash"]
    ):
        raise ExportError("PPTX_VALIDATION", "导出文件大小或输入身份不符")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if len(names) != len(archive.infolist()):
            raise ExportError("PPTX_VALIDATION", "PPTX 压缩结构无效")
        if sum(i.file_size for i in archive.infolist()) > 256 * 1024 * 1024:
            raise ExportError("PPTX_VALIDATION", "PPTX 解压大小超限")
        if archive.testzip():
            raise ExportError("PPTX_VALIDATION", "PPTX 压缩内容损坏")
        ET.fromstring(archive.read("[Content_Types].xml"))
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        if len(presentation.findall("p:sldIdLst/p:sldId", NS)) != len(
            staged["pages"]
        ) or len(report["pages"]) != len(staged["pages"]):
            raise ExportError("PPTX_VALIDATION", "导出页数不完整")
        relationships = {
            r.get("Id"): r.get("Target")
            for r in ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
        }
        rel_key = (
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        targets = [
            relationships.get(s.get(rel_key))
            for s in presentation.findall("p:sldIdLst/p:sldId", NS)
        ]
        if targets != [
            f"slides/slide{i}.xml" for i in range(1, len(staged["pages"]) + 1)
        ]:
            raise ExportError("PPTX_VALIDATION", "PPTX 页面关系顺序错误")
        size = presentation.find("p:sldSz", NS)
        if (
            abs(int(size.attrib["cx"]) - 12192000) > 1
            or int(size.attrib["cy"]) != 6858000
        ):
            raise ExportError("PPTX_VALIDATION", "PPTX 页面尺寸错误")
        for i, (page, page_report) in enumerate(
            zip(staged["pages"], report["pages"]), 1
        ):
            xml = ET.fromstring(archive.read(f"ppt/slides/slide{i}.xml"))
            text = [t.text or "" for t in xml.findall(".//a:t", NS)]
            if (
                text != page_report["nativeTexts"]
                or page_report["sceneId"] != page["sceneId"]
            ):
                raise ExportError("PPTX_VALIDATION", "导出文字或页面顺序错误", i)
            drawn = [
                op["nodeId"] for op in page["scene"]["paintOps"] if op["op"] == "draw"
            ]
            raster_ids = page_report.get("rasterNodeIds", [])
            invisible_ids = page_report.get("invisibleNodeIds", [])
            if (
                len(raster_ids) != len(set(raster_ids))
                or set(raster_ids) & set(invisible_ids)
                or not set(raster_ids + invisible_ids).issubset(drawn)
            ):
                raise ExportError("PPTX_VALIDATION", "局部图片覆盖范围错误", i)
            nodes = {n["id"]: n for n in page["scene"]["nodes"]}
            expected_text = [
                f["displayText"]
                for node_id in drawn
                if nodes[node_id]["kind"] == "text"
                and node_id not in set(raster_ids + invisible_ids)
                for line in nodes[node_id]["text"]["lines"]
                for f in line["fragments"]
                if f["displayText"]
            ]
            if expected_text != text:
                raise ExportError("PPTX_VALIDATION", "Scene 显示文字未完整写入", i)
            page_rels_path = f"ppt/slides/_rels/slide{i}.xml.rels"
            page_rels = (
                {
                    r.get("Id"): r.get("Target")
                    for r in ET.fromstring(archive.read(page_rels_path))
                }
                if page_rels_path in names
                else {}
            )
            images = {}
            raster_count = 0
            for picture in xml.findall(".//p:pic", NS):
                name = picture.find("p:nvPicPr/p:cNvPr", NS).get("name", "")
                if name.startswith("raster:"):
                    raster_count += 1
                elif name.startswith("image:"):
                    node_id = name.removeprefix("image:")
                    if node_id in images:
                        raise ExportError("PPTX_VALIDATION", "图片重复输出", i)
                    rel_id = picture.find("p:blipFill/a:blip", NS).get(
                        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
                    )
                    target = posixpath.normpath(
                        posixpath.join("ppt/slides", page_rels.get(rel_id, ""))
                    )
                    images[node_id] = digest(archive.read(target))
            asset_hashes = {a["id"]: a["sha256"] for a in page["scene"]["assets"]}
            expected_images = {
                node_id: asset_hashes[nodes[node_id]["assetId"]]
                for node_id in drawn
                if nodes[node_id]["kind"] == "image"
                and node_id not in set(raster_ids + invisible_ids)
            }
            if (
                images != expected_images
                or raster_count != page_report["rasterRegions"]
            ):
                raise ExportError("PPTX_VALIDATION", "图片来源或局部合成数量不符", i)
            if sorted(drawn) != sorted(page_report["coveredNodeIds"]) or len(
                drawn
            ) != len(set(page_report["coveredNodeIds"])):
                raise ExportError("PPTX_VALIDATION", "存在遗漏或重复绘制项", i)
            for body in xml.findall(".//a:bodyPr", NS):
                if (
                    body.get("wrap") != "none"
                    or body.find("a:normAutofit", NS) is not None
                    or body.find("a:spAutoFit", NS) is not None
                ):
                    raise ExportError("PPTX_VALIDATION", "文本框重新排版配置错误", i)
            # Root spTree's group transform legally has a zero extent.
            for extent in xml.findall(".//p:sp/p:spPr/a:xfrm/a:ext", NS) + xml.findall(
                ".//p:pic/p:spPr/a:xfrm/a:ext", NS
            ):
                if int(extent.get("cx", "0")) <= 0 or int(extent.get("cy", "0")) <= 0:
                    raise ExportError("PPTX_VALIDATION", "无效对象尺寸", i)
        for name in names:
            if not name.endswith(".rels"):
                continue
            base = posixpath.dirname(posixpath.dirname(name))
            for rel in ET.fromstring(archive.read(name)):
                target = rel.get("Target", "")
                if (
                    rel.get("TargetMode") == "External"
                    or posixpath.normpath(posixpath.join(base, target)).lstrip("/")
                    not in names
                ):
                    raise ExportError("PPTX_VALIDATION", "外部或缺失资源引用")


def install_result(record, token, output: Path, staged: dict):
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    path = output / "presentation.pptx"
    verify_pptx(path, report, staged)
    target = EXPORT_ROOT / record.id / token
    target.parent.mkdir(parents=True, exist_ok=True)
    # Install on the destination volume; the two configured data roots may differ.
    temporary = target.parent / ("." + token + ".tmp")
    temporary.mkdir()
    try:
        shutil.copyfile(path, temporary / "presentation.pptx")
        os.replace(temporary, target)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    raw = (target / "presentation.pptx").read_bytes()
    return {
        "path": (target / "presentation.pptx").relative_to(EXPORT_ROOT).as_posix(),
        "sha256": digest(raw),
        "size": len(raw),
        "report": {
            "warnings": report.get("warnings", []),
            "pages": [
                {
                    "scene_id": p["sceneId"],
                    "native_text_count": len(p["nativeTexts"]),
                    "raster_regions": p["rasterRegions"],
                    "fonts": p.get("fonts", []),
                }
                for p in report["pages"]
            ],
        },
    }


def publish_tx(db, export_id, token, input_hash, result):
    record = db.get(PptxExport, export_id)
    if (
        not record
        or record.status != "RUNNING"
        or record.attempt_token != token
        or record.input_hash != input_hash
    ):
        return False
    record.status, record.finished_at = "SUCCEEDED", utcnow()
    record.output_relative_path, record.file_sha256, record.byte_length = (
        result["path"],
        result["sha256"],
        result["size"],
    )
    record.report_json = canonical(result["report"])
    record.error = record.error_code = None
    event_tx(db, record, "completed")
    return True
