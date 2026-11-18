"""Windows font stack normalization shared by Composer preview and capture."""

from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import subprocess

import tinycss2
from bs4 import BeautifulSoup


_MAP_PATH = (
    Path(__file__).resolve().parents[2]
    / "packages/scene-core/contracts/font-map-win32.json"
)
FONT_MAP = json.loads(_MAP_PATH.read_text(encoding="utf-8"))
FONT_MAP_VERSION = FONT_MAP["version"]
_ALIASES = {key.casefold(): value for key, value in FONT_MAP["aliases"].items()}
_GENERICS = {key.casefold(): value for key, value in FONT_MAP["generics"].items()}
_DEFAULTS = FONT_MAP["defaults"]["stack"]
_FONT_SIZE = re.compile(
    r"(?i)(?:xx-small|x-small|small|medium|large|x-large|xx-large|xxx-large|larger|smaller|"
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:px|pt|pc|in|cm|mm|q|em|rem|ex|ch|vw|vh|vmin|vmax|%))"
    r"(?:\s*/\s*(?:normal|(?:\d+(?:\.\d*)?|\.\d+)(?:(?:px|pt|pc|in|cm|mm|q|em|rem|ex|ch|vw|vh|vmin|vmax|%)?)))?\s+"
)


def _name_key(value: str) -> str:
    return " ".join(value.strip().strip("\"'").split()).casefold()


@lru_cache(maxsize=1)
def installed_win32_families() -> frozenset[str]:
    """Read installed display names from Windows font registry without fuzzy matching."""
    if os.name != "nt":
        return frozenset()
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$keys=@('HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts',"
        "'HKCU:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts');"
        "foreach($key in $keys){if(Test-Path $key){"
        "$item=Get-ItemProperty $key;"
        "$item.PSObject.Properties | Where-Object {$_.Name -notmatch '^PS'} | "
        "ForEach-Object {$_.Name} }}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset(_name_key(name) for name in FONT_MAP["defaults"]["stack"])
    names = set()
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            name = re.sub(r"\s+\((?:TrueType|OpenType)\)\s*$", "", line.strip())
            if not name:
                continue
            names.add(_name_key(name))
            # Registry labels sometimes combine family names for one TTC file.
            names.update(_name_key(part) for part in re.split(r"\s*&\s*", name))
    return frozenset(names)


def _split_families(tokens) -> list[list]:
    families: list[list] = [[]]
    for token in tokens:
        if token.type == "literal" and token.value == ",":
            families.append([])
        else:
            families[-1].append(token)
    return families


def _family_name(tokens) -> str:
    meaningful = [
        token for token in tokens if token.type not in {"whitespace", "comment"}
    ]
    if len(meaningful) == 1 and meaningful[0].type == "string":
        return meaningful[0].value.strip()
    return tinycss2.serialize(tokens).strip().strip("\"'")


def _quote_family(name: str) -> str:
    if re.fullmatch(r"[-_a-zA-Z][-_a-zA-Z0-9]*", name):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def normalize_font_stack(
    value: str, installed: frozenset[str] | set[str] | None = None
) -> str:
    """Prefer any installed source name, then aliases, then explicit defaults."""
    tokens = tinycss2.parse_component_value_list(value)
    groups = [
        group for group in _split_families(tokens) if tinycss2.serialize(group).strip()
    ]
    if not groups:
        return ", ".join(_quote_family(name) for name in _DEFAULTS)
    installed_names = (
        installed_win32_families()
        if installed is None
        else {_name_key(name) for name in installed}
    )
    exact: list[str] = []
    mapped: list[str] = []
    generics: list[str] = []
    opaque: list[str] = []
    for group in groups:
        name = _family_name(group)
        key = _name_key(name)
        if key in installed_names:
            exact.append(tinycss2.serialize(group).strip())
        elif key in _ALIASES:
            mapped.append(_quote_family(_ALIASES[key]))
        elif key in _GENERICS:
            generics.extend(_quote_family(family) for family in _GENERICS[key])
        elif any(token.type == "function" for token in group):
            opaque.append(tinycss2.serialize(group).strip())

    result: list[str] = []
    seen: set[str] = set()
    for family in [
        *opaque,
        *exact,
        *mapped,
        *generics,
        *(_quote_family(name) for name in _DEFAULTS),
    ]:
        key = _name_key(family)
        if key not in seen:
            seen.add(key)
            result.append(family)
    return ", ".join(result)


def _normalize_font_shorthand(value: str, installed) -> str | None:
    match = _FONT_SIZE.search(value)
    if not match:
        return None
    prefix, family_stack = value[: match.end()].rstrip(), value[match.end() :].strip()
    if not family_stack:
        return None
    return f"{prefix} {normalize_font_stack(family_stack, installed)}"


def _rewrite_declarations(nodes, installed) -> bool:
    changed = False
    for node in nodes:
        if getattr(node, "type", None) != "declaration":
            continue
        name = node.lower_name
        value = tinycss2.serialize(node.value)
        normalized = (
            normalize_font_stack(value, installed)
            if name == "font-family" or name == "--ppt-font-family"
            else _normalize_font_shorthand(value, installed) if name == "font" else None
        )
        if normalized is not None and normalized != value.strip():
            node.value = tinycss2.parse_component_value_list(normalized)
            changed = True
    return changed


def _rewrite_rules(rules, installed) -> bool:
    changed = False
    nested_rules = {
        "media",
        "supports",
        "layer",
        "container",
        "scope",
        "starting-style",
    }
    for rule in rules:
        if getattr(rule, "type", None) == "qualified-rule":
            declarations = tinycss2.parse_declaration_list(
                rule.content, skip_whitespace=False, skip_comments=False
            )
            if _rewrite_declarations(declarations, installed):
                rule.content = declarations
                changed = True
        elif getattr(rule, "type", None) == "at-rule" and rule.content is not None:
            if rule.lower_at_keyword in nested_rules:
                nested = tinycss2.parse_rule_list(
                    rule.content, skip_whitespace=False, skip_comments=False
                )
                if _rewrite_rules(nested, installed):
                    rule.content = nested
                    changed = True
            else:
                declarations = tinycss2.parse_declaration_list(
                    rule.content, skip_whitespace=False, skip_comments=False
                )
                if _rewrite_declarations(declarations, installed):
                    rule.content = declarations
                    changed = True
    return changed


def normalize_font_stylesheet(css: str) -> str:
    if os.name != "nt" or not css:
        return css
    rules = tinycss2.parse_stylesheet(css, skip_whitespace=False, skip_comments=False)
    if not _rewrite_rules(rules, installed_win32_families()):
        return css
    return tinycss2.serialize(rules)


def normalize_font_html(html: str) -> tuple[str, bool]:
    if os.name != "nt" or not html:
        return html, False
    soup = BeautifulSoup(html, "html.parser")
    installed = installed_win32_families()
    changed = False
    for element in soup.find_all(True):
        if element.has_attr("style"):
            declarations = tinycss2.parse_declaration_list(
                str(element["style"]), skip_whitespace=False, skip_comments=False
            )
            if _rewrite_declarations(declarations, installed):
                element["style"] = tinycss2.serialize(declarations)
                changed = True
    for style in soup.find_all("style"):
        rules = tinycss2.parse_stylesheet(
            style.get_text(), skip_whitespace=False, skip_comments=False
        )
        if _rewrite_rules(rules, installed):
            style.string = tinycss2.serialize(rules)
            changed = True
    return (str(soup) if changed else html), changed
