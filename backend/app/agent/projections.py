from bs4 import BeautifulSoup
from sqlalchemy import select

from ..db import Outline, PageTask, Presentation, loads
from ..scene_service import find_scene, source_hash


def deck_index(db, subject_id):
    if not subject_id:
        return None
    subject = db.get(Presentation, subject_id)
    outline = db.scalar(select(Outline).where(Outline.presentation_id == subject_id).order_by(Outline.revision.desc()).limit(1))
    document = loads(outline.document_json) if outline else {}
    slides = sorted(document.get("slides", []), key=lambda slide: slide["page_number"])
    tasks = db.scalars(select(PageTask).where(PageTask.presentation_id == subject_id)).all()
    pages = {page.slide_id: page for page in tasks}
    index = []
    for slide in slides:
        page = pages.get(slide["slide_id"])
        index.append({"number": slide["page_number"], "slide_id": slide["slide_id"], "title": slide["title"][:160],
                      "plan": slide["key_message"][:240], "page_id": page.id if page else None,
                      "html_hash": source_hash(db, page) if page else None, "state": page.status if page else "PLANNED"})
    return {"id": subject_id, "state": subject.status, "title": document.get("brief", {}).get("title", subject.user_request[:100]),
            "outline_id": outline.id if outline else None, "outline_hash": outline.content_hash if outline else None,
            "confirmed": bool(outline and outline.confirmed), "pages": index}


def inspect_ppt(db, index, view, numbers):
    if not index:
        return {"status": "unavailable"}
    if view == "deck":
        return {"status": "ok", "source": "index", "state": index["state"], "pages": [
            {"number": page["number"], "title": page["title"], "state": page["state"]} for page in index["pages"]]}
    outline = db.get(Outline, index["outline_id"]) if index["outline_id"] else None
    if not outline or outline.content_hash != index["outline_hash"]:
        return {"status": "unavailable" if not outline else "source_changed"}
    doc = loads(outline.document_json)
    by_number = {page["number"]: page for page in index["pages"]}
    if any(number not in by_number for number in numbers):
        return {"status": "invalid_ref"}
    if view == "outline":
        selected = [slide for slide in doc["slides"] if slide["page_number"] in numbers]
        return {"source": "outline", "ref": outline.content_hash, "status": "ok", "brief": doc["brief"],
                "style": doc["style_spec"]["description"], "slides": [{"number": slide["page_number"],
                "title": slide["title"][:160], "points": [point[:240] for point in slide["content_outline"][:6]]} for slide in selected]}
    return {"pages": [page_excerpt(db, by_number[number]) for number in numbers]}


def page_excerpt(db, ref):
    result = {"number": ref["number"], "ref": ref["html_hash"]}
    page = db.get(PageTask, ref["page_id"]) if ref["page_id"] else None
    if not page or page.status != "SUCCEEDED":
        return {**result, "status": "planned_only", "plan": ref["plan"]}
    if source_hash(db, page) != ref["html_hash"]:
        return {**result, "status": "source_changed"}
    scene = find_scene(db, page)
    if scene:
        scene_data = loads(scene.scene_json)
        items = [item for item in scene_data["textInventory"] if (item.get("target") or {}).get("kind") != "hidden" and (item.get("target") or item.get("nodeId"))]
        text = "\n".join(item["sourceText"] for item in items)
        return {**result, "status": "ok", "source": "scene", "scene_id": scene.id,
                "text": text[:1800], "truncated": len(text) > 1800,
                "diagnostics": [item["code"] for item in scene_data["diagnostics"][:8]]}
    if page.html:
        root = BeautifulSoup(page.html, "html.parser").find(id="ppt-page")
        text = root.get_text(" ", strip=True) if root else ""
        return {**result, "status": "ok", "source": "html", "text": text[:1800], "truncated": len(text) > 1800}
    return {**result, "status": "unavailable"}
