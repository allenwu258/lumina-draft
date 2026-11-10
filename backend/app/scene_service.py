"""Scene persistence and publication. Business queue lives only in SQLite."""

from __future__ import annotations

import asyncio
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import uuid

from bs4 import BeautifulSoup
from jsonschema import Draft7Validator
from sqlalchemy import select

from .db import PageTask, PageScene, SceneConversionJob, utcnow
from .tailwind_profile import TAILWIND_PROFILE, compiled_tailwind_css

ROOT = Path(__file__).resolve().parents[2]
CONVERTER_VERSION = "dom-canvas-0.3.0"
SCENE_ROOT = Path(os.getenv("SCENE_DATA_DIR", ROOT / "data" / "scenes")).resolve()
SCHEMA = json.loads(
    (ROOT / "packages/scene-core/contracts/scene-ir/0.3.0.schema.json").read_text(
        encoding="utf-8"
    )
)
VALIDATOR = Draft7Validator(SCHEMA)


class QueueSignal:
    # Recreated per lifespan, including TestClient's separate event loops.
    event: asyncio.Event | None = None

    def set(self):
        if self.event:
            self.event.set()


WAKE = QueueSignal()


def digest(value: str | bytes) -> str:
    return hashlib.sha256(
        value.encode("utf-8") if isinstance(value, str) else value
    ).hexdigest()


def canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@lru_cache(maxsize=1)
def environment_key() -> str:
    fonts = []
    if os.name == "nt":
        for folder in [
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts",
        ]:
            if folder.is_dir():
                fonts.extend(
                    (p.name, p.stat().st_size, p.stat().st_mtime_ns)
                    for p in folder.iterdir()
                    if p.is_file()
                )
    return digest(
        canonical(
            {
                "platform": platform.platform(),
                "fonts": sorted(fonts),
                "playwright": "1.65.0",
                "viewport": [800, 450, 1],
                "locale": "zh-CN",
                "timezone": "Asia/Shanghai",
            }
        )
    )


def source_hash(db, page: PageTask) -> str | None:
    """Read-only identity projection, including pre-Scene historical pages."""
    if page.html_hash:
        return page.html_hash
    if page.html:
        return digest(page.html)
    scene = db.get(PageScene, page.current_scene_id) if page.current_scene_id else None
    job = db.get(SceneConversionJob, scene.job_id) if scene else None
    return job.html_hash if job and job.page_task_id == page.id else None


def find_scene(db, page: PageTask) -> PageScene | None:
    html_hash = source_hash(db, page)
    if not html_hash or page.status != "SUCCEEDED":
        return None
    query = (
        select(PageScene)
        .join(SceneConversionJob, PageScene.job_id == SceneConversionJob.id)
        .where(
            PageScene.page_task_id == page.id,
            PageScene.schema_version == "0.3.0",
            SceneConversionJob.page_task_id == page.id,
            SceneConversionJob.html_hash == html_hash,
            SceneConversionJob.status == "SUCCEEDED",
        )
    )
    if page.current_scene_id:
        scene = db.scalar(query.where(PageScene.id == page.current_scene_id))
        if scene:
            return scene
    return db.scalar(query.order_by(PageScene.created_at.desc(), PageScene.id).limit(1))


def scene_ref(scene: PageScene, html_hash: str) -> dict:
    return {
        "page_task_id": scene.page_task_id,
        "scene_id": scene.id,
        "scene_hash": scene.scene_hash,
        "schema_version": scene.schema_version,
        "source_html_hash": html_hash,
    }


def summary(db, page: PageTask) -> dict:
    job = (
        db.get(SceneConversionJob, page.current_scene_job_id)
        if page.current_scene_job_id
        else None
    )
    html_hash = source_hash(db, page)
    if job and job.html_hash != html_hash:
        job = None
    scene = find_scene(db, page)
    if scene:
        job = db.get(SceneConversionJob, scene.job_id)
    return {
        "status": (
            "SUCCEEDED"
            if scene
            else (job.status if job and job.status != "SUCCEEDED" else "NOT_REQUESTED")
        ),
        "job_id": job.id if job else None,
        "scene_id": scene.id if scene else None,
        "scene_hash": scene.scene_hash if scene else None,
        "source_html_hash": html_hash,
        "ref": scene_ref(scene, html_hash) if scene else None,
        "error_code": job.error_code if job else None,
        "error": job.error if job else None,
        "warning_count": scene.warning_count if scene else 0,
    }


