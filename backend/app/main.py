from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError

from .db import BusinessRoute, Conversation, ConversationMessage, ConversationTurn, Event, GenerationSettingsRecord, IdempotencyRecord, Job, LlmCall, Outline, PageTask, PageScene, SceneConversionJob, Presentation, Run, dumps, init_db, legacy_reasoning_tier, loads, resolve_default, session_scope, utcnow
from .provider_management import ConfigurationError, resolve_workflow_routes, resolve_business_route, route_issues
from .workflows import ENTRY_WORKFLOWS, WORKFLOWS, scheduled_model_id
from .provider_api import CredentialPut, ModelPatch, ProviderPatch, patch_model, patch_provider, replace_credential, router as provider_management_router, test_model
from .rendering import html_hash
from .runtime import ACTIVE_RUNS, EVENT_BUS, canonical_hash, emit_event, finalize_cancelled_run, scheduler, sync_conversation_run
from .schemas import ConfirmRequest, CreatePresentationRequest, GenerationSettingsUpdate, OutlineDocument, ProviderUpdate, RetryRequest, UpdateOutlineRequest
from .secrets import read_secret
from .scene_api import router as scene_router
from .scene_runtime import HEALTH as SCENE_HEALTH, scene_scheduler
from .scene_service import summary as scene_summary, cancel_conversions, source_hash as page_source_hash
from .export_api import router as export_router
from . import export_service
from .db import PptxExport


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler(stop))
    scene_task = asyncio.create_task(scene_scheduler(stop))
    try:
        yield
    finally:
        stop.set()
        scene_task.cancel()
        try:
            await asyncio.wait_for(task, timeout=3)
        except asyncio.TimeoutError:
            task.cancel()
        finally:
            await asyncio.gather(task, scene_task, return_exceptions=True)


app = FastAPI(title="Lumina Draft API", version="0.1.0", lifespan=lifespan)
app.include_router(scene_router)
app.include_router(export_router)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Idempotency-Key", "Last-Event-ID"])
API = "/api/v1"
TICKETS: dict[str, tuple[str, float, str | None]] = {}


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Pydantic's default error includes submitted values, including write-only keys.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
        for item in exc.errors()
    ]})


@app.exception_handler(StaleDataError)
async def stale_configuration(request: Request, exc: StaleDataError):
    return JSONResponse(status_code=409, content={"detail": {
        "code": "CONFIG_REVISION_CONFLICT", "message": "配置已被其他操作更新，请刷新后重试",
    }})


@app.middleware("http")
async def local_only(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and origin not in {"http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8000", "http://localhost:8000"}:
        return Response("Origin not allowed", status_code=403)
    host = request.headers.get("host", "").split(":")[0]
    if host not in {"localhost", "127.0.0.1", "testserver"}:
        return Response("Host not allowed", status_code=403)
    return await call_next(request)


def fail(code: str, message: str, status: int = 409):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def presentation_dict(p: Presentation) -> dict:
    options = loads(p.options_json)
    width, height = WORKFLOWS[p.workflow_code].canvas(options)
    return {"id": p.id, "user_request": p.user_request, "options": options, "canvas": {"width": width, "height": height}, "status": p.status, "entry_mode": p.entry_mode, "workflow_code": p.workflow_code, "conversation_id": p.conversation_id, "created_at": p.created_at.isoformat(), "updated_at": p.updated_at.isoformat()}


def message_dict(message: ConversationMessage) -> dict:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "turn_id": message.turn_id,
        "seq": message.seq,
        "role": message.role,
        "kind": message.kind,
        "status": message.status,
        "body_text": message.body_text,
        "payload": loads(message.payload_json),
        "payload_schema_version": message.payload_schema_version,
        "created_at": message.created_at.isoformat(),
        "updated_at": message.updated_at.isoformat(),
    }


def outline_dict(o: Outline | None) -> dict | None:
    return None if o is None else {"id": o.id, "revision": o.revision, "content_hash": o.content_hash, "confirmed": o.confirmed, "document": loads(o.document_json)}


def run_dict(r: Run | None) -> dict | None:
    return None if r is None else {"id": r.id, "kind": r.kind, "status": r.status, "error": r.error, "error_code": r.error_code, "created_at": r.created_at.isoformat()}


