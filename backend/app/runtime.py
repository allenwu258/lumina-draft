from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from .config import settings
from .db import ApiEndpoint, ApiProvider, Event, Job, LlmCall, LogicalModel, ModelBinding, Outline, PageTask, Presentation, Run, dumps, loads, resolve_default, session_scope, utcnow
from .prompts import COMPOSER_PROMPT, DIRECTOR_PROMPT
from .providers import ProviderError, ProviderEvent, stream_responses
from .rendering import html_hash, validate_html
from .schemas import OutlineDocument
from .secrets import read_secret


def canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def request_reasoning_effort(snapshot: dict[str, Any]) -> str | None:
    effort = snapshot.get("reasoning_effort", "none")
    return None if effort == "auto" else effort


class DirectorState(TypedDict, total=False):
    presentation_id: str
    run_id: str
    user_request: str
    options: dict[str, Any]
    result: dict[str, Any]
    error: str


class PageState(TypedDict, total=False):
    task_id: str
    run_id: str
    result: str
    error: str


async def run_graph(graph, state):
    return await graph.ainvoke(state)


def build_director_graph():
    async def generate(state: DirectorState):
        prompt = f"用户需求：\n{state['user_request']}\n\n附加选项：{json.dumps(state.get('options', {}), ensure_ascii=False)}\n请严格按系统要求输出结构化说明书。"
        output = await ProviderGateway().complete_json(prompt, state["presentation_id"], "director", state["run_id"])
        return {"result": output}
    graph = StateGraph(DirectorState)
    graph.add_node("generate", generate)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", END)
    return graph.compile()


def build_page_graph():
    async def compose(state: PageState):
        with session_scope() as db:
            task = db.get(PageTask, state["task_id"])
            presentation = db.get(Presentation, task.presentation_id) if task else None
            if not task or not presentation:
                return {"error": "页面任务不存在"}
            outline = db.scalar(select(Outline).where(Outline.presentation_id == task.presentation_id, Outline.confirmed.is_(True)).order_by(Outline.revision.desc()))
            document = loads(outline.document_json)
            slide = loads(task.brief_json)
            slides = document["slides"]
            number = task.page_number
            prev = slides[number - 2] if number > 1 else None
            nxt = slides[number] if number < len(slides) else None
            prompt = "\n".join([
                "整套说明书：", json.dumps({"brief": document["brief"], "style_spec": document["style_spec"]}, ensure_ascii=False),
                "当前页面：", json.dumps(slide, ensure_ascii=False),
                "上一页摘要：", json.dumps({k: prev.get(k) for k in ("title", "key_message", "summary")} if prev else None, ensure_ascii=False),
                "下一页摘要：", json.dumps({k: nxt.get(k) for k in ("title", "key_message", "summary")} if nxt else None, ensure_ascii=False),
                f"页码：{number}/{len(slides)}",
            ])
        if state.get("error"):
            prompt += "\n上一次输出未通过校验：" + state["error"] + "。请完整重写并确保符合所有输出合同。"
        html = await ProviderGateway().complete_html(prompt, task.presentation_id, task.id)
        return {"result": html}
    graph = StateGraph(PageState)
    graph.add_node("compose", compose)
    graph.add_edge(START, "compose")
    graph.add_edge("compose", END)
    return graph.compile()


