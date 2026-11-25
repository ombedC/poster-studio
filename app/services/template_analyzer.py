"""SVG 海报模板解析。

读取设计师导出的原始 .svg（文字未转曲、图层 ID 可能缺失或随机），
提取文字/图片节点与坐标，再用多模态大模型推断业务字段，产出并缓存
``data/templates/<template_id>/schema.json``。

没有配置大模型或调用失败时，会退化为规则推断（``source="heuristic"``），
保证离线也能完成一次可用的人工校对。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import tempfile
from urllib.parse import quote
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from lxml import etree

from app.config import settings
from app.errors import TemplateInvalid, TemplateNotFound
from app.services import llm_client, poster_semantics, svg_browser

logger = logging.getLogger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

SCHEMA_VERSION = 16

TEXT_FIELDS = (
    "main_title",
    "subtitle",
    "activity_tag",
    "slogan",
    "course_title",
    "course_subtitle",
    "lesson_topic",
    "lesson_topic_sub",
    "teacher_name",
    "teacher_school",
    "intro",
    "schedule",
    "org",
    "qr_tip",
    "footer",
    "other",
)
IMAGE_FIELDS = ("portrait_frame", "qr_frame", "background", "decoration", "logo", "other")

FIELD_LABELS = {
    "main_title": "主标题",
    "subtitle": "副标题",
    "activity_tag": "活动标签",
    "slogan": "活动宣传语",
    "course_title": "课程标题",
    "course_subtitle": "课程副标题",
    "lesson_topic": "课题",
    "lesson_topic_sub": "课题副标题",
    "teacher_name": "老师姓名",
    "teacher_school": "学校 / 机构",
    "intro": "老师简介",
    "schedule": "上课时间",
    "org": "主办方",
    "qr_tip": "二维码引导语",
    "footer": "页脚",
    "other": "其他文本",
    "portrait_frame": "人像框",
    "qr_frame": "二维码框",
    "background": "背景",
    "decoration": "装饰图层",
    "logo": "Logo",
}

_CIRCLED_REF = re.compile(r"url\(\s*#([^)\s]+)\s*\)")
_TRANSLATE_RE = re.compile(r"translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)")
_TEMPLATE_ID_RE = re.compile(r"^[0-9a-f]{12}$")


# ---------------------------------------------------------------------------
# SVG 基础解析
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _parse_svg(data: bytes) -> etree._Element:
    if not data:
        raise TemplateInvalid("SVG 内容为空")
    parser = etree.XMLParser(recover=True, huge_tree=True)
    try:
        root = etree.fromstring(data, parser=parser)
    except Exception as exc:
        raise TemplateInvalid(f"SVG 解析失败：{exc}") from exc
    if root is None or _local(root.tag) != "svg":
        raise TemplateInvalid("不是有效的 SVG 文件")
    return root


def _canvas_size(root: etree._Element) -> tuple[float, float]:
    view_box = root.get("viewBox")
    if view_box:
        parts = [float(v) for v in re.split(r"[\s,]+", view_box.strip()) if v]
        if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
            return parts[2], parts[3]
    width = _length_to_px(root.get("width"))
    height = _length_to_px(root.get("height"))
    if width and height:
        return width, height
    raise TemplateInvalid("SVG 缺少 viewBox 或 width/height，无法确定画布尺寸")


def _length_to_px(value: str | None) -> float:
    if not value:
        return 0.0
    match = re.match(r"^\s*(-?[\d.]+)\s*(px|mm|cm|in|pt)?", value)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = (match.group(2) or "px").lower()
    factors = {"px": 1.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "pt": 96 / 72}
    return number * factors.get(unit, 1.0)


def _translate(element: etree._Element) -> tuple[float, float]:
    match = _TRANSLATE_RE.search(element.get("transform") or "")
    if not match:
        return 0.0, 0.0
    return float(match.group(1)), float(match.group(2))


def _text_origin(element: etree._Element) -> tuple[float, float]:
    """文字锚点：优先 transform translate，其次 x/y 属性。"""
    x, y = _translate(element)
    if x == 0.0 and y == 0.0:
        try:
            x = float(element.get("x") or 0)
            y = float(element.get("y") or 0)
        except (TypeError, ValueError):
            return 0.0, 0.0
    return x, y


def _text_font_size(element: etree._Element) -> float:
    for candidate in [element, *list(element)]:
        raw = candidate.get("font-size")
        if raw:
            try:
                return float(raw)
            except ValueError:
                continue
    return 0.0


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _text_stroke(element: etree._Element) -> dict[str, Any] | None:
    """提取文字描边（兼容属性与内联 style）。"""
    node: etree._Element | None = element
    while node is not None:
        color = node.get("stroke")
        width = node.get("stroke-width")
        style = node.get("style") or ""
        color_match = re.search(r"stroke\s*:\s*([^;]+)", style, re.IGNORECASE)
        width_match = re.search(r"stroke-width\s*:\s*([^;]+)", style, re.IGNORECASE)
        if color_match:
            color = color_match.group(1).strip()
        if width_match:
            width = width_match.group(1).strip()
        if color and color.strip().lower() not in {"none", "transparent", ""}:
            try:
                value = float(re.sub(r"[^0-9.]", "", width or "1") or 1)
            except ValueError:
                value = 1.0
            return {
                "stroke_color": color.strip(),
                "stroke_width": round(max(0.1, value), 2),
            }
        node = node.getparent()
    return None


def _remove_redundant_text_layers(root: etree._Element) -> list[dict[str, Any]]:
    """移除坐标几乎重合、内容高度相似的多余 <text> 图层（发光/误复制）。

    同一位置通常有一层深色描边或投影、一层正文，后绘制的在上层。这里保留文档顺序
    靠后的那一层，移除前面的重复层，避免替换文字后出现重影。
    """
    items: list[dict[str, Any]] = []
    for element in root.iter():
        if _local(element.tag) != "text":
            continue
        content = "".join(element.itertext()).strip()
        if not content:
            continue
        x, y = _text_origin(element)
        items.append(
            {
                "element": element,
                "id": element.get("id"),
                "text": content,
                "normalized": _normalize_text(content),
                "x": x,
                "y": y,
                "font": _text_font_size(element),
            }
        )

    redundant: set[int] = set()
    removed: list[dict[str, Any]] = []
    for index, current in enumerate(items):
        if id(current["element"]) in redundant:
            continue
        for other in items[index + 1 :]:
            if id(other["element"]) in redundant:
                continue
            distance = math.hypot(current["x"] - other["x"], current["y"] - other["y"])
            if distance > 5:
                continue
            similarity = SequenceMatcher(
                None, current["normalized"], other["normalized"]
            ).ratio()
            if similarity < 0.9:
                continue
            if abs(current["font"] - other["font"]) > 1.5:
                continue
            # 深色描边层通常就是先绘制、被判定为冗余的那一层，保留到上层
            removed_stroke = _text_stroke(current["element"])
            if removed_stroke and not _text_stroke(other["element"]):
                kept = other["element"]
                kept.set("stroke", removed_stroke["stroke_color"])
                kept.set("stroke-width", str(removed_stroke["stroke_width"]))
            redundant.add(id(current["element"]))
            removed.append(
                {
                    "removed_id": current["id"],
                    "removed_text": current["text"][:80],
                    "kept_id": other["id"],
                    "distance": round(distance, 2),
                    "similarity": round(similarity, 3),
                    "stroke": removed_stroke,
                }
            )
            break

    for item in items:
        element = item["element"]
        if id(element) not in redundant:
            continue
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    return removed


def _assign_ids(root: etree._Element) -> None:
    """给文字 / tspan / image / clipPath 形状补稳定 ID（已有 ID 原样保留）。"""
    existing = {el.get("id") for el in root.iter() if el.get("id")}
    counters: dict[str, int] = {}

    def ensure(element: etree._Element, prefix: str) -> str:
        current = element.get("id")
        if current:
            return current
        while True:
            counters[prefix] = counters.get(prefix, 0) + 1
            candidate = f"{prefix}-{counters[prefix]:03d}"
            if candidate not in existing:
                break
        element.set("id", candidate)
        existing.add(candidate)
        return candidate

    for element in root.iter():
        local = _local(element.tag)
        if local == "text":
            ensure(element, "text")
        elif local == "tspan":
            ensure(element, "tspan")
        elif local == "image":
            ensure(element, "image")
        elif local == "rect" and (element.get("rx") or element.get("ry")):
            ensure(element, "layout-rect")

    for clip in root.iter():
        if _local(clip.tag) != "clipPath":
            continue
        for child in clip:
            ensure(child, "clip")


_TSPAN_RUN_ATTRS = (
    "font-size",
    "font-family",
    "font-weight",
    "font-style",
    "fill",
    "fill-opacity",
    "stroke",
    "stroke-width",
    "letter-spacing",
    "text-anchor",
    "dominant-baseline",
    "style",
    "class",
)


def _flatten_tspan_runs(root: etree._Element) -> None:
    """把带嵌套子 tspan 的外层 tspan 的直接文字拆成独立 tspan。

    Illustrator 有时写成 ``<tspan font-size=40>数学 | <tspan fill=...>寻根之旅</tspan></tspan>``，
    外层前半截没有自己的叶子节点；不拆的话 schema 里就找不到“数学 |”。这里只做结构拆分，
    不改变字体/颜色/位置，随后再统一分配 ID。
    """
    parents = [node for node in root.iter() if _local(node.tag) == "tspan"]
    for parent in parents:
        children = [child for child in parent if _local(child.tag) == "tspan"]
        if not children:
            continue

        def make_run(content: str) -> etree._Element:
            run = etree.Element(parent.tag)
            for attr in _TSPAN_RUN_ATTRS:
                value = parent.get(attr)
                if value is not None:
                    run.set(attr, value)
            if parent.get("{http://www.w3.org/XML/1998/namespace}space"):
                run.set(
                    "{http://www.w3.org/XML/1998/namespace}space",
                    parent.get("{http://www.w3.org/XML/1998/namespace}space"),
                )
            run.text = content
            return run

        if parent.text and parent.text.strip():
            run = make_run(parent.text)
            parent.text = None
            parent.insert(0, run)
        for child in list(parent):
            if child.tail and child.tail.strip():
                run = make_run(child.tail)
                child.tail = None
                parent.insert(list(parent).index(child) + 1, run)


def _inherited_attr(element: etree._Element, name: str) -> str | None:
    node: etree._Element | None = element
    while node is not None:
        value = node.get(name)
        if value:
            return value
        node = node.getparent()
    return None


def _strip_position_arrays(root: etree._Element) -> None:
    """Illustrator writes one absolute x/y per glyph; keep only the first origin."""
    for element in root.iter():
        for name in ("x", "y"):
            raw = element.get(name)
            if not raw:
                continue
            parts = [part for part in re.split(r"[\s,]+", raw.strip()) if part]
            if len(parts) > 1:
                element.set(name, parts[0])


def _run_line_y(element: etree._Element) -> float | None:
    """Baseline offset within the nearest <text>; None when the run is shifted."""
    node: etree._Element | None = element
    while node is not None and _local(node.tag) != "text":
        dy = node.get("dy")
        if dy:
            try:
                if abs(float(re.split(r"[\s,]+", dy.strip())[0])) > 0.5:
                    return None
            except (ValueError, IndexError):
                return None
        y = node.get("y")
        if y:
            try:
                return float(re.split(r"[\s,]+", y.strip())[0])
            except (ValueError, IndexError):
                return None
        node = node.getparent()
    return 0.0


def _run_style(element: etree._Element) -> tuple[float, str, str]:
    """Resolved (font-size, font-weight, fill) used to tell styled runs apart."""

    def inherited(name: str) -> str:
        return re.sub(r"\s+", "", _inherited_attr(element, name) or "").lower()

    try:
        size = float(re.sub(r"[^0-9.]", "", inherited("font-size")) or 0)
    except ValueError:
        size = 0.0
    return (round(size, 1), inherited("font-weight"), inherited("fill"))


def _runs_mergeable(left: etree._Element, right: etree._Element) -> bool:
    """True only for adjacent same-line runs with identical type styling."""
    if len(left) or len(right):
        return False
    if left.get("transform") or right.get("transform") or left.get("rotate") or right.get("rotate"):
        return False
    left_y, right_y = _run_line_y(left), _run_line_y(right)
    if left_y is None or right_y is None or abs(left_y - right_y) >= 5:
        return False
    # A later absolute origin cannot position the earlier, flowing text.
    if right.get("x") is not None and (
        left.get("x") is None
        or _local(left.getparent().tag) == "tspan"
        or _inherited_attr(left, "text-anchor") not in (None, "start")
    ):
        return False
    if any(_inherited_attr(left, attr) != _inherited_attr(right, attr)
           for attr in ("text-anchor", "class", "style")):
        return False
    return _run_style(left) == _run_style(right)


def _merge_identical_runs(root: etree._Element) -> int:
    """Join Illustrator's per-glyph <tspan> fragments back into one editable run.

    Keep explicit origins inside nested lines and leave a flowing run separate
    from a following absolute run. The schema can still bind them as one field.
    """
    merged = 0
    for container in [el for el in root.iter() if _local(el.tag) in {"text", "tspan"}]:
        previous: etree._Element | None = None
        for child in list(container):
            if _local(child.tag) != "tspan":
                previous = None
                continue
            if previous is not None and _runs_mergeable(previous, child):
                content = "".join(child.itertext())
                previous.text = (previous.text or "") + content
                if (previous.get("letter-spacing") or "") != (child.get("letter-spacing") or ""):
                    previous.attrib.pop("letter-spacing", None)
                container.remove(child)
                merged += 1
                continue
            previous = child
    return merged


def _fold_first_run(text: etree._Element) -> bool:
    """Fold the text's own first-line run in, so the line is one selectable field."""
    children = [child for child in text if _local(child.tag) == "tspan"]
    if not children or children[0] is not text[0]:
        return False
    run = children[0]
    if len(run) or run.get("transform") or run.get("rotate"):
        return False
    if _run_line_y(run) != 0:
        return False
    # A differently styled run on the same line is a separate field; leave it alone.
    if any(child is not run and len(child) == 0 and _run_line_y(child) == 0 for child in children):
        return False
    if _run_style(run) != _run_style(text):
        return False
    if any(run.get(attr) is not None for attr in ("text-anchor", "letter-spacing", "class", "style", "dx")):
        return False
    if not (text.text or "").strip() and (run.get("x") or "").strip() not in {"", "0", "0.0"}:
        return False
    content = "".join(run.itertext())
    if not content.strip():
        return False
    text.text = (text.text or "") + content + (run.tail or "")
    run.tail = None
    text.remove(run)
    return True


