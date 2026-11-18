"""Tailwind profile compatibility and bounded per-page utility compilation."""

from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess

from bs4 import BeautifulSoup
from .font_map import normalize_font_html, normalize_font_stylesheet


_ROOT = Path(__file__).resolve().parent
_PROJECT_ROOT = _ROOT.parents[1]
_RUNTIME_COMPILER = _PROJECT_ROOT / "tools" / "compile-tailwind-runtime.mjs"
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
log = logging.getLogger(__name__)

MAX_RUNTIME_CANDIDATES = 4096
MAX_RUNTIME_CLASS_LENGTH = 256
MAX_RUNTIME_CSS_BYTES = 512 * 1024
RUNTIME_COMPILE_TIMEOUT = float(os.getenv("TAILWIND_COMPILE_TIMEOUT_SECONDS", "12"))


@lru_cache(maxsize=1)
def compiled_tailwind_css() -> str:
    return (_ROOT / "tailwind-ppt-v1.css").read_text(encoding="utf-8")


def html_classes(html: str) -> tuple[str, ...]:
    """Extract bounded class candidates without interpreting author CSS/HTML."""
    soup = BeautifulSoup(html, "html.parser")
    values = {
        token
        for element in soup.find_all(True)
        for token in (element.get("class") or [])
        if isinstance(token, str)
    }
    if len(values) > MAX_RUNTIME_CANDIDATES:
        raise ValueError("TAILWIND_CANDIDATE_LIMIT")
    if any(
        len(token) > MAX_RUNTIME_CLASS_LENGTH
        or any(ord(char) < 0x20 for char in token)
        for token in values
    ):
        raise ValueError("TAILWIND_CANDIDATE_INVALID")
    return tuple(sorted(values))


@lru_cache(maxsize=256)
def compile_tailwind_candidates(classes: tuple[str, ...]) -> str:
    """Compile exactly the supplied candidates with the pinned local Tailwind."""
    if not classes:
        return ""
    if len(classes) > MAX_RUNTIME_CANDIDATES or any(
        len(token) > MAX_RUNTIME_CLASS_LENGTH or "\n" in token or "\r" in token
        for token in classes
    ):
        raise ValueError("TAILWIND_CANDIDATE_INVALID")
    node = shutil.which("node") or shutil.which("node.exe")
    if not node or not _RUNTIME_COMPILER.exists():
        raise RuntimeError("TAILWIND_COMPILER_UNAVAILABLE")
    try:
        result = subprocess.run(
            [node, str(_RUNTIME_COMPILER), str(_PROJECT_ROOT)],
            input=json.dumps({"classes": classes}, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=RUNTIME_COMPILE_TIMEOUT,
            check=False,
            cwd=str(_PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("TAILWIND_COMPILE_TIMEOUT") from exc
    except OSError as exc:
        raise RuntimeError(f"TAILWIND_COMPILER_OS_ERROR:{exc.__class__.__name__}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")[:300]
        raise RuntimeError(f"TAILWIND_COMPILE_FAILED:{detail}")
    css = result.stdout
    if len(css.encode("utf-8")) > MAX_RUNTIME_CSS_BYTES:
        raise ValueError("TAILWIND_CSS_LIMIT")
    # Arbitrary values are useful for layout and color, but generated CSS must
    # never turn into a network/resource channel. Reject the compiled artifact
    # rather than trying to sanitize a partially expanded rule set.
    if re.search(r"(?i)(?:url\s*\(|@import\b|@plugin\b|@config\b|expression\s*\()", css):
        raise ValueError("TAILWIND_CSS_UNSAFE")
    return css


def css_for_html(html: str) -> str:
    """Compile this page's utilities, falling back to the frozen profile.

    Compilation is an enhancement to the conversion path. A compiler outage,
    candidate limit, or unsupported arbitrary value must leave the page
    renderable with all known profile utilities rather than fail generation.
    The source HTML remains unchanged, so the converter can still diagnose the
    portions whose CSS was not materialized.
    """
    try:
        root = BeautifulSoup(html, "html.parser").find("main", id="ppt-page")
        classes = html_classes(html)
        if root and root.get("data-css-profile") == TAILWIND_PROFILE and set(classes) <= TAILWIND_CLASS_SET:
            return normalize_font_stylesheet(compiled_tailwind_css())
        return normalize_font_stylesheet(compile_tailwind_candidates(classes))
    except (RuntimeError, ValueError, OSError) as exc:
        # A local compiler outage or an unsafe/oversized candidate must not
        # erase a page's usable legacy styles. The unresolved class remains in
        # the source and the converter retains its best-effort diagnostics.
        log.warning("Tailwind runtime compilation fell back to the frozen profile: %s", exc)
        return normalize_font_stylesheet(compiled_tailwind_css())


def preview_html(html: str, *, css: str | None = None) -> str:
    html, _ = normalize_font_html(html)
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main", id="ppt-page")
    if root is None:
        return html
    if css is None:
        # Preview tickets can be requested before Scene conversion finishes.
        # Compile the page candidates here as well, so the relaxed Composer
        # contract still renders Tailwind utilities in that early preview.
        css = css_for_html(html)
    if not css:
        return html

    style = soup.new_tag(
        "style",
        attrs={
            "data-tailwind-profile": TAILWIND_PROFILE,
            "data-tailwind-runtime": "true",
        },
    )
    style.string = css
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)
    head.insert(0, style)
    return str(soup)
