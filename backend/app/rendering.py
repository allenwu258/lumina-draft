from __future__ import annotations

import hashlib
import re
from bs4 import BeautifulSoup
import tinycss2
from .tailwind_profile import TAILWIND_CLASS_SET, TAILWIND_PROFILE
from .workflows import PPT_CANVAS


MAX_HTML_BYTES = 256 * 1024


def html_hash(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def validate_html(
    html: str, *, canvas: tuple[int, int] = PPT_CANVAS, require_profile: bool = False
) -> tuple[bool, str]:
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
    profile = root.get("data-css-profile")
    if require_profile and (profile != TAILWIND_PROFILE or root.name != "main" or root.parent != soup.body):
        return False, "设计页面必须使用受控 CSS profile，并以 main#ppt-page 作为主画布"
    if profile and profile != TAILWIND_PROFILE:
        return False, "不支持的页面 CSS profile"
    if profile == TAILWIND_PROFILE:
        valid, reason = validate_tailwind_page(soup, root, canvas)
        if not valid:
            return valid, reason
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
    width, height = canvas
    if not re.search(rf"width\s*:\s*{width}px\b", combined, re.I) or not re.search(rf"height\s*:\s*{height}px\b", combined, re.I):
        return False, f"画布必须声明 width:{width}px 和 height:{height}px"
    return True, "ok"


def validate_tailwind_page(soup, root, canvas: tuple[int, int] = PPT_CANVAS) -> tuple[bool, str]:
    classes = [token for element in soup.find_all(True) for token in element.get("class", [])]
    if not classes:
        return False, "Tailwind 页面必须使用受控 utility class"
    if any(token not in TAILWIND_CLASS_SET for token in classes):
        return False, "页面使用了未登记的 Tailwind class"
    if any(element.has_attr("style") for element in soup.find_all(True)):
        return False, "Tailwind 页面不允许 style 属性"

    styles = soup.find_all("style")
    if len(styles) != 1:
        return False, "Tailwind 页面必须只有一条画布 CSS 规则"
    rules = tinycss2.parse_stylesheet(styles[0].get_text(), skip_whitespace=True, skip_comments=True)
    if len(rules) != 1 or rules[0].type != "qualified-rule" or tinycss2.serialize(rules[0].prelude).strip() != "#ppt-page":
        return False, "Tailwind 页面只允许 #ppt-page 画布规则"
    declarations = tinycss2.parse_declaration_list(rules[0].content, skip_whitespace=True, skip_comments=True)
    if any(item.type != "declaration" or item.important for item in declarations):
        return False, "Tailwind 画布规则格式无效"
    values = {item.lower_name: tinycss2.serialize(item.value).strip() for item in declarations}
    required = {
        "width": f"{canvas[0]}px",
        "height": f"{canvas[1]}px",
        "box-sizing": "border-box",
        "overflow": "hidden",
    }
    if any(values.get(name) != value for name, value in required.items()):
        return False, f"Tailwind 画布尺寸必须为 {canvas[0]}x{canvas[1]} CSS 像素，且为 border-box、overflow:hidden"
    colors = ["--ppt-background", "--ppt-surface", "--ppt-primary", "--ppt-text", "--ppt-muted"]
    if any(not re.fullmatch(r"#[0-9a-fA-F]{6}", values.get(name, "")) for name in colors):
        return False, "Tailwind 配色变量必须使用六位十六进制颜色"
    sizes = ["--ppt-title-size", "--ppt-body-size", "--ppt-caption-size"]
    if any(not re.fullmatch(r"(?:[8-9]|[1-5][0-9]|6[0-4])px", values.get(name, "")) for name in sizes):
        return False, "Tailwind 字号变量必须为 8 至 64px"
    font = values.get("--ppt-font-family", "")
    if not font or not re.fullmatch(r"[A-Za-z0-9 ,_'\"-]+", font):
        return False, "Tailwind 字体变量格式无效"
    if set(values) != set(required) | set(colors) | set(sizes) | {"--ppt-font-family"}:
        return False, "Tailwind 画布规则包含未知属性"
    if "bg-ppt-background" not in root.get("class", []):
        return False, "Tailwind 画布必须使用 bg-ppt-background"
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

