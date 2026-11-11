"""One durable FIFO, one supervised Node/Chromium execution slot."""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import uuid

from sqlalchemy import select

from .db import PageTask, PptxExport, SceneConversionJob, session_scope, utcnow
from . import export_service
from .rendering import validate_html
from .scene_service import (
    ROOT,
    SCENE_ROOT,
    CONVERTER_VERSION,
    DESIGN_CONVERTER_VERSION,
    SUPPORTED_CONVERTERS,
    WAKE,
    canonical,
    digest,
    publish_tx,
    enqueue_tx,
    validate_scene,
    job_canvas,
    matches_target,
    page_target,
)
from .tailwind_profile import preview_html

log = logging.getLogger(__name__)
HEALTH = {
    "status": "starting",
    "active_job_id": None,
    "error": None,
    "converter_version": CONVERTER_VERSION,
    "converter_versions": SUPPORTED_CONVERTERS,
}


class WorkerError(Exception):
    def __init__(self, code, message="", page=None):
        self.code = code
        self.page = page
        super().__init__(message or code)


class WindowsJob:
    """Own only this worker tree; kill Chromium even if Node exits unexpectedly."""

    def __init__(self, process):
        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "read",
                    "write",
                    "other",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", IO),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000
        try:
            if (
                not self.handle
                or not self.api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
                or not self.api.AssignProcessToJobObject(
                    self.handle, int(process._handle)
                )
            ):
                raise WorkerError(
                    "WORKER_PROCESS_CONTAINMENT", "无法建立转换进程的 Windows Job"
                )
            ntdll = ctypes.WinDLL("ntdll")
            ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            if ntdll.NtResumeProcess(int(process._handle)) != 0:
                raise WorkerError("WORKER_PROCESS_CONTAINMENT")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class SceneWorker:
    def __init__(self):
        self.process = None
        self.job = None
        self.errors = None

    def _read(self, process):
        raw = process.stdout.readline(65537)
        if not raw or len(raw) > 65536 or not raw.endswith(b"\n"):
            raise WorkerError("WORKER_DISCONNECTED", "转换 Worker 已断开")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError("WORKER_PROTOCOL", "转换 Worker 返回无效消息") from exc

    async def start(self):
        if self.process and self.process.poll() is None:
            return
        await self.close()
        node, daemon = (
            shutil.which("node"),
            ROOT / "services/slide-worker/dist/index.js",
        )
        if not node or not daemon.is_file():
            raise WorkerError(
                "WORKER_BUILD_REQUIRED",
                "请先在仓库根目录执行 npm ci、npm run build:scene 和 npm run browser:install",
            )
        SCENE_ROOT.mkdir(parents=True, exist_ok=True)
        self.errors = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                [node, str(daemon), str(SCENE_ROOT)],
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.errors,
                creationflags=(
                    (subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0
                ),
                start_new_session=os.name != "nt",
            )
            if os.name == "nt":
                self.job = WindowsJob(self.process)
            ready = await asyncio.wait_for(
                asyncio.to_thread(self._read, self.process), 30
            )
            if (
                ready.get("type") != "ready"
                or ready.get("protocol") != 2
                or "export-pptx" not in ready.get("capabilities", [])
                or ready.get("converterVersion") != CONVERTER_VERSION
                or not set(SUPPORTED_CONVERTERS).issubset(ready.get("converterVersions", []))
            ):
                raise WorkerError(
                    "WORKER_HANDSHAKE", "转换 Worker 版本不匹配，请重新构建"
                )
            HEALTH.update(status="ready", error=None, chromium=ready["chromium"])
        except BaseException as exc:
            diagnostic = ""
            if self.errors:
                self.errors.seek(0)
                diagnostic = self.errors.read(3000).decode("utf-8", "replace")
            await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise WorkerError(
                getattr(exc, "code", "WORKER_START_FAILED"),
                str(exc) + ("; " + diagnostic if diagnostic else ""),
            ) from exc

    async def execute(self, job: SceneConversionJob, token: str, work: Path, cancelled):
        raw = canonical(
            {
                "html": preview_html(job.source_html, css=job.css),
                "htmlHash": job.html_hash,
                "cssHash": job.css_hash,
                "converterVersion": job.converter_version,
                "canvas": {"width": job.canvas_width, "height": job.canvas_height},
            }
        )
        await asyncio.to_thread((work / "input.json").write_text, raw, encoding="utf-8")
        return await self.request("parse", job.id, token, work, cancelled, 35)

    async def export_pptx(self, record, token, work, staged, cancelled):
        await asyncio.to_thread(
            (work / "input.json").write_text, canonical(staged), encoding="utf-8"
        )
        return await self.request("export-pptx", record.id, token, work, cancelled, 125)

    async def request(self, operation, request_id, token, work, cancelled, timeout):
        await self.start()
        request = {
            "type": operation,
            "request_id": request_id,
            "attempt_token": token,
            "input": (work / "input.json").relative_to(SCENE_ROOT).as_posix(),
            "output": (work / "out").relative_to(SCENE_ROOT).as_posix(),
        }
        process = self.process
        try:
            process.stdin.write((json.dumps(request) + "\n").encode())
            process.stdin.flush()
        except OSError as exc:
            await self.close()
            raise WorkerError("WORKER_DISCONNECTED", "转换 Worker 已断开") from exc
        read = asyncio.create_task(asyncio.to_thread(self._read, process))
        try:
            for _ in range(timeout * 5):
                done, _pending = await asyncio.wait({read}, timeout=0.2)
                if cancelled():
                    raise WorkerError("CONVERSION_CANCELLED")
                if done:
                    result = read.result()
                    if (
                        result.get("request_id") != request_id
                        or result.get("attempt_token") != token
                    ):
                        raise WorkerError("WORKER_RESULT_IDENTITY")
                    if result.get("type") == "error":
                        raise WorkerError(
                            result.get("code", "CONVERSION_FAILED"),
                            result.get("message", ""),
                            result.get("page_number"),
                        )
                    if result.get("type") != "result" or (
                        result.get("scene") != "scene.json"
                        if operation == "parse"
                        else result.get("file") != "presentation.pptx"
                    ):
                        raise WorkerError("WORKER_PROTOCOL")
                    return work / "out"
            raise WorkerError(
                "CONVERSION_TIMEOUT" if operation == "parse" else "EXPORT_TIMEOUT",
                f"执行超过 {timeout} 秒",
            )
        except BaseException:
            await self.close()
            raise
        finally:
            if not read.done():
                read.cancel()
            await asyncio.gather(read, return_exceptions=True)

    async def close(self):
        process, self.process = self.process, None
        if process:
            if process.poll() is None:
                try:
                    process.stdin.write(b'{"type":"shutdown"}\n')
                    process.stdin.flush()
                    await asyncio.to_thread(process.wait, 2)
                except (OSError, subprocess.TimeoutExpired):
                    if os.name == "nt":
                        if self.job:
                            self.job.close()
                        else:
                            process.kill()
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
            if self.job:
                self.job.close()
                self.job = None
            try:
                await asyncio.to_thread(process.wait, 3)
            except subprocess.TimeoutExpired:
                process.kill()
            for pipe in (process.stdin, process.stdout):
                try:
                    pipe.close()
                except OSError:
                    # Broken Windows pipes must not mask the worker diagnostic.
                    pass
        if self.errors:
            self.errors.close()
            self.errors = None


