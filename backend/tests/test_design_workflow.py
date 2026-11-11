"""Independent Design orchestration through the public API and real scene worker."""

import json
import re
import time

import pytest
from sqlalchemy import select

# Establish the isolated test database before importing application settings.
from backend.tests.test_core import client, close_database, document, valid_html, wait_status  # noqa: F401
from backend.app import main, runtime, scene_service
from backend.app.db import (
    BusinessRouteOverride, ConversationTurn, Job, LlmCall, Presentation,
    SceneConversionJob, dumps, loads, session_scope,
)
from backend.app.providers import ProviderEvent
from backend.app.rendering import validate_html
from backend.app.workflows import DESIGN, PPT
from backend.tests.test_provider_management import (  # noqa: F401
    create_model, create_provider, managed_client, seed_default,
)


def design_body(width=1080, height=1920, pages=2):
    return {
        "user_request": "Create a series of event posters",
        "entry_mode": "design",
        "options": {"canvas": {"width": width, "height": height}, "page_count": pages},
    }


@pytest.fixture
def design_client(client, monkeypatch):
    calls = []
    output = {"pages": 2}

    async def stream(**kwargs):
        calls.append(kwargs)
        if kwargs.get("json_schema"):
            doc = document()
            doc["slides"] = doc["slides"][:output["pages"]]
            text = json.dumps(doc)
        else:
            match = re.search(r"固定画布 (\d+)×(\d+)", kwargs["instructions"])
            assert match, kwargs["instructions"]
            width, height = match.groups()
            text = valid_html().replace("800px", width + "px").replace("450px", height + "px")
        yield ProviderEvent("text_delta", text)
        yield ProviderEvent("completed", {"text": text, "usage": {}})

    monkeypatch.setattr(runtime, "stream_responses", stream)
    return client, calls, output


@pytest.mark.parametrize("width,height", [(1080, 1920), (1080, 1080), (1280, 720)])
def test_design_outline_confirmation_html_and_real_scene(design_client, width, height):
    client, calls, _ = design_client
    body = design_body(width, height)
    headers = {"Idempotency-Key": f"design-{width}-{height}"}
    response = client.post("/api/v1/presentations", json=body, headers=headers)
    assert response.status_code == 202, response.text
    assert client.post("/api/v1/presentations", json=body, headers=headers).json() == response.json()
    conflict = design_body(900, 1200)
    assert client.post("/api/v1/presentations", json=conflict, headers=headers).status_code == 409
    pid = response.json()["presentation_id"]
    snapshot = wait_status(client, pid, {"WAITING_CONFIRMATION", "FAILED"})
    assert snapshot["presentation"]["status"] == "WAITING_CONFIRMATION", snapshot
    assert snapshot["presentation"]["canvas"] == {"width": width, "height": height}
    assert snapshot["presentation"]["workflow_code"] == DESIGN.code
    assert snapshot["pages"] == [] and len(calls) == 1
    assert "DesignPlanner" in calls[0]["instructions"]
    with session_scope() as db:
        config = loads(db.get(Presentation, pid).config_snapshot_json)
        assert set(config["resolved_routes"]) == {"default", "design_outline", "design_html"}
        assert db.scalar(select(ConversationTurn).where(ConversationTurn.presentation_id == pid)).workflow_code == DESIGN.code
        assert db.scalar(select(Job).where(Job.presentation_id == pid)).model_id == config["resolved_routes"]["design_outline"]["model_id"]
    outline = snapshot["latest_outline"]
    outline["document"]["slides"][0]["design_notes"] = "Use generous margins and a prominent event title"
    edited = client.put(f"/api/v1/presentations/{pid}/outline", json={
        "outline_id": outline["id"], "content_hash": outline["content_hash"], "document": outline["document"],
    })
    assert edited.status_code == 200
    outline = edited.json()
    assert client.post(f"/api/v1/presentations/{pid}/confirm", json={
        "outline_id": outline["id"], "content_hash": outline["content_hash"],
    }).status_code == 202
    snapshot = wait_status(client, pid, {"READY", "PARTIAL_FAILED"})
    assert snapshot["presentation"]["status"] == "READY", [(p["error_code"], p["error"]) for p in snapshot["pages"]]
    assert len(calls) == 3 and all("DesignPageComposer" in call["instructions"] for call in calls[1:])
    assert any("Use generous margins" in call["prompt"] for call in calls[1:])
    until = time.monotonic() + 15
    while time.monotonic() < until:
        snapshot = client.get(f"/api/v1/presentations/{pid}").json()
        if all(p["scene"]["status"] in {"SUCCEEDED", "FAILED"} for p in snapshot["pages"]):
            break
        time.sleep(.05)
    for page in snapshot["pages"]:
        assert page["scene"]["status"] == "SUCCEEDED", page["scene"]
        assert page["scene"]["ref"]["schema_version"] == "0.5.0"
        scene = client.get(f'/api/v1/scenes/{page["scene"]["scene_id"]}').json()
        assert scene["page"]["width"] == width and scene["page"]["height"] == height
        html = client.get(f'/api/v1/page-tasks/{page["id"]}/html').text
        assert validate_html(html, canvas=(width, height), require_profile=True)[0]
    with session_scope() as db:
        jobs = db.scalars(select(SceneConversionJob).where(SceneConversionJob.presentation_id == pid)).all()
        assert all((job.canvas_width, job.canvas_height) == (width, height) for job in jobs)
        assert all(job.converter_version == scene_service.DESIGN_CONVERTER_VERSION for job in jobs)
        assert {call.business_code for call in db.scalars(select(LlmCall).where(LlmCall.presentation_id == pid))} == {"design_outline", "design_html"}
    assert not snapshot["export_source"]["can_export"]
    exported = client.post(f"/api/v1/presentations/{pid}/exports", json={"expected_source_hash": "a" * 64})
    assert exported.status_code == 409 and exported.json()["detail"]["code"] == "EXPORT_NOT_SUPPORTED"


