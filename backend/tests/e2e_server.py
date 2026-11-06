"""Local-only browser test server. All model calls and secrets stay in memory."""

import json
import os
import tempfile
from pathlib import Path

_temporary = tempfile.TemporaryDirectory(prefix="lumina-provider-e2e-")
os.environ["DATABASE_URL"] = (
    "sqlite:///" + (Path(_temporary.name) / "test.db").as_posix()
)
os.environ["DS_API_FILE"] = str(Path(_temporary.name) / "missing.txt")
os.environ.pop("DS_API_KEY", None)

from backend.app import db, main, provider_api, runtime  # noqa: E402
from backend.app.providers import ProviderEvent  # noqa: E402

_secrets = {}
for module in (db, provider_api):
    module.save_secret = lambda ref, value: _secrets.__setitem__(ref, value)
for module in (main, provider_api, runtime):
    module.read_secret = lambda ref: _secrets.get(ref)
provider_api.delete_secret = lambda ref: _secrets.pop(ref, None) is not None


async def fake_stream(**kwargs):
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
        text = '<!doctype html><html><head><style>#ppt-page{width:800px;height:450px}</style></head><body><main id="ppt-page">Provider 验收完成</main></body></html>'
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


if __name__ == "__main__":
    import uvicorn

    try:
        uvicorn.run(main.app, host="127.0.0.1", port=8000, access_log=False)
    finally:
        db.engine.dispose()
        _temporary.cleanup()