def is_current(job_id, token):
    with session_scope() as db:
        job = db.get(SceneConversionJob, job_id)
        page = db.get(PageTask, job.page_task_id) if job else None
        return bool(
            job
            and job.status == "RUNNING"
            and job.attempt_token == token
            and page
            and page.current_scene_job_id == job.id
            and page.html_hash == job.html_hash
            and matches_target(job, page_target(db, page))
        )


async def recover():
    from .runtime import EVENT_WRITE_LOCK, EVENT_BUS, append_event_tx

    presentations = set()
    abandoned = []
    async with EVENT_WRITE_LOCK:
        with session_scope() as db:
            for record in db.scalars(
                select(PptxExport).where(
                    PptxExport.status.in_(["PREPARING", "QUEUED", "RUNNING"])
                )
            ):
                if record.export_version != export_service.EXPORT_VERSION:
                    if record.attempt_token:
                        abandoned.append(
                            SCENE_ROOT / ".work" / record.id / record.attempt_token
                        )
                    export_service.fail_tx(
                        db,
                        record,
                        export_service.ExportError(
                            "EXPORT_VERSION_CHANGED", "导出引擎已升级，请重新发起导出"
                        ),
                    )
                    presentations.add(record.presentation_id)
                    continue
                if record.status != "RUNNING":
                    continue
                if record.attempt_token:
                    abandoned.append(
                        SCENE_ROOT / ".work" / record.id / record.attempt_token
                    )
                record.status, record.attempt_token, record.queued_at = (
                    "QUEUED",
                    None,
                    utcnow(),
                )
                presentations.add(record.presentation_id)
                export_service.event_tx(db, record, "queued")
            for job in db.scalars(
                select(SceneConversionJob).where(
                    SceneConversionJob.status.in_(["QUEUED", "RUNNING"])
                )
            ):
                if job.attempt_token:
                    abandoned.append(SCENE_ROOT / ".work" / job.id / job.attempt_token)
                presentations.add(job.presentation_id)
                page = db.get(PageTask, job.page_task_id)
                if not page or not matches_target(job, page_target(db, page)):
                    job.status, job.attempt_token, job.finished_at = (
                        "CANCELLED",
                        None,
                        utcnow(),
                    )
                    job.error_code, job.error = (
                        "CONVERTER_VERSION_CHANGED",
                        "转换引擎已升级，旧版本任务已取消",
                    )
                    append_event_tx(
                        db,
                        job.presentation_id,
                        "page.scene.cancelled",
                        {
                            "page_task_id": job.page_task_id,
                            "job_id": job.id,
                            "status": "CANCELLED",
                            "error_code": job.error_code,
                            "error": job.error,
                        },
                        job.run_id,
                    )
                    if (
                        page
                        and page.current_scene_job_id == job.id
                        and page.html_hash == job.html_hash
                        and page.status == "SUCCEEDED"
                        and page.html
                    ):
                        enqueue_tx(db, page, frozen_css=job.css)
                    continue
                if job.status != "RUNNING":
                    continue
                job.status, job.attempt_token, job.queued_at = "QUEUED", None, utcnow()
                append_event_tx(
                    db,
                    job.presentation_id,
                    "page.scene.queued",
                    {
                        "page_task_id": job.page_task_id,
                        "job_id": job.id,
                        "status": "QUEUED",
                        "recovered": True,
                    },
                    job.run_id,
                )
    for path in abandoned:
        if path.resolve().is_relative_to((SCENE_ROOT / ".work").resolve()):
            await asyncio.to_thread(shutil.rmtree, path, True)
    for p in presentations:
        await EVENT_BUS.notify(p)


