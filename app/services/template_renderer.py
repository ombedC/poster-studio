"""海报合成：把排课文案 + 老师照片套进模板 SVG，输出预览与高清 PNG。

流程
----
1. 用大模型（或规则）把用户粘贴的整段文案解析成 ``{节点ID: 新文字}``；
2. 按 schema 里的 portrait 槽位调用现有 ``pipeline.run_pipeline`` 处理照片
   （head_shoulder / framed_circle / full_body），以 Base64 嵌入对应 ``<image>``，
   保留模板原有的 ``clipPath`` 蒙版；
3. 文字替换后若超出原文本框宽度，用无头浏览器按比例缩小 font-size（≥ 85%）；
4. Playwright 以 ``device_scale_factor=2`` 导出高清 PNG 到 ``data/outputs``。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from lxml import etree
from PIL import Image
from app.config import settings
from app.errors import BackgroundRemovalUnavailable, TemplateInvalid
from app.services import image_processor as ip
from app.services import (
    llm_client,
    poster_semantics,
    svg_browser,
    template_analyzer as analyzer,
    text_incremental,
)
from app.services.face_detector import get_detector
from app.services.pipeline import run_pipeline

logger = logging.getLogger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

_BRACKET_RE = re.compile(r"(【[^】]*】)")


@dataclass
class RenderResult:
    job_id: str
    svg_path: Path
    png_path: Path | None
    width: int
    height: int
    values: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    font_adjustments: list[dict[str, Any]] = field(default_factory=list)
    engine: str = "heuristic"
    timings: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 文案解析
# ---------------------------------------------------------------------------
def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _nearest_text(element: etree._Element) -> etree._Element:
    node: etree._Element | None = element
    while node is not None and not node.tag.endswith("text"):
        node = node.getparent()
    return node if node is not None else element


def _apply_stroke_style(element: etree._Element, style: dict[str, Any]) -> None:
    color = style.get("stroke_color")
    if not color:
        return
    text_element = _nearest_text(element)
    width = style.get("stroke_width") or 1
    text_element.set("stroke", str(color))
    text_element.set("stroke-width", str(width))
    existing = text_element.get("style") or ""
    kept = []
    for part in existing.split(";"):
        part = part.strip()
        if not part:
            continue
        name = part.split(":", 1)[0].strip().lower()
        if name in {"paint-order", "stroke-linejoin"}:
            continue
        kept.append(part)
    kept.extend(["paint-order:stroke fill", "stroke-linejoin:round"])
    text_element.set("style", ";".join(kept))


def _clear_letter_spacing(
    element: etree._Element, target: dict[str, Any], by_id
) -> None:
    nodes: list[etree._Element] = [element]
    parent = element.getparent()
    if parent is not None:
        nodes.append(parent)
    for member_id in target.get("member_ids") or []:
        node = by_id.get(member_id)
        if node is not None:
            nodes.append(node)
    for node in nodes:
        raw = node.get("letter-spacing")
        if not raw:
            continue
        try:
            number = float(re.sub(r"[^0-9.\-]", "", raw) or "0")
        except ValueError:
            number = 0.0
        if number > 0:
            node.attrib.pop("letter-spacing", None)


def parse_text_values(
    schema: dict[str, Any],
    text: str,
    *,
    use_llm: bool = True,
    today: date | None = None,
    current_values: dict[str, str] | None = None,
) -> tuple[dict[str, str], str, list[str]]:
    result = parse_text_patch(schema, text, use_llm=use_llm, today=today, current_values=current_values)
    return result['values'], result['engine'], result['warnings']


def parse_text_patch(schema, text, *, use_llm=True, today=None, current_values=None):
    warnings: list[str] = []
    text = (text or '').strip()
    today = today or poster_semantics.today_in_china()
    entries = [dict(entry) for entry in schema.get('texts', [])]
    poster_semantics.annotate_roles(entries, repair_activity=True)
    by_id = {entry['id']: entry for entry in entries}
    baseline = {key: (current_values or {}).get(key, entry.get('text') or '') for key, entry in by_id.items()}
    schema = {**schema, 'texts': entries}
    value, hint = text_incremental.directive(text)
    dimensions = text_incremental.input_dimensions(value)
    explicit_role, _ = poster_semantics._labeled(text)
    short = bool(text) and len(text) <= 16 and not re.search(r'[\n。；;]', text)
    inferred = 'partial_edit' if dimensions or value != text or (short and not explicit_role) else 'full_notice'
    model_intent, proposed = None, {}
    if text and use_llm and llm_client.llm_configured():
        try:
            model_intent, proposed = _llm_parse_values(schema, text, today=today, current_values=baseline)
        except Exception:
            logger.warning('parse-text: invalid or unavailable LLM response', exc_info=True)
            warnings.append('大模型解析失败，已改用安全规则解析')

    intent = inferred if inferred == 'partial_edit' or '\n' in text or explicit_role else (model_intent or inferred)
    candidates = {key: patch['new_text'] for key, patch in proposed.items()}
    used_model = set()
    if intent == 'partial_edit':
        if dimensions:
            values = text_incremental.dimension_patches(entries, baseline, dimensions)
            used_model = {key for key, value in values.items() if candidates.get(key) == value}
        elif poster_semantics.is_schedule(value) and not re.search(r'[\n。；;]', value):
            # Only the schedule extractor participates in this branch, never notice title inference.
            schedule_roles = {'schedule', 'course_date', 'course_time'}
            schedule_entries = [e for e in entries if e['role'] in schedule_roles and poster_semantics.is_schedule(baseline[e['id']])]
            values = poster_semantics.build_values({'texts': schedule_entries}, value, today, align_titles=False)
            for entry in schedule_entries:
                candidate = candidates.get(entry['id'])
                if candidate and poster_semantics.is_schedule(candidate) and poster_semantics._clock(candidate):
                    values[entry['id']] = candidate
            used_model = {key for key, value in values.items() if candidates.get(key) == value}
        else:
            values = {}
            # Explicit literal replacement (e.g. 把试听改为体验); no semantic guess or whole-cell overwrite.
            if hint and value:
                values = {key: old.replace(hint, value) for key, old in baseline.items()
                          if not text_incremental.teacher_field(by_id[key]) and hint in old and old != hint}
                used_model = {key for key, value in values.items() if candidates.get(key) == value}
            else:
                safe = {key: patch['new_text'] for key, patch in proposed.items()
                        if patch['old_text'] == baseline.get(key)
                        and not text_incremental.teacher_field(by_id[key])
                        and text_incremental.is_literal_patch(baseline[key], patch['new_text'], value)}
                values.update(safe)
                used_model = set(safe)
    else:
        notice_entries = [e for e in entries if e["role"] not in {"schedule", "course_date", "course_time"}
                          or not baseline[e["id"]] or poster_semantics.is_schedule(baseline[e["id"]])
                          or poster_semantics.is_schedule(e.get("text") or "")]
        values = poster_semantics.build_values({"texts": notice_entries}, text, today, candidates, baseline, align_titles=False)
        used_model = set(values) & set(candidates)
    values = {key: value for key, value in values.items() if value != baseline[key]}
    if not values:
        warnings.append('没有从文案中识别出可安全更新的字段，已保留画布内容')
    patches = [{'slot_key': key, 'old_text': baseline[key], 'new_text': value,
                'reason': proposed[key]['reason'] if key in used_model and candidates[key] == value
                else ('保留原文结构，替换匹配内容' if intent == 'partial_edit' else '提取排课通知中的明确内容')}
               for key, value in values.items()]
    return {'intent': intent, 'patches': patches, 'values': values,
            'engine': 'llm' if used_model.intersection(values) else 'heuristic', 'warnings': warnings}


def _llm_parse_values(
    schema: dict[str, Any],
    text: str,
    *,
    today: date,
    current_values: dict[str, str] | None = None,
) -> tuple[str, dict[str, dict[str, str]]]:
    current_values = current_values or {}
    fields = []
    for entry in schema.get("texts", []):
        node_id = entry["id"]
        fields.append({
            "slot_key": node_id,
            "field": entry["field"],
            "role": entry.get("role", entry["field"]),
            "label": entry.get("label", entry["field"]),
            "current_text": current_values.get(node_id, entry.get("text") or ""),
            "default_text": entry.get("text") or "",
            "teacher_slot": entry.get("teacher_slot"),
            "bio_index": entry.get("bio_index"),
            "course_slot": entry.get("course_slot"),
            "max_width": entry.get("max_width", 0),
        })
    system = (
        "输出协议：严格 JSON {intent: full_notice|partial_edit, patches:[{slot_key,old_text,new_text,reason}]}，"
        "仅给确实改变且 old_text 精确匹配 current_text 的槽位；slot_key 必须是给定 id。"
        "你是教培海报排版助手。根据排课原文填写模板字段，只能使用给定的节点 ID，"
        "返回严格 JSON，不要 Markdown。\n\n"
        "先判断用户是在提交整份排课通知，还是仅调整当前版面中的部分信息。"
        "必须先读每个坑位的 current_text（画布上当前显示的文字），"
        "名称相近的字段之间（如主标题与副标题、课程标题）严禁只凭字段名盲填；"
        "按当前文案里的信息维度匹配，别把活动地区写进课程名。\n\n"
        "【规则 A：槽位内局部保留替换】\n"
        "当输入是简短局部信息（单独一个地名如 广东、年份如 2027、年级如 初二、"
        "学科如 物理，或 改成广东 / 时间改为周日晚7点 这类短指令），而不是一整段新文案时，"
        "严禁用该短词覆盖整个槽位。必须对照该槽位 current_text，"
        "只替换其中同维度的子词，完整保留其他前缀、后缀与结构。\n"
        "正例：current_text=\"2026福建九年级\"，输入=\"广东\" → 输出 \"2026广东九年级\"。\n"
        "反例：输出 \"广东\"（丢掉 2026 与 九年级）——严格禁止。\n"
        "正例：current_text=\"2026福建九年级\"，输入=\"2027\" → 输出 \"2027福建九年级\"。\n"
        "只替换用户提到的那一维：用户只说 广东 时，年级必须原样保留（\"2026广东九年级\"），"
        "不得顺手改成 \"2026广东中考\" 或别的年级。\n"
        "只有用户给出完整新标题或整段排课通知时，才允许整格替换。\n\n"
        "【意图与示例】只输厦门：partial_edit，2026漳州九年级→2026厦门九年级，"
        "漳州市名优骨干教师点拨学科重难点→厦门市名优骨干教师点拨学科重难点；"
        "福建中考公益课、语文期末冲刺课、漳州市学科带头人均不变。\n"
        "只输广东：partial_edit，福建中考公益课→广东中考公益课；旧活动城市和宣传语同步为广东/广东省。\n"
        "只输初二：partial_edit，2026漳州九年级→2026漳州初二，中考标签不能擅改。\n"
        "只输2026厦门九年级：partial_edit，同构主标题局部替换，宣传语联动，绝不覆盖课程标题。\n"
        "主标题：2027厦门初二\\n课程：数学复习课\\n老师：陈小明\\n学校：厦门一中\\n时间：12月12日19:30："
        "full_notice，只有明确给出的对应槽位才改；没有新老师的荣誉，就不要编造。\n\n"
        "【规则 B：活动级跨槽联动 + 师资履历保护】\n"
        "当用户改动了 地区/省份、年份、年级、学科 等活动级通用属性时，必须扫描全部槽位：\n"
        "- 需要同步联动：主标题、副标题/宣传标语、顶部活动标签；"
        "行政区后缀要与原写法一致（福建省 ↔ 广东省，福建 ↔ 广东）。\n"
        "  正例：subtitle=\"福建省名优骨干教师点拨学科重难点\"，输入 广东 → "
        "\"广东省名优骨干教师点拨学科重难点\"（保留“省”后缀）。\n"
        "- 严禁盲目篡改：用户没有明确说换老师时，不得修改老师姓名、学校、职称与荣誉履历，"
        "必须原样保留（例如 泉州市培元中学、福建省优秀教师、"
        "福建省“十三五”中小学第一批学科带头人），绝不能把省份替换进这些履历里，"
        "以免伪造师资头衔。\n"
        "  反例：把 \"高级教师、福建省优秀教师\" 改成 \"高级教师、广东省优秀教师\"——禁止。"
    )
    user = f"""
