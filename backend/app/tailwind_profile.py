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


def _utility_catalog(classes: tuple[str, ...]) -> str:
    """Describe the exact manifest with finite, lossless brace choices."""
    families: dict[str, list[str]] = {}
    literals = []
    for token in classes:
        prefix, separator, suffix = token.rpartition("-")
        if not separator or token.startswith("-"):
            literals.append(token)
        else:
            families.setdefault(prefix, []).append(suffix)
    groups: dict[tuple[str, ...], list[str]] = {}
    for prefix, suffixes in families.items():
        groups.setdefault(tuple(sorted(suffixes)), []).append(prefix)

    def choices(values) -> str:
        return values[0] if len(values) == 1 else "{" + ",".join(values) + "}"

    return " ".join(literals + [
        choices(prefixes) + "-" + choices(suffixes)
        for suffixes, prefixes in groups.items()
    ])


COMPOSER_UTILITIES = _utility_catalog(TAILWIND_CLASSES)


@lru_cache(maxsize=1)
def compiled_tailwind_css() -> str:
    return (_ROOT / "tailwind-ppt-v1.css").read_text(encoding="utf-8")


def preview_html(html: str, *, css: str | None = None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main", id="ppt-page")
    if root is None or root.get("data-css-profile") != TAILWIND_PROFILE:
        return html

    style = soup.new_tag("style", attrs={"data-tailwind-profile": TAILWIND_PROFILE})
    style.string = compiled_tailwind_css() if css is None else css
    soup.head.insert(0, style)
    return str(soup)
