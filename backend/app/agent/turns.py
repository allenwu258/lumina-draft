from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text

from ..db import Conversation, ConversationMessage, ConversationTurn, IdempotencyRecord, Outline, Presentation, Run, backfill_agent_batch, dumps, loads, session_scope, utcnow
from ..provider_management import ConfigurationError, resolve_business_route, route_issues
from ..runtime import EVENT_BUS, append_event_tx, canonical_hash
from . import leader
from .projections import deck_index, project_references
from .batches import enqueue_generation, retry_batch

router = APIRouter(prefix="/api/v1/presentations", tags=["workbench"])


class ViewRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_message_id: str = Field(min_length=1, max_length=36)
    slide_id: str | None = None
    source_ref: str | None = None


class ComposeDeckAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["compose_deck"]
    artifact_message_id: str = Field(min_length=1, max_length=36)
    expected_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class RetryDeckAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["retry_deck"]
    artifact_message_id: str = Field(min_length=1, max_length=36)


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=20000)
    slide_id: str | None = None
    source_ref: str | None = None
    view_ref: ViewRef | None = None
    action: Annotated[ComposeDeckAction | RetryDeckAction, Field(discriminator="type")] | None = None

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("请输入消息")
        return value.strip()


def active_leader(db, conversation_id):
    return db.scalar(select(ConversationTurn.id).where(
        ConversationTurn.conversation_id == conversation_id,
        ConversationTurn.workflow_code == "agent_leader",
        ConversationTurn.state.in_(["QUEUED", "RUNNING"])))


def queue_turn(db, work, conversation, body):
    if body.action:
        return persist_turn(db, work, conversation, body, {"action": body.action.model_dump()})
    try:
        config = resolve_business_route(db, "agent_leader")
    except ConfigurationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if issues := route_issues(db, config):
        raise HTTPException(409, "；".join(issues))
    card_id = body.view_ref.artifact_message_id if body.view_ref else None
    explicit, selected_index, candidates, unresolved = project_references(db, work, body.text, card_id)
    if card_id and not selected_index:
        raise HTTPException(409, "所选卡片已失效，请重新选择")
    viewed = selected_index or deck_index(db, work.id)
    index = explicit or viewed
    selected = None
    slide_id = body.view_ref.slide_id if body.view_ref else body.slide_id
    source_ref = body.view_ref.source_ref if body.view_ref else body.source_ref
    if slide_id:
        selected = next((page for page in viewed["pages"] if page["slide_id"] == slide_id), None)
        if not selected or source_ref not in {value for value in (selected["html_hash"], viewed["outline_hash"]) if value}:
            raise HTTPException(409, "所选页面版本已更新，请刷新")
        selected = dict(selected, source_ref=source_ref, artifact_message_id=card_id)
    history = db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id,
                         ConversationMessage.kind.in_(["text", "action", "reply", "clarify", "suggest"]))
                         .order_by(ConversationMessage.seq.desc()).limit(6)).all()
    inputs = {"user": body.text, "selected": selected, "memory": loads(conversation.memory_json), "deck": index,
              "explicit_version": bool(explicit), "unresolved_version": unresolved, "selected_artifact": card_id,
              "artifacts": [{"ref": ref, **{key: value for key, value in entry.items() if key != "pages"},
                             "page_count": len(entry["pages"])} for ref, entry in candidates],
              "recent": [{"id": row.id, "role": row.role, "text": row.body_text[:900]} for row in reversed(history) if row.body_text]}
    budget = leader.input_budget(config)
    while inputs["recent"] and len(dumps(inputs).encode()) > budget:
        inputs["recent"].pop(0)
    while inputs["artifacts"] and len(dumps(inputs).encode()) > budget:
        inputs["artifacts"].pop()
    if len(dumps(inputs).encode()) > budget:
        raise HTTPException(422, "当前内容超出模型上下文预算，请缩短消息或选择更大上下文的模型")
    return persist_turn(db, work, conversation, body, {"input": inputs, "config": config})


