"""设计师 SVG 上传前的清洗。

Illustrator / 浏览器导出或手工改过的 SVG 常见三类问题：

1. ``font-family`` 属性里嵌套了未转义的双引号；
2. 文本或属性里有孤立的 ``&``；
3. 只有 ``width`` / ``height`` 或只有 ``viewBox``，缺少另一侧。

这里只做最小修复，保留原有图层、文字与样式，不改动视觉内容。
"""

from __future__ import annotations

import re
from typing import Any

from lxml import etree

from app.errors import TemplateInvalid

_CDATA_RE = re.compile(r"(<!\[CDATA\[.*?\]\]>)", re.DOTALL)
_ENTITY_RE = re.compile(r"&(?!(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);)")
_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_FONT_FAMILY_RE = re.compile(
    r'(font-family\s*=\s*)(")(.*?)(")(?=\s*(?:[A-Za-z_:][-A-Za-z0-9_:.]*\s*=|/?>))',
    re.DOTALL,
)
_FONT_FAMILY_SINGLE_RE = re.compile(
    r"(font-family\s*=\s*)(')(.*?)(')(?=\s*(?:[A-Za-z_:][-A-Za-z0-9_:.]*\s*=|/?>))",
    re.DOTALL,
)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FALLBACK_FONT = "Alibaba PuHuiTi 3.0"


def _known_font_names() -> set[str]:
    from app.services import fonts

    faces = fonts.discover_fonts()
    known: set[str] = set()
    for face in faces:
        for name in [*face.families, *face.aliases]:
            if name:
                known.add(name.strip().casefold())
    return known


def _map_font_stack(value: str, known: set[str]) -> tuple[str, bool]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    mapped: list[str] = []
    changed = False
    for item in items:
        name = item.strip().strip("\"'")
        if name.casefold() in known:
            mapped.append(item)
        else:
            mapped.append(f"'{_FALLBACK_FONT}'")
            changed = True
    result: list[str] = []
    for item in mapped:
        if item not in result:
            result.append(item)
    return ", ".join(result), changed


def _node_font_size(element: etree._Element) -> float:
    node: etree._Element | None = element
    while node is not None:
        raw = node.get("font-size")
        if raw:
            match = _NUMBER_RE.search(raw)
            if match:
                try:
                    return float(match.group(0))
                except ValueError:
                    pass
        node = node.getparent()
    return 0.0


def _is_bold_node(element: etree._Element, family: str) -> bool:
    weight = (element.get("font-weight") or "").strip().lower()
    if weight in {"bold", "bolder"}:
        return True
    match = _NUMBER_RE.search(weight)
    if match:
        try:
            if float(match.group(0)) >= 600:
                return True
        except ValueError:
            pass
    lowered = family.casefold()
    if any(token in lowered for token in ("bold", "heavy", "black", "semibold")):
        return True
    return _node_font_size(element) >= 60


def _map_style_fonts(value: str, known: set[str]) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        mapped, changed = _map_font_stack(match.group(2), known)
        if changed:
            count += 1
        return match.group(1) + mapped

    return (
        re.sub(
            r"(font-family\s*:\s*)([^;]+)",
            replace,
            value,
            flags=re.IGNORECASE,
        ),
        count,
    )


_WEIGHT_TOKENS = (
    ("105", 800),
    ("heavy", 800),
    ("115", 900),
    ("black", 900),
    ("95", 850),
    ("extrabold", 850),
    ("85", 700),
    ("bold", 700),
    ("75", 600),
    ("semibold", 600),
    ("65", 500),
    ("medium", 500),
    ("55", 400),
    ("regular", 400),
    ("45", 300),
    ("light", 300),
    ("35", 200),
    ("thin", 200),
)
_FONT_DECLARATION = re.compile(r"(^|;)\s*(font-family|font-weight)\s*:\s*([^;]*)(?=;|$)", re.I)
_IMPORTANT = re.compile(r"\s*!\s*important\s*$", re.I)
_CSS_WIDE_KEYWORDS = {"inherit", "initial", "unset", "revert", "revert-layer"}
_LEGACY_WEIGHTS = {
    "h": 800,
    "bl": 900,
    "b": 700,
    "eb": 850,
    "s": 600,
    "m": 500,
    "r": 400,
    "l": 300,
    "t": 200,
}


