import re

from bs4 import BeautifulSoup
from sqlalchemy import select

from ..db import ConversationMessage, Outline, PageTask, Presentation, Run, loads
from ..scene_service import find_scene, source_hash


def deck_index(db, subject_id, *, outline=None, batch=None):
    if not subject_id:
        return None
    subject = db.get(Presentation, subject_id)
    if batch:
        outline = db.get(Outline, batch.outline_id)
    elif outline is None:
        outline = db.scalar(select(Outline).where(Outline.presentation_id == subject_id).order_by(Outline.revision.desc()).limit(1))
        if outline:
            batch = db.scalar(select(Run).where(Run.outline_id == outline.id))
    document = loads(outline.document_json) if outline else {}
    slides = sorted(document.get("slides", []), key=lambda slide: slide["page_number"])
    scope = PageTask.batch_id == batch.id if batch else PageTask.batch_id.is_(None)
    tasks = db.scalars(select(PageTask).where(PageTask.presentation_id == subject_id, scope)).all()
    pages = {page.slide_id: page for page in tasks}
    index = []
    for slide in slides:
        page = pages.get(slide["slide_id"])
        index.append({"number": slide["page_number"], "slide_id": slide["slide_id"], "title": slide["title"][:160],
                      "plan": slide["key_message"][:240], "page_id": page.id if page else None,
                      "html_hash": source_hash(db, page) if page else None, "state": page.status if page else "PLANNED"})
    latest = db.scalar(select(Run).where(Run.batch_id == batch.id).order_by(Run.created_at.desc(), Run.id.desc())) if batch else None
    return {"id": subject_id, "state": latest.status if latest else "PLANNED" if outline else "DRAFT",
            "title": document.get("brief", {}).get("title", subject.user_request[:100]),
            "batch_id": batch.id if batch else None, "revision": outline.revision if outline else None,
            "outline_id": outline.id if outline else None, "outline_hash": outline.content_hash if outline else None,
            "confirmed": bool(outline and outline.confirmed), "pages": index}


def card_index(db, work, card):
    if not card or card.conversation_id != work.conversation_id:
        return None
    payload = loads(card.payload_json)
    if card.kind == "outline_card" and payload.get("outline_id"):
        outline = db.get(Outline, payload["outline_id"])
        if outline and outline.presentation_id == work.id:
            return deck_index(db, work.id, outline=outline)
    if card.kind == "generation_card":
        attempt = db.get(Run, payload["run_id"]) if payload.get("run_id") else None
        batch_id = payload.get("batch_id") or (attempt.batch_id if attempt else None)
        batch = db.get(Run, batch_id) if batch_id else None
        if batch and batch.presentation_id == work.id and batch.outline_id:
            return deck_index(db, work.id, batch=batch)
    return None


def project_references(db, work, user_text, card_id):
    outline_versions = {int(version) for version in re.findall(r"(?<![A-Za-z0-9_])v([0-9]{1,9})(?![A-Za-z0-9_])", user_text, re.I)}
    explicit = None
    if len(outline_versions) == 1:
        outline = db.scalar(select(Outline).where(Outline.presentation_id == work.id, Outline.revision == next(iter(outline_versions))))
        if outline:
            explicit = deck_index(db, work.id, outline=outline)
    card = db.get(ConversationMessage, card_id) if card_id else None
    selected = card_index(db, work, card)
    candidates = []
    for kind in ("outline_card", "generation_card"):
        cards = db.scalars(select(ConversationMessage).where(
            ConversationMessage.conversation_id == work.conversation_id, ConversationMessage.kind == kind,
        ).order_by(ConversationMessage.seq.desc()).limit(3)).all()
        for item in cards:
            if index := card_index(db, work, item):
                candidates.append((item.id, index))
    return explicit, selected, candidates, bool(outline_versions and not explicit)


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
