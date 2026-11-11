"""A small persistent export workflow. SceneIR is the only page input model."""

from __future__ import annotations

import json
import os
import posixpath
from pathlib import Path
import re
import shutil
import zipfile
import struct
import zlib
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
    SCENE_TARGET,
    SCENE_ROOT,
    canonical,
    digest,
    ensure_page_scene,
    scene_ref,
    source_hash,
    validate_scene,
)

EXPORT_VERSION = "scene-pptx-0.2.0"
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


def request_identity(presentation_id, source):
    return digest(canonical([presentation_id, source, EXPORT_VERSION, SCENE_TARGET]))


def exportable_ref(result):
    # Old display may remain while enhancement fails. Only source-less historical pages opt out.
    return (
        result["ref"]
        if result["up_to_date"] or result["error_code"] == "SOURCE_HTML_MISSING"
        else None
    )


def create_tx(db, presentation_id, expected_hash, retry=False):
    manifest = source_manifest(db, presentation_id)
    source = digest(canonical(manifest))
    if source != expected_hash:
        raise ExportError("SOURCE_CHANGED", "作品已更新，请刷新后重新导出")
    manifest["target"] = SCENE_TARGET
    request_hash = request_identity(presentation_id, source)
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
            item["conversion_job_id"], item["scene"] = result["job_id"], exportable_ref(
                result
            )
            if result["error_code"] == "SOURCE_HTML_MISSING":
                item["compatibility_warning"] = result["error"]
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
    if (
        manifest.get("target") != SCENE_TARGET
        or record.export_version != EXPORT_VERSION
    ):
        fail_tx(
            db,
            record,
            ExportError("EXPORT_VERSION_CHANGED", "导出引擎已升级，请重新发起导出"),
        )
        return
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
    return {
        "title": manifest["title"],
        "inputHash": record.input_hash,
        "pages": pages,
        "warnings": [
            p["compatibility_warning"]
            for p in manifest["pages"]
            if p.get("compatibility_warning")
        ],
    }


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main",
}


