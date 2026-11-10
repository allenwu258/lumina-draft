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
from .db import ApiCredential, ApiEndpoint, ApiProvider, Conversation, ConversationMessage, ConversationTurn, Event, Job, LlmCall, LogicalModel, ModelBinding, Outline, PageTask, Presentation, ProviderTest, Run, dumps, loads, session_scope, utcnow
from .prompts import COMPOSER_PROMPT, DIRECTOR_PROMPT
from .providers import ProviderError, stream_responses
from .provider_management import MODEL_LIMITER, ConfigurationError, configuration_hash, resolve_business_route, resolve_model_config
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
        output = await ProviderGateway().complete_json(prompt, state["presentation_id"], "director", state["run_id"], "ppt_outline")
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
        html = await ProviderGateway().complete_html(prompt, task.presentation_id, task.id, "ppt_html", run_id=state["run_id"])
        return {"result": html}
    graph = StateGraph(PageState)
    graph.add_node("compose", compose)
    graph.add_edge(START, "compose")
    graph.add_edge("compose", END)
    return graph.compile()


class ProviderGateway:
    async def _stream(
        self,
        prompt: str,
        presentation_id: str | None,
        purpose: str,
        schema: dict | None = None,
        page_task_id: str | None = None,
        run_id: str | None = None,
        business_code: str = "default",
        resolved_config: dict[str, Any] | None = None,
        instructions: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        with session_scope() as db:
            presentation = db.get(Presentation, presentation_id) if presentation_id else None
            snapshot = loads(presentation.config_snapshot_json) if presentation else {}
            config = resolved_config or (snapshot.get("resolved_routes") or {}).get(business_code)
            if not config and snapshot.get("model_id"):
                try:
                    config = resolve_model_config(db, snapshot["model_id"], "off", business_code)
                except ConfigurationError as exc:
                    raise ProviderError("CONFIG_NOT_READY", "该作品的历史模型配置已不可用") from exc
                if config:
                    config = dict(config)
                    config.update({
                        "base_url": snapshot.get("base_url", config["base_url"]),
                        "remote_model_id": snapshot.get("model", config["remote_model_id"]),
                        "credential_ref": snapshot.get("credential_ref", config.get("credential_ref")),
                        "protocol": snapshot.get("protocol", config["protocol"]),
                        "reasoning_effort": request_reasoning_effort(snapshot),
                    })
                    legacy_credential = db.scalar(select(ApiCredential).where(
                        ApiCredential.endpoint_id == config["endpoint_id"],
                        ApiCredential.secret_ref == config.get("credential_ref"),
                    )) if config.get("credential_ref") else None
                    config["credential_id"] = legacy_credential.id if legacy_credential else None
                    config["credential_version"] = legacy_credential.version if legacy_credential else None
                    config["config_hash"] = configuration_hash({key: value for key, value in config.items() if key != "config_hash"})
            if not config:
                try:
                    config = resolve_business_route(db, business_code)
                except ConfigurationError as exc:
                    raise ProviderError(exc.code, str(exc)) from exc
            owner = db.get(ApiProvider, config.get("provider_id"))
            endpoint = db.get(ApiEndpoint, config.get("endpoint_id"))
            binding = db.get(ModelBinding, config.get("binding_id"))
            model_record = db.get(LogicalModel, config.get("model_id"))
            if not owner or not endpoint or not binding or not model_record:
                raise ProviderError("CONFIG_NOT_READY", "模型配置不存在")
            if not owner.enabled or owner.archived_at or not endpoint.enabled or not binding.enabled or not model_record.enabled or model_record.archived_at:
                raise ProviderError("CONFIG_NOT_READY", "Provider 或模型已停用")
            credential_id = config.get("credential_id")
            if credential_id:
                credential = db.get(ApiCredential, credential_id)
                if not credential or credential.status == "revoked":
                    raise ProviderError("CREDENTIAL_REVOKED", "该作品使用的 API 凭据已撤销")
            base_url = config.get("base_url", endpoint.base_url)
            model = config.get("remote_model_id", config.get("model", binding.remote_model_id))
            credential_ref = config.get("credential_ref", endpoint.credential_ref) or ""
            protocol = config.get("protocol", endpoint.protocol)
            if protocol != "openai_responses":
                raise ProviderError("UNSUPPORTED_PROTOCOL", "当前仅支持 OpenAI Responses 协议")
            call = LlmCall(
                presentation_id=presentation_id,
                purpose=purpose,
                model=model,
                status="QUEUED",
                queue_reason="MODEL_CAPACITY",
                provider_id=owner.id,
                model_id=model_record.id,
                business_code=business_code if purpose != "provider_test" else None,
                config_snapshot_json=dumps({key: value for key, value in config.items() if key != "api_key"}),
            )
            db.add(call)
            db.flush()
            call_id = call.id
            provider_id, model_id = owner.id, model_record.id
            auth_mode = config.get("auth_mode", endpoint.auth_mode)
            allow_private_http = config.get("allow_private_http", endpoint.allow_private_http)
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
        error_code: str | None = None
        response_id: str | None = None
        final_text: str | None = None
        started_at = time.monotonic()
        first_token_at: float | None = None
        admitted_at: float | None = None

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

        queue_duration_ms = 0
        queued_at = time.monotonic()
        try:
            if presentation_id:
                await emit_event(presentation_id, "llm.queued", {"call_id": call_id, "model_id": model_id, "queue_reason": "MODEL_CAPACITY"}, run_id)
            async with MODEL_LIMITER.acquire(model_id, run_id=run_id or purpose, timeout_seconds=settings.model_queue_timeout_seconds):
                admitted_at = time.monotonic()
                queue_duration_ms = int((time.monotonic() - queued_at) * 1000)
                # Configuration can be revoked or disabled while waiting for a slot.
                with session_scope() as db:
                    current_owner = db.get(ApiProvider, provider_id)
                    current_endpoint = db.get(ApiEndpoint, endpoint.id)
                    current_model = db.get(LogicalModel, model_id)
                    current_binding = db.get(ModelBinding, binding.id)
                    if not current_owner or not current_endpoint or not current_model or not current_binding or not all((
                        current_owner.enabled, current_endpoint.enabled, current_model.enabled, current_binding.enabled,
                    )) or current_owner.archived_at or current_model.archived_at:
                        raise ProviderError("CONFIG_NOT_READY", "Provider 或模型已停用")
                    if auth_mode == "bearer":
                        credential = db.get(ApiCredential, credential_id) if credential_id else None
                        if not credential or credential.status not in {"active", "retired"}:
                            raise ProviderError("CREDENTIAL_REVOKED", "该作品使用的 API 凭据已撤销")
                try:
                    key = read_secret(credential_ref) if auth_mode == "bearer" else ""
                except Exception as exc:
                    raise ProviderError("CREDENTIAL_STORE_UNAVAILABLE", "系统凭据存储暂不可用") from exc
                if auth_mode == "bearer" and not key:
                    raise ProviderError("CREDENTIAL_NOT_CONFIGURED", "请先配置 API 凭据")
                with session_scope() as db:
                    call = db.get(LlmCall, call_id)
                    call.status, call.queue_duration_ms, call.admitted_at = "RUNNING", queue_duration_ms, utcnow()
                    if page_task_id:
                        page = db.get(PageTask, page_task_id)
                        run = db.get(Run, run_id) if run_id else None
                        if not page or page.run_id != run_id or not run or run.status != "RUNNING":
                            raise asyncio.CancelledError()
                        page.status, page.attempt = "RUNNING", page.attempt + 1
                        page_number = page.page_number
                if page_task_id and presentation_id:
                    await emit_event(presentation_id, "page.started", {"page_task_id": page_task_id, "page_number": page_number}, run_id)
                if presentation_id:
                    await emit_event(presentation_id, "llm.started", {"call_id": call_id, "model_id": model_id, "queue_duration_ms": queue_duration_ms}, run_id)
                request_options = {
                    "base_url": base_url,
                    "api_key": key or "",
                    "model": model,
                    "instructions": instructions or (DIRECTOR_PROMPT if purpose == "director" else COMPOSER_PROMPT),
                    "prompt": prompt,
                    "json_schema": schema,
                    "reasoning_effort": config.get("reasoning_effort"),
                    "auth_mode": auth_mode,
                    "allow_private_http": allow_private_http,
                }
                if max_output_tokens is not None:
                    request_options["max_output_tokens"] = max_output_tokens
                async for event in stream_responses(
                    **request_options,
                ):
                    if event.kind == "text_delta":
                        if first_token_at is None:
                            first_token_at = time.monotonic()
                        output.append(event.data)
                        total_bytes += len(event.data.encode("utf-8"))
                        if total_bytes > 256 * 1024:
                            raise ProviderError("OUTPUT_TOO_LARGE", "模型输出超过大小限制")
                        if purpose == "director":
                            director_stream_buffer.append(("output", event.data))
                            if time.monotonic() - last_director_flush >= 0.12 or sum(len(value) for _, value in director_stream_buffer) >= 2048:
                                await flush_director_stream()
                        if page_task_id and presentation_id:
                            buffer.append(event.data)
                            if time.monotonic() - last_flush >= 0.15 or sum(map(len, buffer)) >= 16384:
                                await emit_page_delta(presentation_id, page_task_id, "".join(buffer), run_id)
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
                        status, error, error_code = "REFUSED", "模型拒绝了该请求", "MODEL_REFUSED"
                    elif event.kind == "completed":
                        status = "REFUSED" if refused else "SUCCEEDED"
                        usage = event.data.get("usage", {}) if isinstance(event.data, dict) else {}
                        response_id = event.data.get("response_id") if isinstance(event.data, dict) else None
                        final_text = event.data.get("text") if isinstance(event.data, dict) else None
                    elif event.kind in {"incomplete", "refused", "failed"}:
                        status = event.kind.upper()
                        error_code = "MODEL_" + event.kind.upper()
                        error = "模型未生成完整结果"
                        usage = event.data.get("usage", {}) if isinstance(event.data, dict) else {}
                        if event.kind == "incomplete" and isinstance(event.data, dict):
                            reason = event.data.get("reason")
                            if reason == "max_output_tokens":
                                error = "供应商因输出 token 上限截断了结果"
                            elif reason == "content_filter":
                                error = "供应商的内容过滤中断了生成"
                if page_task_id and presentation_id and status == "SUCCEEDED":
                    with session_scope() as db:
                        page = db.get(PageTask, page_task_id)
                        if page and page.run_id == run_id:
                            page.status = "VALIDATING"
                    await emit_event(presentation_id, "page.validating", {"page_task_id": page_task_id}, run_id)
        except asyncio.TimeoutError:
            status, error_code, error = "FAILED", "CALL_QUEUE_TIMEOUT", "等待模型调用名额超时"
            raise ProviderError(error_code, error)
        except asyncio.CancelledError:
            status, error_code, error = "CANCELLED", "CALL_CANCELLED", "调用已取消"
            raise
        except ProviderError as exc:
            error = str(exc)
            error_code = exc.code
            status = "REFUSED" if exc.code == "MODEL_REFUSED" else "FAILED"
            raise
        except ConfigurationError as exc:
            status, error_code, error = "FAILED", exc.code, str(exc)
            raise ProviderError(error_code, error) from exc
        except Exception as exc:
            status, error_code, error = "FAILED", "PROVIDER_CALL_FAILED", "模型调用失败"
            raise ProviderError(error_code, error) from exc
        finally:
            if admitted_at is None:
                queue_duration_ms = int((time.monotonic() - queued_at) * 1000)
            await flush_director_stream()
            if page_task_id and presentation_id and buffer:
                await emit_page_delta(presentation_id, page_task_id, "".join(buffer), run_id)
            with session_scope() as db:
                call = db.get(LlmCall, call_id)
                if call:
                    call.status = status
                    call.input_tokens = usage.get("input_tokens")
                    call.output_tokens = usage.get("output_tokens")
                    call.total_tokens = usage.get("total_tokens")
                    call.error = error
                    call.error_code = error_code
                    call.provider_response_id = response_id
                    call.queue_duration_ms = queue_duration_ms
                    call.time_to_first_token_ms = int((first_token_at - admitted_at) * 1000) if first_token_at and admitted_at else None
                    call.total_duration_ms = int((time.monotonic() - started_at) * 1000)
                    call.finished_at = utcnow()
            if presentation_id:
                await emit_event(presentation_id, "llm.usage.updated", {"call_id": call_id, "purpose": purpose})
                if error_code == "CALL_QUEUE_TIMEOUT":
                    await emit_event(presentation_id, "call.queue_timeout", {"call_id": call_id, "model_id": model_id, "queue_duration_ms": queue_duration_ms}, run_id)
        if status != "SUCCEEDED":
            raise ProviderError(error_code or "MODEL_INCOMPLETE", error or "模型未完成输出")
        return final_text if final_text is not None else "".join(output)

    async def complete_json(self, prompt: str, presentation_id: str, purpose: str, run_id: str | None = None, business_code: str = "ppt_outline") -> dict[str, Any]:
        schema = OutlineDocument.model_json_schema()
        text = await self._stream(prompt, presentation_id, purpose, schema, run_id=run_id, business_code=business_code)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("INVALID_MODEL_JSON", "Director 返回的 JSON 无法解析") from exc

    async def complete_html(self, prompt: str, presentation_id: str, page_task_id: str, business_code: str = "ppt_html", *, run_id: str | None = None) -> str:
        return await self._stream(prompt, presentation_id, "page_composer", None, page_task_id, run_id=run_id, business_code=business_code)


def append_event_tx(db, presentation_id: str, event_type: str, payload: dict[str, Any], run_id: str | None = None) -> None:
    """Caller holds EVENT_WRITE_LOCK; state and event commit together."""
    current = db.scalar(select(Event.seq).where(Event.presentation_id == presentation_id).order_by(Event.seq.desc()).limit(1)) or 0
    db.add(Event(presentation_id=presentation_id, seq=current + 1, run_id=run_id, type=event_type, payload_json=dumps(payload)))
    db.flush()


async def emit_event(presentation_id: str, event_type: str, payload: dict[str, Any], run_id: str | None = None) -> None:
    async with EVENT_WRITE_LOCK:
        with session_scope() as db:
            append_event_tx(db, presentation_id, event_type, payload, run_id)
    await EVENT_BUS.notify(presentation_id)


async def finalize_cancelled_run(run_id: str) -> bool:
    """Finalize a requested cancellation, including tasks cancelled before startup."""
    with session_scope() as db:
        run = db.get(Run, run_id)
        if not run or run.status != "CANCEL_REQUESTED":
            return False
        presentation_id = run.presentation_id
        run.status = "CANCELLED"
        job = db.scalar(select(Job).where(Job.run_id == run_id))
        if job and job.status in {"QUEUED", "RUNNING"}:
            job.status, job.finished_at = "CANCELLED", utcnow()
        presentation = db.get(Presentation, presentation_id)
        if presentation and presentation.status in {"PLANNING", "GENERATING"}:
            presentation.status = "CANCELLED"
        for page in db.scalars(
            select(PageTask).where(
                PageTask.run_id == run_id,
                PageTask.status.in_(
                    [
                        "RUNNING",
                        "PENDING",
                        "WAITING_CAPACITY",
                        "WAITING_MODEL_CAPACITY",
                        "VALIDATING",
                    ]
                ),
            )
        ).all():
            page.status, page.error_code, page.error = (
                "CANCELLED",
                "PAGE_CANCELLED",
                "页面生成已取消",
            )
    from .scene_service import cancel_conversions
    await cancel_conversions(run_id)
    await emit_event(
        presentation_id,
        "run.cancelled",
        {"run_id": run_id, "status": "CANCELLED"},
        run_id,
    )
    sync_conversation_run(run_id, "CANCELLED")
    return True


async def emit_page_delta(presentation_id: str, task_id: str, delta: str, run_id: str | None = None) -> None:
    await emit_event(presentation_id, "page.html.delta", {"page_task_id": task_id, "delta": delta}, run_id)


def sync_conversation_run(run_id: str, status: str, *, error: str | None = None) -> None:
    """Project a run transition onto the persisted conversation messages.

    Conversation messages are the durable UI projection. Runtime events remain the live
    transport, while this function makes refreshes and reopened works converge to the same
    state even when a worker exits between two SSE events.
    """
    with session_scope() as db:
        run = db.get(Run, run_id)
        if not run:
            return
        presentation = db.get(Presentation, run.presentation_id)
        if not presentation or not presentation.conversation_id:
            return
        conversation = db.get(Conversation, presentation.conversation_id)
        turn = db.scalar(
            select(ConversationTurn)
            .where(ConversationTurn.presentation_id == presentation.id)
            .order_by(ConversationTurn.turn_no.desc())
        )
        if not conversation:
            return
        message_status = {
            "RUNNING": "STREAMING",
            "QUEUED": "STREAMING",
            "SUCCEEDED": "READY",
            "READY": "READY",
            "PARTIAL_FAILED": "FAILED",
            "FAILED": "FAILED",
            "CANCELLED": "CANCELLED",
        }.get(status, "READY")
        for message in db.scalars(
            select(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation.id,
                ConversationMessage.turn_id == (turn.id if turn else None),
            )
        ).all():
            payload = loads(message.payload_json)
            if payload.get("run_id") != run_id:
                continue
            payload["status"] = status
            if error:
                payload["error"] = error
            message.status = message_status
            message.payload_json = dumps(payload)
        if turn:
            turn.active_run_id = run_id if status in {"QUEUED", "RUNNING"} else None
            turn.state = (
                "GENERATING" if run.kind == "GENERATION" and status in {"QUEUED", "RUNNING"}
                else "PLANNING" if run.kind == "DIRECTOR" and status in {"QUEUED", "RUNNING"}
                else "WAITING_CONFIRMATION" if run.kind == "DIRECTOR" and status == "SUCCEEDED"
                else "COMPLETED" if status in {"SUCCEEDED", "READY"}
                else "FAILED" if status in {"FAILED", "PARTIAL_FAILED"}
                else "CANCELLED" if status == "CANCELLED"
                else turn.state
            )
            if status in {"QUEUED", "RUNNING"}:
                turn.completed_at = None
            elif status in {"FAILED", "PARTIAL_FAILED", "CANCELLED"} or (
                run.kind == "GENERATION" and status in {"SUCCEEDED", "READY"}
            ):
                turn.completed_at = utcnow()
        if status in {"SUCCEEDED", "READY"} and run.kind == "GENERATION":
            conversation.state = "COMPLETED"
        elif status in {"FAILED", "PARTIAL_FAILED"}:
            conversation.state = "FAILED"
        elif status == "CANCELLED":
            conversation.state = "CANCELLED"
        else:
            conversation.state = "ACTIVE"


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
    sync_conversation_run(run_id, "RUNNING")
    await emit_event(presentation_id, "director.stream.reset", {"run_id": run_id}, run_id)
    await emit_event(presentation_id, "director.started", {"run_id": run_id}, run_id)
    try:
        result = await run_graph(DIRECTOR_GRAPH, {"presentation_id": presentation_id, "run_id": run_id, "user_request": request, "options": options})
        doc = OutlineDocument.model_validate(result["result"])
        normalized = doc.model_dump()
        normalized["slides"] = [s | {"slide_id": str(uuid.uuid4())} for s in normalized["slides"]]
        digest = canonical_hash(normalized)
        with session_scope() as db:
            outline = Outline(presentation_id=presentation_id, revision=1, content_hash=digest, document_json=dumps(normalized))
            db.add(outline)
            db.flush()
            run, presentation = db.get(Run, run_id), db.get(Presentation, presentation_id)
            run.status, presentation.status = "SUCCEEDED", "WAITING_CONFIRMATION"
            if presentation and presentation.conversation_id:
                turn = db.scalar(select(ConversationTurn).where(ConversationTurn.presentation_id == presentation_id).order_by(ConversationTurn.turn_no.desc()))
                conversation = db.get(Conversation, presentation.conversation_id)
                if turn and conversation:
                    activity = db.scalar(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id, ConversationMessage.kind == "activity").order_by(ConversationMessage.seq.desc()))
                    if activity:
                        activity.status = "READY"
                        activity.payload_json = dumps({"stage_code": "outline_ready", "label": "大纲已生成，等待确认", "run_id": run_id})
                    seq = conversation.last_message_seq + 1
                    db.add(ConversationMessage(
                        conversation_id=conversation.id,
                        turn_id=turn.id,
                        seq=seq,
                        role="assistant",
                        kind="outline_card",
                        status="READY",
                        payload_json=dumps({"outline_id": outline.id, "revision": outline.revision, "content_hash": digest, "status": "READY"}),
                    ))
                    conversation.last_message_seq = seq
        await emit_event(presentation_id, "outline.ready", {"run_id": run_id, "outline": normalized, "content_hash": digest}, run_id)
        await emit_event(presentation_id, "run.completed", {"run_id": run_id, "status": "SUCCEEDED", "kind": "DIRECTOR"}, run_id)
        sync_conversation_run(run_id, "SUCCEEDED")
    except Exception as exc:
        message = str(exc)[:500]
        error_code = exc.code if isinstance(exc, ProviderError) else "DIRECTOR_FAILED"
        with session_scope() as db:
            run, presentation = db.get(Run, run_id), db.get(Presentation, presentation_id)
            if run: run.status, run.error_code, run.error = "FAILED", error_code, message
            if presentation: presentation.status = "FAILED"
        sync_conversation_run(run_id, "FAILED", error=message)
        await emit_event(presentation_id, "run.failed", {"run_id": run_id, "error": message, "error_code": error_code}, run_id)