@pytest.mark.parametrize("canvas", [
    {"width": 800, "height": 450}, {"width": 1080, "height": 1920},
    {"width": 900, "height": 1200}, {"width": 1920, "height": 1080},
])
def test_design_html_size_contract(canvas):
    size = canvas["width"], canvas["height"]
    html = valid_html().replace("800px", f"{size[0]}px").replace("450px", f"{size[1]}px")
    assert validate_html(html, canvas=size, require_profile=True)[0]
    assert not validate_html(html.replace(f"{size[1]}px", "319px"), canvas=size, require_profile=True)[0]
    assert not validate_html(html.replace('data-css-profile="tailwind-ppt-v1"', ""), canvas=size, require_profile=True)[0]


@pytest.mark.parametrize("options", [
    {"canvas": {"width": 319, "height": 450}},
    {"canvas": {"width": 1920, "height": 1920}},
    {"canvas": {"width": 1080.5, "height": 1920}},
    {"canvas": {"width": True, "height": 1920}},
    {"canvas": {"width": 1080, "height": 1920}, "page_count": 0},
    {"canvas": {"width": 1080, "height": 1920}, "page_count": 21},
    {"canvas": {"width": 1080, "height": 1920}, "page_count": "2"},
])
def test_design_rejects_invalid_create_options(client, options):
    assert client.post("/api/v1/presentations", json={**design_body(), "options": options}).status_code == 422


def test_page_count_failure_retry_keeps_frozen_route_budget_and_canvas(design_client):
    client, calls, output = design_client
    assert client.put("/api/v1/settings/generation", json={"reasoning_effort": "low"}).status_code == 200
    created = client.post("/api/v1/presentations", json=design_body(pages=1)).json()
    pid = created["presentation_id"]
    failed = wait_status(client, pid, {"FAILED"})
    assert failed["latest_run"]["error_code"] == "DESIGN_PAGE_COUNT_MISMATCH"
    assert failed["pages"] == []
    output["pages"] = 1
    # Existing documents retain both stage routes when the live defaults change.
    assert client.put("/api/v1/settings/generation", json={"reasoning_effort": "max"}).status_code == 200
    assert client.post(f"/api/v1/presentations/{pid}/retry").status_code == 202
    ready = wait_status(client, pid, {"WAITING_CONFIRMATION", "FAILED"})
    assert ready["presentation"]["status"] == "WAITING_CONFIRMATION", ready
    assert ready["presentation"]["canvas"] == {"width": 1080, "height": 1920}
    assert [call["reasoning_effort"] for call in calls] == ["low", "low"]
    assert "design_planner" in runtime.OUTLINE_GRAPHS[DESIGN.code].nodes
    assert "design_page_composer" in runtime.PAGE_GRAPHS[DESIGN.code].nodes
    assert runtime.OUTLINE_GRAPHS[DESIGN.code] is not runtime.OUTLINE_GRAPHS[PPT.code]


def test_design_route_unavailable_does_not_block_ppt_or_agent(managed_client):
    provider = create_provider(managed_client)
    model = create_model(managed_client, provider)
    seed_default(model)
    assert managed_client.post(f'/api/v1/settings/llm/models/{model["id"]}/test').json()["status"] == "passed"
    with session_scope() as db:
        db.add(BusinessRouteOverride(code="design_html", model_id="missing-design-model"))
    response = managed_client.post("/api/v1/presentations", json=design_body())
    assert response.status_code == 404
    for mode in ("ppt", "agent"):
        created = managed_client.post("/api/v1/presentations", json={"user_request": "Test", "entry_mode": mode})
        assert created.status_code == 202, created.text
        assert created.json()["workflow_code"] == PPT.code