后端当前日期（中国标准时间）：{today.isoformat()}，一周从星期一开始。
模板文字字段（slot_key / 语义角色 / 中文含义 / 画布当前文案 / 模板默认示例）：
{json.dumps(fields, ensure_ascii=False)}

排课原文：
{text}

要求：
1. 只提取原文中明确出现并表达的字段；未提及的字段一律省略（不要返回空字符串，也不要返回 null）。
2. 先做语义比对：把用户输入和每个坑位的 current_text 对照，例如
   - 地域/年份/年级替换原来写相同维度的标题、标签或宣传语坑位；
   - 课程名、学科名替换原来写课程名的坑位；
   - 时间、地点、姓名、学校各自替换同类坑位。
   只有确实对应上的坑位才返回，其余坑位一律保持不动。
3. 严禁因为字段名里有“标题”就把内容塞进某个标题坑位；无法确定对应哪个坑位时，宁可不返回。
4. 多门课程 / 多位老师按顺序填入各自的 course_slot / teacher_slot，不要合并；简介按 bio_index 顺序。
5. 学校、姓名、简介和日期时间只返回原文中的对应片段；原文没有写具体日期时，禁止把“周几”换算成某月某日，也不要编造任何日期、时间、姓名。
6. 不要复制字段标签、项目符号或序号，不要替换原文没提到的字段。
7. 只返回 JSON：{{"intent":"full_notice|partial_edit","patches":[{{"slot_key":"给定节点 ID","old_text":"当前文案","new_text":"新文案","reason":"原因"}}],"notes":"说明"}}；未改槽位不列出。
"""
    data = llm_client.chat_json(
        system,
        user,
        max_tokens=settings.llm_max_tokens,
        temperature=0.2,
    )
    baseline = {field['slot_key']: field['current_text'] for field in fields}
    intent = data.get('intent')
    if intent not in ('full_notice', 'partial_edit') or not isinstance(data.get('patches'), list):
        raise ValueError('Invalid text patch envelope')
    patches: dict[str, dict[str, str]] = {}
    seen = set()
    for patch in data['patches']:
        if not isinstance(patch, dict):
            continue
        key, old, new, reason = (patch.get(k) for k in ('slot_key', 'old_text', 'new_text', 'reason'))
        if not isinstance(key, str) or key not in baseline:
            continue
        if key in seen:
            patches.pop(key, None)
            continue
        seen.add(key)
        if old != baseline[key] or not isinstance(new, str) or not new.strip() or new == old:
            continue
        if not isinstance(reason, str) or not reason.strip():
            continue
        patches[key] = {'old_text': old, 'new_text': new, 'reason': reason.strip()}
    return intent, patches


# ---------------------------------------------------------------------------
# SVG 文字 / 图片替换
# ---------------------------------------------------------------------------
def _index_by_id(root: etree._Element) -> dict[str, etree._Element]:
    return {element.get("id"): element for element in root.iter() if element.get("id")}


def _has_ancestor_transform(element: etree._Element) -> bool:
    node = element.getparent()
    while node is not None:
        if node.get("transform"):
            return True
        node = node.getparent()
    return False


def _mat_mul(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _transform_matrix(value: str | None) -> tuple[float, ...]:
    matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    if not value:
        return matrix
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", value):
        nums = [float(item) for item in re.findall(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", args)]
        if name == "matrix" and len(nums) == 6:
            piece = tuple(nums)
        elif name == "translate" and nums:
            piece = (1.0, 0.0, 0.0, 1.0, nums[0], nums[1] if len(nums) > 1 else 0.0)
        elif name == "scale" and nums:
            piece = (nums[0], 0.0, 0.0, nums[1] if len(nums) > 1 else nums[0], 0.0, 0.0)
        elif name == "rotate" and nums:
            angle = math.radians(nums[0])
            cos = math.cos(angle)
            sin = math.sin(angle)
            piece = (cos, sin, -sin, cos, 0.0, 0.0)
            if len(nums) >= 3:
                cx, cy = nums[1], nums[2]
                piece = _mat_mul((1.0, 0.0, 0.0, 1.0, cx, cy), _mat_mul(piece, (1.0, 0.0, 0.0, 1.0, -cx, -cy)))
        elif name == "skewX" and nums:
            piece = (1.0, 0.0, math.tan(math.radians(nums[0])), 1.0, 0.0, 0.0)
        elif name == "skewY" and nums:
            piece = (1.0, math.tan(math.radians(nums[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        matrix = _mat_mul(matrix, piece)
    return matrix


def _parent_matrix(element: etree._Element) -> tuple[float, ...]:
    chain: list[etree._Element] = []
    node = element.getparent()
    while node is not None:
        chain.append(node)
        node = node.getparent()
    matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for ancestor in reversed(chain):
        matrix = _mat_mul(matrix, _transform_matrix(ancestor.get("transform")))
    return matrix


def _inverse_matrix(matrix: tuple[float, ...]) -> tuple[float, ...] | None:
    a, b, c, d, e, f = matrix
    det = a * d - b * c
    if abs(det) < 1e-9:
        return None
    return (
        d / det,
        -b / det,
        -c / det,
        a / det,
        (c * f - d * e) / det,
        (b * e - a * f) / det,
    )


def _to_local_box(element: etree._Element, box: dict[str, float]) -> dict[str, float]:
    inverse = _inverse_matrix(_parent_matrix(element))
    if inverse is None:
        return box
    a, b, c, d, e, f = inverse
    x = float(box.get("x", 0))
    y = float(box.get("y", 0))
    width = float(box.get("width", 1))
    height = float(box.get("height", 1))
    points = []
    for px, py in ((x, y), (x + width, y), (x, y + height), (x + width, y + height)):
        points.append((a * px + c * py + e, b * px + d * py + f))
    left = min(px for px, _ in points)
    top = min(py for _, py in points)
    right = max(px for px, _ in points)
    bottom = max(py for _, py in points)
    return {"x": left, "y": top, "width": max(1.0, right - left), "height": max(1.0, bottom - top)}


def _style_runs(
    element: etree._Element, target: dict[str, Any], by_id
) -> tuple[list[tuple[str, str]], str]:
    """按文档顺序提取文字片段及其颜色，用于保留句内多色（黄色括号、红底白字等）。"""
    text_element = element if element.tag.endswith("text") else element.getparent()
    default_fill = ((text_element.get("fill") if text_element is not None else "") or "").strip()
    if not default_fill:
        default_fill = "#000000"
    if target.get("kind") == "tspan":
        member_ids = target.get("member_ids") or [target["id"]]
        nodes = [by_id.get(member_id) for member_id in member_ids]
        nodes = [node for node in nodes if node is not None]
    else:
        nodes = [element]

    runs: list[tuple[str, str]] = []

    def push(value: str | None, fill: str | None) -> None:
        if not value:
            return
        color = (fill or default_fill).strip() or default_fill
        if runs and runs[-1][1].lower() == color.lower():
            runs[-1] = (runs[-1][0] + value, runs[-1][1])
        else:
            runs.append((value, color))

    for node in nodes:
        if node is element:
            push(node.text, node.get("fill"))
        else:
            # 组内成员：直接文字 + 其 tail
            push(node.text, node.get("fill"))
            push(node.tail, node.get("fill"))
        if target.get("kind") == "text":
            for child in node.iter():
                if child is node:
                    continue
                push(child.text, child.get("fill"))
                push(child.tail, child.get("fill"))
    return runs, default_fill


def _build_segments(
    text: str, runs: list[tuple[str, str]], default_fill: str, highlight: str | None,
    *, exact: bool = False,
) -> list[tuple[str, str]]:
    original = "".join(run[0] for run in runs)
    unchanged = original == text if exact else _normalize_text(original) == _normalize_text(text)
    if unchanged and runs:
        return runs

    spans: list[tuple[int, int, str, str]] = []
    for content, fill in runs:
        if fill.lower() == default_fill.lower() or len(content.strip()) < 2:
            continue
        start = text.find(content)
        if start < 0:
            continue
        end = start + len(content)
        if any(not (end <= s or start >= e) for s, e, _, _ in spans):
            continue
        spans.append((start, end, content, fill))

    if not spans and highlight:
        match = _BRACKET_RE.search(text)
        if match:
            spans.append((match.start(), match.end(), match.group(1), highlight))
    if not spans:
        return [(text, default_fill)]

    spans.sort(key=lambda item: item[0])
    segments: list[tuple[str, str]] = []
    cursor = 0
    for start, end, content, fill in spans:
        if start > cursor:
            segments.append((text[cursor:start], default_fill))
        segments.append((content, fill))
        cursor = end
    if cursor < len(text):
        segments.append((text[cursor:], default_fill))
    return segments


def _set_merged_value(
    element: etree._Element,
    entry: dict[str, Any],
    value: str,
    by_id,
) -> None:
    """同行合并字段：写入首个节点，并清掉同组其余碎片节点。"""
    targets = entry.get("targets") or [{"id": entry["id"], "kind": entry.get("kind", "text")}]
    first = targets[0]
    for member_id in (first.get("member_ids") or [])[1:]:
        node = by_id.get(member_id)
        if node is not None and node.getparent() is not None:
            node.getparent().remove(node)
    for child in list(element):
        element.remove(child)
    default_fill = (entry.get("fill") or "#ffffff").strip() or "#ffffff"
    if entry.get("font_size"):
        element.set("font-size", str(entry["font_size"]))
    _clear_letter_spacing(element, first, by_id)
    _write_segments(element, [(value, default_fill)], default_fill)
    for target in targets[1:]:
        node = by_id.get(target["id"])
        if node is not None and node is not element and node.getparent() is not None:
            node.getparent().remove(node)


def _set_font_size(element, size):
    element.set("font-size", f"{size:g}")
    style = re.sub(r"(?:^|;)\s*font-size\s*:[^;]*", "", element.get("style", ""), flags=re.I).strip("; ")
    element.set("style", f"{style + ';' if style else ''}font-size:{size:g}px!important")


def apply_text_styles(
    root: etree._Element,
    schema: dict[str, Any],
    styles: dict[str, dict[str, Any]] | None,
) -> None:
    """应用画板工具条的字号缩放与 2px 位移。"""
    if not styles:
        return
    by_id = _index_by_id(root)
    entries = {entry["id"]: entry for entry in schema.get("texts", [])}
    moved: list[etree._Element] = []
    for entry_id, style in styles.items():
        entry = entries.get(entry_id)
        if entry is None:
            continue
        target = (entry.get("targets") or [{"id": entry["id"]}])[0]
        element = by_id.get(target["id"])
        if element is None:
            continue
        try:
            scale = float(style.get("font_scale") or 1)
        except (TypeError, ValueError):
            scale = 1.0
        if abs(scale - 1) > 1e-4 or style.get("manual_font_size"):
            base = float(entry.get("base_font_size") or entry.get("font_size") or 1)
            try:
                size = float(style.get("font_size") or base * scale)
            except (ValueError, TypeError):
                size = base * scale
            if math.isfinite(size) and size > 0:
                for item in entry.get("targets") or [entry]:
                    node = by_id.get(item["id"])
                    if node is not None:
                        _set_font_size(node, size)
        try:
            dx = float(style.get("dx") or 0)
            dy = float(style.get("dy") or 0)
        except (TypeError, ValueError):
            dx = dy = 0.0
        if dx or dy:
            text_element = _nearest_text(element)
            # A parent <text> and its own <tspan> both carrying the same field would
            # otherwise translate twice. One rigid offset per outermost text.
            if any(node is text_element for node in moved):
                continue
            moved.append(text_element)
            existing = (text_element.get("transform") or "").strip()
            text_element.set(
                "transform",
                f"{existing} translate({dx:g} {dy:g})".strip(),
            )


def _write_segments(
    element: etree._Element, segments: list[tuple[str, str]], default_fill: str
) -> None:
    element.text = None
    last: etree._Element | None = None

    # 逐字 tspan 经常把两个 — 拆成两个片段，先合并再包 -0.12em
    merged: list[tuple[str, str]] = []
    for content, fill in segments:
        if (
            merged
            and content
            and set(content) == {"—"}
            and set(merged[-1][0]) == {"—"}
            and merged[-1][1].lower() == fill.lower()
        ):
            merged[-1] = (merged[-1][0] + content, fill)
        else:
            merged.append((content, fill))

    def append_plain(content: str) -> None:
        nonlocal last
        if last is None:
            element.text = (element.text or "") + content
        else:
            last.tail = (last.tail or "") + content

    def append_colored(content: str, fill: str, spacing: str | None = None) -> None:
        nonlocal last
        child = etree.SubElement(element, f"{{{SVG_NS}}}tspan")
        if fill.lower() != default_fill.lower():
            child.set("fill", fill)
        if spacing:
            child.set("letter-spacing", spacing)
        child.text = content
        last = child

    for content, fill in merged:
        if not content:
            continue
        for part in re.split(r"(—{2,})", content):
            if not part:
                continue
            if part and set(part) == {"—"} and len(part) >= 2:
                # 标准两个全角破折号，不再叠加负字距（避免两个 — 粘成一个）
                append_colored(part, fill)
            elif fill.lower() == default_fill.lower():
                append_plain(part)
            else:
                append_colored(part, fill)


def _set_text_value(
    element: etree._Element,
    target: dict[str, Any],
    value: str,
    by_id,
    *,
    keep_children: bool = False,
    exact: bool = False,
) -> bool:
    text = value if exact else " ".join(value.split())
    if exact and (text != text.strip() or "  " in text):
        element.set(XML_SPACE, "preserve")
    if not text:
        element.text = None
        if not keep_children:
            for child in list(element):
                element.remove(child)
        if target.get("collapse"):
            for member_id in target.get("member_ids", [])[1:]:
                other = by_id.get(member_id)
                if other is not None and other.getparent() is not None:
                    other.getparent().remove(other)
        return True
    runs, default_fill = _style_runs(element, target, by_id)
    # 内容没变：原样保留设计师的逐字定位与破折号间距
    original = "".join(run[0] for run in runs)
    unchanged = original == text if exact else _normalize_text(original) == _normalize_text(text)
    if original and unchanged:
        return True
    if "—" in text:
        _clear_letter_spacing(element, target, by_id)
    if target["kind"] == "text":
        if keep_children:
            element.text = text
            return True
        for child in list(element):
            element.remove(child)
    else:
        # tspan 是独立字段：只清空当前 tspan 的子节点，父 <text> 下的兄弟 tspan 保留
        for child in list(element):
            element.remove(child)
        if target.get("collapse"):
            for member_id in target.get("member_ids", [])[1:]:
                other = by_id.get(member_id)
                if other is not None and other.getparent() is not None:
                    other.getparent().remove(other)
    highlight = None
    for _, fill in runs:
        if fill.lower() != default_fill.lower():
            highlight = fill
            break
    segments = _build_segments(text, runs, default_fill, highlight, exact=exact)
    _write_segments(element, segments, default_fill)
    if value != value.strip():
        element.set(XML_SPACE, "preserve")
    return True


def _consume_leading_chars(
    schema: dict[str, Any],
    values: dict[str, str],
    by_id: dict[str, etree._Element],
    exact_text_ids: frozenset[str],
) -> tuple[set[str], dict[str, float]]:
    """处理 Illustrator 把行首单字拆成独立节点的情况。

    如果目标新文本的第一个字，正好等于同一水平线左侧紧邻的单字节点，
    则清空/移除该单字，避免出现“扫扫码”。
    """
    consumed: set[str] = set()
    shifts: dict[str, float] = {}
    entries = [entry for entry in schema.get("texts", []) if entry.get("text")]

    for entry in entries:
        value = values.get(entry["id"])
        if entry["id"] in exact_text_ids or value is None or not str(value).strip():
            continue
        first_char = str(value).strip()[0]
        for other in entries:
            if other["id"] == entry["id"] or other["id"] in consumed or other["id"] in exact_text_ids:
                continue
            other_text = (other.get("text") or "").strip()
            if len(other_text) != 1 or other_text != first_char:
                continue
            if abs(float(other.get("y", 0)) - float(entry.get("y", 0))) >= 5:
                continue
            gap = float(entry.get("x", 0)) - (
                float(other.get("x", 0)) + float(other.get("width", 0))
            )
            if gap < -6 or gap > 8:
                continue

            consumed.add(other["id"])
            shifts[entry["id"]] = float(other.get("x", 0))
            element = by_id.get(other["id"])
            if element is None:
                break
            has_children = any(child.tag.endswith("tspan") for child in element)
            if has_children:
                # 只去掉这一颗“行首单字”，保留同一 <text> 里的其他 tspan
                element.text = None
            elif element.getparent() is not None:
                element.getparent().remove(element)
            break

    return consumed, shifts


def _apply_text_values(
    root: etree._Element,
    schema: dict[str, Any],
    values: dict[str, str],
    exact_text_ids: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], list[str]]:
    by_id = _index_by_id(root)
    applied: list[dict[str, Any]] = []
    warnings: list[str] = []

    # 哪些 tspan 自己就是独立字段：替换 <text> 时要保留它们
    mapped_tspan_ids: set[str] = set()
    for entry in schema.get("texts", []):
        for target in entry.get("targets") or []:
            if target.get("kind") == "tspan":
                mapped_tspan_ids.add(target["id"])

    consumed, shifts = _consume_leading_chars(schema, values, by_id, exact_text_ids)

    # 先给所有带描边的字段补上 paint-order / stroke，即使本次没有改文字
    for entry in schema.get("texts", []):
        style = entry.get("style") or {}
        if not style.get("stroke_color"):
            continue
        for target in entry.get("targets") or [{"id": entry["id"], "kind": entry.get("kind", "text")}]:
            element = by_id.get(target["id"])
            if element is not None:
                _apply_stroke_style(element, style)

    for entry in schema.get("texts", []):
        if not entry.get("merge"):
            continue
        value = values.get(entry["id"])
        exact = entry["id"] in exact_text_ids
        if value is None or (not exact and not str(value).strip() and not entry.get("role")):
            continue
        text = str(value) if exact else " ".join(str(value).split())
        if not exact and _normalize_text(text) == _normalize_text(entry.get("text", "")):
            continue
        target = (entry.get("targets") or [{"id": entry["id"]}])[0]
        element = by_id.get(target["id"])
        if element is None:
            continue
        if exact and (text != text.strip() or "  " in text):
            element.set(XML_SPACE, "preserve")
        _set_merged_value(element, entry, text, by_id)
        applied.append(entry)

    for entry in schema.get("texts", []):
        if entry.get("merge"):
            continue
        if entry["id"] in consumed:
            continue
        value = entry.get("id") and values.get(entry["id"])
        exact = entry["id"] in exact_text_ids
        if value is None or (not exact and not str(value).strip() and not entry.get("role")):
            # Legacy callers may send empty placeholders to keep defaults.
            continue
        changed = False
        for target in entry.get("targets") or [
            {"id": entry["id"], "kind": entry.get("kind", "text")}
        ]:
            element = by_id.get(target["id"])
            if element is None:
                continue
            keep_children = False
            if target["kind"] == "text":
                keep_children = any(
                    child.get("id") in mapped_tspan_ids
                    for child in element.iter()
                    if child is not element and child.tag.endswith("tspan")
                )
            if _set_text_value(
                element, target, str(value), by_id, keep_children=keep_children, exact=exact
            ):
                changed = True
                # 行首单字被吞掉后，把剩余文本左移回原来的行首
                leading_x = shifts.get(entry["id"])
                if leading_x is not None and target["kind"] == "tspan":
                    raw_x = element.get("x")
                    if raw_x is not None:
                        try:
                            origin = float(entry.get("x", 0)) - float(raw_x)
                            element.set("x", f"{leading_x - origin:.2f}")
                        except (TypeError, ValueError):
                            pass
        if changed:
            applied.append(entry)
        else:
            warnings.append(f"文字节点 {entry['id']} 未找到或内容为空，已保留默认文案")

    _apply_text_layouts(root, schema)
    return applied, warnings


def _apply_text_layouts(root: etree._Element, schema: dict[str, Any]) -> None:
    layouts = schema.get("text_layouts") or []
    if not layouts:
        return
    by_id = _index_by_id(root)
    for layout in layouts:
        parent = by_id.get(layout["parent_id"])
        first = by_id.get(layout["first_id"])
        if parent is None or first is None:
            continue
        inverse = _inverse_matrix(_mat_mul(_parent_matrix(parent), _transform_matrix(parent.get("transform"))))
        if not inverse:
            continue
        a, b, c, d, e, f = inverse
        x, y = layout["center"], layout["baseline"]
        # One absolute position creates one centered chunk; following runs flow from it.
        parent.set("text-anchor", "middle")
        first.set("text-anchor", "middle")
        parent.set("x", f"{a * x + c * y + e:g}")
        parent.set("y", f"{b * x + d * y + f:g}")
        for node_id in [layout["first_id"], *layout["following_ids"]]:
            node = by_id.get(node_id)
            if node is None or node is parent:
                continue
            for attr in ("x", "y", "dy", "text-anchor"):
                node.attrib.pop(attr, None)
            if node is first or not layout["gap"]:
                node.attrib.pop("dx", None)
            else:
                node.set("dx", str(layout["gap"]))


# ---------------------------------------------------------------------------
# 老师照片 / 二维码
# ---------------------------------------------------------------------------
def _photo_cache_key(photo: bytes, mode: str, target: tuple[int, int]) -> str:
    digest = hashlib.sha1(photo)
    digest.update(mode.encode("ascii"))
    digest.update(f"{target[0]}x{target[1]}".encode("ascii"))
    return digest.hexdigest()[:16]


def _process_portrait(
    photo: bytes, mode: str, frame: dict[str, float], scale: int
) -> tuple[bytes, list[str]]:
    width = max(1, int(round(float(frame.get("width") or 1))))
    height = max(1, int(round(float(frame.get("height") or 1))))
    warnings: list[str] = []
    if mode == "framed_circle":
        side = min(1600, max(256, int(round(max(width, height) * scale))))
        target = (side, side)
    else:
        target = (
            min(1600, max(64, int(round(width * scale)))),
            min(1600, max(64, int(round(height * scale)))),
        )

    cache_dir = settings.poster_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = _photo_cache_key(photo, mode, target)
    cache_path = cache_dir / f"{cache_key}.png"
    if cache_path.is_file():
        return cache_path.read_bytes(), warnings

    image = ip.decode_image(photo)
    result = run_pipeline(
        image,
        mode=mode,
        target_size=target,
        detector=get_detector(),
        border_width=0,
    )
    payload = ip.encode_png(result.image)
    cache_path.write_bytes(payload)
    warnings.extend(result.warnings)
    return payload, warnings


def _embed_image(
    root: etree._Element,
    entry: dict[str, Any],
    payload: bytes,
    *,
    reposition: bool,
) -> None:
    by_id = _index_by_id(root)
    element = by_id.get(entry["id"])
    if element is None:
        raise TemplateInvalid(f"模板中找不到图片节点 {entry['id']}")
    data_uri = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
    element.set("href", data_uri)
    if element.get(f"{{{XLINK_NS}}}href") is not None:
        element.set(f"{{{XLINK_NS}}}href", data_uri)
    element.set("preserveAspectRatio", "xMidYMid meet" if entry.get("field") == "qr_frame" else "xMidYMid slice")

    if reposition:
        frame = entry.get("frame") or {
            "x": entry.get("x", 0),
            "y": entry.get("y", 0),
            "width": entry.get("width", 1),
            "height": entry.get("height", 1),
        }
        # frame 是画布全局坐标；优先用浏览器实测的局部框（clipPath 真实包围盒），
        # 缺失时再按祖先 transform 反算到父级局部坐标。
        local = entry.get("local_frame") or _to_local_box(element, frame)
        # 旧 transform 会与新图片尺寸叠加，必须移除
        element.attrib.pop("transform", None)
        element.set("x", f"{local['x']:.2f}")
        element.set("y", f"{local['y']:.2f}")
        element.set("width", f"{max(1.0, local['width']):.2f}")
        element.set("height", f"{max(1.0, local['height']):.2f}")


# ---------------------------------------------------------------------------
# 字号自适应
# ---------------------------------------------------------------------------
def _font_fit_entries(schema, values):
    entries = schema.get("texts", [])
    changed = {e["id"] for e in entries if e["id"] in values
               and str(values[e["id"]]).strip() != (e.get("text") or "").strip()}
    groups = {e.get("font_fit_group") for e in entries if e["id"] in changed} - {None}
    return [e for e in entries if e["id"] in changed or e.get("font_fit_group") in groups]


def _font_fit_requests(entries, styles):
    requests = {}
    for entry in entries:
        style = (styles or {}).get(entry["id"], {})
        for target in entry.get("targets") or [entry]:
            requests[target["id"]] = {
                "base_font_size": entry.get("base_font_size") or entry.get("font_size"),
                "max_width": entry.get("max_width"),
                "font_fit_group": entry.get("font_fit_group"),
                "manual_font_size": style.get("manual_font_size") or bool(style.get("font_scale") not in (None, 1, "1")),
                "direct_text": bool(target.get("preserve_children")),
                "min_ratio": 0 if entry.get("font_fit_group") else 0.85,
            }
    return requests


def _apply_font_sizes(
    root: etree._Element,
    applied: list[dict[str, Any]],
    fits: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    by_id = _index_by_id(root)
    adjustments: list[dict[str, Any]] = []
    warnings: list[str] = []

    for entry in applied:
        fit = fits.get(entry["id"])
        if not fit:
            continue
        original = float(entry.get("base_font_size") or entry.get("font_size") or 0)
        final = float(fit.get("font_size") or original)
        if original <= 0 or final <= 0:
            continue
        ratio = final / original
        for target in entry.get("targets") or [entry]:
            element = by_id.get(target["id"])
            target_fit = fits.get(target["id"])
            if element is not None and target_fit:
                size = float(target_fit["font_size"])
                _set_font_size(element, size)
        adjustments.append(
            {
                "id": entry["id"],
                "field": entry.get("field"),
                "from": round(original, 2),
                "to": round(final, 2),
                "ratio": round(ratio, 3),
                "overflow": bool(fit.get("overflow")),
            }
        )
        if fit.get("overflow"):
            warnings.append(
                f"「{entry.get('text', '')[:10]}…」在 85% 字号下仍可能超出文本框"
            )
    return adjustments, warnings


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 工作台：纯 SVG 预览 + 高清导出
# ---------------------------------------------------------------------------
@dataclass
class ExportResult:
    job_id: str
    svg_path: Path
    png_path: Path
    width: int
    height: int
    warnings: list[str] = field(default_factory=list)
    font_adjustments: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


def _load_root(schema: dict[str, Any]) -> etree._Element:
    path = analyzer.schema_svg_path(schema)
    return etree.fromstring(
        path.read_bytes(),
        parser=etree.XMLParser(recover=True, huge_tree=True),
    )


def _apply_image_map(
    root: etree._Element,
    schema: dict[str, Any],
    images: dict[str, bytes] | None,
) -> list[str]:
    warnings: list[str] = []
    if not images:
        return warnings
    by_key = {entry[key]: entry for entry in schema.get("images", []) for key in ("key", "avatar_key") if entry.get(key)}
    by_id = {entry.get("id"): entry for entry in schema.get("images", [])}
    for key, payload in images.items():
        entry = by_key.get(key) or by_id.get(key)
        if entry is None:
            warnings.append(f"图片坑位 {key} 不在当前模板中，已忽略")
            continue
        try:
            _embed_image(root, entry, payload, reposition=True)
        except Exception as exc:
            warnings.append(f"图片坑位 {key} 嵌入失败：{exc}")
    return warnings


def build_svg(
    schema: dict[str, Any],
    values: dict[str, str],
    images: dict[str, bytes] | None = None,
    styles: dict[str, dict[str, Any]] | None = None,
    *,
    exact_text_ids: frozenset[str] = frozenset(),
) -> tuple[str, list[str]]:
    """不启动浏览器的快速套版，返回纯 SVG 文本（供前端毫秒级预览）。"""
    root = _load_root(schema)
    warnings: list[str] = []
    _, text_warnings = _apply_text_values(root, schema, values or {}, exact_text_ids)
    warnings.extend(text_warnings)
    warnings.extend(_apply_image_map(root, schema, images))
    apply_text_styles(root, schema, styles)
    svg = etree.tostring(root, encoding="unicode")
    return svg, warnings


def export_poster(
    schema: dict[str, Any],
    values: dict[str, str],
    images: dict[str, bytes] | None = None,
    *,
    scale: int = 2,
    styles: dict[str, dict[str, Any]] | None = None,
) -> ExportResult:
    """套版 + 浏览器字号自适应 + device_scale_factor 导出 PNG。"""
    started = time.perf_counter()
    timings: dict[str, float] = {}
    warnings: list[str] = []
    root = _load_root(schema)
    canvas = (float(schema["canvas"]["width"]), float(schema["canvas"]["height"]))

    started_text = time.perf_counter()
    applied, text_warnings = _apply_text_values(root, schema, values or {})
    warnings.extend(text_warnings)
    warnings.extend(_apply_image_map(root, schema, images))
    apply_text_styles(root, schema, styles)
    timings["build_ms"] = round((time.perf_counter() - started_text) * 1000, 1)

    job_id = uuid.uuid4().hex[:12]
    job_dir = settings.output_dir / "posters" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    working_path = job_dir / "poster.svg"
    working_path.write_bytes(
        etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)
    )

    final_scale = max(1, min(settings.poster_max_scale, int(scale or settings.poster_render_scale)))
    fit_entries = _font_fit_entries(schema, values or {})
    fit_requests = _font_fit_requests(fit_entries, styles)

    started_fit = time.perf_counter()
    fits = svg_browser.fit_and_export(
        working_path,
        canvas,
        fit_requests,
        out_png=job_dir / "poster.png",
        scale=final_scale,
    )
    timings["render_ms"] = round((time.perf_counter() - started_fit) * 1000, 1)
    adjustments, fit_warnings = _apply_font_sizes(root, fit_entries, fits)
    warnings.extend(fit_warnings)
    working_path.write_bytes(
        etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)
    )

    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return ExportResult(
        job_id=job_id,
        svg_path=working_path,
        png_path=job_dir / "poster.png",
        width=int(round(canvas[0] * final_scale)),
        height=int(round(canvas[1] * final_scale)),
        warnings=warnings,
        font_adjustments=adjustments,
        timings=timings,
    )


def render_poster(
    schema: dict[str, Any],
    *,
    values: dict[str, str] | None = None,
    text: str = "",
    photos: dict[int, bytes] | None = None,
    qr_image: bytes | None = None,
    preview_only: bool = False,
    scale: int | None = None,
) -> RenderResult:
    """生成海报。``photos`` 的 key 是 portrait slot（0,1,…）。"""
    started = time.perf_counter()
    timings: dict[str, float] = {}
    warnings: list[str] = []
    template_id = schema["template_id"]
    svg_path = analyzer.template_svg_path(template_id)
    root = etree.fromstring(
        svg_path.read_bytes(),
        parser=etree.XMLParser(recover=True, huge_tree=True),
    )
    canvas = (
        float(schema["canvas"]["width"]),
        float(schema["canvas"]["height"]),
    )

    engine = "manual"
    if values is None:
        values, engine, parse_warnings = parse_text_values(schema, text)
        warnings.extend(parse_warnings)
    values = {key: _clean(value) for key, value in (values or {}).items() if value is not None}

    started_text = time.perf_counter()
    applied, text_warnings = _apply_text_values(root, schema, values)
    warnings.extend(text_warnings)
    timings["text_ms"] = round((time.perf_counter() - started_text) * 1000, 1)

    started_images = time.perf_counter()
    photos = photos or {}
    for portrait in schema.get("portraits", []):
        slot = int(portrait.get("slot", 0))
        photo = photos.get(slot)
        if not photo:
            continue
        entry = next(
            (item for item in schema.get("images", []) if item["id"] == portrait["image_id"]),
            None,
        )
        if entry is None:
            continue
        try:
            payload, photo_warnings = _process_portrait(
                photo,
                portrait.get("mode") or "head_shoulder",
                entry.get("frame") or entry,
                scale or settings.poster_render_scale,
            )
            warnings.extend(photo_warnings)
            _embed_image(root, entry, payload, reposition=True)
        except BackgroundRemovalUnavailable as exc:
            warnings.append(f"人像 {slot + 1} 抠图失败，保留模板原图：{exc}")
        except Exception as exc:  # 单张照片失败不阻断整张海报
            warnings.append(f"人像 {slot + 1} 处理失败，保留模板原图：{exc}")

    if qr_image:
        qr_entry = next(
            (item for item in schema.get("images", []) if item.get("field") == "qr_frame"),
            None,
        )
        if qr_entry is not None:
            try:
                _embed_image(root, qr_entry, _resize_png(qr_image, qr_entry, scale or 2), reposition=True)
            except Exception as exc:
                warnings.append(f"二维码嵌入失败：{exc}")
    timings["image_ms"] = round((time.perf_counter() - started_images) * 1000, 1)

    job_id = uuid.uuid4().hex[:12]
    job_dir = settings.output_dir / "posters" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    working_path = job_dir / "poster.svg"
    working_path.write_bytes(
        etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)
    )

    fit_entries = _font_fit_entries(schema, values or {})
    fit_requests = _font_fit_requests(fit_entries, None)

    final_scale = max(1, min(settings.poster_max_scale, int(scale or settings.poster_render_scale)))
    png_path: Path | None = None
    started_fit = time.perf_counter()
    try:
        fits = svg_browser.fit_and_export(
            working_path,
            canvas,
            fit_requests,
            out_png=None if preview_only else job_dir / "poster.png",
            scale=final_scale,
        )
        timings["fit_ms"] = round((time.perf_counter() - started_fit) * 1000, 1)
        adjustments, fit_warnings = _apply_font_sizes(root, fit_entries, fits)
        warnings.extend(fit_warnings)
        working_path.write_bytes(
            etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)
        )
        if not preview_only:
            png_path = job_dir / "poster.png"
    except Exception as exc:
        warnings.append(f"无头浏览器渲染失败：{exc}")
        adjustments = []
        if not preview_only:
            raise

    width = int(round(canvas[0] * final_scale))
    height = int(round(canvas[1] * final_scale))
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 1)

    return RenderResult(
        job_id=job_id,
        svg_path=working_path,
        png_path=png_path,
        width=width,
        height=height,
        values=values,
        warnings=warnings,
        font_adjustments=adjustments,
        engine=engine,
        timings=timings,
    )


def _resize_png(image_bytes: bytes, entry: dict[str, Any], scale: int) -> bytes:
    frame = entry.get("frame") or entry
    width = min(1600, max(32, int(round(float(frame.get("width") or 1) * scale))))
    height = min(1600, max(32, int(round(float(frame.get("height") or 1) * scale))))
    with Image.open(io.BytesIO(image_bytes)) as image:
        resized = image.convert("RGBA")
        resized.thumbnail((width, height), Image.LANCZOS)
    buffer = io.BytesIO()
    resized.save(buffer, format="PNG")
    return buffer.getvalue()