async def compose_page(presentation_id: str, run_id: str, task_id: str) -> None:
    try:
        reason = ""
        for attempt_number in range(1, 3):
            with session_scope() as db:
                task = db.get(PageTask, task_id)
                run = db.get(Run, run_id)
                if not task or task.run_id != run_id or not run or run.status != "RUNNING":
                    raise asyncio.CancelledError()
                task.status = "WAITING_MODEL_CAPACITY"
                number = task.page_number
            await emit_event(presentation_id, "page.queued", {"page_task_id": task_id, "page_number": number, "queue_reason": "MODEL_CAPACITY"}, run_id)
            result = await run_graph(PAGE_GRAPH, {"task_id": task_id, "run_id": run_id, "error": reason})
            html = result["result"]
            valid, reason = validate_html(html)
            if valid:
                break
            if attempt_number < 2:
                await emit_event(presentation_id, "page.retrying", {"page_task_id": task_id, "page_number": number, "reason": reason, "attempt": attempt_number + 1}, run_id)
        if not valid:
            raise ProviderError("INVALID_HTML", reason)
        digest = html_hash(html)
        from .scene_service import enqueue_tx, WAKE
        async with EVENT_WRITE_LOCK:
            with session_scope() as db:
                task, run = db.get(PageTask, task_id), db.get(Run, run_id)
                if not task or task.run_id != run_id or not run or run.status != "RUNNING":
                    raise asyncio.CancelledError()
                task.status, task.html, task.html_hash, task.error, task.error_code = "SUCCEEDED", html, digest, None, None
                append_event_tx(db, presentation_id, "page.completed", {"page_task_id": task_id, "page_number": number, "html_hash": digest, "status": "SUCCEEDED"}, run_id)
                enqueue_tx(db, task, retry=True)
        WAKE.set()
        await EVENT_BUS.notify(presentation_id)
    except asyncio.CancelledError:
        with session_scope() as db:
            task = db.get(PageTask, task_id)
            if task and task.run_id == run_id and task.status not in {"SUCCEEDED", "FAILED"}:
                task.status, task.error_code, task.error = "CANCELLED", "PAGE_CANCELLED", "页面生成已取消"
        raise
    except Exception as exc:
        message = str(exc)[:500]
        error_code = exc.code if isinstance(exc, ProviderError) else "PAGE_GENERATION_FAILED"
        with session_scope() as db:
            task = db.get(PageTask, task_id)
            if task and task.run_id == run_id:
                task.status, task.error_code, task.error = "FAILED", error_code, message
        await emit_event(presentation_id, "page.failed", {"page_task_id": task_id, "error": message, "error_code": error_code}, run_id)