def job_dict(job: Job | None) -> dict | None:
    if not job:
        return None
    return {
        "id": job.id,
        "status": job.status,
        "queue_reason": job.queue_reason,
        "queue_duration_ms": job.queue_duration_ms,
        "queued_at": job.queued_at.isoformat() if job.queued_at else None,
        "error_code": job.error_code,
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def page_dict(p: PageTask, db) -> dict:
    return {"id": p.id, "run_id": p.run_id, "slide_id": p.slide_id, "page_number": p.page_number, "title": p.title, "status": p.status, "html_hash": page_source_hash(db, p), "error": p.error, "error_code": getattr(p, "error_code", None), "attempt": p.attempt, "scene": scene_summary(db, p)}


def conversation_projection(db, presentation: Presentation, outline: Outline | None, runs: list[Run]) -> dict:
    conversation = db.get(Conversation, presentation.conversation_id) if getattr(presentation, "conversation_id", None) else None
    messages = []
    if conversation:
        messages = [message_dict(item) for item in db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.seq)
        ).all()]
    confirmed = bool(outline and outline.confirmed)
    has_generation = any(run.kind == "GENERATION" for run in runs)
    status = presentation.status
    return {
        "id": conversation.id if conversation else None,
        "entry_mode": getattr(presentation, "entry_mode", "ppt"),
        "interaction_mode": conversation.interaction_mode if conversation else "single_turn",
        "state": conversation.state if conversation else "ACTIVE",
        "messages": messages,
        "allowed_actions": {
            "send_message": False,
            "edit_outline": status == "WAITING_CONFIRMATION",
            "confirm_outline": status == "WAITING_CONFIRMATION" and not confirmed,
            "cancel_run": status in {"PLANNING", "GENERATING"},
            "retry_run": status in {"FAILED", "PARTIAL_FAILED", "CANCELLED"},
        },
        "allowed_views": {
            "outline": True,
            "generation": confirmed and has_generation,
        },
    }


def provider_dict(route, model, binding, endpoint, owner) -> dict:
    return {"id": owner.id, "code": owner.code, "name": owner.name, "protocol": endpoint.protocol, "base_url": endpoint.base_url, "model": binding.remote_model_id, "enabled": owner.enabled and endpoint.enabled and model.enabled and binding.enabled, "revision": endpoint.revision, "credential_configured": bool(endpoint.credential_ref), "business_route": route.code, "logical_model_id": model.id, "binding_id": binding.id, "endpoint_id": endpoint.id}


@app.get(f"{API}/health/live")
@app.get(f"{API}/health/ready")
def health():
    return {"status": "ok", "scene_worker": dict(SCENE_HEALTH)}


@app.get(f"{API}/settings/llm/default")
def get_provider():
    with session_scope() as db:
        return provider_dict(*resolve_default(db))


@app.get(f"{API}/settings/generation")
def get_generation_settings():
    with session_scope() as db:
        setting = db.get(GenerationSettingsRecord, "default")
        return {"reasoning_effort": setting.reasoning_effort, "revision": setting.revision}


@app.put(f"{API}/settings/generation")
def update_generation_settings(body: GenerationSettingsUpdate):
    with session_scope() as db:
        setting = db.get(GenerationSettingsRecord, "default")
        setting.reasoning_effort = body.reasoning_effort
        setting.revision += 1
        route = db.get(BusinessRoute, "default")
        if route:
            route.reasoning_tier = legacy_reasoning_tier(db, route.model_id, body.reasoning_effort)
            route.revision += 1
            db.flush()
            for code in ("default", "ppt_outline", "ppt_html"):
                try:
                    resolve_business_route(db, code)
                except ConfigurationError as exc:
                    fail(exc.code, str(exc), exc.status_code)
        db.flush()
        return {"reasoning_effort": setting.reasoning_effort, "revision": setting.revision}


@app.put(f"{API}/settings/llm/default", deprecated=True)
def update_provider(body: ProviderUpdate):
    """Compatibility facade; all writes use the versioned management commands."""
    with session_scope() as db:
        _, model, _, endpoint, owner = resolve_default(db)
        owner_id, model_id, revision = owner.id, model.id, owner.revision
        fields = ModelPatch(
            expected_revision=model.revision, remote_model_id=body.model,
            display_name=body.name, context_window_tokens=model.context_window_tokens,
            max_output_tokens=model.max_output_tokens,
            model_concurrency=model.max_concurrency,
            enabled=body.enabled,
        )
        provider_fields = ProviderPatch(
            expected_revision=revision, name=body.name, protocol="openai_responses",
            base_url=body.base_url, auth_mode="bearer" if body.api_key else endpoint.auth_mode,
            allow_private_http=endpoint.allow_private_http,
            enabled=body.enabled,
        )
    updated = patch_provider(owner_id, provider_fields)
    if body.api_key:
        replace_credential(owner_id, CredentialPut(
            expected_revision=updated["revision"], api_key=body.api_key,
        ))
    patch_model(model_id, fields)
    with session_scope() as db:
        return provider_dict(*resolve_default(db))


