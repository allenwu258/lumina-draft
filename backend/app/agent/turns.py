from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text

from ..db import Conversation, ConversationMessage, ConversationTurn, IdempotencyRecord, Presentation, Run, dumps, loads, session_scope, utcnow
from ..provider_management import ConfigurationError, resolve_business_route, route_issues
from ..runtime import EVENT_BUS, append_event_tx, canonical_hash
from . import leader
from .projections import deck_index

router = APIRouter(prefix="/api/v1/presentations", tags=["workbench"])


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=20000)
    slide_id: str | None = None
    source_ref: str | None = None

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
    try:
        config = resolve_business_route(db, "agent_leader")
    except ConfigurationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if issues := route_issues(db, config):
        raise HTTPException(409, "；".join(issues))
    index = deck_index(db, work.id)
    selected = None
    if body.slide_id:
        selected = next((page for page in index["pages"] if page["slide_id"] == body.slide_id), None)
        if not selected or body.source_ref not in {value for value in (selected["html_hash"], index["outline_hash"]) if value}:
            raise HTTPException(409, "所选页面版本已更新，请刷新")
        selected = dict(selected, source_ref=body.source_ref)
    history = db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id,
                         ConversationMessage.kind.in_(["text", "reply", "clarify", "suggest"]))
                         .order_by(ConversationMessage.seq.desc()).limit(6)).all()
    inputs = {"user": body.text, "selected": selected, "memory": loads(conversation.memory_json), "deck": index,
              "recent": [{"id": row.id, "role": row.role, "text": row.body_text[:900]} for row in reversed(history) if row.body_text]}
    budget = leader.input_budget(config)
    while inputs["recent"] and len(dumps(inputs).encode()) > budget:
        inputs["recent"].pop(0)
    if len(dumps(inputs).encode()) > budget:
        raise HTTPException(422, "当前内容超出模型上下文预算，请缩短消息或选择更大上下文的模型")
    work.updated_at = utcnow()
    conversation.latest_turn_no += 1
    turn = ConversationTurn(conversation_id=conversation.id, presentation_id=work.id,
                            turn_no=conversation.latest_turn_no, workflow_code="agent_leader", request_id=body.client_request_id,
                            context_json=dumps({"version": 1, "digest": canonical_hash(body.model_dump()), "input": inputs, "config": config}))
    db.add(turn)
    db.flush()
    message = leader.append_message(db, conversation, turn, "user", "text", body.text)
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
            if loads(existing.context_json)["digest"] != canonical_hash(body.model_dump()):
                raise HTTPException(409, "请求标识已用于其他消息")
            return {"turn_id": existing.id}
        if active_leader(db, chat.id):
            raise HTTPException(409, "上一轮仍在处理中")
        if chat.interaction_mode == "single_turn" and db.scalar(select(Run.id).where(Run.presentation_id == work.id,
                Run.status.in_(["QUEUED", "RUNNING", "CANCEL_REQUESTED"]))):
            raise HTTPException(409, "请在当前生成任务完成后继续对话")
        chat.interaction_mode = "multi_turn"
        turn_id = queue_turn(db, work, chat, body)
    leader.launch(turn_id)
    await EVENT_BUS.notify(presentation_id)
    return {"turn_id": turn_id}


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