def _suffix_weight(family: str) -> int | None:
    for name in family.casefold().split(","):
        name = name.strip().strip("\"'")
        name = re.sub(r"(?:[-_ ]+gbpc-euc-h|_l\d+)$", "", name)
        legacy = re.fullmatch(r"alibaba[-_ ]?puhuiti[-_ ]?([a-z]{1,2})", name)
        if legacy and legacy[1] in _LEGACY_WEIGHTS:
            return _LEGACY_WEIGHTS[legacy[1]]
        modern = re.fullmatch(r"alibaba[-_ ]?puhuiti[-_ ]?3(?:\.0)?[-_ ]+(\d{2,3})(?:[-_ ]*[a-z][a-z0-9]*)?", name)
        if modern:
            weight = dict(_WEIGHT_TOKENS).get(modern[1])
            if weight is not None:
                return weight
        if re.match(r"alibaba[-_ ]?puhuiti(?:\b|[_-])", name):
            for token, weight in _WEIGHT_TOKENS:
                if re.search(rf"(?<![a-z0-9]){token}(?![a-z0-9])", name):
                    return weight
    return None


def _normalize_unsuffixed_style(style: str) -> str:
    def replace(match: re.Match[str]) -> str:
        prefix, name, raw = match.groups()
        priority = _IMPORTANT.search(raw)
        value = _IMPORTANT.sub("", raw).strip()
        if name.lower() == "font-family" and value.lower() not in _CSS_WIDE_KEYWORDS and not value.lower().startswith("var("):
            value = f"'{_FALLBACK_FONT}'"
        elif name.lower() == "font-weight" and value.lower() == "bold":
            value = "700"
        else:
            return match[0]
        return f"{prefix}{name}:{value}{priority[0] if priority else ''}"

    return _FONT_DECLARATION.sub(replace, style)


def _font_declarations(style: str) -> dict[str, tuple[str, bool]]:
    declarations: dict[str, tuple[str, bool]] = {}
    for match in _FONT_DECLARATION.finditer(style):
        name, raw = match[2].lower(), match[3].strip()
        important = bool(_IMPORTANT.search(raw))
        if important or not declarations.get(name, ("", False))[1]:
            declarations[name] = (_IMPORTANT.sub("", raw), important)
    return declarations


def _set_font_declarations(style: str, weight: int, *, family: bool, important: bool = False) -> str:
    properties = _font_declarations(style)
    style = _FONT_DECLARATION.sub(lambda m: "" if family or m[2].lower() == "font-weight" else m[0], style)
    parts = [style.strip("; \t\r\n")]
    if family:
        priority = " !important" if important or properties.get("font-family", ("", False))[1] else ""
        parts.append(f"font-family:'{_FALLBACK_FONT}'{priority}")
    priority = " !important" if important or properties.get("font-weight", ("", False))[1] else ""
    parts.append(f"font-weight:{weight}{priority}")
    return ";".join(part for part in parts if part)


def _normalize_font_families(root: etree._Element) -> list[str]:
    """把所有字体统一成 Alibaba PuHuiTi 3.0，并用 font-weight 区分字重。"""
    count = 0
    style_elements = [element for element in root.iter() if _local(element.tag) == "style" and element.text]
    has_font_rules = any(re.search(r"\bfont(?:-family|-weight)?\s*:", element.text, re.I) for element in style_elements)
    for element in root.iter():
        if _local(element.tag) == "style":
            continue
        family = element.get("font-family") or ""
        style = element.get("style") or ""
        properties = _font_declarations(style)
        family_text = properties.get("font-family", ("", False))[0]
        suffix = _suffix_weight(family_text) if family_text else None
        if suffix is None and family:
            suffix = _suffix_weight(family)
        if family and family.strip().lower() not in _CSS_WIDE_KEYWORDS and not family.strip().lower().startswith("var("):
            element.set("font-family", f"'{_FALLBACK_FONT}'")
        if suffix is not None:
            element.set("font-weight", str(suffix))
            # Presentation attributes lose to inline declarations and stylesheet rules.
            if family_text or "font-weight" in properties or has_font_rules or re.search(r"(^|;)\s*font\s*:", style, re.I):
                element.set("style", _set_font_declarations(style, suffix, family=bool(family_text) or has_font_rules, important=has_font_rules))
        else:
            if (element.get("font-weight") or "").strip().lower() == "bold":
                element.set("font-weight", "700")
            updated = _normalize_unsuffixed_style(style)
            if updated != style:
                element.set("style", updated)
        if family or family_text:
            count += 1

    for style_element in style_elements:
        def fix_rule(match: re.Match[str]) -> str:
            nonlocal count
            selector, block = match.groups()
            if selector.lstrip().lower().startswith("@font-face"):
                return match[0]
            properties = _font_declarations(block)
            family, _ = properties.get("font-family", ("", False))
            suffix = _suffix_weight(family)
            if family:
                count += 1
            updated = (_set_font_declarations(block, suffix, family=True, important=True)
                       if suffix is not None else _normalize_unsuffixed_style(block))
            return selector + "{" + updated + "}"

        style_element.text = re.sub(r"([^{}]+)\{([^{}]*)\}", fix_rule, style_element.text)
    if count:
        return [f"统一 {count} 处字体为 {_FALLBACK_FONT}（按字重匹配）"]
    return []