@app.post(f"{API}/settings/llm/default/test", deprecated=True)
async def test_provider():
    with session_scope() as db:
        _, model, _, _, _ = resolve_default(db)
        model_id = model.id
    result = await test_model(model_id)
    passed = result["status"] == "passed"
    return {"status": "SUCCEEDED" if passed else "FAILED",
            "message": "连接成功" if passed else "模型未通过文本流或结构化输出检查"}


@app.get(f"{API}/presentations")
def list_presentations():
    with session_scope() as db:
        return {"items": [presentation_dict(p) for p in db.scalars(select(Presentation).order_by(Presentation.updated_at.desc()).limit(100)).all()]}


@app.get(f"{API}/conversations/{{conversation_id}}")
def get_conversation(conversation_id: str):
    """Read the durable conversation projection independently from a workbench view."""
    with session_scope() as db:
        conversation = db.get(Conversation, conversation_id)
        if not conversation:
            fail("NOT_FOUND", "会话不存在", 404)
        turns = db.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.conversation_id == conversation_id)
            .order_by(ConversationTurn.turn_no)
        ).all()
        messages = db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.seq)
        ).all()
        return {
            "id": conversation.id,
            "entry_mode": conversation.entry_mode,
            "interaction_mode": conversation.interaction_mode,
            "state": conversation.state,
            "latest_turn_no": conversation.latest_turn_no,
            "last_message_seq": conversation.last_message_seq,
            "turns": [
                {
                    "id": turn.id,
                    "turn_no": turn.turn_no,
                    "presentation_id": turn.presentation_id,
                    "workflow_code": turn.workflow_code,
                    "state": turn.state,
                    "active_run_id": turn.active_run_id,
                    "created_at": turn.created_at.isoformat(),
                    "completed_at": turn.completed_at.isoformat() if turn.completed_at else None,
                }
                for turn in turns
            ],
            "messages": [message_dict(message) for message in messages],
            "created_at": conversation.created_at.isoformat(),
            "updated_at": conversation.updated_at.isoformat(),
        }