async def run_generation(presentation_id: str, run_id: str) -> None:
    await emit_event(presentation_id, "generation.started", {"run_id": run_id}, run_id)
    sync_conversation_run(run_id, "RUNNING")
    with session_scope() as db:
        tasks = list(db.scalars(select(PageTask).where(PageTask.run_id == run_id, PageTask.status != "SUCCEEDED").order_by(PageTask.page_number)).all())
    async with asyncio.TaskGroup() as group:
        for task in tasks:
            group.create_task(compose_page(presentation_id, run_id, task.id))
    with session_scope() as db:
        statuses = [t.status for t in db.scalars(select(PageTask).where(PageTask.run_id == run_id)).all()]
        run, presentation = db.get(Run, run_id), db.get(Presentation, presentation_id)
        success = all(s == "SUCCEEDED" for s in statuses)
        run.status, run.error_code, presentation.status = ("SUCCEEDED" if success else "PARTIAL_FAILED"), (None if success else "PAGE_GENERATION_FAILED"), ("READY" if success else "PARTIAL_FAILED")
    sync_conversation_run(run_id, "SUCCEEDED" if success else "PARTIAL_FAILED")
    await emit_event(presentation_id, "run.completed", {"run_id": run_id, "status": "SUCCEEDED" if success else "PARTIAL_FAILED", "kind": "GENERATION"}, run_id)