def _fix_font_family_quotes(text: str) -> tuple[str, bool]:
    changed = False

    def fix_tag(match: re.Match[str]) -> str:
        nonlocal changed
        tag = match.group(0)
        if "font-family" not in tag:
            return tag
        original = tag

        def double(match2: re.Match[str]) -> str:
            body = match2.group(3)
            if '"' not in body:
                return match2.group(0)
            body = body.replace('"', "&quot;")
            return f'{match2.group(1)}"{body}"'

        def single(match2: re.Match[str]) -> str:
            body = match2.group(3)
            if "'" not in body:
                return match2.group(0)
            body = body.replace("'", "&apos;")
            return f"{match2.group(1)}'{body}'"

        tag = _FONT_FAMILY_RE.sub(double, tag)
        tag = _FONT_FAMILY_SINGLE_RE.sub(single, tag)
        if tag != original:
            changed = True
        return tag

    return _TAG_RE.sub(fix_tag, text), changed


def _escape_ampersands(text: str) -> tuple[str, bool]:
    changed = False
    parts = _CDATA_RE.split(text)
    for index, part in enumerate(parts):
        if part.startswith("<![CDATA["):
            continue
        replaced = _ENTITY_RE.sub("&amp;", part)
        if replaced != part:
            changed = True
            parts[index] = replaced
    return "".join(parts), changed


def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _float_attr(element: etree._Element, name: str) -> float | None:
    raw = element.get(name)
    if not raw:
        return None
    match = _NUMBER_RE.search(raw)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _ensure_canvas(root: etree._Element) -> list[str]:
    notes: list[str] = []
    cfg = root.get("viewBox")
    width = _float_attr(root, "width")
    height = _float_attr(root, "height")

    viewbox: tuple[float, float, float, float] | None = None
    if cfg:
        values = [float(value) for value in _NUMBER_RE.findall(cfg)]
        if len(values) == 4 and values[2] > 0 and values[3] > 0:
            viewbox = (values[0], values[1], values[2], values[3])

    if viewbox is None and width and height and width > 0 and height > 0:
        viewbox = (0.0, 0.0, width, height)
        root.set("viewBox", "0 0 %s %s" % (round(width, 3), round(height, 3)))
        notes.append("补充 viewBox")
    elif viewbox is not None and (not width or not height):
        root.set("width", str(round(viewbox[2], 3)))
        root.set("height", str(round(viewbox[3], 3)))
        notes.append("按 viewBox 补充 width/height")

    if viewbox is None:
        raise TemplateInvalid("SVG 缺少有效的 viewBox / width / height，无法确定画布尺寸")
    return notes


def sanitize_svg(raw: bytes) -> tuple[bytes, list[str]]:
    """清洗 SVG 并返回 (规范化字节, 修复说明)。"""
    if not raw:
        raise TemplateInvalid("SVG 内容为空")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TemplateInvalid(f"SVG 不是 UTF-8 编码：{exc}") from exc

    notes: list[str] = []
    text, quoted = _fix_font_family_quotes(text)
    if quoted:
        notes.append("修复 font-family 内嵌引号")
    text, escaped = _escape_ampersands(text)
    if escaped:
        notes.append("转义孤立 &")

    parser = etree.XMLParser(recover=True, huge_tree=True, remove_blank_text=False)
    try:
        root = etree.fromstring(text.encode("utf-8"), parser=parser)
    except Exception as exc:
        raise TemplateInvalid(f"SVG 无法解析：{exc}") from exc
    if root is None or _local(root.tag) != "svg":
        raise TemplateInvalid("上传文件不是有效的 SVG（根节点不是 <svg>）")

    notes.extend(_normalize_font_families(root))
    notes.extend(_ensure_canvas(root))
    cleaned = etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)
    return cleaned, notes