@app.post(f"{API}/presentations", status_code=202)
async def create_presentation(body: CreatePresentationRequest, idempotency_key: str | None = Header(default=None, max_length=200)):
    request_hash = canonical_hash(body.model_dump())
    workflow = ENTRY_WORKFLOWS.get(body.entry_mode)
    if not workflow:
        fail("WORKFLOW_NOT_AVAILABLE", "图片生成尚未开放", 409)
    with session_scope() as db:
        if idempotency_key:
            prior = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == "presentations.create", IdempotencyRecord.key == idempotency_key))
            if prior:
                if prior.request_hash != request_hash: fail("IDEMPOTENCY_CONFLICT", "幂等键已用于其他请求")
                return loads(prior.response_json)
        try:
            resolved_routes = resolve_workflow_routes(db, workflow)
        except ConfigurationError as exc:
            fail(exc.code, str(exc), exc.status_code)
        route, model, binding, endpoint, owner = resolve_default(db)
        for config in resolved_routes.values():
            issues = route_issues(db, config)
            if issues:
                fail("CONFIG_NOT_READY", config["business_code"] + "：" + "；".join(issues))
            if config["auth_mode"] == "bearer":
                try:
                    if not read_secret(config["credential_ref"]):
                        fail("CONFIG_NOT_READY", "请先配置业务 Provider 的 API Key")
                except HTTPException:
                    raise
                except Exception:
                    fail("CREDENTIAL_STORE_UNAVAILABLE", "系统凭据存储暂不可用", 503)
        generation_settings = db.get(GenerationSettingsRecord, "default")
        snapshot = {
            "business_code": route.code,
            "route_revision": route.revision,
            "model_id": model.id,
            "model_revision": model.revision,
            "binding_id": binding.id,
            "binding_revision": binding.revision,
            "endpoint_id": endpoint.id,
            "endpoint_revision": endpoint.revision,
            "provider_id": owner.id,
            "base_url": endpoint.base_url,
            "model": binding.remote_model_id,
            "credential_ref": endpoint.credential_ref,
            "protocol": endpoint.protocol,
            "reasoning_effort": generation_settings.reasoning_effort,
            "resolved_routes": resolved_routes,
        }
        conversation = Conversation(entry_mode=body.entry_mode, interaction_mode="single_turn", state="ACTIVE")
        db.add(conversation)
        db.flush()
        presentation = Presentation(
            user_request=body.user_request,
            options_json=dumps(body.options.model_dump(exclude_none=True)),
            status="PLANNING",
            config_snapshot_json=dumps(snapshot),
            conversation_id=conversation.id,
            entry_mode=body.entry_mode,
            workflow_code=workflow.code,
        )
        db.add(presentation)
        db.flush()
        run = Run(presentation_id=presentation.id, kind="DIRECTOR", status="QUEUED")
        db.add(run)
        db.flush()
        db.add(Job(
            run_id=run.id,
            presentation_id=presentation.id,
            kind="DIRECTOR",
            model_id=scheduled_model_id(snapshot, "DIRECTOR", workflow.code),
            status="QUEUED",
        ))
        turn = ConversationTurn(
            conversation_id=conversation.id,
            turn_no=1,
            presentation_id=presentation.id,
            workflow_code=workflow.code,
            state="QUEUED",
            active_run_id=run.id,
        )
        db.add(turn)
        db.flush()
        user_message = ConversationMessage(
            conversation_id=conversation.id,
            turn_id=turn.id,
            seq=1,
            role="user",
            kind="text",
            status="READY",
            body_text=body.user_request,
            payload_json=dumps({"entry_mode": body.entry_mode, "language": body.options.language}),
        )
        activity_message = ConversationMessage(
            conversation_id=conversation.id,
            turn_id=turn.id,
            seq=2,
            role="assistant",
            kind="activity",
            status="STREAMING",
            body_text=None,
            payload_json=dumps({"stage_code": "director", "label": "正在理解需求并整理大纲", "run_id": run.id}),
        )
        db.add_all([user_message, activity_message])
        db.flush()
        turn.input_message_id = user_message.id
        conversation.last_message_seq = 2
        conversation.latest_turn_no = 1
        pid, rid, cid, tid = presentation.id, run.id, conversation.id, turn.id
        response = {"presentation_id": pid, "conversation_id": cid, "turn_id": tid, "run_id": rid, "entry_mode": body.entry_mode, "status": "PLANNING"}
        width, height = workflow.canvas(body.options.model_dump())
        response.update(workflow_code=workflow.code, canvas={"width": width, "height": height})
        if idempotency_key:
            db.add(IdempotencyRecord(scope="presentations.create", key=idempotency_key, request_hash=request_hash, response_json=dumps(response)))
    await emit_event(pid, "presentation.created", {"presentation_id": pid, "run_id": rid}, rid)
    await emit_event(pid, "job.waiting_scheduler", {"run_id": rid, "queue_reason": "SCHEDULER"}, rid)
    return response