def request_digest(body, version=3):
    excluded = {"action"} if body.action is None else set()
    if version == 1 and body.view_ref is None:
        excluded.add("view_ref")
    return canonical_hash(body.model_dump(exclude=excluded))


def persist_turn(db, work, conversation, body, frozen):
    work.updated_at = utcnow()
    conversation.latest_turn_no += 1
    turn = ConversationTurn(conversation_id=conversation.id, presentation_id=work.id,
                            turn_no=conversation.latest_turn_no, workflow_code="agent_leader", request_id=body.client_request_id,
                            context_json=dumps({"version": 3, "digest": request_digest(body), **frozen}))
    db.add(turn)
    db.flush()
    message = leader.append_message(db, conversation, turn, "user", "action" if body.action else "text", body.text,
                                    {"action": frozen["action"]} if body.action else None)
    db.flush()
    turn.input_message_id = message.id
    append_event_tx(db, work.id, "turn.queued", {"turn_id": turn.id})
    return turn.id


async def create_work(body, key, digest):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if key:
            prior = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == "presentations.create", IdempotencyRecord.key == key))
            if prior:
                if prior.request_hash != digest:
                    raise HTTPException(409, "请求标识已用于其他内容")
                return loads(prior.response_json)
        conversation = Conversation(entry_mode="agent", interaction_mode="multi_turn", latest_turn_no=0)
        db.add(conversation)
        db.flush()
        work = Presentation(user_request=body.user_request, options_json=dumps(body.options.model_dump(exclude_none=True)),
                            status="DRAFT", entry_mode="agent", workflow_code="ppt_create", conversation_id=conversation.id)
        db.add(work)
        db.flush()
        turn_id = queue_turn(db, work, conversation, TurnInput(text=body.user_request, client_request_id="initial"))
        result = {"presentation_id": work.id, "conversation_id": conversation.id, "turn_id": turn_id, "run_id": None,
                  "entry_mode": "agent", "status": "DRAFT", "workflow_code": "ppt_create", "canvas": {"width": 800, "height": 450}}
        if key:
            db.add(IdempotencyRecord(scope="presentations.create", key=key, request_hash=digest, response_json=dumps(result)))
    leader.launch(turn_id)
    return result


@router.post("/{presentation_id}/turns", status_code=202)
async def send_turn(presentation_id: str, body: TurnInput):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        work = db.get(Presentation, presentation_id)
        if not work or work.entry_mode != "agent":
            raise HTTPException(404, "Agent 作品不存在")
        chat = db.get(Conversation, work.conversation_id)
        existing = db.scalar(select(ConversationTurn).where(ConversationTurn.conversation_id == chat.id,
                             ConversationTurn.request_id == body.client_request_id))
        if existing:
            frozen = loads(existing.context_json)
            if frozen["digest"] != request_digest(body, frozen["version"]):
                raise HTTPException(409, "请求标识已用于其他消息")
            return {"turn_id": existing.id}
        if active_leader(db, chat.id):
            raise HTTPException(409, "上一轮仍在处理中")
        if chat.interaction_mode == "single_turn" and db.scalar(select(Run.id).where(Run.presentation_id == work.id,
                Run.status.in_(["QUEUED", "RUNNING", "CANCEL_REQUESTED"]))):
            raise HTTPException(409, "请在当前生成任务完成后继续对话")
        if chat.interaction_mode == "single_turn":
            backfill_agent_batch(db, work)
            db.flush()
            chat.interaction_mode = "multi_turn"
        turn_id = queue_turn(db, work, chat, body)
    leader.launch(turn_id)
    await EVENT_BUS.notify(presentation_id)
    return {"turn_id": turn_id}


