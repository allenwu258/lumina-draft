from sqlalchemy import select

from .db import Job, Outline, Run, dumps, loads
from .provider_management import ConfigurationError, resolve_business_route, route_issues
from .workflows import PPT, scheduled_model_id


def enqueue_director(db, work, *, turn_id=None, request=None):
    run = Run(presentation_id=work.id, kind="DIRECTOR", status="QUEUED", turn_id=turn_id, director_input=request)
    db.add(run)
    db.flush()
    db.add(Job(run_id=run.id, presentation_id=work.id, kind=run.kind,
               model_id=scheduled_model_id(loads(work.config_snapshot_json), run.kind, work.workflow_code)))
    work.status = "PLANNING"
    return run


def freeze_route(db, work, code):
    config = resolve_business_route(db, code)
    if issues := route_issues(db, config):
        raise ConfigurationError("CONFIG_NOT_READY", "；".join(issues))
    snapshot = loads(work.config_snapshot_json)
    snapshot.setdefault("resolved_routes", {})[code] = config
    work.config_snapshot_json = dumps(snapshot)


def delegate_outline(db, work, turn, context, instruction):
    if work.status not in {"DRAFT", "WAITING_CONFIRMATION", "FAILED", "CANCELLED"}:
        raise ConfigurationError("INVALID_STATE", "当前作品状态不支持起草或修订大纲。")
    base = db.scalar(select(Outline).where(Outline.presentation_id == work.id).order_by(Outline.revision.desc()))
    if (base.id if base else None) != context["deck"]["outline_id"] or (base and base.confirmed):
        raise ConfigurationError("OUTLINE_VERSION_CONFLICT", "请根据当前待确认的大纲重新发起修订。")
    freeze_route(db, work, PPT.outline_route)
    request = dumps({"本轮用户要求": context["user"], "首轮需求": work.user_request, "长期需求": context["memory"],
                     "最近对话": context["recent"], "整稿任务": instruction,
                     "基准大纲": loads(base.document_json) if base else None})
    return enqueue_director(db, work, turn_id=turn.id, request=request)