@app.get(f"{API}/presentations/{{presentation_id}}")
def get_presentation(presentation_id: str):
    with session_scope() as db:
        p = db.get(Presentation, presentation_id)
        if not p: fail("NOT_FOUND", "作品不存在", 404)
        outline = db.scalar(select(Outline).where(Outline.presentation_id == p.id).order_by(Outline.revision.desc()))
        runs = db.scalars(select(Run).where(Run.presentation_id == p.id).order_by(Run.created_at.desc())).all()
        pages = db.scalars(select(PageTask).where(PageTask.presentation_id == p.id).order_by(PageTask.page_number)).all()
        active_run = next((r for r in runs if r.status in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}), None)
        active_job = db.scalar(select(Job).where(Job.run_id == active_run.id)) if active_run else None
        latest_job = db.scalar(select(Job).where(Job.run_id == runs[0].id)) if runs else None
        last_seq = db.scalar(select(Event.seq).where(Event.presentation_id == p.id).order_by(Event.seq.desc()).limit(1)) or 0
        running_ids = {page.id for page in pages if page.status in {"RUNNING", "VALIDATING"}}
        streams: dict[str, str] = {}
        if running_ids:
            stream_events = db.scalars(select(Event).where(Event.presentation_id == p.id, Event.seq <= last_seq, Event.type.in_(["page.started", "page.html.delta", "page.retrying"])).order_by(Event.seq)).all()
            for event in stream_events:
                payload = loads(event.payload_json)
                task_id = payload.get("page_task_id")
                if task_id not in running_ids:
                    continue
                if event.type in {"page.started", "page.retrying"}:
                    streams[task_id] = ""
                else:
                    streams[task_id] = streams.get(task_id, "") + payload.get("delta", "")
        director_stream = {"reasoning": "", "output": ""}
        director_run = next((r for r in runs if r.kind == "DIRECTOR"), None)
        stream_run = active_run if active_run and active_run.kind == "DIRECTOR" else director_run
        if stream_run:
            director_events = db.scalars(select(Event).where(Event.presentation_id == p.id, Event.run_id == stream_run.id, Event.seq <= last_seq, Event.type.in_(["director.stream.reset", "director.stream.delta"])).order_by(Event.seq)).all()
            for event in director_events:
                payload = loads(event.payload_json)
                if event.type == "director.stream.reset":
                    director_stream = {"reasoning": "", "output": ""}
                    continue
                channel = payload.get("channel")
                if channel in director_stream:
                    director_stream[channel] += payload.get("delta", "")
        conversation = conversation_projection(db, p, outline, runs)
        export_source = export_service.source_summary(db, p.id)
        latest_export = db.scalar(select(PptxExport).where(PptxExport.presentation_id == p.id, PptxExport.request_hash == export_service.request_identity(p.id, export_source["source_hash"])).order_by(PptxExport.created_at.desc()).limit(1)) if export_source["can_export"] else None
        return {"presentation": presentation_dict(p), "conversation": conversation, "latest_outline": outline_dict(outline), "active_run": run_dict(active_run), "active_job": job_dict(active_job), "latest_run": run_dict(runs[0]) if runs else None, "latest_job": job_dict(latest_job), "pages": [page_dict(x, db) for x in pages], "active_streams": streams, "director_stream": director_stream if stream_run else None, "last_event_seq": last_seq, "export_source": export_source, "latest_export": export_service.export_dict(latest_export) if latest_export else None}


@app.get(f"{API}/presentations/{{presentation_id}}/usage")
def presentation_usage(presentation_id: str):
    with session_scope() as db:
        if not db.get(Presentation, presentation_id):
            fail("NOT_FOUND", "作品不存在", 404)
        count, input_sum, output_sum, total_sum, input_count, output_count, total_count = db.execute(
            select(
                func.count(LlmCall.id),
                func.sum(LlmCall.input_tokens), func.sum(LlmCall.output_tokens), func.sum(LlmCall.total_tokens),
                func.count(LlmCall.input_tokens), func.count(LlmCall.output_tokens), func.count(LlmCall.total_tokens),
            ).where(LlmCall.presentation_id == presentation_id, LlmCall.purpose.in_(["director", "page_composer"]))
        ).one()
        return {
            "presentation_id": presentation_id,
            "call_count": count,
            "input_tokens": input_sum,
            "output_tokens": output_sum,
            "total_tokens": total_sum,
            "input_unreported_calls": count - input_count,
            "output_unreported_calls": count - output_count,
            "total_unreported_calls": count - total_count,
        }


@app.put(f"{API}/presentations/{{presentation_id}}/outline")
async def update_outline(presentation_id: str, body: UpdateOutlineRequest):
    document = body.document.model_dump()
    with session_scope() as db:
        p = db.get(Presentation, presentation_id)
        if not p: fail("NOT_FOUND", "作品不存在", 404)
        old = db.scalar(select(Outline).where(Outline.presentation_id == p.id).order_by(Outline.revision.desc()))
        if p.status != "WAITING_CONFIRMATION": fail("INVALID_STATE", "当前不能编辑说明书")
        if not old or old.id != body.outline_id or old.content_hash != body.content_hash: fail("OUTLINE_VERSION_CONFLICT", "说明书已更新，请刷新")
        before = loads(old.document_json)
        if [(x["slide_id"], x["page_number"]) for x in before["slides"]] != [(x["slide_id"], x["page_number"]) for x in document["slides"]]:
            fail("INVALID_SLIDES", "不能增删页面或修改页面顺序", 422)
        outline = Outline(presentation_id=p.id, revision=old.revision+1, content_hash=canonical_hash(document), document_json=dumps(document))
        db.add(outline)
        db.flush()
        if p.conversation_id:
            card = db.scalar(select(ConversationMessage).where(
                ConversationMessage.conversation_id == p.conversation_id,
                ConversationMessage.kind == "outline_card",
            ).order_by(ConversationMessage.seq.desc()))
            if card:
                card.payload_json = dumps({"outline_id": outline.id, "revision": outline.revision, "content_hash": outline.content_hash, "status": "READY"})
        result = outline_dict(outline)
    await emit_event(presentation_id, "outline.updated", {"outline_id": result["id"], "content_hash": result["content_hash"]})
    return result