def action_turn(db, work, chat, label):
    chat.latest_turn_no += 1
    turn = ConversationTurn(conversation_id=chat.id, presentation_id=work.id, turn_no=chat.latest_turn_no,
                            workflow_code="ppt_create", state="COMPLETED",
                            context_json=dumps({"version": 2, "action": label}), completed_at=utcnow())
    db.add(turn)
    db.flush()
    message = leader.append_message(db, chat, turn, "user", "action", label)
    db.flush()
    turn.input_message_id = message.id
    return turn


async def confirm_outline(presentation_id, body):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        work = db.get(Presentation, presentation_id)
        chat = db.get(Conversation, work.conversation_id)
        scope = f"agent.confirm:{work.id}"
        digest = canonical_hash(body.model_dump())
        if body.client_request_id:
            prior = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope,
                                                             IdempotencyRecord.key == body.client_request_id))
            if prior:
                if prior.request_hash != digest:
                    raise HTTPException(409, "请求标识已用于其他大纲")
                return loads(prior.response_json)
        else:
            latest = db.scalar(select(Outline.id).where(Outline.presentation_id == work.id).order_by(Outline.revision.desc()).limit(1))
            if latest != body.outline_id:
                raise HTTPException(409, "请从大纲卡确认这个历史版本")
        turn = action_turn(db, work, chat, "确认大纲并生成 PPT")
        try:
            run, card, created = enqueue_generation(db, work, chat, turn, body.outline_id, body.content_hash)
        except ConfigurationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        if not created:
            leader.finish(db, turn, "COMPLETED", "reply", "这版大纲已确认，可从原 PPT 卡查看。",
                          {"batch_id": run.id, "message_id": card.id if card else None})
        result = {"presentation_id": work.id, "turn_id": turn.id, "run_id": run.id, "batch_id": run.id,
                  "message_id": card.id if card else None, "status": work.status}
        if body.client_request_id:
            db.add(IdempotencyRecord(scope=scope, key=body.client_request_id, request_hash=digest, response_json=dumps(result)))
    await EVENT_BUS.notify(presentation_id)
    return result


class RetryBatchInput(BaseModel):
    client_request_id: str = Field(min_length=1, max_length=80)


@router.post("/{presentation_id}/batches/{batch_id}/retry", status_code=202)
async def retry_generation(presentation_id: str, batch_id: str, body: RetryBatchInput):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        work, root = db.get(Presentation, presentation_id), db.get(Run, batch_id)
        if not work or not root or root.presentation_id != presentation_id or root.batch_id != root.id:
            raise HTTPException(404, "PPT 批次不存在")
        chat = db.get(Conversation, work.conversation_id)
        scope = f"batch.retry:{batch_id}"
        prior = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == body.client_request_id))
        if prior:
            return loads(prior.response_json)
        turn = action_turn(db, work, chat, "重试 PPT 生成")
        try:
            run, created = retry_batch(db, work, chat, root, turn)
        except ConfigurationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        result = {"run_id": run.id, "batch_id": root.id, "status": run.status}
        db.add(IdempotencyRecord(scope=scope, key=body.client_request_id,
                                 request_hash=canonical_hash(body.model_dump()), response_json=dumps(result)))
    if created:
        await EVENT_BUS.notify(presentation_id)
    return result


@router.post("/{presentation_id}/turns/{turn_id}/cancel")
async def cancel_turn(presentation_id: str, turn_id: str):
    with session_scope() as db:
        turn = db.get(ConversationTurn, turn_id)
        if not turn or turn.presentation_id != presentation_id or turn.workflow_code != "agent_leader":
            raise HTTPException(404, "回合不存在")
        if turn.state in {"QUEUED", "RUNNING"}:
            leader.finish(db, turn, "CANCELLED", "cancelled", "本轮已取消")
        state = turn.state
    if task := leader.ACTIVE.get(turn_id):
        task.cancel()
    await EVENT_BUS.notify(presentation_id)
    return {"state": state}
