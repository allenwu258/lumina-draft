from .db import Job, Outline, Run, dumps, loads
from .provider_management import ConfigurationError
from .workflows import PPT, scheduled_model_id
from .agent.batches import require_production_slot, route_snapshot


def enqueue_director(db, work, *, turn_id=None, request=None, config_snapshot=None):
    run = Run(presentation_id=work.id, kind="DIRECTOR", status="QUEUED", turn_id=turn_id,
              director_input=request, config_snapshot_json=config_snapshot)
    db.add(run)
    db.flush()
    db.add(Job(run_id=run.id, presentation_id=work.id, kind=run.kind,
               model_id=scheduled_model_id(loads(config_snapshot or work.config_snapshot_json), run.kind, work.workflow_code)))
    work.status = "PLANNING"
    return run


def delegate_outline(db, work, turn, context, instruction, reference):
    require_production_slot(db, work)
    base = db.get(Outline, reference["outline_id"]) if reference and reference["outline_id"] else None
    if reference and reference["outline_id"] and (
        not base or base.presentation_id != work.id or base.content_hash != reference["outline_hash"]
    ):
        raise ConfigurationError("OUTLINE_VERSION_CONFLICT", "所引用的大纲已失效，请重新选择。")
    request = dumps({"本轮用户要求": context["user"], "首轮需求": work.user_request, "长期需求": context["memory"],
                     "最近对话": context["recent"], "整稿任务": instruction,
                     "基准大纲": loads(base.document_json) if base else None})
    return enqueue_director(db, work, turn_id=turn.id, request=request,
                            config_snapshot=route_snapshot(db, PPT.outline_route))