@app.post(f"{API}/presentations/{{presentation_id}}/confirm", status_code=202)
async def confirm_presentation(presentation_id: str, body: ConfirmRequest):
    with session_scope() as db:
        p = db.get(Presentation, presentation_id)
        if not p: fail("NOT_FOUND", "作品不存在", 404)
        outline = db.scalar(select(Outline).where(Outline.presentation_id == p.id).order_by(Outline.revision.desc()))
        if outline and outline.confirmed and outline.id == body.outline_id and outline.content_hash == body.content_hash:
            run = db.scalar(select(Run).where(Run.presentation_id == p.id, Run.kind == "GENERATION").order_by(Run.created_at.desc()))
            return {"presentation_id": p.id, "run_id": run.id, "status": p.status}
        if p.status != "WAITING_CONFIRMATION": fail("INVALID_STATE", "当前不能确认说明书")
        if not outline or outline.id != body.outline_id or outline.content_hash != body.content_hash: fail("OUTLINE_VERSION_CONFLICT", "说明书已更新，请刷新")
        document = loads(outline.document_json)
        outline.confirmed, p.status = True, "GENERATING"
        run = Run(presentation_id=p.id, kind="GENERATION", status="QUEUED")
        db.add(run)
        db.flush()
        for slide in document["slides"]:
            db.add(PageTask(presentation_id=p.id, run_id=run.id, slide_id=slide["slide_id"], page_number=slide["page_number"], title=slide["title"], brief_json=dumps(slide)))
        snapshot = loads(p.config_snapshot_json)
        db.add(Job(
            run_id=run.id,
            presentation_id=p.id,
            kind="GENERATION",
            model_id=scheduled_model_id(snapshot, "GENERATION", p.workflow_code),
            status="QUEUED",
        ))
        if p.conversation_id:
            conversation = db.get(Conversation, p.conversation_id)
            turn = db.scalar(select(ConversationTurn).where(ConversationTurn.presentation_id == p.id).order_by(ConversationTurn.turn_no.desc()))
            if conversation and turn:
                card = db.scalar(select(ConversationMessage).where(
                    ConversationMessage.conversation_id == conversation.id,
                    ConversationMessage.kind == "outline_card",
                ).order_by(ConversationMessage.seq.desc()))
                if card:
                    card.payload_json = dumps({"outline_id": outline.id, "revision": outline.revision, "content_hash": outline.content_hash, "status": "CONFIRMED"})
                next_seq = conversation.last_message_seq + 1
                db.add(ConversationMessage(
                    conversation_id=conversation.id,
                    turn_id=turn.id,
                    seq=next_seq,
                    role="user",
                    kind="action",
                    status="READY",
                    payload_json=dumps({"action": "confirm_outline", "outline_id": outline.id, "revision": outline.revision, "content_hash": outline.content_hash}),
                ))
                db.add(ConversationMessage(
                    conversation_id=conversation.id,
                    turn_id=turn.id,
                    seq=next_seq + 1,
                    role="assistant",
                    kind="generation_card",
                    status="STREAMING",
                    payload_json=dumps({"run_id": run.id, "outline_id": outline.id, "page_count": len(document["slides"]), "status": "QUEUED"}),
                ))
                conversation.last_message_seq = next_seq + 1
                turn.active_run_id = run.id
                turn.state = "GENERATING"
                turn.completed_at = None
        rid, count = run.id, len(document["slides"])
    await emit_event(presentation_id, "generation.queued", {"run_id": rid, "page_count": count}, rid)
    await emit_event(presentation_id, "job.waiting_scheduler", {"run_id": rid, "queue_reason": "SCHEDULER"}, rid)
    return {"presentation_id": presentation_id, "run_id": rid, "status": "GENERATING", "page_count": count}


