from dataclasses import dataclass
from typing import Callable

from .prompts import COMPOSER_PROMPT, DIRECTOR_PROMPT
from .design_prompts import DESIGN_PLANNER_PROMPT, design_page_prompt
from .schemas import DesignCanvas

PPT_CANVAS = (800, 450)


@dataclass(frozen=True)
class WorkflowSpec:
    code: str
    outline_route: str
    page_route: str
    outline_instructions: str
    page_instructions: Callable[[tuple[int, int]], str]

    def route_for(self, kind: str) -> str:
        return self.outline_route if kind == "DIRECTOR" else self.page_route

    def canvas(self, options: dict) -> tuple[int, int]:
        if self.code == DESIGN.code:
            canvas = DesignCanvas.model_validate(options["canvas"])
            return canvas.width, canvas.height
        return PPT_CANVAS


PPT = WorkflowSpec(
    "ppt_create", "ppt_outline", "ppt_html", DIRECTOR_PROMPT, lambda _: COMPOSER_PROMPT
)
DESIGN = WorkflowSpec(
    "design_create", "design_outline", "design_html", DESIGN_PLANNER_PROMPT,
    design_page_prompt,
)
WORKFLOWS = {workflow.code: workflow for workflow in (PPT, DESIGN)}
ENTRY_WORKFLOWS = {"ppt": PPT, "agent": PPT, "design": DESIGN}
ROUTE_LABELS = {
    "default": "全局默认",
    PPT.outline_route: "PPT 大纲生成",
    PPT.page_route: "PPT 内容 HTML 生成",
    DESIGN.outline_route: "设计大纲生成",
    DESIGN.page_route: "设计页面 HTML 生成",
}
OUTLINE_ROUTES = frozenset(workflow.outline_route for workflow in WORKFLOWS.values())


def scheduled_model_id(snapshot: dict, kind: str, workflow_code: str = PPT.code):
    workflow = WORKFLOWS[workflow_code]
    route = (snapshot.get("resolved_routes") or {}).get(workflow.route_for(kind)) or {}
    return route.get("model_id") or (snapshot.get("model_id") if workflow == PPT else None)
