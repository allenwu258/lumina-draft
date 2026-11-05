from __future__ import annotations

import hashlib
import re
from bs4 import BeautifulSoup
import tinycss2


MAX_HTML_BYTES = 256 * 1024


def html_hash(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def validate_html(html: str) -> tuple[bool, str]:
    if not html or len(html.encode("utf-8")) > MAX_HTML_BYTES:
        return False, "HTML 为空或超过 256 KiB"
    if "```" in html or re.search(r"^\s*(Here|说明|解释)[:：]", html, re.I):
        return False, "输出包含解释或 Markdown 围栏"
    soup = BeautifulSoup(html, "html.parser")
    if len(soup.find_all("html")) != 1 or len(soup.find_all("head")) != 1 or len(soup.find_all("body")) != 1:
        return False, "必须输出完整 HTML 文档"
    roots = soup.find_all(id="ppt-page")
    if len(roots) != 1:
        return False, "必须有唯一 id=ppt-page 的主画布"
    root = roots[0]
    if soup.find(["script", "iframe", "object", "embed", "form", "base", "link", "img", "audio", "video", "source", "foreignobject", "animate", "set", "use"]):
        return False, "包含不允许的主动或外部资源元素"
    if soup.find("meta", attrs={"http-equiv": True}):
        return False, "不允许 meta http-equiv"
    for element in soup.find_all(True):
        for attr, value in element.attrs.items():
            if attr.lower().startswith("on"):
                return False, "包含事件处理器"
            if attr.lower() in {"href", "src", "action", "formaction", "xlink:href", "srcset"}:
                return False, "包含 URL 属性"
            if attr.lower() == "style" and not safe_css(str(value), declarations=True):
                return False, "内联 CSS 包含不允许的资源或表达式"
    styles = "\n".join(s.get_text(" ") for s in soup.find_all("style"))
    if not safe_css(styles):
        return False, "CSS 包含不允许的资源或表达式"
    combined = styles + " " + str(root.get("style", ""))
    if not re.search(r"width\s*:\s*800px\b", combined, re.I) or not re.search(r"height\s*:\s*450px\b", combined, re.I):
        return False, "画布必须声明 width:800px 和 height:450px"
    return True, "ok"


def safe_css(css: str, declarations: bool = False) -> bool:
    nodes = tinycss2.parse_declaration_list(css, skip_whitespace=True, skip_comments=True) if declarations else tinycss2.parse_stylesheet(css, skip_whitespace=True, skip_comments=True)
    def safe_tokens(tokens) -> bool:
        for token in tokens:
            kind = getattr(token, "type", "")
            if kind in {"error", "url", "at-rule"}:
                return False
            if kind == "function" and token.lower_name in {"url", "expression", "image", "image-set", "cross-fade"}:
                return False
            for attr in ("content", "arguments", "value", "prelude"):
                children = getattr(token, attr, None)
                if isinstance(children, list) and not safe_tokens(children):
                    return False
        return True
    return safe_tokens(nodes)