class ProviderGateway:
    async def _stream(self, prompt: str, presentation_id: str, purpose: str, schema: dict | None = None, page_task_id: str | None = None, run_id: str | None = None) -> str:
        with session_scope() as db:
            presentation = db.get(Presentation, presentation_id)
            snapshot = loads(presentation.config_snapshot_json) if presentation else {}
            route, logical_model, binding, endpoint, owner = resolve_default(db)
            if snapshot.get("endpoint_id"):
                endpoint = db.get(ApiEndpoint, snapshot["endpoint_id"])
                owner = db.get(ApiProvider, snapshot.get("provider_id"))
                binding = db.get(ModelBinding, snapshot.get("binding_id"))
                logical_model = db.get(LogicalModel, snapshot.get("model_id"))
            call = LlmCall(presentation_id=presentation_id, purpose=purpose, model=snapshot.get("model", binding.remote_model_id if binding else settings.api_model), status="RUNNING")
            db.add(call)
            db.flush()
            call_id = call.id
            if not endpoint or not owner or not binding or not logical_model or not all([owner.enabled, endpoint.enabled, binding.enabled, logical_model.enabled]):
                raise ProviderError("CONFIG_NOT_READY", "default 模型未配置或已停用")
            base_url, model = snapshot.get("base_url", endpoint.base_url), snapshot.get("model", binding.remote_model_id)
            key = read_secret(snapshot.get("credential_ref") or endpoint.credential_ref or "") or ""
        output: list[str] = []
        buffer: list[str] = []
        director_stream_buffer: list[tuple[str, str]] = []
        last_flush = time.monotonic()
        last_director_flush = last_flush
        total_bytes = 0
        reasoning_bytes = 0
        refused = False
        usage: dict[str, Any] = {}
        status = "FAILED"
        error: str | None = None

        async def flush_director_stream() -> None:
            nonlocal last_director_flush
            if not director_stream_buffer or not run_id:
                return
            segments: list[tuple[str, str]] = []
            for channel, delta in director_stream_buffer:
                if segments and segments[-1][0] == channel:
                    segments[-1] = (channel, segments[-1][1] + delta)
                else:
                    segments.append((channel, delta))
            director_stream_buffer.clear()
            for channel, delta in segments:
                await emit_event(presentation_id, "director.stream.delta", {"run_id": run_id, "channel": channel, "delta": delta}, run_id)
            last_director_flush = time.monotonic()

        try:
            async for event in stream_responses(base_url=base_url, api_key=key, model=model, instructions=DIRECTOR_PROMPT if purpose == "director" else COMPOSER_PROMPT, prompt=prompt, json_schema=schema, reasoning_effort=request_reasoning_effort(snapshot)):
                if event.kind == "text_delta":
                    output.append(event.data)
                    total_bytes += len(event.data.encode("utf-8"))
                    if total_bytes > 256 * 1024:
                        raise ProviderError("OUTPUT_TOO_LARGE", "模型输出超过大小限制")
                    if purpose == "director":
                        director_stream_buffer.append(("output", event.data))
                        if time.monotonic() - last_director_flush >= 0.12 or sum(len(value) for _, value in director_stream_buffer) >= 2048:
                            await flush_director_stream()
                    if page_task_id:
                        buffer.append(event.data)
                        if time.monotonic() - last_flush >= 0.15 or sum(map(len, buffer)) >= 16384:
                            await emit_page_delta(presentation_id, page_task_id, "".join(buffer))
                            buffer.clear()
                            last_flush = time.monotonic()
                elif event.kind == "reasoning_delta" and purpose == "director":
                    reasoning_bytes += len(event.data.encode("utf-8"))
                    if reasoning_bytes > 256 * 1024:
                        raise ProviderError("REASONING_TOO_LARGE", "模型 reasoning 输出超过大小限制")
                    director_stream_buffer.append(("reasoning", event.data))
                    if time.monotonic() - last_director_flush >= 0.12 or sum(len(value) for _, value in director_stream_buffer) >= 2048:
                        await flush_director_stream()
                elif event.kind == "refusal_delta":
                    refused = True
                    status, error = "REFUSED", "模型拒绝了该请求"
                elif event.kind == "completed":
                    status, usage = ("REFUSED" if refused else "SUCCEEDED"), event.data.get("usage", {})
                elif event.kind in {"incomplete", "refused", "failed"}:
                    status, error, usage = event.kind.upper(), "模型未生成完整结果", event.data.get("usage", {}) if isinstance(event.data, dict) else {}
                    if event.kind == "incomplete" and isinstance(event.data, dict):
                        reason = event.data.get("reason")
                        if reason == "max_output_tokens":
                            error = "供应商因输出 token 上限截断了结果"
                        elif reason == "content_filter":
                            error = "供应商的内容过滤中断了生成"
        except ProviderError as exc:
            error = str(exc)
            status = "FAILED"
            raise
        finally:
            await flush_director_stream()
            if page_task_id and buffer:
                await emit_page_delta(presentation_id, page_task_id, "".join(buffer))
            with session_scope() as db:
                call = db.get(LlmCall, call_id)
                if call:
                    call.status = status
                    call.input_tokens = usage.get("input_tokens")
                    call.output_tokens = usage.get("output_tokens")
                    call.total_tokens = usage.get("total_tokens")
                    call.error = error
                    call.finished_at = utcnow()
            await emit_event(presentation_id, "llm.usage.updated", {"call_id": call_id, "purpose": purpose})
        if status != "SUCCEEDED":
            raise ProviderError("MODEL_INCOMPLETE", error or "模型未完成输出")
        return "".join(output)

    async def complete_json(self, prompt: str, presentation_id: str, purpose: str, run_id: str | None = None) -> dict[str, Any]:
        schema = OutlineDocument.model_json_schema()
        text = await self._stream(prompt, presentation_id, purpose, schema, run_id=run_id)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("INVALID_MODEL_JSON", "Director 返回的 JSON 无法解析") from exc

    async def complete_html(self, prompt: str, presentation_id: str, page_task_id: str) -> str:
        return await self._stream(prompt, presentation_id, "page_composer", None, page_task_id)