def ensure_page_scene(
    db, page: PageTask, expected_source_hash: str, *, retry=False
) -> dict:
    """Shared preview/export/generation entry; caller owns the write transaction."""
    actual = source_hash(db, page)
    if actual != expected_source_hash:
        raise ValueError("HTML_REVISION_CONFLICT")
    if page.status != "SUCCEEDED" or not actual:
        raise ValueError("PAGE_NOT_READY")
    if not page.html_hash:
        page.html_hash = actual
    scene = find_scene(db, page)
    if scene:
        if digest(scene.scene_json) != scene.scene_hash:
            raise ValueError("SCENE_CORRUPT")
        page.current_scene_id, page.current_scene_job_id = scene.id, scene.job_id
        return summary(db, page)
    if not page.html:
        raise ValueError("SOURCE_HTML_MISSING")
    if digest(page.html) != actual:
        raise ValueError("SOURCE_HASH_MISMATCH")
    job = (
        db.get(SceneConversionJob, page.current_scene_job_id)
        if page.current_scene_job_id
        else None
    )
    if job and job.page_task_id == page.id and job.html_hash == actual:
        if job.status == "SUCCEEDED":
            raise ValueError("SCENE_MISSING_OR_UNSUPPORTED")
        if job.status in {"QUEUED", "RUNNING"} or (
            job.status in {"FAILED", "CANCELLED"} and not retry
        ):
            return summary(db, page)
        if retry and job.status in {"FAILED", "CANCELLED"}:
            from .runtime import append_event_tx

            job.status, job.attempt, job.attempt_token = "QUEUED", 0, None
            job.error_code = job.error = job.started_at = job.finished_at = None
            job.queued_at = utcnow()
            append_event_tx(
                db,
                page.presentation_id,
                "page.scene.queued",
                {"page_task_id": page.id, "job_id": job.id, "status": "QUEUED"},
                page.run_id,
            )
            return summary(db, page)
    enqueue_tx(db, page, retry=retry)
    return summary(db, page)


def enqueue_tx(db, page: PageTask, *, retry: bool = False) -> SceneConversionJob:
    """Caller holds the event write lock. No browser IO inside this transaction."""
    from .runtime import append_event_tx

    if page.status != "SUCCEEDED" or not page.html or not page.html_hash:
        raise ValueError("PAGE_NOT_READY")
    root = BeautifulSoup(page.html, "html.parser").find(id="ppt-page")
    css = (
        compiled_tailwind_css()
        if root and root.get("data-css-profile") == TAILWIND_PROFILE
        else ""
    )
    css_hash = digest(css)
    identity = digest(
        canonical([page.html_hash, css_hash, CONVERTER_VERSION, environment_key()])
    )
    job = db.scalar(
        select(SceneConversionJob).where(
            SceneConversionJob.page_task_id == page.id,
            SceneConversionJob.input_hash == identity,
        )
    )
    created = not job
    if not job:
        job = SceneConversionJob(
            id=str(uuid.uuid4()),
            page_task_id=page.id,
            presentation_id=page.presentation_id,
            run_id=page.run_id,
            input_hash=identity,
            html_hash=page.html_hash,
            source_html=page.html,
            css=css,
            css_hash=css_hash,
            converter_version=CONVERTER_VERSION,
            environment_key=environment_key(),
            status="QUEUED",
            attempt=0,
        )
        db.add(job)
        db.flush()
    if retry and job.status in {"FAILED", "CANCELLED"}:
        job.status, job.attempt, job.attempt_token = "QUEUED", 0, None
        job.run_id = page.run_id
        job.error_code = job.error = job.started_at = job.finished_at = None
        job.queued_at = utcnow()
        created = True
    if page.current_scene_job_id != job.id:
        # Superseded work is fenced out before it can publish.
        old = (
            db.get(SceneConversionJob, page.current_scene_job_id)
            if page.current_scene_job_id
            else None
        )
        if old and old.status in {"QUEUED", "RUNNING"}:
            old.status, old.attempt_token, old.finished_at = "CANCELLED", None, utcnow()
        page.current_scene_job_id, page.current_scene_id = job.id, None
    if job.status == "SUCCEEDED":
        scene = db.scalar(select(PageScene).where(PageScene.job_id == job.id))
        page.current_scene_id = scene.id if scene else None
    if created:
        append_event_tx(
            db,
            page.presentation_id,
            "page.scene.queued",
            {"page_task_id": page.id, "job_id": job.id, "status": "QUEUED"},
            page.run_id,
        )
    return job