async def scene_scheduler(stop: asyncio.Event):
    from .runtime import EVENT_WRITE_LOCK, EVENT_BUS, append_event_tx

    WAKE.event = asyncio.Event()
    worker = SceneWorker()
    current = None
    try:
        await recover()
        try:
            await worker.start()
        except WorkerError as exc:
            HEALTH.update(status="unavailable", error=str(exc)[:1600])
            log.error("Scene worker unavailable: %s", exc)
        while not stop.is_set():
            WAKE.event.clear()
            changed_presentations = set()
            async with EVENT_WRITE_LOCK:
                with session_scope() as db:
                    for pending in db.scalars(
                        select(PptxExport).where(PptxExport.status == "PREPARING")
                    ):
                        old = (pending.status, pending.input_manifest_json)
                        export_service.advance_tx(db, pending)
                        if old != (pending.status, pending.input_manifest_json):
                            changed_presentations.add(pending.presentation_id)
                    db.flush()
                    current = db.scalar(
                        select(SceneConversionJob)
                        .where(SceneConversionJob.status == "QUEUED")
                        .order_by(SceneConversionJob.queued_at, SceneConversionJob.id)
                        .limit(1)
                    )
                    export = db.scalar(
                        select(PptxExport)
                        .where(PptxExport.status == "QUEUED")
                        .order_by(PptxExport.queued_at, PptxExport.id)
                        .limit(1)
                    )
                    if export and (
                        not current
                        # SQLite reads UTC timestamps without tzinfo; freshly
                        # prepared exports in this session still carry UTC tzinfo.
                        or (export.queued_at.replace(tzinfo=None), export.id)
                        < (current.queued_at.replace(tzinfo=None), current.id)
                    ):
                        current = export
                    if current:
                        current.status, current.attempt_token = "RUNNING", str(
                            uuid.uuid4()
                        )
                        current.attempt, current.started_at = (
                            current.attempt + 1,
                            utcnow(),
                        )
                        if isinstance(current, PptxExport):
                            export_service.event_tx(db, current, "started")
                        else:
                            append_event_tx(
                                db,
                                current.presentation_id,
                                "page.scene.started",
                                {
                                    "page_task_id": current.page_task_id,
                                    "job_id": current.id,
                                    "status": "RUNNING",
                                },
                                current.run_id,
                            )
            for changed_id in changed_presentations:
                await EVENT_BUS.notify(changed_id)
            if not current:
                try:
                    await asyncio.wait_for(WAKE.event.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
                continue
            token = current.attempt_token
            HEALTH["active_job_id"] = current.id
            await EVENT_BUS.notify(current.presentation_id)
            work = SCENE_ROOT / ".work" / current.id / token
            try:
                work.mkdir(parents=True, exist_ok=True)
                if isinstance(current, PptxExport):
                    staged = await asyncio.to_thread(
                        export_service.stage_input, current, work
                    )
                    output = await worker.export_pptx(
                        current,
                        token,
                        work,
                        staged,
                        lambda: stop.is_set()
                        or not export_is_current(current.id, token),
                    )
                    installed = await asyncio.to_thread(
                        export_service.install_result, current, token, output, staged
                    )
                    async with EVENT_WRITE_LOCK:
                        with session_scope() as db:
                            export_service.publish_tx(
                                db, current.id, token, current.input_hash, installed
                            )
                    HEALTH.update(status="ready", error=None)
                    continue
                valid, reason = validate_html(
                    current.source_html, canvas=job_canvas(current),
                    require_profile=current.converter_version == DESIGN_CONVERTER_VERSION,
                )
                if not valid:
                    raise WorkerError("INVALID_SOURCE_HTML", reason)
                output = await worker.execute(
                    current,
                    token,
                    work,
                    lambda: stop.is_set() or not is_current(current.id, token),
                )

                def verify_install():
                    path = output / "scene.json"
                    if path.stat().st_size > 24 * 1024 * 1024:
                        raise ValueError("SCENE_SIZE_LIMIT")
                    scene = json.loads(path.read_text(encoding="utf-8"))
                    encoded = validate_scene(scene, current, output)
                    target = SCENE_ROOT / current.id / digest(encoded)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        os.replace(output, target)
                    return scene, encoded, target.relative_to(SCENE_ROOT).as_posix()

                scene, encoded, installed = await asyncio.to_thread(verify_install)
                async with EVENT_WRITE_LOCK:
                    with session_scope() as db:
                        publish_tx(db, current.id, token, scene, encoded, installed)
                HEALTH.update(status="ready", error=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if stop.is_set():
                    raise asyncio.CancelledError() from exc
                code = getattr(exc, "code", "INVALID_SCENE")
                async with EVENT_WRITE_LOCK:
                    with session_scope() as db:
                        if isinstance(current, PptxExport):
                            record = db.get(PptxExport, current.id)
                            if (
                                record
                                and record.status == "RUNNING"
                                and record.attempt_token == token
                            ):
                                if (
                                    code
                                    in {
                                        "WORKER_DISCONNECTED",
                                        "BROWSER_CRASHED",
                                        "WORKER_START_FAILED",
                                    }
                                    and record.attempt < 2
                                ):
                                    (
                                        record.status,
                                        record.attempt_token,
                                        record.queued_at,
                                    ) = ("QUEUED", None, utcnow())
                                    export_service.event_tx(db, record, "queued")
                                else:
                                    export_service.fail_tx(db, record, exc)
                            continue
                        job = db.get(SceneConversionJob, current.id)
                        if (
                            job
                            and job.status == "RUNNING"
                            and job.attempt_token == token
                        ):
                            infrastructure = code in {
                                "WORKER_DISCONNECTED",
                                "BROWSER_CRASHED",
                                "CONVERSION_TIMEOUT",
                                "WORKER_START_FAILED",
                            }
                            job.status = (
                                "QUEUED"
                                if infrastructure and job.attempt < 2
                                else "FAILED"
                            )
                            job.attempt_token, job.error_code, job.error = (
                                None,
                                code,
                                str(exc)[:1600],
                            )
                            job.finished_at = (
                                utcnow() if job.status == "FAILED" else None
                            )
                            append_event_tx(
                                db,
                                job.presentation_id,
                                (
                                    "page.scene.queued"
                                    if job.status == "QUEUED"
                                    else "page.scene.failed"
                                ),
                                {
                                    "page_task_id": job.page_task_id,
                                    "job_id": job.id,
                                    "status": job.status,
                                    "error_code": code,
                                    "error": job.error,
                                },
                                job.run_id,
                            )
                if code.startswith("WORKER_") or code == "BROWSER_CRASHED":
                    HEALTH.update(status="unavailable", error=str(exc)[:1600])
                log.warning("Scene conversion %s: %s", current.id, str(exc)[:1600])
            finally:
                await asyncio.to_thread(shutil.rmtree, work, True)
                HEALTH["active_job_id"] = None
                await EVENT_BUS.notify(current.presentation_id)
                current = None
    finally:
        await worker.close()
        await recover()
        WAKE.event = None
        HEALTH.update(status="stopped", active_job_id=None)


def export_is_current(export_id, token):
    with session_scope() as db:
        record = db.get(PptxExport, export_id)
        return bool(
            record and record.status == "RUNNING" and record.attempt_token == token
        )