def png_dimensions(raw):
    """Validate the compatibility resource itself, not an unrelated stored PNG hash."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) < 45:
        raise ExportError("PPTX_VALIDATION", "PNG 兼容资源无效")
    pos, compressed, dims, ended = 8, bytearray(), None, False
    while pos + 12 <= len(raw):
        size = struct.unpack(">I", raw[pos : pos + 4])[0]
        tag, data = raw[pos + 4 : pos + 8], raw[pos + 8 : pos + 8 + size]
        if (
            pos + size + 12 > len(raw)
            or zlib.crc32(tag + data) & 0xFFFFFFFF
            != struct.unpack(">I", raw[pos + size + 8 : pos + size + 12])[0]
        ):
            raise ExportError("PPTX_VALIDATION", "PNG 校验失败")
        if tag == b"IHDR":
            if size != 13 or dims:
                raise ExportError("PPTX_VALIDATION", "PNG 尺寸信息无效")
            dims = struct.unpack(">II", data[:8])
            if min(dims) < 1 or max(dims) > 8192 or dims[0] * dims[1] > 32_000_000:
                raise ExportError("PPTX_VALIDATION", "PNG 尺寸超限")
        elif tag == b"IDAT":
            compressed.extend(data)
        elif tag == b"IEND":
            ended = True
            break
        pos += size + 12
    if not ended or not dims or not compressed:
        raise ExportError("PPTX_VALIDATION", "PNG 内容不完整")
    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(
            compressed, min(256 * 1024 * 1024, dims[0] * dims[1] * 9 + dims[1] * 8)
        )
        if not decoded or not decoder.eof:
            raise ValueError("incomplete")
    except (ValueError, zlib.error) as exc:
        raise ExportError("PPTX_VALIDATION", "PNG 图像数据无效") from exc
    return dims


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
            partial_regions = [
                r for r in page_report.get("rasterDetails", []) if r.get("fragmentKeys")
            ]
            partial_nodes = {n for r in partial_regions for n in r["nodeIds"]}
            whole_raster = set(raster_ids) - partial_nodes
            raster_fragments = {
                key for r in partial_regions for key in r["fragmentKeys"]
            }
            expected_fragments = {
                f"{node_id}/{li}/{fi}": f["displayText"]
                for node_id in drawn
                if nodes[node_id]["kind"] == "text"
                and node_id not in whole_raster | set(invisible_ids)
                for li, line in enumerate(nodes[node_id]["text"]["lines"])
                for fi, f in enumerate(line["fragments"])
                if f["displayText"]
            }
            if not raster_fragments.issubset(expected_fragments):
                raise ExportError("PPTX_VALIDATION", "Scene 局部文字来源无效", i)
            native_records = page_report.get("nativeFragments", [])
            actual_fragments = {}
            for item in native_records:
                key = item["fragmentKey"]
                if not key.startswith(item["nodeId"] + "/") or key in raster_fragments:
                    raise ExportError("PPTX_VALIDATION", "Scene 文字重复输出", i)
                actual_fragments[key] = actual_fragments.get(key, "") + item["text"]
            if (
                actual_fragments
                != {
                    key: value
                    for key, value in expected_fragments.items()
                    if key not in raster_fragments
                }
                or [r["text"] for r in native_records] != text
            ):
                raise ExportError("PPTX_VALIDATION", "Scene 显示文字未完整写入", i)
            xml_records = []
            for shape in xml.findall(".//p:sp", NS):
                properties = shape.find("p:nvSpPr/p:cNvPr", NS)
                name = properties.get("name", "") if properties is not None else ""
                contents = "".join(t.text or "" for t in shape.findall(".//a:t", NS))
                if contents:
                    xml_records.append((name.removeprefix("text:"), contents))
            if xml_records != [(r["nodeId"], r["text"]) for r in native_records]:
                raise ExportError("PPTX_VALIDATION", "Scene 文字对象顺序错误", i)
            page_rels_path = f"ppt/slides/_rels/slide{i}.xml.rels"
            page_rels = (
                {
                    r.get("Id"): r.get("Target")
                    for r in ET.fromstring(archive.read(page_rels_path))
                }
                if page_rels_path in names
                else {}
            )
            images, vectors = {}, {}
            raster_count = 0
            svg_report = {r["objectName"]: r for r in page_report.get("svgRegions", [])}
            if len(svg_report) != len(page_report.get("svgRegions", [])):
                raise ExportError("PPTX_VALIDATION", "重复矢量区域", i)
            embed_key = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"

            def media(rel_id):
                target = posixpath.normpath(
                    posixpath.join("ppt/slides", page_rels.get(rel_id, ""))
                )
                if not target.startswith("ppt/media/") or target not in names:
                    raise ExportError("PPTX_VALIDATION", "图片资源引用错误", i)
                return archive.read(target)

            for picture in xml.findall(".//p:pic", NS):
                name = picture.find("p:nvPicPr/p:cNvPr", NS).get("name", "")
                blip = picture.find("p:blipFill/a:blip", NS)
                raw = media(blip.get(embed_key))
                dims = png_dimensions(raw)
                svg_blip = blip.find(".//asvg:svgBlip", NS)
                primary_hash = digest(raw)
                if svg_blip is not None:
                    primary = media(svg_blip.get(embed_key))
                    primary_hash = digest(primary)
                    if (
                        name not in svg_report
                        or primary_hash != svg_report[name]["sha256"]
                    ):
                        raise ExportError("PPTX_VALIDATION", "SVG 主资源来源不符", i)
                    root = ET.fromstring(primary)
                    if root.tag != "{http://www.w3.org/2000/svg}svg":
                        raise ExportError("PPTX_VALIDATION", "SVG 主资源无效", i)
                    frame = svg_report[name]["frame"]
                    ratio = frame["width"] / frame["height"]
                    if abs(dims[0] / dims[1] - ratio) > 0.02 * ratio:
                        raise ExportError("PPTX_VALIDATION", "SVG 兼容预览比例不符", i)
                    vectors[name] = primary_hash
                elif name in svg_report:
                    raise ExportError("PPTX_VALIDATION", "SVG 主资源缺失", i)
                if name.startswith("raster:"):
                    raster_count += 1
                elif name.startswith("image:"):
                    node_id = name.removeprefix("image:")
                    if node_id in images:
                        raise ExportError("PPTX_VALIDATION", "图片重复输出", i)
                    images[node_id] = primary_hash
                elif not name.startswith("vector:"):
                    raise ExportError("PPTX_VALIDATION", "未知图片对象", i)
            assets = {a["id"]: a for a in page["scene"]["assets"]}
            expected_images = {
                node_id: assets[nodes[node_id]["assetId"]]["sha256"]
                for node_id in drawn
                if nodes[node_id]["kind"] == "image"
                and node_id not in set(raster_ids + invisible_ids)
            }
            for region in svg_report.values():
                if not set(region["nodeIds"]).issubset(drawn) or any(
                    nodes[n]["kind"] != "shape"
                    for n in region["nodeIds"]
                    if region["reasonCode"] != "SOURCE_SVG"
                ):
                    raise ExportError("PPTX_VALIDATION", "SVG 覆盖范围无效", i)
            if (
                images != expected_images
                or raster_count != page_report["rasterRegions"]
                or vectors != {name: r["sha256"] for name, r in svg_report.items()}
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
                    "svg_regions": p.get("svgRegions", []),
                    "raster_details": p.get("rasterDetails", []),
                    "invisible_node_ids": p.get("invisibleNodeIds", []),
                    "asset_text_count": p.get("assetTextCount", 0),
                    "vector_text_count": p.get("vectorTextCount", 0),
                    "raster_asset_text_count": p.get("rasterAssetTextCount", 0),
                    "raster_text_count": p.get("rasterTextCount", 0),
                    "compatibility_png_count": p.get("compatibilityPngCount", 0),
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
