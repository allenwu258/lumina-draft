"""The fixed Tailwind utility contract shared by prompt, admission and preview."""

from functools import lru_cache
import json
from pathlib import Path

from bs4 import BeautifulSoup


_ROOT = Path(__file__).resolve().parent
_MANIFEST = json.loads((_ROOT / "tailwind-ppt-v1.json").read_text(encoding="utf-8"))
TAILWIND_PROFILE = _MANIFEST["profile"]
TAILWIND_CLASSES = tuple(_MANIFEST["classes"])
TAILWIND_CLASS_SET = frozenset(TAILWIND_CLASSES)


@lru_cache(maxsize=1)
def compiled_tailwind_css() -> str:
    return (_ROOT / "tailwind-ppt-v1.css").read_text(encoding="utf-8")


def preview_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main", id="ppt-page")
    if root is None or root.get("data-css-profile") != TAILWIND_PROFILE:
        return html

    style = soup.new_tag("style", attrs={"data-tailwind-profile": TAILWIND_PROFILE})
    style.string = compiled_tailwind_css()
    soup.head.insert(0, style)
    return str(soup)