@app.post(f"{API}/presentations/{{presentation_id}}/retry", status_code=202)
async def retry_presentation(presentation_id: str):
    with session_scope() as db:
        p = db.get(Presentation, presentation_id)
        if not p: fail("NOT_FOUND", "作品不存在", 404)
        previous = db.scalar(select(Run).where(Run.presentation_id == p.id).order_by(Run.created_at.desc()))
        if p.status in {"FAILED", "CANCELLED"} and previous.kind == "DIRECTOR":
            run = Run(presentation_id=p.id, kind="DIRECTOR", status="QUEUED")
            p.status = "PLANNING"
            db.add(run)
            db.flush()
            db.add(Job(
                run_id=run.id,
                presentation_id=p.id,
                kind="DIRECTOR",
                model_id=scheduled_model_id(loads(p.config_snapshot_json), run.kind, p.workflow_code),
            ))
            retry_kind = "activity"
        elif p.status in {"PARTIAL_FAILED", "CANCELLED"} and previous.kind == "GENERATION":
            run = Run(presentation_id=p.id, kind="GENERATION", status="QUEUED")
            db.add(run)
            db.flush()
            for task in db.scalars(select(PageTask).where(PageTask.presentation_id == p.id)).all():
                task.run_id = run.id
                if task.status != "SUCCEEDED": task.status, task.error = "PENDING", None
            p.status = "GENERATING"
            db.add(Job(
                run_id=run.id,
                presentation_id=p.id,
                kind="GENERATION",
                model_id=scheduled_model_id(loads(p.config_snapshot_json), run.kind, p.workflow_code),
            ))
            retry_kind = "generation_card"
        else:
            fail("INVALID_STATE", "当前没有可以重试的任务")
        if p.conversation_id:
            conversation = db.get(Conversation, p.conversation_id)
            turn = db.scalar(select(ConversationTurn).where(ConversationTurn.presentation_id == p.id).order_by(ConversationTurn.turn_no.desc()))
            if conversation and turn:
                seq = conversation.last_message_seq + 1
                db.add(ConversationMessage(
                    conversation_id=conversation.id,
                    turn_id=turn.id,
                    seq=seq,
                    role="user",
                    kind="action",
                    status="READY",
                    payload_json=dumps({"action": "retry_run", "previous_run_id": previous.id, "run_id": run.id}),
                ))
                payload = {"run_id": run.id, "status": "QUEUED", "stage_code": "director", "label": "正在重试大纲生成"}
                if retry_kind == "generation_card":
                    payload = {"run_id": run.id, "status": "QUEUED", "page_count": db.query(PageTask).filter(PageTask.presentation_id == p.id).count()}
                db.add(ConversationMessage(
                    conversation_id=conversation.id,
                    turn_id=turn.id,
                    seq=seq + 1,
                    role="assistant",
                    kind=retry_kind,
                    status="STREAMING",
                    payload_json=dumps(payload),
                ))
                conversation.last_message_seq = seq + 1
                turn.active_run_id = run.id
                turn.state = "PLANNING" if retry_kind == "activity" else "GENERATING"
                turn.completed_at = None
        rid, kind = run.id, run.kind
    await emit_event(presentation_id, "run.queued", {"run_id": rid, "kind": kind}, rid)
    await emit_event(presentation_id, "job.waiting_scheduler", {"run_id": rid, "queue_reason": "SCHEDULER"}, rid)
    return {"run_id": rid, "status": "QUEUED"}


@app.post(f"{API}/runs/{{run_id}}/cancel", status_code=202)
async def cancel_run(run_id: str):
    with session_scope() as db:
        run = db.get(Run, run_id)
        if not run: fail("NOT_FOUND", "任务不存在", 404)
        if run.status not in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
            return {"run_id": run_id, "status": run.status}
        presentation_id = run.presentation_id
        job = db.scalar(select(Job).where(Job.run_id == run_id))
        queued = run.status == "QUEUED"
        already_requested = run.status == "CANCEL_REQUESTED"
        if queued:
            run.status = "CANCELLED"
            if job:
                job.status, job.finished_at = "CANCELLED", utcnow()
            p = db.get(Presentation, presentation_id)
            if p: p.status = "CANCELLED"
            for page in db.scalars(select(PageTask).where(PageTask.run_id == run_id, PageTask.status == "PENDING")).all():
                page.status, page.error_code, page.error = "CANCELLED", "PAGE_CANCELLED", "页面生成已取消"
        elif not already_requested:
            run.status = "CANCEL_REQUESTED"
    await cancel_conversions(run_id)
    active = ACTIVE_RUNS.get(run_id)
    if active:
        active.cancel()

    if active and not queued:
        if not already_requested:
            await emit_event(presentation_id, "run.cancel_requested", {"run_id": run_id}, run_id)
        return {"run_id": run_id, "status": "CANCEL_REQUESTED"}

    if not queued:
        await finalize_cancelled_run(run_id)
        with session_scope() as db:
            run = db.get(Run, run_id)
            status = run.status if run else "CANCELLED"
        return {"run_id": run_id, "status": status}

    sync_conversation_run(run_id, "CANCELLED")
    await emit_event(presentation_id, "run.cancelled", {"run_id": run_id, "status": "CANCELLED"}, run_id)
    return {"run_id": run_id, "status": "CANCELLED"}