def _collect_text_leaves(root: etree._Element) -> list[dict[str, Any]]:
    """把每个 ``<text>`` 拆成可独立替换的片段。

    * ``<text>`` 的直接文字（在第一个 tspan 之前）单独成片；
    * 连续、同字号、同 y 的 tspan 合并成一行（避免逐字拆分）；
    * 其他 tspan 各自成片（例如学校名与老师姓名、两行简介）。
    """
    leaves: list[dict[str, Any]] = []
    for text in root.iter():
        if _local(text.tag) != "text":
            continue
        text_id = text.get("id")
        base_font = text.get("font-size")
        tspans = [
            node
            for node in text.iter()
            if _local(node.tag) == "tspan"
            and not any(_local(child.tag) == "tspan" for child in node.iterdescendants())
        ]

        direct = (text.text or "").strip()
        if direct:
            next_abs_x = 0.0
            if tspans:
                origin_x, _ = _translate(text)
                try:
                    next_abs_x = origin_x + float(tspans[0].get("x") or 0)
                except (TypeError, ValueError):
                    next_abs_x = 0.0
            leaves.append(
                {
                    "id": text_id,
                    "kind": "text",
                    "parent_id": text_id,
                    "line_index": 0,
                    "text": direct,
                    "element": text,
                    "member_ids": [text_id],
                    "preserve_children": bool(tspans),
                    "collapse": False,
                    "next_abs_x": next_abs_x,
                    "font_size_attr": base_font,
                    "fill": text.get("fill"),
                    "anchor": text.get("text-anchor"),
                    "stroke": _text_stroke(text),
                }
            )

        groups: list[list[etree._Element]] = []
        for tspan in tspans:
            content = "".join(tspan.itertext()).strip()
            if not content:
                continue
            font = _inherited_attr(tspan, "font-size") or base_font
            try:
                font_key = round(float(font), 1) if font else 0.0
            except ValueError:
                font_key = 0.0
            fill_key = (
                _inherited_attr(tspan, "fill") or text.get("fill") or ""
            ).strip().lower()
            position = tspan.get("y") or "0"
            try:
                y_key = round(float(position), 1)
            except ValueError:
                y_key = 0.0
            if groups:
                previous = groups[-1][-1]
                previous_font = _inherited_attr(previous, "font-size") or base_font
                try:
                    previous_font_key = round(float(previous_font), 1) if previous_font else 0.0
                except ValueError:
                    previous_font_key = 0.0
                previous_fill_key = (
                    _inherited_attr(previous, "fill") or text.get("fill") or ""
                ).strip().lower()
                previous_y = previous.get("y") or "0"
                try:
                    previous_y_key = round(float(previous_y), 1)
                except ValueError:
                    previous_y_key = 0.0
                if (
                    previous_font_key == font_key
                    and previous_y_key == y_key
                    and previous_fill_key == fill_key
                ):
                    groups[-1].append(tspan)
                    continue
            groups.append([tspan])

        for index, group in enumerate(groups):
            content = "".join("".join(node.itertext()) for node in group).strip()
            if not content:
                continue
            leaves.append(
                {
                    "id": group[0].get("id"),
                    "kind": "tspan",
                    "parent_id": text_id,
                    "line_index": index,
                    "text": content,
                    "element": group[0],
                    "member_ids": [node.get("id") for node in group],
                    "preserve_children": False,
                    "collapse": len(group) > 1,
                    "font_size_attr": _inherited_attr(group[0], "font-size") or base_font,
                    "fill": _inherited_attr(group[0], "fill") or text.get("fill"),
                    "anchor": text.get("text-anchor") or group[0].get("text-anchor"),
                    "stroke": _text_stroke(group[0]) or _text_stroke(text),
                }
            )
    return leaves