async def emit_event(presentation_id: str, event_type: str, payload: dict[str, Any], run_id: str | None = None) -> None:
    async with EVENT_WRITE_LOCK:
        with session_scope() as db:
            current = db.scalar(select(Event.seq).where(Event.presentation_id == presentation_id).order_by(Event.seq.desc()).limit(1)) or 0
            db.add(Event(presentation_id=presentation_id, seq=current + 1, run_id=run_id, type=event_type, payload_json=dumps(payload)))
    await EVENT_BUS.notify(presentation_id)


async def emit_page_delta(presentation_id: str, task_id: str, delta: str) -> None:
    await emit_event(presentation_id, "page.html.delta", {"page_task_id": task_id, "delta": delta})


class EventBus:
    def __init__(self):
        self.conditions: dict[str, asyncio.Condition] = {}

    def condition(self, presentation_id: str) -> asyncio.Condition:
        return self.conditions.setdefault(presentation_id, asyncio.Condition())

    async def notify(self, presentation_id: str):
        condition = self.condition(presentation_id)
        async with condition:
            condition.notify_all()


EVENT_BUS = EventBus()
EVENT_WRITE_LOCK = asyncio.Lock()
ACTIVE_RUNS: dict[str, asyncio.Task] = {}
DIRECTOR_GRAPH = build_director_graph()
PAGE_GRAPH = build_page_graph()


async def run_director(presentation_id: str, run_id: str) -> None:
    with session_scope() as db:
        presentation = db.get(Presentation, presentation_id)
        run = db.get(Run, run_id)
        if not presentation or not run:
            return
        if run.status == "SUCCEEDED":
            return
        run.status = "RUNNING"
        presentation.status = "PLANNING"
        request, options = presentation.user_request, loads(presentation.options_json)
    await emit_event(presentation_id, "director.stream.reset", {"run_id": run_id}, run_id)
    await emit_event(presentation_id, "director.started", {"run_id": run_id}, run_id)
    try:
        result = await run_graph(DIRECTOR_GRAPH, {"presentation_id": presentation_id, "run_id": run_id, "user_request": request, "options": options})
        doc = OutlineDocument.model_validate(result["result"])
        normalized = doc.model_dump()
        normalized["slides"] = [s | {"slide_id": str(uuid.uuid4())} for s in normalized["slides"]]
        digest = canonical_hash(normalized)
        with session_scope() as db:
            db.add(Outline(presentation_id=presentation_id, revision=1, content_hash=digest, document_json=dumps(normalized)))
            run, presentation = db.get(Run, run_id), db.get(Presentation, presentation_id)
            run.status, presentation.status = "SUCCEEDED", "WAITING_CONFIRMATION"
        await emit_event(presentation_id, "outline.ready", {"run_id": run_id, "outline": normalized, "content_hash": digest}, run_id)
        await emit_event(presentation_id, "run.completed", {"run_id": run_id, "status": "SUCCEEDED", "kind": "DIRECTOR"}, run_id)
    except Exception as exc:
        message = str(exc)[:500]
        with session_scope() as db:
            run, presentation = db.get(Run, run_id), db.get(Presentation, presentation_id)
            if run: run.status, run.error = "FAILED", message
            if presentation: presentation.status = "FAILED"
        await emit_event(presentation_id, "run.failed", {"run_id": run_id, "error": message}, run_id)