async def scheduler(stop: asyncio.Event) -> None:
    """Durable, fair scheduler with bounded concurrent runs."""
    with session_scope() as db:
        for call in db.scalars(select(LlmCall).where(LlmCall.status.in_(["QUEUED", "RUNNING"]))).all():
            call.status, call.error_code, call.error = "CANCELLED", "CALL_INTERRUPTED", "后端重启，调用已中断"
            call.finished_at = utcnow()
        for test in db.scalars(select(ProviderTest).where(ProviderTest.status == "running")).all():
            test.status, test.finished_at = "cancelled", utcnow()
        for job in db.scalars(select(Job).where(Job.status == "RUNNING")).all():
            run = db.get(Run, job.run_id)
            if run and run.status in {"SUCCEEDED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}:
                job.status = "SUCCEEDED" if run.status in {"SUCCEEDED", "PARTIAL_FAILED"} else run.status
                continue
            job.status = "QUEUED"
            job.started_at, job.queued_at = None, utcnow()
            if run and run.kind == "GENERATION":
                for task in db.scalars(select(PageTask).where(PageTask.run_id == run.id, PageTask.status.in_(["RUNNING", "WAITING_CAPACITY", "WAITING_MODEL_CAPACITY", "VALIDATING"]))).all():
                    task.status = "PENDING"
            elif run and run.kind == "DIRECTOR":
                run.status = "QUEUED"
    active_tasks: dict[str, asyncio.Task] = {}
    active_model_keys: dict[str, str] = {}
    fair_model_cursor: str | None = None

    def elapsed_seconds(start: datetime, end: datetime) -> float:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return (end - start).total_seconds()

    def job_key(job: Job, snapshot_json: str | None) -> str:
        snapshot = loads(snapshot_json) if snapshot_json else {}
        routes = snapshot.get("resolved_routes") or {}
        route = routes.get("ppt_outline" if job.kind == "DIRECTOR" else "ppt_html") or {}
        model_id = job.model_id or route.get("model_id") or snapshot.get("model_id")
        return str(model_id or f"unknown:{job.presentation_id}")

    async def execute(job_id: str, run_id: str, presentation_id: str, kind: str) -> None:
        try:
            await (run_director(presentation_id, run_id) if kind == "DIRECTOR" else run_generation(presentation_id, run_id))
        except asyncio.CancelledError:
            await finalize_cancelled_run(run_id)
            raise
        except Exception as exc:
            message = str(exc)[:500]
            code = exc.code if isinstance(exc, ProviderError) else "JOB_EXECUTION_FAILED"
            with session_scope() as db:
                run = db.get(Run, run_id)
                presentation = db.get(Presentation, presentation_id)
                job = db.get(Job, job_id)
                if run:
                    run.status, run.error_code, run.error = "FAILED", code, message
                if presentation:
                    presentation.status = "FAILED" if kind == "DIRECTOR" else "PARTIAL_FAILED"
                if job:
                    job.status, job.error_code, job.error, job.finished_at = "FAILED", code, message, utcnow()
            sync_conversation_run(run_id, "FAILED", error=message)
            await emit_event(presentation_id, "run.failed", {"run_id": run_id, "error": message, "error_code": code}, run_id)
        finally:
            ACTIVE_RUNS.pop(run_id, None)
            with session_scope() as db:
                job = db.get(Job, job_id)
                run = db.get(Run, run_id)
                if job and run and run.status in {"SUCCEEDED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}:
                    job.status = "SUCCEEDED" if run.status in {"SUCCEEDED", "PARTIAL_FAILED"} else run.status
                    job.finished_at = utcnow()

    async def wait_for_active_tasks() -> None:
        try:
            done, _ = await asyncio.wait(
                active_tasks.values(),
                timeout=0.25,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            for task in active_tasks.values():
                task.cancel()
            await asyncio.gather(*active_tasks.values(), return_exceptions=True)
            for run_id in active_tasks:
                await finalize_cancelled_run(run_id)
            raise
        for task in done:
            for run_id, current in list(active_tasks.items()):
                if current is task:
                    if task.cancelled():
                        await finalize_cancelled_run(run_id)
                    ACTIVE_RUNS.pop(run_id, None)
                    active_model_keys.pop(run_id, None)
                    active_tasks.pop(run_id, None)
                    break

    while not stop.is_set() or active_tasks:
        if stop.is_set():
            if not active_tasks:
                break
            await wait_for_active_tasks()
            continue

        with session_scope() as db:
            queued_rows = db.execute(
                select(Job, Presentation.config_snapshot_json)
                .outerjoin(Presentation, Presentation.id == Job.presentation_id)
                .where(Job.status == "QUEUED")
                .order_by(Job.queued_at, Job.created_at)
            ).all()
            queued = [row[0] for row in queued_rows]
            model_keys = {
                job.id: job_key(job, snapshot_json)
                for job, snapshot_json in queued_rows
            }
            now = utcnow()
            model_ids = set(model_keys.values()) | set(active_model_keys.values())
            model_limits = dict(
                db.execute(
                    select(LogicalModel.id, LogicalModel.max_concurrency).where(
                        LogicalModel.id.in_(model_ids)
                    )
                ).all()
            ) if model_ids else {}
            expired = [job for job in queued if elapsed_seconds(job.queued_at, now) > settings.scheduler_queue_timeout_seconds]
            for job in expired:
                run = db.get(Run, job.run_id)
                job.status, job.error_code, job.error, job.finished_at = "FAILED", "JOB_QUEUE_TIMEOUT", "等待任务调度超时", now
                if run:
                    run.status, run.error_code, run.error = "FAILED", "JOB_QUEUE_TIMEOUT", "等待任务调度超时"
                    presentation = db.get(Presentation, run.presentation_id)
                    if presentation:
                        presentation.status = "FAILED" if run.kind == "DIRECTOR" else "PARTIAL_FAILED"
                    if run.kind == "GENERATION":
                        for page in db.scalars(select(PageTask).where(PageTask.run_id == run.id, PageTask.status == "PENDING")).all():
                            page.status, page.error_code, page.error = "FAILED", "JOB_QUEUE_TIMEOUT", "等待任务调度超时"
        for job in expired:
            sync_conversation_run(job.run_id, "FAILED", error="等待任务调度超时")
            await emit_event(job.presentation_id, "job.queue_timeout", {"job_id": job.id, "run_id": job.run_id, "error_code": "JOB_QUEUE_TIMEOUT"}, job.run_id)
        if stop.is_set():
            continue
        queued = [job for job in queued if job not in expired]
        capacity = max(0, settings.max_active_runs - len(active_tasks))
        if capacity:
            groups: dict[str, list[Job]] = {}
            for job in queued:
                groups.setdefault(model_keys[job.id], []).append(job)
            keys = sorted(groups)
            active_counts: dict[str, int] = {}
            for model_id in active_model_keys.values():
                active_counts[model_id] = active_counts.get(model_id, 0) + 1
            if keys:
                selected: list[Job] = []
                projected_counts = active_counts.copy()
                while len(selected) < capacity:
                    start = keys.index(fair_model_cursor) if fair_model_cursor in keys else 0
                    for offset in range(len(keys)):
                        index = (start + offset) % len(keys)
                        key = keys[index]
                        if (
                            groups[key]
                            and projected_counts.get(key, 0) < model_limits.get(key, 1)
                        ):
                            selected.append(groups[key].pop(0))
                            projected_counts[key] = projected_counts.get(key, 0) + 1
                            fair_model_cursor = keys[(index + 1) % len(keys)]
                            break
                    else:
                        break
                for job in selected:
                    if stop.is_set():
                        break
                    with session_scope() as db:
                        row = db.get(Job, job.id)
                        if not row or row.status != "QUEUED":
                            continue
                        now = utcnow()
                        queue_duration_ms = int(elapsed_seconds(row.queued_at, now) * 1000)
                        row.status, row.started_at, row.queue_duration_ms = "RUNNING", now, queue_duration_ms
                        run = db.get(Run, row.run_id)
                        if run:
                            run.status = "RUNNING"
                        run_id, presentation_id, kind = row.run_id, row.presentation_id, row.kind
                    task = asyncio.create_task(execute(job.id, run_id, presentation_id, kind))
                    active_tasks[run_id] = task
                    active_model_keys[run_id] = model_keys[job.id]
                    ACTIVE_RUNS[run_id] = task
                    await emit_event(presentation_id, "job.started", {"job_id": job.id, "run_id": run_id, "queue_duration_ms": queue_duration_ms}, run_id)
        if active_tasks:
            await wait_for_active_tasks()
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

