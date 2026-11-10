"""Local-only browser test server. All model calls and secrets stay in memory."""

import json
import asyncio
import os
import tempfile
from pathlib import Path

_temporary = tempfile.TemporaryDirectory(prefix="lumina-provider-e2e-")
os.environ["DATABASE_URL"] = (
    "sqlite:///" + (Path(_temporary.name) / "test.db").as_posix()
)
os.environ["DS_API_FILE"] = str(Path(_temporary.name) / "missing.txt")
os.environ["SCENE_DATA_DIR"] = str(Path(_temporary.name) / "scenes")
os.environ["PPTX_EXPORT_DIR"] = str(Path(_temporary.name) / "exports")
os.environ.pop("DS_API_KEY", None)

from backend.app import db, main, provider_api, runtime  # noqa: E402
from backend.app.providers import ProviderEvent  # noqa: E402

_secrets = {}
for module in (db, provider_api):
    module.save_secret = lambda ref, value: _secrets.__setitem__(ref, value)
for module in (main, provider_api, runtime):
    module.read_secret = lambda ref: _secrets.get(ref)
provider_api.delete_secret = lambda ref: _secrets.pop(ref, None) is not None
_html_release = asyncio.Event()
_html_release.set()
_active_html = 0
_web_origin = "http://127.0.0.1:" + os.getenv("E2E_WEB_PORT", "15173")


@main.app.middleware("http")
async def isolated_browser_origin(request, call_next):
    # Keep production's loopback Origin/CSP policy unchanged on isolated test ports.
    if request.headers.get("origin") == _web_origin:
        request.scope["headers"] = [
            (name, b"http://127.0.0.1:5173" if name == b"origin" else value)
            for name, value in request.scope["headers"]
        ]
    response = await call_next(request)
    if policy := response.headers.get("content-security-policy"):
        response.headers["content-security-policy"] = policy.replace(
            "http://127.0.0.1:5173", _web_origin
        )
    return response


async def fake_stream(**kwargs):
    global _active_html
    schema = kwargs.get("json_schema") or {}
    if "ok" in schema.get("properties", {}):
        text = '{"ok":true}'
    elif schema:
        text = json.dumps(
            {
                "schema_version": "1.0",
                "brief": {
                    "title": "Provider 验收作品",
                    "purpose": "验证模型路由",
                    "audience": "开发团队",
                    "language": "zh-CN",
                    "tone": "专业",
                    "assumptions": [],
                },
                "style_spec": {
                    "description": "清晰简洁",
                    "colors": {
                        "background": "#FFFFFF",
                        "surface": "#FFFFFF",
                        "primary": "#7056D8",
                        "text": "#222222",
                        "muted_text": "#888888",
                    },
                    "typography": {
                        "font_family": "Arial",
                        "title_size_px": 32,
                        "body_size_px": 18,
                        "caption_size_px": 12,
                    },
                    "layout_rules": ["保持清晰"],
                },
                "slides": [
                    {
                        "page_number": 1,
                        "page_type": "content",
                        "title": "闭环验证",
                        "key_message": "路由生效",
                        "content_outline": ["模型独立选择"],
                        "design_notes": "简洁",
                        "summary": "验证独立模型",
                    }
                ],
            },
            ensure_ascii=False,
        )
    elif kwargs.get("max_output_tokens"):
        text = "OK"
    else:
        text = '<!doctype html><html><head><meta charset="utf-8"><style>#ppt-page{width:800px;height:450px;box-sizing:border-box;overflow:hidden;--ppt-background:#F8FAFC;--ppt-surface:#FFFFFF;--ppt-primary:#2563EB;--ppt-text:#1F2937;--ppt-muted:#64748B;--ppt-title-size:32px;--ppt-body-size:18px;--ppt-caption-size:12px;--ppt-font-family:Arial,sans-serif}</style></head><body><main id="ppt-page" data-css-profile="tailwind-ppt-v1" class="bg-ppt-background p-8"><div class="flex items-center gap-4 bg-ppt-surface p-4"><h1 id="tailwind-proof" class="font-ppt text-ppt-title font-semibold text-ppt-text">Provider 验收完成</h1></div></main></body></html>'
        _active_html += 1
        try:
            await _html_release.wait()
        finally:
            _active_html -= 1
    if schema and "concurrency-check" in kwargs.get("prompt", ""):
        doc = json.loads(text)
        doc["slides"] = [
            dict(doc["slides"][0], page_number=n, title=f"Page {n}")
            for n in range(1, 21)
        ]
        text = json.dumps(doc, ensure_ascii=False)
    yield ProviderEvent("text_delta", text)
    yield ProviderEvent(
        "completed",
        {
            "text": text,
            "response_id": "e2e-response",
            "usage": {"input_tokens": 5, "output_tokens": 10, "total_tokens": 15},
        },
    )


runtime.stream_responses = fake_stream


@main.app.get("/mock/v1/models")
def fixture_models():
    return {
        "data": [
            {"id": "outline-model", "name": "Outline Model", "context_length": 128000},
            {"id": "html-model", "name": "HTML Model"},
        ]
    }


@main.app.post("/api/v1/mock/html-capacity")
async def html_capacity(body: dict):
    if body.get("hold"):
        _html_release.clear()
    else:
        _html_release.set()
    return {"active": _active_html}


@main.app.get("/api/v1/mock/html-capacity")
async def html_capacity_status():
    return {"active": _active_html}


if __name__ == "__main__":
    import uvicorn

    try:
        uvicorn.run(
            main.app,
            host="127.0.0.1",
            port=int(os.getenv("E2E_API_PORT", "18000")),
            access_log=False,
        )
    finally:
        db.engine.dispose()
        _temporary.cleanup()