async def compose_page(presentation_id: str, run_id: str, task_id: str, semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        with session_scope() as db:
            task = db.get(PageTask, task_id)
            if not task: return
            task.status, task.attempt = "RUNNING", task.attempt + 1
            number = task.page_number
        await emit_event(presentation_id, "page.started", {"page_task_id": task_id, "page_number": number}, run_id)
        try:
            reason = ""
            for attempt_number in range(1, 3):
                result = await run_graph(PAGE_GRAPH, {"task_id": task_id, "run_id": run_id, "error": reason})
                html = result["result"]
                valid, reason = validate_html(html)
                if valid:
                    break
                if attempt_number < 2:
                    await emit_event(presentation_id, "page.retrying", {"page_task_id": task_id, "page_number": number, "reason": reason, "attempt": attempt_number + 1}, run_id)
            if not valid: raise ProviderError("INVALID_HTML", reason)
            digest = html_hash(html)
            with session_scope() as db:
                task = db.get(PageTask, task_id)
                task.status, task.html, task.html_hash, task.error = "SUCCEEDED", html, digest, None
            await emit_event(presentation_id, "page.completed", {"page_task_id": task_id, "page_number": number, "html_hash": digest, "status": "SUCCEEDED"}, run_id)
        except Exception as exc:
            message = str(exc)[:500]
            with session_scope() as db:
                task = db.get(PageTask, task_id)
                if task: task.status, task.error = "FAILED", message
            await emit_event(presentation_id, "page.failed", {"page_task_id": task_id, "page_number": number, "error": message}, run_id)


async def run_generation(presentation_id: str, run_id: str) -> None:
    await emit_event(presentation_id, "generation.started", {"run_id": run_id}, run_id)
    with session_scope() as db:
        tasks = list(db.scalars(select(PageTask).where(PageTask.run_id == run_id, PageTask.status != "SUCCEEDED")).all())
    semaphore = asyncio.Semaphore(settings.max_concurrency)
    await asyncio.gather(*(compose_page(presentation_id, run_id, task.id, semaphore) for task in tasks))
    with session_scope() as db:
        statuses = [t.status for t in db.scalars(select(PageTask).where(PageTask.run_id == run_id)).all()]
        run, presentation = db.get(Run, run_id), db.get(Presentation, presentation_id)
        success = all(s == "SUCCEEDED" for s in statuses)
        run.status, presentation.status = ("SUCCEEDED" if success else "PARTIAL_FAILED"), ("READY" if success else "PARTIAL_FAILED")
    await emit_event(presentation_id, "run.completed", {"run_id": run_id, "status": "SUCCEEDED" if success else "PARTIAL_FAILED", "kind": "GENERATION"}, run_id)


async def scheduler(stop: asyncio.Event) -> None:
    """Single-process durable queue consumer; interrupted work is re-queued on startup."""
    with session_scope() as db:
        for job in db.scalars(select(Job).where(Job.status == "RUNNING")).all():
            run = db.get(Run, job.run_id)
            if run and run.status in {"SUCCEEDED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}:
                job.status = "SUCCEEDED" if run.status in {"SUCCEEDED", "PARTIAL_FAILED"} else run.status
                continue
            job.status = "QUEUED"
            if run and run.kind == "GENERATION":
                for task in db.scalars(select(PageTask).where(PageTask.run_id == run.id, PageTask.status == "RUNNING")).all():
                    task.status = "PENDING"
            elif run and run.kind == "DIRECTOR":
                run.status = "QUEUED"
    while not stop.is_set():
        job_id = run_id = presentation_id = kind = None
        with session_scope() as db:
            job = db.scalar(select(Job).where(Job.status == "QUEUED").order_by(Job.created_at).limit(1))
            if job:
                job.status = "RUNNING"
                run_id, presentation_id, kind = job.run_id, job.presentation_id, job.kind
                run = db.get(Run, run_id)
                if run: run.status = "RUNNING"
        if run_id:
            try:
                active = asyncio.create_task(run_director(presentation_id, run_id) if kind == "DIRECTOR" else run_generation(presentation_id, run_id))
                ACTIVE_RUNS[run_id] = active
                await active
            except asyncio.CancelledError:
                if stop.is_set():
                    raise
                with session_scope() as db:
                    run, presentation = db.get(Run, run_id), db.get(Presentation, presentation_id)
                    if run: run.status = "CANCELLED"
                    if presentation: presentation.status = "CANCELLED"
                    for page in db.scalars(select(PageTask).where(PageTask.run_id == run_id, PageTask.status.in_(["RUNNING", "PENDING"]))).all():
                        page.status = "CANCELLED"
                await emit_event(presentation_id, "run.cancelled", {"run_id": run_id, "status": "CANCELLED"}, run_id)
            finally:
                ACTIVE_RUNS.pop(run_id, None)
                with session_scope() as db:
                    job = db.scalar(select(Job).where(Job.run_id == run_id))
                    run = db.get(Run, run_id)
                    if job and run and run.status in {"SUCCEEDED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}:
                        job.status = "SUCCEEDED" if run.status in {"SUCCEEDED", "PARTIAL_FAILED"} else run.status
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