def _collect_images(root: etree._Element) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for element in root.iter():
        if _local(element.tag) != "image":
            continue
        href = element.get("href") or element.get(f"{{{XLINK_NS}}}href") or ""
        clip_ids: list[str] = []
        clip_shapes: list[str] = []
        clip_measure_ids: list[str] = []
        node: etree._Element | None = element
        while node is not None:
            reference = node.get("clip-path") or ""
            match = _CIRCLED_REF.search(reference)
            if match:
                clip_id = match.group(1)
                clip_ids.append(clip_id)
                shape_element = _clip_shape_element(root, clip_id)
                if shape_element is not None:
                    clip_shapes.append(_local(shape_element.tag))
                    if shape_element.get("id"):
                        clip_measure_ids.append(shape_element.get("id"))
            node = node.getparent()
        images.append(
            {
                "id": element.get("id"),
                "element": element,
                "href_kind": (
                    "data" if href.startswith("data:") else ("external" if href else "empty")
                ),
                "href_length": len(href),
                "clip_ids": clip_ids,
                "clip_shapes": clip_shapes,
                "clip_measure_ids": clip_measure_ids,
                "clip_shape": (
                    "circle"
                    if "circle" in clip_shapes
                    else (clip_shapes[0] if clip_shapes else None)
                ),
                "transform": element.get("transform"),
            }
        )
    return images


def _clip_shape_element(root: etree._Element, clip_id: str) -> etree._Element | None:
    for clip in root.iter():
        if _local(clip.tag) == "clipPath" and clip.get("id") == clip_id:
            for child in clip:
                if _local(child.tag) in {"circle", "rect", "path", "ellipse", "polygon"}:
                    return child
    return None


# ---------------------------------------------------------------------------
# 度量
# ---------------------------------------------------------------------------
def _union(boxes: Iterable[tuple[float, float, float, float]]):
    boxes = [box for box in boxes if box[2] > 0 and box[3] > 0]
    if not boxes:
        return None
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[0] + box[2] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    return (left, top, right - left, bottom - top)