def validate_scene(scene: dict, job: SceneConversionJob, output: Path) -> str:
    error = next(VALIDATOR.iter_errors(scene), None)
    if error:
        raise ValueError(
            f"SCENE_SCHEMA: {'/'.join(map(str, error.absolute_path))} ({error.validator})"
        )
    if scene["source"] != {
        "htmlHash": job.html_hash,
        "cssHash": job.css_hash,
        "converterVersion": job.converter_version,
    }:
        raise ValueError("SCENE_SOURCE_MISMATCH")
    nodes = {n["id"]: n for n in scene["nodes"]}
    if len(nodes) != len(scene["nodes"]):
        raise ValueError("SCENE_DUPLICATE_ID")
    stack, drawn = [], set()
    for op in scene["paintOps"]:
        node = nodes[op["nodeId"]]
        group = node["kind"] in {"group", "table"}
        if op["op"] == "end":
            if not stack or stack.pop() != node["id"]:
                raise ValueError("SCENE_UNBALANCED_GROUP")
        else:
            if node["id"] in drawn or group != (op["op"] == "begin"):
                raise ValueError("SCENE_INVALID_PAINT")
            drawn.add(node["id"])
            if group:
                stack.append(node["id"])
        if node["kind"] == "text":
            t = node["text"]
            if t["ownerId"] not in nodes:
                raise ValueError("SCENE_MISSING_OWNER")
            runs = {r["id"] for p in t["paragraphs"] for r in p["runs"]}
            source_length = len(t["sourceText"].encode("utf-16-le")) // 2
            for line in t["lines"]:
                for f in line["fragments"]:
                    if (
                        f["runId"] not in runs
                        or not 0 <= f["sourceStart"] < f["sourceEnd"] <= source_length
                    ):
                        raise ValueError("SCENE_TEXT_RANGE")
    if stack or drawn != set(nodes):
        raise ValueError("SCENE_UNPAINTED_NODE")
    assets = {a["id"]: a for a in scene["assets"]}
    if len(assets) != len(scene["assets"]):
        raise ValueError("SCENE_DUPLICATE_ASSET")
    total = 0
    for asset in assets.values():
        path = (output / asset["file"]).resolve()
        if path.parent != output.resolve() or path.is_symlink():
            raise ValueError("SCENE_ASSET_PATH")
        size = path.stat().st_size
        total += size
        if size > 8 * 1024 * 1024 or total > 32 * 1024 * 1024:
            raise ValueError("SCENE_ASSET_LIMIT")
        raw = path.read_bytes()
        if (
            not raw.startswith(b"\x89PNG\r\n\x1a\n")
            or size != asset["byteLength"]
            or digest(raw) != asset["sha256"]
        ):
            raise ValueError("SCENE_ASSET_HASH")
        if len(raw) < 24 or struct.unpack(">II", raw[16:24]) != (
            asset["width"],
            asset["height"],
        ):
            raise ValueError("SCENE_ASSET_DIMENSIONS")
        if any(n not in nodes for n in asset["coveredNodeIds"]):
            raise ValueError("SCENE_ASSET_COVERAGE")
    for n in nodes.values():
        if n["kind"] == "image" and n["assetId"] not in assets:
            raise ValueError("SCENE_MISSING_ASSET")
        if n["kind"] == "table" and any(
            c["nodeId"] not in nodes for c in n.get("cells", [])
        ):
            raise ValueError("SCENE_TABLE_REFERENCE")
    inventory = {item["sourcePath"]: item for item in scene["textInventory"]}
    if len(inventory) != len(scene["textInventory"]):
        raise ValueError("SCENE_DUPLICATE_SOURCE")
    for item in inventory.values():
        if item["nodeId"] and item["nodeId"] not in nodes:
            raise ValueError("SCENE_INVENTORY_REFERENCE")
    for node in nodes.values():
        if node["kind"] == "text":
            source = inventory.get(node["sourcePath"], {})
            if (
                source.get("nodeId") != node["id"]
                or source.get("sourceText") != node["text"]["sourceText"]
            ):
                raise ValueError("SCENE_TEXT_PROVENANCE")
    return canonical(scene)


def publish_tx(
    db, job_id: str, token: str, scene: dict, scene_json: str, asset_dir: str
) -> bool:
    from .runtime import append_event_tx

    job = db.get(SceneConversionJob, job_id)
    page = db.get(PageTask, job.page_task_id) if job else None
    if (
        not job
        or job.status != "RUNNING"
        or job.attempt_token != token
        or not page
        or page.current_scene_job_id != job.id
        or page.html_hash != job.html_hash
    ):
        return False
    result = PageScene(
        id=str(uuid.uuid4()),
        job_id=job.id,
        page_task_id=page.id,
        schema_version=scene["schemaVersion"],
        scene_hash=digest(scene_json),
        warning_count=len(scene["diagnostics"]),
        scene_json=scene_json,
        asset_dir=asset_dir,
    )
    db.add(result)
    job.status, job.finished_at, job.error_code, job.error = (
        "SUCCEEDED",
        utcnow(),
        None,
        None,
    )
    page.current_scene_id = result.id
    append_event_tx(
        db,
        job.presentation_id,
        "page.scene.completed",
        {
            "page_task_id": page.id,
            "job_id": job.id,
            "scene_id": result.id,
            "scene_hash": result.scene_hash,
            "status": "SUCCEEDED",
        },
        job.run_id,
    )
    return True


async def cancel_conversions(run_id: str):
    from .db import session_scope
    from .runtime import EVENT_WRITE_LOCK, EVENT_BUS, append_event_tx

    presentations = set()
    async with EVENT_WRITE_LOCK:
        with session_scope() as db:
            for job in db.scalars(
                select(SceneConversionJob).where(
                    SceneConversionJob.run_id == run_id,
                    SceneConversionJob.status.in_(["QUEUED", "RUNNING"]),
                )
            ):
                job.status, job.attempt_token, job.finished_at = (
                    "CANCELLED",
                    None,
                    utcnow(),
                )
                presentations.add(job.presentation_id)
                append_event_tx(
                    db,
                    job.presentation_id,
                    "page.scene.cancelled",
                    {
                        "page_task_id": job.page_task_id,
                        "job_id": job.id,
                        "status": "CANCELLED",
                    },
                    run_id,
                )
    for p in presentations:
        await EVENT_BUS.notify(p)