@app.get(f"{API}/page-tasks/{{task_id}}/html")
def page_html(task_id: str):
    return _page_html_response(task_id)


def _page_html_response(task_id: str, *, include_tailwind: bool = False, scene_id: str | None = None):
    with session_scope() as db:
        page = db.get(PageTask, task_id)
        if not page: fail("NOT_FOUND", "页面不存在", 404)
        if page.status != "SUCCEEDED" or not page.html: fail("PAGE_NOT_READY", "页面尚未完成")
        html = page.html
        css = None
        if scene_id:
            scene = db.get(PageScene, scene_id)
            job = db.get(SceneConversionJob, scene.job_id) if scene and scene.page_task_id == task_id else None
            if not job: fail("SCENE_NOT_FOUND", "对应场景不存在", 404)
            html, css = job.source_html, job.css
    if include_tailwind:
        from .tailwind_profile import preview_html
        html = preview_html(html, css=css)
    return HTMLResponse(html, headers={"Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self' http://127.0.0.1:5173 http://localhost:5173", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.post(f"{API}/page-tasks/{{task_id}}/preview-tickets")
def create_ticket(task_id: str, scene_id: str | None = None):
    with session_scope() as db:
        page = db.get(PageTask, task_id)
        if not page or page.status != "SUCCEEDED": fail("PAGE_NOT_READY", "页面尚未完成")
        digest = page.html_hash
        if scene_id:
            scene = db.get(PageScene, scene_id)
            if not scene or scene.page_task_id != task_id: fail("SCENE_NOT_FOUND", "对应场景不存在", 404)
    ticket = secrets.token_urlsafe(32)
    TICKETS[ticket] = (task_id, time.time() + 120, scene_id)
    return {"preview_url": f"{API}/previews/{task_id}?ticket={ticket}", "html_hash": digest}


@app.get(f"{API}/previews/{{task_id}}")
def preview(task_id: str, ticket: str):
    entry = TICKETS.get(ticket)
    if not entry or entry[0] != task_id or entry[1] < time.time():
        TICKETS.pop(ticket, None)
        fail("INVALID_TICKET", "预览链接已过期", 403)
    return _page_html_response(task_id, include_tailwind=True, scene_id=entry[2])


@app.get(f"{API}/presentations/{{presentation_id}}/events")
async def events(presentation_id: str, request: Request, after: int = 0, last_event_id: str | None = Header(default=None)):
    if last_event_id:
        try: after = max(after, int(last_event_id))
        except ValueError: fail("INVALID_CURSOR", "事件游标无效", 400)
    with session_scope() as db:
        if not db.get(Presentation, presentation_id): fail("NOT_FOUND", "作品不存在", 404)
    async def stream():
        cursor = after
        while not await request.is_disconnected():
            with session_scope() as db:
                rows = db.scalars(select(Event).where(Event.presentation_id == presentation_id, Event.seq > cursor).order_by(Event.seq).limit(200)).all()
                batch = [{"seq": e.seq, "type": e.type, "run_id": e.run_id, "timestamp": e.created_at.isoformat(), "payload": loads(e.payload_json)} for e in rows]
            for event in batch:
                cursor = event["seq"]
                envelope = {"protocol_version": "1", "presentation_id": presentation_id, **event}
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"
            if batch: continue
            yield ": heartbeat\n\n"
            condition = EVENT_BUS.condition(presentation_id)
            async with condition:
                try: await asyncio.wait_for(condition.wait(), timeout=3)
                except asyncio.TimeoutError: pass
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.include_router(provider_management_router)