def _apply_measurements(
    leaves: list[dict[str, Any]],
    images: list[dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    canvas: tuple[float, float],
) -> None:
    for leaf in leaves:
        member_ids = leaf.get("member_ids") or [leaf["id"]]
        boxes = []
        font_size = 0.0
        for member_id in member_ids:
            metric = metrics.get(member_id) or {}
            if metric:
                boxes.append(
                    (
                        float(metric.get("x", 0)),
                        float(metric.get("y", 0)),
                        float(metric.get("w", 0)),
                        float(metric.get("h", 0)),
                    )
                )
                font_size = font_size or float(metric.get("font_size") or 0)
        union = _union(boxes)
        element = leaf["element"]
        parent = element if leaf["kind"] == "text" else next((p for p in element.iterancestors() if _local(p.tag) == "text"), element)
        if union is None:
            font_size = _length_to_px(leaf["font_size_attr"]) or 16
            x, y = _translate(parent)
            x += _length_to_px(element.get("x") or parent.get("x"))
            y += _length_to_px(element.get("y") or parent.get("y"))
            width = max(8.0, len(leaf["text"]) * font_size * 0.98)
            height = font_size * 1.2
            union = (x, y - font_size, width, height)
        max_width = max(8.0, union[2] * 1.02)
        next_abs_x = leaf.get("next_abs_x") or 0
        if next_abs_x:
            available = next_abs_x - union[0]
            if 8 < available < max_width:
                max_width = available
        metric = metrics.get(leaf["id"], {})
        try:
            baseline = _translate(parent)[1] + float(element.get("y") or parent.get("y") or 0)
        except ValueError:
            baseline = union[1] + font_size
        leaf.update(
            {
                "baseline": metric.get("baseline", baseline if not metric else union[1] + font_size * 0.8),
                "x": round(union[0], 2),
                "y": round(union[1], 2),
                "w": round(union[2], 2),
                "h": round(union[3], 2),
                "font_size": round(font_size or float(leaf["font_size_attr"] or 16), 2),
                "max_width": round(max_width, 2),
                "anchor": leaf["anchor"] or "start",
            }
        )

    for image in images:
        metric = metrics.get(image["id"]) or {}
        if not metric:
            element = image["element"]
            x, y = _translate(element)
            x += float(element.get("x") or 0)
            y += float(element.get("y") or 0)
            width = float(element.get("width") or 0)
            height = float(element.get("height") or 0)
            metric = {"x": x, "y": y, "w": width, "h": height}
        box = (
            float(metric.get("x", 0)),
            float(metric.get("y", 0)),
            float(metric.get("w", 0)),
            float(metric.get("h", 0)),
        )
        clip_boxes = []
        for clip_id in image.get("clip_measure_ids") or []:
            clip_metric = metrics.get(clip_id)
            if clip_metric:
                clip_boxes.append(
                    (
                        float(clip_metric.get("x", 0)),
                        float(clip_metric.get("y", 0)),
                        float(clip_metric.get("w", 0)),
                        float(clip_metric.get("h", 0)),
                    )
                )
        measured_clips = metric.get("clip_boxes")
        if measured_clips is not None:
            def intersect(items):
                return _intersect((b["x"], b["y"], b["w"], b["h"]) for b in items)

            clip_frame = intersect(measured_clips["canvas"])
            local_frame = intersect(measured_clips["local"])
            if local_frame:
                image["local_frame"] = dict(zip(("x", "y", "width", "height"), local_frame))
        else:
            clip_frame = _intersect(clip_boxes)
        frame = clip_frame or box
        image.update(
            {
                "x": round(frame[0], 2),
                "y": round(frame[1], 2),
                "w": round(frame[2], 2),
                "h": round(frame[3], 2),
                "frame": {
                    "x": round(frame[0], 2),
                    "y": round(frame[1], 2),
                    "width": round(frame[2], 2),
                    "height": round(frame[3], 2),
                },
                "aspect": round(frame[2] / frame[3], 3) if frame[3] else 1.0,
            }
        )


def _text_layouts(root, leaves, metrics):
    """Mark rows that can safely share a single SVG text chunk."""
    parents = {el.get("id"): el for el in root.iter() if _local(el.tag) == "text"}
    rects = [el for el in root.iter() if _local(el.tag) == "rect" and el.get("id")
             and not any(_local(a.tag) in {"defs", "clipPath", "mask", "symbol"} for a in el.iterancestors())]
    layouts = []
    for parent_id, parent in parents.items():
        runs = [leaf for leaf in leaves if leaf["parent_id"] == parent_id]
        if not runs or parent.get("rotate") or any(run.get("collapse") for run in runs):
            continue
        same_row = len(runs) > 1 and all(
            run["element"] is parent or run["element"].getparent() is parent for run in runs
        ) and max(run["baseline"] for run in runs) - min(run["baseline"] for run in runs) <= 3
        text = "".join(run["text"] for run in runs)
        topic = same_row and bool(re.search(r"[丨|｜]|——", text)) and len({run["font_size"] for run in runs}) > 1
        x0, x1 = min(r["x"] for r in runs), max(r["x"] + r["w"] for r in runs)
        y = runs[0]["baseline"]
        boxes = []
        for rect in rects:
            metric = metrics.get(rect.get("id"))
            if not metric:
                if rect.getparent() is not root or rect.get("transform"):
                    continue
                metric = {k: _length_to_px(rect.get(attr)) for k, attr in (("x", "x"), ("y", "y"), ("w", "width"), ("h", "height"))}
            rx, ry, w, h = (metric[k] for k in ("x", "y", "w", "h"))
            rounded = rect.get("rx") or rect.get("ry") or metric.get("rounded")
            if (topic or rounded) and h > 0 and rx <= x0 + 4 and x1 <= rx + w + 4 and ry <= y <= ry + h:
                if topic or h <= max(r["font_size"] for r in runs) * 3.5:
                    boxes.append((w * h, rx + w / 2))
        capsule = (len(runs) == 1 or same_row) and bool(boxes)
        if not topic and not capsule:
            continue
        if any(run["element"].get("transform") for run in runs if run["element"] is not parent):
            continue
        layouts.append({"parent_id": parent_id, "first_id": runs[0]["id"],
                        "following_ids": [r["id"] for r in runs[1:]],
                        "center": min(boxes)[1] if boxes else (x0 + x1) / 2,
                        "baseline": y, "gap": 12 if topic else 0})
    return layouts


def _target(leaf: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": leaf["id"],
        "kind": leaf["kind"],
        "collapse": bool(leaf.get("collapse")),
        "preserve_children": bool(leaf.get("preserve_children")),
        "member_ids": leaf.get("member_ids") or [leaf["id"]],
    }


def _merge_duplicate_leaves(leaves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并视觉上完全重叠的重复文字层（描边/阴影双写），只保留一个可编辑项。"""
    merged: list[dict[str, Any]] = []
    used: set[int] = set()
    for index, leaf in enumerate(leaves):
        if index in used:
            continue
        used.add(index)
        group = [leaf]
        for other_index in range(index + 1, len(leaves)):
            if other_index in used:
                continue
            other = leaves[other_index]
            if (
                leaf["text"] == other["text"]
                and abs(leaf["font_size"] - other["font_size"]) < 0.3
                and abs(
                    (leaf["x"] + leaf["w"] / 2) - (other["x"] + other["w"] / 2)
                )
                < 4
                and abs(
                    (leaf["y"] + leaf["h"] / 2) - (other["y"] + other["h"] / 2)
                )
                < 4
            ):
                group.append(other)
                used.add(other_index)
        primary = dict(leaf)
        for item in group:
            if not primary.get("stroke") and item.get("stroke"):
                primary["stroke"] = item["stroke"]
        primary["targets"] = [_target(item) for item in group]
        merged.append(primary)
    return merged


def _merge_same_line_leaves(leaves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把同一行被拆碎的 <text>/tspan 合并成一个逻辑字段。

    判定：基线 y 差 < 8px、字号差 ≤3、fill/stroke 相同、按 x 排序后相邻间距
    小于字号的 1.5 倍。合并后保留首个节点的坐标与样式，渲染时清掉同组其他节点。
    """
    def baseline(leaf: dict[str, Any]) -> float:
        return leaf["y"] + leaf["font_size"] * 0.8

    def targets(leaf: dict[str, Any]) -> list[dict[str, Any]]:
        return leaf.get("targets") or [_target(leaf)]

    def dash_only(leaf: dict[str, Any]) -> bool:
        return bool(leaf["text"].strip()) and set(leaf["text"].strip()) == {"—"}

    ordered = sorted(leaves, key=lambda item: (baseline(item), item["x"]))
    groups: list[list[dict[str, Any]]] = []
    for leaf in ordered:
        for group in groups:
            head = group[0]
            if abs(baseline(leaf) - baseline(head)) >= 8:
                continue
            # 同一父 <text> 下的相邻 tspan 不能一律视为独立字段：
            # “4” + “月11日”这类同字号、同色、同基线的碎片必须合并；
            # 真正不同的多字号/多色 tspan 会由下面的字号、fill 检查挡住。
            if abs(leaf["font_size"] - head["font_size"]) > 3 and not (
                dash_only(leaf) or dash_only(head)
            ):
                continue
            if (leaf.get("fill") or "").lower() != (head.get("fill") or "").lower():
                continue
            if (leaf.get("stroke") or {}) != (head.get("stroke") or {}):
                continue
            group.append(leaf)
            break
        else:
            groups.append([leaf])

    merged: list[dict[str, Any]] = []
    for group in groups:
        group.sort(key=lambda item: item["x"])
        current = dict(group[0])
        current["targets"] = list(targets(current))
        current["member_ids"] = list(current.get("member_ids") or [current["id"]])
        for nxt in group[1:]:
            gap = nxt["x"] - (current["x"] + current["w"])
            limit = (
                max(current["font_size"], nxt["font_size"]) * 3
                if dash_only(current) or dash_only(nxt)
                else current["font_size"] * 1.5
            )
            if gap >= limit:
                merged.append(current)
                current = dict(nxt)
                current["targets"] = list(targets(current))
                current["member_ids"] = list(current.get("member_ids") or [current["id"]])
                continue
            if gap < -current["font_size"] * 0.8:
                merged.append(current)
                current = dict(nxt)
                current["targets"] = list(targets(current))
                current["member_ids"] = list(current.get("member_ids") or [current["id"]])
                continue
            if dash_only(current) and not dash_only(nxt):
                # 破折号并入后面的主文本，字号/颜色跟随主文本
                current["font_size"] = nxt["font_size"]
                current["fill"] = nxt["fill"]
                current["stroke"] = nxt["stroke"]
            current["text"] = current["text"] + nxt["text"]
            right = max(current["x"] + current["w"], nxt["x"] + nxt["w"])
            current["w"] = right - current["x"]
            current["h"] = max(current["h"], nxt["h"])
            current["max_width"] = max(current.get("max_width") or 0, current["w"])
            current["targets"] = current["targets"] + targets(nxt)
            current["member_ids"] = current["member_ids"] + list(
                nxt.get("member_ids") or [nxt["id"]]
            )
            current["merge"] = True
        merged.append(current)
    return merged


def _intersect(boxes: Iterable[tuple[float, float, float, float]]):
    boxes = [box for box in boxes if box[2] > 0 and box[3] > 0]
    if not boxes:
        return None
    left = max(box[0] for box in boxes)
    top = max(box[1] for box in boxes)
    right = min(box[0] + box[2] for box in boxes)
    bottom = min(box[1] + box[3] for box in boxes)
    if right <= left or bottom <= top:
        return None
    return (left, top, right - left, bottom - top)


# ---------------------------------------------------------------------------
# 规则推断（离线兜底）
# ---------------------------------------------------------------------------
def _heuristic_mapping(
    leaves: list[dict[str, Any]],
    images: list[dict[str, Any]],
    canvas: tuple[float, float],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    width, height = canvas
    sizes = sorted((leaf["font_size"] for leaf in leaves), reverse=True)
    largest = sizes[0] if sizes else 0
    second = sizes[1] if len(sizes) > 1 else 0

    text_map: dict[str, dict[str, Any]] = {}
    counters: dict[str, int] = {}

    def assign(leaf: dict[str, Any], field: str, role: str | None = None) -> None:
        counters[field] = counters.get(field, 0) + 1
        mapping = {"field": field, "slot": counters[field] - 1}
        if role:
            mapping["role"] = role
        text_map[leaf["id"]] = mapping

    recognized = {
        "main_title": "main_title", "sub_title": "subtitle", "topic_title": "course_title",
        "topic_sub": "course_subtitle", "teacher_name": "teacher_name", "teacher_org": "teacher_school",
        "teacher_bio": "intro", "schedule": "schedule", "course_date": "schedule",
        "course_time": "schedule", "org": "org", "qr_tip": "qr_tip",
    }
    ordered = sorted(leaves, key=lambda item: (item["y"], item["x"]))
    for leaf in ordered:
        text = leaf["text"].strip()
        font = leaf["font_size"]
        role = poster_semantics.template_role(text)
        if role in recognized:
            assign(leaf, recognized[role], role)
        elif text and font >= largest - 0.01 and leaf["y"] < height * 0.5:
            assign(leaf, "main_title")
        elif text and font >= second - 0.01 and leaf["y"] < height * 0.6:
            assign(leaf, "subtitle")
        elif (
            len(text) <= 5
            and 38 <= font < largest * 0.85
            and leaf["y"] > height * 0.25
        ):
            assign(leaf, "teacher_name")
        elif len(text) >= 12:
            assign(leaf, "intro")
        elif leaf["y"] > height * 0.88:
            assign(leaf, "footer")
        else:
            assign(leaf, "other")

    image_map: dict[str, dict[str, Any]] = {}
    portrait_index = 0
    qr_index = 0
    for image in sorted(images, key=lambda item: item["y"]):
        area_ratio = (image["w"] * image["h"]) / max(1.0, width * height)
        shape = image.get("clip_shape")
        aspect = image.get("aspect") or 1.0
        if area_ratio > 0.5:
            field = "background"
        elif shape == "circle":
            field = "portrait_frame"
            mode = "framed_circle"
        elif aspect < 0.85 and image["h"] > height * 0.25:
            field = "portrait_frame"
            mode = "full_body" if image["h"] > height * 0.5 else "head_shoulder"
        elif image["w"] < width * 0.3 and image["h"] < height * 0.2 and image["y"] > height * 0.6:
            field = "qr_frame"
            mode = None
        elif image["h"] < height * 0.06:
            field = "decoration"
            mode = None
        else:
            field = "decoration"
            mode = None
        entry: dict[str, Any] = {"field": field}
        if field == "portrait_frame":
            entry["slot"] = portrait_index
            entry["mode"] = mode
            portrait_index += 1
        elif field == "qr_frame":
            entry["slot"] = qr_index
            entry["mode"] = None
            qr_index += 1
        else:
            entry["slot"] = 0
            entry["mode"] = None
        image_map[image["id"]] = entry

    return text_map, image_map


# ---------------------------------------------------------------------------
# 大模型推断
# ---------------------------------------------------------------------------
def _llm_mapping(
    leaves: list[dict[str, Any]],
    images: list[dict[str, Any]],
    canvas: tuple[float, float],
    preview_png: bytes | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    payload = {
        "canvas": {"width": round(canvas[0], 2), "height": round(canvas[1], 2)},
        "texts": [
            {
                "id": leaf["id"],
                "text": leaf["text"],
                "font_size": leaf["font_size"],
                "bbox": [
                    round(leaf["x"], 1),
                    round(leaf["y"], 1),
                    round(leaf["w"], 1),
                    round(leaf["h"], 1),
                ],
                "anchor": leaf["anchor"],
            }
            for leaf in sorted(leaves, key=lambda item: (item["y"], item["x"]))
        ],
        "images": [
            {
                "id": image["id"],
                "frame": image["frame"],
                "clip_shape": image["clip_shape"],
                "aspect": image["aspect"],
            }
            for image in images
        ],
    }
    system = (
        "你是教培海报模板结构分析专家。只能根据给定的 SVG 节点信息和预览图判断，"
        "不得编造节点 ID。返回严格 JSON，不要 Markdown。"
    )
    user = f"""
请把下面海报模板的文字节点和图片节点映射到业务字段。

兼容文字 field 枚举：{", ".join(TEXT_FIELDS)}
精确语义 role 枚举：{", ".join(poster_semantics.SEMANTIC_ROLES)}
图片字段枚举：{", ".join(IMAGE_FIELDS)}

规则：
1. 主标题通常是字号最大、位置最靠上的文字；副标题次之。
2. 课程标题一般含学科或“丨”分隔，课程副标题常以“——”开头。
3. 老师姓名可能是短文本（2-4 字）或“学校 + 姓名”；学校/机构常以“中学/小学/学校/一中”结尾。按老师位置顺序使用从 0 开始的 teacher_slot；教师简介按行标为 teacher_bio_1、teacher_bio_2、teacher_bio_3。
4. 时间字段区分 course_date（单独月日）、course_time（钟点，可含星期）、schedule（单坑位合并日期和时间）；和课题一起按从 0 开始的 course_slot 标注。课题用 topic_title、topic_sub；二维码引导语含“扫码/朋友圈/领取”。
5. 只有一个圆形或竖长图框时 slot=0；多个人像按从上到下编号。
6. 人像 mode 只能是 head_shoulder / framed_circle / full_body：
   圆形裁切用 framed_circle，卡片半身用 head_shoulder，竖长全身用 full_body。
7. 装饰条、背景图、Logo 等标为 decoration/background/logo。
8. 无法判断时填 other。

模板节点：
{json.dumps(payload, ensure_ascii=False)}

返回格式：
{{"texts":[{{"id":"text-001","field":"main_title","slot":0}}],
  "images":[{{"id":"image-003","field":"portrait_frame","slot":0,"mode":"framed_circle"}}],
  "summary":"一句话描述版式"}}
"""
    data = llm_client.chat_json(
        system,
        user,
        image_png=preview_png,
        max_tokens=settings.llm_max_tokens,
        temperature=0.1,
    )

    text_map: dict[str, dict[str, Any]] = {}
    for item in data.get("texts") or []:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "")
        field = str(item.get("field") or "other")
        if field not in TEXT_FIELDS:
            field = "other"
        if any(leaf["id"] == node_id for leaf in leaves):
            text_map[node_id] = {
                "field": field,
                "slot": int(item.get("slot") or 0),
            }
            role = item.get("role")
            if role in poster_semantics.SEMANTIC_ROLES:
                text_map[node_id]["role"] = role
            for slot_key in ("teacher_slot", "course_slot"):
                if isinstance(item.get(slot_key), int) and item[slot_key] >= 0:
                    text_map[node_id][slot_key] = item[slot_key]

    image_map: dict[str, dict[str, Any]] = {}
    for item in data.get("images") or []:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "")
        field = str(item.get("field") or "other")
        if field not in IMAGE_FIELDS:
            field = "other"
        mode = item.get("mode")
        if mode not in {"head_shoulder", "framed_circle", "full_body", None}:
            mode = None
        if any(image["id"] == node_id for image in images):
            image_map[node_id] = {
                "field": field,
                "slot": int(item.get("slot") or 0),
                "mode": mode,
            }

    return text_map, image_map, str(data.get("summary") or "")


# ---------------------------------------------------------------------------
# schema 组装
# ---------------------------------------------------------------------------
def _build_schema(
    *,
    template_id: str,
    name: str,
    canvas: tuple[float, float],
    leaves: list[dict[str, Any]],
    images: list[dict[str, Any]],
    text_map: dict[str, dict[str, Any]],
    image_map: dict[str, dict[str, Any]],
    source: str,
    summary: str,
    warnings: list[str],
    has_preview: bool,
) -> dict[str, Any]:
    texts: list[dict[str, Any]] = []
    for leaf in sorted(leaves, key=lambda item: (item["y"], item["x"])):
        mapping = text_map.get(leaf["id"]) or {"field": "other", "slot": 0}
        texts.append(
            {
                "id": leaf["id"],
                "kind": leaf["kind"],
                "parent_id": leaf["parent_id"],
                "line_index": leaf["line_index"],
                "member_ids": leaf.get("member_ids") or [leaf["id"]],
                "collapse": bool(leaf.get("collapse")),
                "preserve_children": bool(leaf.get("preserve_children")),
                "merge": bool(leaf.get("merge")),
                "targets": leaf.get("targets") or [_target(leaf)],
                "field": mapping["field"],
                "slot": int(mapping.get("slot") or 0),
                "text": leaf["text"],
                "x": leaf["x"],
                "y": leaf["y"],
                "width": leaf["w"],
                "height": leaf["h"],
                "font_size": leaf["font_size"],
                "base_font_size": leaf["font_size"],
                "max_width": leaf["max_width"],
                "bio_max_width": leaf.get("bio_max_width"),
                "anchor": leaf["anchor"],
                "fill": leaf["fill"],
                "style": leaf.get("stroke") or {},
            }
        )

    poster_semantics.annotate_roles(texts, text_map, repair_activity=True)
    role_slots: dict[str, int] = {}
    for entry in texts:
        if entry['field'] in {'main_title', 'activity_tag', 'slogan', 'course_title'}:
            field = entry['field']
            entry['slot'] = role_slots.get(field, 0)
            role_slots[field] = entry['slot'] + 1
    for entry in texts:
        available = entry.pop("bio_max_width")
        if entry["role"].startswith("teacher_bio"):
            entry["font_fit_group"] = f"teacher_bio:{entry.get('teacher_slot', 0)}"
            if available:
                entry["max_width"] = available

    image_entries: list[dict[str, Any]] = []
    portrait_slots = {
        image["id"]: slot
        for slot, image in enumerate(sorted(
            (image for image in images if image_map.get(image["id"], {}).get("field") == "portrait_frame"),
            key=lambda image: (image["y"], image["x"]),
        ))
    }
    for image in images:
        mapping = image_map.get(image["id"]) or {"field": "other", "slot": 0}
        image_entries.append(
            {
                "id": image["id"],
                "field": mapping["field"],
                "slot": portrait_slots.get(image["id"], int(mapping.get("slot") or 0)),
                "mode": mapping.get("mode"),
                "frame": image["frame"],
                **({"local_frame": image["local_frame"]} if image.get("local_frame") else {}),
                "x": image["x"],
                "y": image["y"],
                "width": image["w"],
                "height": image["h"],
                "aspect": image["aspect"],
                "clip_shape": image["clip_shape"],
                "clip_ids": image["clip_ids"],
                "href_kind": image["href_kind"],
                "editable": mapping["field"] in {"portrait_frame", "qr_frame"},
            }
        )

    # 同字段多节点时生成 key：teacher_name_1 / teacher_name_2 ...
    counts: dict[str, int] = {}
    for entry in texts:
        counts[entry["field"]] = counts.get(entry["field"], 0) + 1
    for entry in texts:
        field = entry["field"]
        if counts.get(field, 0) > 1:
            entry["key"] = f"{field}_{entry['slot'] + 1}"
            entry["label"] = f"{FIELD_LABELS.get(field, field)} {entry['slot'] + 1}"
        else:
            entry["key"] = field
            entry["label"] = FIELD_LABELS.get(field, field)

    for entry in image_entries:
        field = entry["field"]
        if field == "portrait_frame":
            entry["key"] = f"portrait_{entry['slot'] + 1}"
            entry["avatar_key"] = f"avatar_{entry['slot'] + 1}"
            entry["label"] = f"人像框 {entry['slot'] + 1}"
        elif field == "qr_frame":
            entry["key"] = "qr_frame" if entry["slot"] == 0 else f"qr_frame_{entry['slot'] + 1}"
            entry["label"] = FIELD_LABELS.get(field, field)
        else:
            entry["key"] = field
            entry["label"] = FIELD_LABELS.get(field, field)

    fields: list[dict[str, Any]] = []
    for entry in texts:
        fields.append(
            {
                "key": entry["key"],
                "label": entry["label"],
                "kind": "text",
                "node_ids": [entry["id"]],
                "field": entry["field"],
            }
        )
    for entry in image_entries:
        if entry["field"] in {"portrait_frame", "qr_frame"}:
            fields.append(
                {
                    "key": entry["key"],
                    "label": entry["label"],
                    "kind": "image",
                    "node_ids": [entry["id"]],
                    "field": entry["field"],
                    "mode": entry["mode"],
                }
            )

    portraits = [
        {
            "key": entry["key"],
            "image_id": entry["id"],
            "slot": entry["slot"],
            "mode": entry["mode"] or "head_shoulder",
            "width": entry["width"],
            "height": entry["height"],
            "clip_shape": entry["clip_shape"],
        }
        for entry in image_entries
        if entry["field"] == "portrait_frame"
    ]

    return {
        "version": SCHEMA_VERSION,
        "template_id": template_id,
        "name": name,
        "canvas": {"width": round(canvas[0], 2), "height": round(canvas[1], 2)},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "summary": summary,
        "has_preview": has_preview,
        "texts": texts,
        "images": image_entries,
        "fields": fields,
        "portraits": portraits,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def analyze_template(
    svg_bytes: bytes,
    *,
    name: str = "",
    force: bool = False,
    use_llm: bool = True,
    use_browser: bool = True,
) -> dict[str, Any]:
    """解析 SVG 模板并缓存 schema。相同内容直接返回缓存。"""
    digest = hashlib.sha1(svg_bytes).hexdigest()
    template_id = digest[:12]
    directory = settings.template_dir / template_id
    schema_path = directory / "schema.json"
    if schema_path.exists() and not force:
        try:
            cached = json.loads(schema_path.read_text(encoding="utf-8"))
            # v1-v8 没有同行碎片合并 / 多字号 tspan 拆分 / 蒙版优先图片框，自动重新解析
            if cached.get("version", 1) >= SCHEMA_VERSION:
                return cached
        except Exception:  # 缓存损坏则重新解析
            logger.warning("模板缓存损坏，重新解析：%s", schema_path)

    root = _parse_svg(svg_bytes)
    canvas = _canvas_size(root)
    _flatten_tspan_runs(root)
    _strip_position_arrays(root)
    _merge_identical_runs(root)
    for text in [el for el in root.iter() if _local(el.tag) == "text"]:
        _fold_first_run(text)
    _assign_ids(root)
    warnings: list[str] = []
    removed_layers = _remove_redundant_text_layers(root)
    if removed_layers:
        warnings.append(
            f"已移除 {len(removed_layers)} 个坐标重叠的重复文字图层，避免替换后重影"
        )

    directory.mkdir(parents=True, exist_ok=True)
    svg_path = directory / "template.svg"
    svg_path.write_bytes(
        etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)
    )

    leaves = _collect_text_leaves(root)
    images = _collect_images(root)

    metrics: dict[str, dict[str, Any]] = {}
    preview_bytes: bytes | None = None
    if use_browser:
        measure_ids = []
        for leaf in leaves:
            measure_ids.extend(leaf.get("member_ids") or [leaf["id"]])
        measure_ids.extend(image["id"] for image in images)
        measure_ids.extend(el.get("id") for el in root.iter() if _local(el.tag) == "rect" and el.get("id"))
        for image in images:
            measure_ids.extend(image.get("clip_measure_ids") or [])
        try:
            metrics = svg_browser.measure_svg(svg_path, canvas, measure_ids)
            preview_path = directory / "preview.png"
            svg_browser.screenshot_svg(svg_path, canvas, preview_path, scale=1)
            preview_bytes = preview_path.read_bytes()
        except Exception as exc:
            warnings.append(f"浏览器度量失败，坐标改用估算值：{exc}")
            preview_bytes = None

    _apply_measurements(leaves, images, metrics, canvas)
    layouts = _text_layouts(root, leaves, metrics)
    leaves = _merge_duplicate_leaves(leaves)
    leaves = _merge_same_line_leaves(leaves)
    cards = [metrics[el.get("id")] for el in root.iter()
             if _local(el.tag) == "rect" and el.get("id") in metrics
             and (el.get("rx") or el.get("ry"))
             and not any(_local(p.tag) in {"defs", "clipPath", "mask"} for p in el.iterancestors())]
    for leaf in leaves:
        x, y = leaf["x"], leaf["baseline"]
        enclosing = [box for box in cards if box["x"] <= x <= box["x"] + box["w"] - leaf["w"] + 4
                     and box["y"] <= y <= box["y"] + box["h"]
                     and box["h"] > 3 * leaf["font_size"]]
        if enclosing:
            box = min(enclosing, key=lambda box: box["w"] * box["h"])
            leaf["bio_max_width"] = max(leaf["w"], box["x"] + box["w"] - x - 12)

    text_map, image_map = _heuristic_mapping(leaves, images, canvas)
    summary = "规则推断版式"
    source = "heuristic"

    if use_llm and llm_client.llm_configured():
        try:
            llm_texts, llm_images, llm_summary = _llm_mapping(
                leaves, images, canvas, preview_bytes
            )
            text_map.update(llm_texts)
            image_map.update(llm_images)
            summary = llm_summary or summary
            source = "llm"
        except Exception as exc:
            warnings.append(f"大模型推断失败，已使用规则结果：{exc}")
    elif use_llm:
        warnings.append("未配置大模型，已使用规则推断（可人工校对）")

    schema = _build_schema(
        template_id=template_id,
        name=name or "未命名模板",
        canvas=canvas,
        leaves=leaves,
        images=images,
        text_map=text_map,
        image_map=image_map,
        source=source,
        summary=summary,
        warnings=warnings,
        has_preview=preview_bytes is not None,
    )
    schema["text_layouts"] = layouts
    schema["removed_duplicates"] = removed_layers
    schema_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return schema


def load_schema(template_id: str) -> dict[str, Any]:
    if not _TEMPLATE_ID_RE.match(template_id or ""):
        raise TemplateNotFound(f"模板 ID 不合法：{template_id}")
    schema_path = settings.template_dir / template_id / "schema.json"
    if not schema_path.is_file():
        raise TemplateNotFound(f"模板不存在或尚未解析：{template_id}")
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TemplateInvalid(f"模板 schema 损坏：{exc}") from exc


def template_svg_path(template_id: str) -> Path:
    if not _TEMPLATE_ID_RE.match(template_id or ""):
        raise TemplateNotFound(f"模板 ID 不合法：{template_id}")
    path = settings.template_dir / template_id / "template.svg"
    if not path.is_file():
        raise TemplateNotFound(f"模板 SVG 不存在：{template_id}")
    return path


def list_templates() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not settings.template_dir.exists():
        return items
    for directory in sorted(settings.template_dir.iterdir()):
        schema_path = directory / "schema.json"
        if not schema_path.is_file():
            continue
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append(
            {
                "template_id": schema.get("template_id", directory.name),
                "name": schema.get("name", directory.name),
                "canvas": schema.get("canvas", {}),
                "created_at": schema.get("created_at"),
                "source": schema.get("source"),
                "has_preview": schema.get("has_preview", False),
                "text_count": len(schema.get("texts", [])),
                "portrait_count": len(schema.get("portraits", [])),
            }
        )
    return items


# ---------------------------------------------------------------------------
# 按文件名管理模板（商业工作台使用：data/templates/<name>.fixed.svg）
# ---------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    if (
        not name
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
        or ".." in name
    ):
        raise TemplateNotFound(f"模板名称不合法：{name}")
    return name


def display_name_of(name: str) -> str:
    return name[: -len(".fixed")] if name.lower().endswith(".fixed") else name


def named_schema_path(name: str) -> Path:
    return settings.template_dir / f"{_safe_name(name)}.schema.json"


def named_svg_path(name: str) -> Path:
    return settings.template_dir / f"{_safe_name(name)}.template.svg"


def source_svg_path(name: str) -> Path:
    return settings.template_dir / f"{_safe_name(name)}.fixed.svg"


def is_named_template_analyzed(name: str) -> bool:
    return named_schema_path(name).is_file()


def list_named_templates() -> list[dict[str, Any]]:
    """扫描 data/templates/*.fixed.svg，返回工作台画廊需要的元信息。"""
    items: list[dict[str, Any]] = []
    if not settings.template_dir.exists():
        return items
    for path in sorted(settings.template_dir.glob("*.fixed.svg"), key=lambda p: p.name):
        name = path.name[: -len(".fixed.svg")]
        thumb = settings.template_dir / "thumbs" / f"{name}.jpg"
        schema = None
        if named_schema_path(name).is_file():
            try:
                schema = json.loads(named_schema_path(name).read_text(encoding="utf-8"))
            except Exception:
                schema = None
        items.append(
            {
                "name": name,
                "display_name": display_name_of(name),
                "analyzed": schema is not None,
                "thumbnail_url": f"/api/v1/posters/templates/{quote(name, safe='')}/thumbnail",
                "has_thumbnail": thumb.is_file(),
                "canvas": (schema or {}).get("canvas"),
                "text_count": len((schema or {}).get("texts", [])),
                "portrait_count": len((schema or {}).get("portraits", [])),
                "source": (schema or {}).get("source"),
                "updated_at": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds"),
            }
        )
    return items


def revival_backup_path(name: str) -> Path:
    source = source_svg_path(name)
    return source.with_name(source.stem + ".pre-revival.svg")


def revive_named_template(name: str, *, use_llm: bool = True, retry: bool = False) -> dict[str, Any]:
    """重新识别时，优先用备份验证已复活文字，不回滚其他图层。"""
    from app.services.text_revival import revive_outlined_text
    from app.services.svg_sanitizer import sanitize_svg

    source = source_svg_path(name)
    if not source.is_file():
        raise TemplateNotFound(f"模板文件不存在：{source.name}")
    backup = revival_backup_path(name)
    if not retry:
        return revive_outlined_text(source, use_llm=use_llm)

    original = source.read_bytes()
    cleaned, notes = sanitize_svg(original)
    root = _parse_svg(cleaned)
    nodes = [el for el in root.iter() if _local(el.tag) == "text" and el.get("data-revived") == "1"]
    with tempfile.TemporaryDirectory(dir=source.parent, prefix="reanalyze-") as work:
        trial = Path(work) / "template.svg"
        if nodes and backup.is_file():
            trial.write_bytes(sanitize_svg(backup.read_bytes())[0])
            report = revive_outlined_text(trial, use_llm=use_llm)
            candidates = _parse_svg(trial.read_bytes())
            current_by_text = {}
            candidates_by_text = {}
            for elements, index in ((nodes, current_by_text), (candidates.iter(), candidates_by_text)):
                for el in elements:
                    if _local(el.tag) == "text" and el.get("data-revived") == "1":
                        index.setdefault("".join(el.itertext()).strip(), []).append(el)
            report["revived"] = [item for item in report["revived"]
                                 if len(current_by_text.get(item["text"], [])) == len(candidates_by_text.get(item["text"], [])) == 1]
            for item in report["revived"]:
                current = current_by_text[item["text"]][0]
                candidate = candidates_by_text[item["text"]][0]
                for attr in ("font-size", "font-weight"):
                    if candidate.get(attr) is not None:
                        current.set(attr, candidate.get(attr))
            report["revived_count"] = len(report["revived"])
            report["backup"] = backup.name
            trial.write_bytes(etree.tostring(root, encoding="utf-8", xml_declaration=True))
        else:
            trial.write_bytes(cleaned)
            report = revive_outlined_text(trial, use_llm=use_llm)
            if nodes:
                report["warnings"].append("缺少复活前备份，无法重新测量已转为可编辑文字的原轮廓")
            trial_backup = trial.with_name(trial.stem + ".pre-revival.svg")
            if trial_backup.exists() and not backup.exists():
                shutil.copyfile(trial_backup, backup)
                report["backup"] = backup.name
        report["warnings"].extend(notes)
        # Refuse to overwrite a template changed by another request during OCR.
        if source.read_bytes() != original:
            raise TemplateInvalid("重新识别期间模板已更改，请重试")
        trial.replace(source)
        return report


def analyze_named_template(
    name: str, *, force: bool = False, use_llm: bool = True
) -> dict[str, Any]:
    """解析 data/templates/<name>.fixed.svg 并生成 <name>.schema.json。"""
    _safe_name(name)
    source = source_svg_path(name)
    if not source.is_file():
        raise TemplateNotFound(f"模板文件不存在：{source.name}")

    schema_path = named_schema_path(name)
    if schema_path.is_file() and not force:
        try:
            cached = json.loads(schema_path.read_text(encoding="utf-8"))
            if cached.get("version", 1) >= SCHEMA_VERSION:
                return cached
        except Exception:
            pass

    schema = analyze_template(
        source.read_bytes(),
        name=display_name_of(name),
        force=force,
        use_llm=use_llm,
    )
    normalized = settings.template_dir / schema["template_id"] / "template.svg"
    target_svg = named_svg_path(name)
    target_svg.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(normalized, target_svg)

    schema["name"] = name
    schema["display_name"] = display_name_of(name)
    schema["source_file"] = source.name
    schema["svg_path"] = str(target_svg.relative_to(settings.data_dir)).replace("\\", "/")
    schema["analyzed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    schema_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return schema


def load_named_schema(name: str) -> dict[str, Any]:
    path = named_schema_path(name)
    if not path.is_file():
        raise TemplateNotFound(f"模板尚未解析：{name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TemplateInvalid(f"模板 schema 损坏：{exc}") from exc


def schema_svg_path(schema: dict[str, Any]) -> Path:
    """schema 对应的规范化 SVG；兼容旧的按哈希缓存。"""
    relative = schema.get("svg_path")
    if relative:
        path = (settings.data_dir / relative).resolve()
        if path.is_file():
            return path
    return template_svg_path(str(schema.get("template_id", "")))


def svg_canvas_bytes(data: bytes) -> tuple[float, float]:
    return _canvas_size(_parse_svg(data))


def svg_canvas(path: Path) -> tuple[float, float]:
    """快速读取 SVG 画布尺寸（不解析文字/图片）。"""
    return svg_canvas_bytes(path.read_bytes())