def sanitize_generated_svg(raw: bytes) -> tuple[bytes, list[str]]:
    """Validate and isolate generated SVG before preview or inline editing."""
    import hashlib

    import tinycss2

    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    try:
        root = etree.fromstring(raw.decode("utf-8-sig").encode("utf-8"), parser)
    except (etree.XMLSyntaxError, UnicodeError) as exc:
        raise TemplateInvalid("生成结果不是有效的 UTF-8 SVG") from exc
    svg_ns = "http://www.w3.org/2000/svg"
    if root.tag not in {"svg", f"{{{svg_ns}}}svg"} or root.getroottree().docinfo.doctype:
        raise TemplateInvalid("生成结果必须是独立 SVG 文档，且不能包含 DTD")

    scope_value = hashlib.sha256(raw).hexdigest()[:20]
    selector_prefix = f'[data-generated-svg="{scope_value}"]'

    def safe_tokens(tokens):
        for token in tokens:
            if token.type == "error":
                return False
            if token.type == "url" and not token.value.startswith("#"):
                return False
            if token.type == "function":
                if token.lower_name in {"url", "src", "image", "image-set", "-webkit-image-set"}:
                    args = [arg for arg in token.arguments if arg.type not in {"whitespace", "comment"}]
                    if token.lower_name != "url" or len(args) != 1 or args[0].type != "string" or not args[0].value.startswith("#"):
                        return False
                elif not safe_tokens(token.arguments):
                    return False
            if token.type in {"() block", "[] block", "{} block"} and not safe_tokens(token.content):
                return False
        return True

    def clean_declarations(value):
        rules = tinycss2.parse_declaration_list(value, skip_comments=True, skip_whitespace=True)
        excluded = {"behavior", "position", "z-index", "pointer-events", "cursor", "content", "-moz-binding"}
        return tinycss2.serialize([rule for rule in rules if rule.type == "declaration"
                                   and rule.lower_name not in excluded and safe_tokens(rule.value)])

    def clean_stylesheet(value):
        rules = tinycss2.parse_stylesheet(value, skip_comments=True, skip_whitespace=True)
        css = []
        for rule in rules:
            if rule.type != "qualified-rule" or not safe_tokens(rule.prelude):
                continue
            selectors = [[]]
            for token in rule.prelude:
                if token.type == "literal" and token.value == ",":
                    selectors.append([])
                else:
                    selectors[-1].append(token)
            names = [tinycss2.serialize(tokens).strip() for tokens in selectors]
            names = [name for name in names if name and "&" not in name]
            scoped = [f"{selector_prefix} :is({name}), {selector_prefix}:is({name})" for name in names]
            if scoped:
                css.append(", ".join(scoped) + "{" + clean_declarations(rule.content) + "}")
        return "\n".join(css)

    allowed = set("svg g defs symbol use title desc metadata path rect circle ellipse line polyline polygon text tspan textPath style linearGradient radialGradient stop pattern clipPath mask marker filter feBlend feColorMatrix feComponentTransfer feComposite feConvolveMatrix feDiffuseLighting feDisplacementMap feDistantLight feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR feGaussianBlur feMerge feMergeNode feMorphology feOffset fePointLight feSpecularLighting feSpotLight feTile feTurbulence".split())
    for element in list(root.iter()):
        if element is not root and element.getparent() is None:
            continue
        if not isinstance(element.tag, str):
            element.getparent().remove(element)
            continue
        qname = etree.QName(element)
        if qname.namespace not in {None, svg_ns} or qname.localname not in allowed:
            element.getparent().remove(element)
            continue
        if qname.namespace is None:
            element.tag = f"{{{svg_ns}}}{qname.localname}"
        for attr, value in list(element.attrib.items()):
            key = etree.QName(attr).localname.lower() if attr.startswith("{") else attr.lower()
            if (key.startswith(("on", "x-", "@", ":", "data-")) or key == "base"
                    or (key in {"href", "src"} and not value.strip().startswith("#"))):
                element.attrib.pop(attr)
            elif key == "style":
                element.set(attr, clean_declarations(value))
            elif not safe_tokens(tinycss2.parse_component_value_list(value)):
                element.attrib.pop(attr)
        if qname.localname == "style":
            element.text = clean_stylesheet("".join(element.itertext()))
            del element[:]
    root.set("data-generated-svg", scope_value)
    # Prefer the default namespace so HTML insertion sees an SVG element.
    etree.cleanup_namespaces(root, top_nsmap={None: svg_ns})
    if root.prefix is not None:
        namespaced = etree.Element(f"{{{svg_ns}}}svg", nsmap={None: svg_ns,
                                   "xlink": "http://www.w3.org/1999/xlink"}, attrib=dict(root.attrib))
        namespaced.text = root.text
        namespaced.extend(root)
        root = namespaced
    notes = _ensure_canvas(root)
    return etree.tostring(root, encoding="utf-8", xml_declaration=True), notes


def clean_svg_content(raw: bytes) -> tuple[bytes, list[str]]:
    """兼容命名：清洗 SVG 内容。"""
    return sanitize_svg(raw)
