import uuid

from sqlalchemy import select

from ..db import ConversationMessage, Job, Outline, PageTask, Run, dumps, loads, utcnow
from ..provider_management import ConfigurationError, resolve_business_route, route_issues
from ..runtime import append_event_tx
from ..workflows import PPT, scheduled_model_id


ACTIVE_STATUSES = ("QUEUED", "RUNNING", "CANCEL_REQUESTED")


def require_production_slot(db, work):
    if db.scalar(select(Run.id).where(Run.presentation_id == work.id, Run.status.in_(ACTIVE_STATUSES)).limit(1)):
        raise ConfigurationError("WORK_BUSY", "已有制作任务在运行，请在它结束后再发起制作。")


def route_snapshot(db, code):
    config = resolve_business_route(db, code)
    if issues := route_issues(db, config):
        raise ConfigurationError("CONFIG_NOT_READY", "；".join(issues))
    return dumps({"resolved_routes": {code: config}})


def batch_for_outline(db, outline_id):
    return db.scalar(select(Run).where(Run.outline_id == outline_id))


def batch_card(db, chat, batch_id):
    cards = db.scalars(select(ConversationMessage).where(
        ConversationMessage.conversation_id == chat.id, ConversationMessage.kind == "generation_card",
    ).order_by(ConversationMessage.seq.desc()))
    for card in cards:
        payload = loads(card.payload_json)
        if payload.get("batch_id"):
            if payload["batch_id"] == batch_id:
                return card
        elif payload.get("run_id"):
            attempt = db.get(Run, payload["run_id"])
            if attempt and attempt.batch_id == batch_id:
                return card
    return None


def enqueue_generation(db, work, chat, turn, outline_id, content_hash):
    outline = db.get(Outline, outline_id)
    if not outline or outline.presentation_id != work.id or outline.content_hash != content_hash:
        raise ConfigurationError("OUTLINE_VERSION_CONFLICT", "大纲引用已失效，请重新选择。")
    prior = batch_for_outline(db, outline.id)
    if prior:
        return prior, batch_card(db, chat, prior.id), False
    if outline.confirmed:
        raise ConfigurationError("LEGACY_BATCH_UNAVAILABLE", "历史成品缺少可核对的制页批次。")
    require_production_slot(db, work)
    run_id = str(uuid.uuid4())
    run = Run(id=run_id, batch_id=run_id, outline_id=outline.id, turn_id=turn.id,
              presentation_id=work.id, kind="GENERATION", config_snapshot_json=route_snapshot(db, PPT.page_route))
    db.add(run)
    db.flush()
    slides = sorted(loads(outline.document_json)["slides"], key=lambda slide: slide["page_number"])
    for slide in slides:
        db.add(PageTask(presentation_id=work.id, batch_id=run.id, run_id=run.id, slide_id=slide["slide_id"],
                        page_number=slide["page_number"], title=slide["title"], brief_json=dumps(slide)))
    announce_generation(db, work, turn, run)
    outline.confirmed = True
    chat.last_message_seq += 1
    chat.updated_at = utcnow()
    card = ConversationMessage(conversation_id=chat.id, turn_id=turn.id, seq=chat.last_message_seq,
                               role="assistant", kind="generation_card", status="STREAMING", payload_schema_version=2,
                               payload_json=dumps({"batch_id": run.id, "outline_id": outline.id, "revision": outline.revision,
                                                   "run_id": run.id, "page_count": len(slides), "status": "QUEUED"}))
    db.add(card)
    db.flush()
    return run, card, True


def announce_generation(db, work, turn, run):
    snapshot = loads(run.config_snapshot_json or work.config_snapshot_json)
    db.add(Job(run_id=run.id, presentation_id=work.id, kind=run.kind,
               model_id=scheduled_model_id(snapshot, run.kind, work.workflow_code)))
    work.status = "GENERATING"
    turn.state, turn.active_run_id, turn.completed_at = "GENERATING", run.id, None
    append_event_tx(db, work.id, "generation.queued", {"run_id": run.id, "batch_id": run.batch_id}, run.id)


def retry_batch(db, work, chat, root, turn):
    latest = db.scalar(select(Run).where(Run.batch_id == root.id).order_by(Run.created_at.desc(), Run.id.desc()))
    if latest.status in ACTIVE_STATUSES:
        return latest, False
    if latest.status not in {"PARTIAL_FAILED", "FAILED", "CANCELLED"}:
        raise ConfigurationError("INVALID_STATE", "此批次没有可重试的页面。")
    require_production_slot(db, work)
    run = Run(batch_id=root.id, turn_id=turn.id, kind="GENERATION", presentation_id=work.id,
              config_snapshot_json=root.config_snapshot_json or work.config_snapshot_json)
    db.add(run)
    db.flush()
    for page in db.scalars(select(PageTask).where(PageTask.batch_id == root.id, PageTask.status != "SUCCEEDED")):
        page.run_id, page.status, page.error, page.error_code = run.id, "PENDING", None, None
    card = batch_card(db, chat, root.id)
    if card:
        card.status = "STREAMING"
        payload = loads(card.payload_json)
        payload.pop("error", None)
        card.payload_json = dumps({**payload, "batch_id": root.id, "run_id": run.id, "status": "QUEUED"})
    announce_generation(db, work, turn, run)
    return run, True
