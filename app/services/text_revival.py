"""把 SVG 中被转曲（outline）的文字路径复活为可编辑的 `<text>`。

流程：渲染预览图 → 本地 OCR 识别文字行 → 与现有 `<text>` 的实测包围盒比对
→ 对没有活文字覆盖的行定位同一区域内的 `<path>` 轮廓 → 提取填充/渐变/几何
→ 插入新 `<text>` 并删除轮廓 → 用无头浏览器量新文字做几何验证。

设计原则：任何不确定情况都跳过并记录 warning，绝不误删原路径。
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from lxml import etree

from app.config import settings
from app.services import llm_client, ocr, svg_browser

logger = logging.getLogger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"
LANG_PUNCT_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)
GRAD_URL_RE = re.compile(r"url\(\s*['\"]?#([^)'\"]+)")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse_svg(data: bytes) -> etree._Element:
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(data, parser=parser)
    if root is None:
        raise ValueError("SVG 解析失败")
    return root


def _canvas_size(root: etree._Element) -> tuple[float, float]:
    view_box = root.get("viewBox") or ""
    parts = [float(x) for x in re.split(r"[\s,]+", view_box.strip()) if _NUMBER_RE.fullmatch(x or "")]
    if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
        return parts[2], parts[3]
    width = float(_NUMBER_RE.search(root.get("width") or "0").group(0) or 0) if _NUMBER_RE.search(root.get("width") or "") else 0
    height = float(_NUMBER_RE.search(root.get("height") or "0").group(0) or 0) if _NUMBER_RE.search(root.get("height") or "") else 0
    return (width or 1000.0, height or 1000.0)


def _ensure_ids(root: etree._Element) -> None:
    """给 path/g/text 补 ID，供浏览器检查与回写对应。"""
    existing = {el.get("id") for el in root.iter() if el.get("id")}
    counters: dict[str, int] = {}

    def ensure(element: etree._Element, prefix: str) -> None:
        if element.get("id"):
            return
        while True:
            counters[prefix] = counters.get(prefix, 0) + 1
            candidate = f"revival-{prefix}-{counters[prefix]:03d}"
            if candidate not in existing:
                break
        element.set("id", candidate)
        existing.add(candidate)

    for element in root.iter():
        local = _local(element.tag)
        if local == "path":
            ensure(element, "path")
        elif local == "g":
            ensure(element, "group")
        elif local == "text":
            ensure(element, "text")


def _write_svg(path: Path, root: etree._Element) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True))


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    return (right - left) * (bottom - top)


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    inter = _overlap(a, b)
    if inter <= 0:
        return 0.0
    area = a[2] * a[3] + b[2] * b[3] - inter
    return inter / area if area else 0.0


def _line_bbox(line: dict[str, Any]) -> tuple[float, float, float, float]:
    x, y, w, h = line["bbox"]
    return float(x), float(y), float(w), float(h)


def _has_visible_text(value: str) -> bool:
    return bool(value.strip()) and not LANG_PUNCT_RE.match(value.strip())


def _text_boxes(root: etree._Element, canvas: tuple[float, float], svg_path: Path) -> list[tuple[float, float, float, float]]:
    ids = [el.get("id") for el in root.iter() if _local(el.tag) == "text" and el.get("id")]
    if not ids:
        return []
    metrics = svg_browser.measure_svg(svg_path, canvas, ids)
    boxes = []
    for metric in metrics.values():
        x, y, w, h = (float(metric.get(key, 0)) for key in ("x", "y", "w", "h"))
        if w > 2 and h > 2:
            boxes.append((x, y, w, h))
    return boxes


def _covered_by_text(line_box: tuple[float, float, float, float], boxes: list[tuple[float, float, float, float]]) -> bool:
    line_area = line_box[2] * line_box[3]
    if line_area <= 0:
        return False
    overlapping = [box for box in boxes if _overlap(line_box, box) > 0]
    if not overlapping:
        return False
    # 多个碎片 text 的并集覆盖
    left = min(box[0] for box in overlapping)
    top = min(box[1] for box in overlapping)
    right = max(box[0] + box[2] for box in overlapping)
    bottom = max(box[1] + box[3] for box in overlapping)
    union_area = (right - left) * (bottom - top)
    inter = _overlap(line_box, (left, top, right - left, bottom - top))
    if inter / line_area >= settings.revival_min_iou:
        return True
    return any(_iou(line_box, box) >= 0.25 for box in overlapping)


def _candidate_groups(
    line_box: tuple[float, float, float, float],
    paths: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    lx, ly, lw, lh = line_box
    line_area = max(1.0, lw * lh)
    candidates: list[dict[str, Any]] = []
    for path in paths:
        if path.get("clipped"):
            continue
        px, py, pw, ph = (float(path.get(key, 0)) for key in ("x", "y", "w", "h"))
        if pw < 1 or ph < 1:
            continue
        if pw > lw * 2.6 or ph > lh * 2.6:
            continue
        pbox = (px, py, pw, ph)
        inter = _overlap(line_box, pbox)
        center_inside = lx <= px + pw / 2 <= lx + lw and ly <= py + ph / 2 <= ly + lh
        if inter <= 0 and not center_inside:
            continue
        if inter / max(1.0, pw * ph) < 0.18 and not center_inside:
            continue
        key = str(path.get("parentId") or path.get("transformedAncestorId") or "__root__")
        candidates.append({"path": path, "key": key, "box": pbox, "line_box": line_box})

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        grouped.setdefault(item["key"], []).append(item)

    groups: list[dict[str, Any]] = []
    for key, items in grouped.items():
        if not items or len(items) > 220:
            continue
        fills = {str(item["path"].get("fill") or "") for item in items}
        if len(fills) > 2:
            continue
        left = min(item["box"][0] for item in items)
        top = min(item["box"][1] for item in items)
        right = max(item["box"][0] + item["box"][2] for item in items)
        bottom = max(item["box"][1] + item["box"][3] for item in items)
        union = (left, top, right - left, bottom - top)
        coverage = _overlap(line_box, union) / line_area
        if coverage < 0.32:
            continue
        if union[2] > lw * 2.1 or union[3] > lh * 2.1:
            continue
        groups.append(
            {
                "key": key,
                "paths": [item["path"] for item in items],
                "box": union,
                "coverage": coverage,
                "iou": _iou(line_box, union),
            }
        )
    groups.sort(key=lambda item: (item["coverage"], item["iou"], len(item["paths"])), reverse=True)
    return groups


def _style_fill(element: etree._Element) -> str:
    style = element.get("style") or ""
    match = re.search(r"fill\s*:\s*([^;]+)", style, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return (element.get("fill") or "").strip()


def _raw_fill(element: etree._Element) -> str:
    node: etree._Element | None = element
    while node is not None:
        value = _style_fill(node)
        if value and value not in {"none", "transparent", "inherit"}:
            return value
        node = node.getparent()
    return ""


def _normalize_gradient_value(value: str | None, origin: float, size: float) -> str:
    if value is None:
        return ""
    value = str(value).strip()
    if value.endswith("%"):
        return value
    try:
        number = float(value)
    except ValueError:
        return value
    if size <= 0:
        return "0"
    return f"{(number - origin) / size:.4f}"


def _clone_gradient(root: etree._Element, fill: str, bbox: tuple[float, float, float, float]) -> str:
    match = GRAD_URL_RE.search(fill or "")
    if not match:
        return fill
    gradient_id = match.group(1)
    source = next((el for el in root.iter() if el.get("id") == gradient_id), None)
    if source is None or _local(source.tag) not in {"linearGradient", "radialGradient"}:
        return fill
    clone = copy.deepcopy(source)
    new_id = f"revived-grad-{abs(hash((gradient_id, bbox[0], bbox[1], bbox[2], bbox[3]))) % 100000:05d}"
    clone.set("id", new_id)
    original_units = source.get("gradientUnits") or "objectBoundingBox"
    clone.set("gradientUnits", "objectBoundingBox")
    if original_units != "objectBoundingBox":
        x, y, w, h = bbox
        if _local(clone.tag) == "linearGradient":
            clone.set("x1", _normalize_gradient_value(source.get("x1", "0%"), x, w))
            clone.set("y1", _normalize_gradient_value(source.get("y1", "0%"), y, h))
            clone.set("x2", _normalize_gradient_value(source.get("x2", "100%"), x, w))
            clone.set("y2", _normalize_gradient_value(source.get("y2", "0%"), y, h))
        else:
            clone.set("cx", _normalize_gradient_value(source.get("cx", "50%"), x, w))
            clone.set("cy", _normalize_gradient_value(source.get("cy", "50%"), y, h))
            clone.set("r", _normalize_gradient_value(source.get("r", "50%"), 0, max(w, h)))
    clone.attrib.pop("gradientTransform", None)
    defs = next((el for el in root.iter() if _local(el.tag) == "defs"), None)
    if defs is None:
        defs = etree.Element(f"{{{SVG_NS}}}defs")
        root.insert(0, defs)
    defs.append(clone)
    return f"url(#{new_id})"


def _font_weight(height: float) -> int:
    if height >= 55:
        return 800
    if height >= 48:
        return 700
    return 500


def _insert_text(
    root: etree._Element,
    path_elements: list[etree._Element],
    text_element: etree._Element,
) -> None:
    if not path_elements:
        return
    parent = path_elements[0].getparent()
    if parent is None or _local(parent.tag) in {"defs", "clipPath", "mask"}:
        return
    highest = parent
    node = parent
    while node is not None and node is not root:
        if node.get("transform") or node.get("clip-path") or node.get("clipPath"):
            highest = node
        node = node.getparent()
    container = highest.getparent()
    if container is None:
        root.append(text_element)
    else:
        try:
            index = list(container).index(highest)
        except ValueError:
            index = len(container) - 1
        container.insert(index + 1, text_element)
    for element in path_elements:
        parent_node = element.getparent()
        if parent_node is None:
            continue
        parent_node.remove(element)
        if (
            _local(parent_node.tag) == "g"
            and len(parent_node) == 0
            and not (parent_node.text or "").strip()
            and parent_node.getparent() is not None
        ):
            parent_node.getparent().remove(parent_node)


def _make_text(
    root: etree._Element,
    fill: str,
    bbox: tuple[float, float, float, float],
    text: str,
    index: int,
    canvas: tuple[float, float],
) -> etree._Element:
    x, y, w, h = bbox
    element = etree.Element(f"{{{SVG_NS}}}text")
    element.set("id", f"revived-text-{index:03d}")
    # 居中线：左右留白基本对称才用 middle，否则左对齐
    centered = abs((x + w / 2) - canvas[0] / 2) < canvas[0] * 0.08 and x > canvas[0] * 0.04
    anchor = "middle" if centered else "start"
    element.set("text-anchor", anchor)
    element.set("x", f"{(x + w / 2) if centered else x:.2f}")
    element.set("y", f"{y + h * 0.86:.2f}")
    element.set("font-family", "'Alibaba PuHuiTi 3.0'")
    element.set("font-size", str(max(8, round(h * 1.02))))
    element.set("font-weight", str(_font_weight(h)))
    element.set("fill", fill)
    element.set("data-revived", "1")
    if " " in text:
        element.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    element.text = text
    return element


def _title_reference(bbox, titles, canvas):
    x, y, w, h = bbox
    if h < 48:
        return None
    matches = []
    for title in titles:
        tx, ty, tw, th = title["box"]
        if not (0 < tw and 0.92 <= th / h <= 1.08 and 0.5 <= abs(ty - y) / h <= 3):
            continue
        if title.get("scale") != [1, 1] or not 100 <= title["weight"] <= 900:
            continue
        if title["anchor"] == "middle":
            alignment = abs((tx + tw / 2) - (x + w / 2))
        else:
            alignment = abs(tx - x)
        if alignment <= min(h * 0.25, canvas[0] * 0.03):
            matches.append((abs(ty - y) + alignment, title))
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0])
    if len(matches) > 1 and matches[1][0] - matches[0][0] < h * 0.1 and (
        matches[1][1]["weight"], matches[1][1]["size"]
    ) != (matches[0][1]["weight"], matches[0][1]["size"]):
        return None
    return matches[0][1]


def _fit_errors(metric, target):
    return tuple(abs(actual / expected - 1) for actual, expected in zip(metric["box"][2:], target[2:]))


def _fit_outline(probe, text_id, bbox, start_size, reference):
    probes = probe(text_id, [start_size])
    if not probes or any(v <= 0 for v in probes[0]["box"][2:]):
        return None
    w, h = probes[0]["box"][2:]
    ideals = [start_size * bbox[2] / w, start_size * bbox[3] / h]
    sizes = {n for ideal in ideals for n in range(max(1, round(ideal) - 3), round(ideal) + 4)}
    ref_size = round(reference["size"]) if reference else 0
    if ref_size > 0:
        sizes.add(ref_size)
    matches = [m for m in probe(text_id, sorted(sizes)) if max(_fit_errors(m, bbox)) < 0.01]
    if not matches:
        return None
    if reference and abs(reference["box"][3] / bbox[3] - 1) < 0.01:
        match = next((m for m in matches if m["size"] == ref_size), None)
        if match:
            return match
    return min(matches, key=lambda m: (max(_fit_errors(m, bbox)), abs(m["size"] - start_size)))


def _try_line(
    root: etree._Element,
    line: dict[str, Any],
    groups: list[dict[str, Any]],
    canvas: tuple[float, float],
    work_dir: Path,
    index: int,
    titles=(),
) -> tuple[etree._Element | None, dict[str, Any] | None, str | None]:
    text = line["text"].strip()
    line_box = _line_bbox(line)
    for group in groups:
        path_ids = [path.get("id") for path in group["paths"] if path.get("id")]
        if not path_ids:
            continue
        trial = copy.deepcopy(root)
        by_id = {el.get("id"): el for el in trial.iter() if el.get("id")}
        path_elements = [by_id[pid] for pid in path_ids if pid in by_id]
        if not path_elements:
            continue
        bbox = group["box"]
        fill = _raw_fill(path_elements[0])
        if not fill:
            fill = str(group["paths"][0].get("fill") or "#ffffff")
        fill = _clone_gradient(trial, fill, bbox)
        new_text = _make_text(trial, fill, bbox, text, index, canvas)
        _insert_text(trial, path_elements, new_text)
        trial_path = work_dir / f"trial-{index:03d}.svg"
        _write_svg(trial_path, trial)
        container = new_text.getparent()
        if container is None:
            continue
        container_id = "__root__" if container is trial else container.get("id")
        reference = _title_reference(bbox, [title for title in titles if title.get("container_id") == container_id], canvas)
        if reference:
            new_text.set("font-weight", str(reference["weight"]))
            new_text.set("font-family", reference["family"])
            _write_svg(trial_path, trial)
        text_id = new_text.get("id")
        with svg_browser.text_ink_probe(trial_path, canvas) as probe:
            fit = _fit_outline(probe, text_id, bbox, int(new_text.get("font-size")), reference)
        if fit is None:
            continue
        new_text.set("font-size", str(fit["size"]))
        mx, my, mw, mh = fit["box"]
        sx, sy = fit["scale"]
        new_text.set("x", f"{float(new_text.get('x')) + (bbox[0] - mx) / sx:.4f}")
        new_text.set("y", f"{float(new_text.get('y')) + (bbox[1] + bbox[3] - my - mh) / sy:.4f}")
        _write_svg(trial_path, trial)
        with svg_browser.text_ink_probe(trial_path, canvas) as probe:
            final = probe(text_id)
        if (not final or final[0].get("size") != fit["size"]
                or final[0].get("weight", int(new_text.get("font-weight"))) != int(new_text.get("font-weight"))
                or max(_fit_errors(final[0], bbox)) >= 0.01):
            continue
        fx, fy, fw, fh = final[0]["box"]
        if abs(fx - bbox[0]) > max(0.02, bbox[2] * 0.01) or abs(fy + fh - bbox[1] - bbox[3]) > max(0.02, bbox[3] * 0.01):
            continue
        report = {
            "text": text,
            "bbox": [round(value, 2) for value in line_box],
            "old_paths": path_ids,
            "new_text_id": new_text.get("id"),
            "font_size": float(new_text.get("font-size") or 0),
            "font_weight": int(new_text.get("font-weight") or 0),
            "fill": new_text.get("fill"),
            "confidence": round(float(line.get("confidence") or 0), 3),
            "reference_text_id": reference["id"] if reference else None,
            "outline_bbox": [round(v, 4) for v in bbox],
            "glyph_bbox": [round(v, 4) for v in final[0]["box"]],
            "width_error": _fit_errors(final[0], bbox)[0],
            "height_error": _fit_errors(final[0], bbox)[1],
        }
        return trial, report, None
    return None, None, "整数字号未能使字形宽高误差同时小于 1%，保留原路径"


def _refine_with_llm(
    lines: list[dict[str, Any]], preview_png: bytes
) -> list[dict[str, Any]]:
    if not lines or not llm_client.llm_configured() or not settings.llm_vision:
        return lines
    payload = [
        {"text": line["text"], "bbox": [round(v, 1) for v in line["bbox"]]}
        for line in lines
    ]
    system = "你是中文海报 OCR 校对助手。只校正已有行，不新增或删除行。返回严格 JSON。"
    user = (
        "下面是 OCR 识别的文字行和坐标。请根据图片校正错字、漏字，保持 bbox 不变，"
        "返回 {\"lines\":[{\"text\":\"...\",\"bbox\":[...]}]}：\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        data = llm_client.chat_json(
            system,
            user,
            image_png=preview_png,
            image_detail="high",
            max_tokens=settings.llm_max_tokens,
            temperature=0.0,
        )
        corrected = data.get("lines")
        if not isinstance(corrected, list):
            return lines
        out: list[dict[str, Any]] = []
        for original, item in zip(lines, corrected):
            if not isinstance(item, dict):
                out.append(original)
                continue
            text = str(item.get("text") or "").strip()
            bbox = item.get("bbox")
            if not text or not isinstance(bbox, list) or len(bbox) != 4:
                out.append(original)
                continue
            try:
                box = tuple(float(value) for value in bbox)
            except (TypeError, ValueError):
                out.append(original)
                continue
            out.append({**original, "text": text, "bbox": box})
        return out
    except Exception as exc:
        logger.warning("LLM OCR 校对失败，保留 OCR 结果：%s", exc)
        return lines


def revive_outlined_text(
    svg_path: Path,
    *,
    use_llm: bool = True,
) -> dict[str, Any]:
    """原地复活 SVG 中的转曲文字；返回报告。失败时文件保持不变。"""
    report: dict[str, Any] = {
        "available": False,
        "revived": [],
        "skipped": [],
        "warnings": [],
        "revived_count": 0,
    }
    if not settings.revive_outlined_text:
        report["warnings"].append("转曲文字复活已关闭（REVIVE_OUTLINED_TEXT=false）")
        return report
    if not svg_path.is_file():
        report["warnings"].append("模板文件不存在")
        return report
    if not settings.ocr_enabled:
        report["warnings"].append("未启用 OCR，跳过转曲文字复活")
        return report
    if not ocr.ocr_available():
        report["warnings"].append("未安装/无法加载 OCR（rapidocr-onnxruntime），跳过转曲文字复活")
        return report

    original_bytes = svg_path.read_bytes()
    root = _parse_svg(original_bytes)
    canvas = _canvas_size(root)
    _ensure_ids(root)

    work_dir = settings.template_dir / "revival"
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir, prefix="work-") as temp_name:
        work = Path(temp_name)
        working_svg = work / "working.svg"
        _write_svg(working_svg, root)
        preview_path = work / "preview.png"
        try:
            svg_browser.screenshot_svg(working_svg, canvas, preview_path, scale=1)
        except Exception as exc:
            report["warnings"].append(f"预览图生成失败，跳过复活：{exc}")
            return report
        try:
            lines = ocr.detect_text_lines(preview_path.read_bytes(), canvas)
        except Exception as exc:
            report["warnings"].append(f"OCR 识别失败，跳过复活：{exc}")
            return report
        if use_llm:
            lines = _refine_with_llm(lines, preview_path.read_bytes())
        lines = [line for line in lines if _has_visible_text(line.get("text", ""))]
        if not lines:
            report["warnings"].append("OCR 未识别到有效文字行")
            return report
        try:
            boxes = _text_boxes(root, canvas, working_svg)
        except Exception as exc:
            report["warnings"].append(f"现有文字度量失败，跳过复活：{exc}")
            return report
        try:
            paths = svg_browser.inspect_paths(working_svg, canvas)
        except Exception as exc:
            report["warnings"].append(f"path 检查失败，跳过复活：{exc}")
            return report
        report["available"] = True

        title_ids = [el.get("id") for el in root.iter() if _local(el.tag) == "text" and el.get("id")
                     and el.get("data-revived") != "1" and len("".join(el.itertext()).strip()) >= 3]
        titles = []
        if title_ids:
            try:
                with svg_browser.text_ink_probe(working_svg, canvas) as probe:
                    by_id = {el.get("id"): el for el in root.iter() if el.get("id")}
                    for text_id in title_ids:
                        metric = probe(text_id)
                        parent = by_id[text_id].getparent()
                        container_id = "__root__" if parent is root else parent.get("id") if parent is not None else None
                        if container_id and metric and metric[0]["box"][3] >= 48:
                            titles.append({**metric[0], "id": text_id, "container_id": container_id})
            except Exception as exc:
                report["warnings"].append(f"邻近标题字形度量失败：{exc}")

        counts: dict[str, int] = {}
        revived = 0
        for line in sorted(lines, key=lambda item: (item["bbox"][1], item["bbox"][0])):
            line_box = _line_bbox(line)
            if _covered_by_text(line_box, boxes):
                continue
            groups = _candidate_groups(line_box, paths)
            if not groups:
                report["skipped"].append(
                    {"text": line["text"], "bbox": [round(v, 1) for v in line_box], "reason": "未找到可信轮廓组"}
                )
                continue
            counts["line"] = counts.get("line", 0) + 1
            used_ids = {el.get("id") for el in root.iter() if el.get("id")}
            while f"revived-text-{counts['line']:03d}" in used_ids:
                counts["line"] += 1
            try:
                trial, revival, reason = _try_line(
                    root, line, groups, canvas, work, counts["line"], titles
                )
            except Exception as exc:
                logger.warning("字形验证失败，保留原路径：%s", exc)
                trial, revival, reason = None, None, "字形度量失败，保留原路径"
            if trial is None or revival is None:
                report["skipped"].append(
                    {"text": line["text"], "bbox": [round(v, 1) for v in line_box], "reason": reason or "几何验证未通过"}
                )
                continue
            root = trial
            revived += 1
            report["revived"].append(revival)

        report["revived_count"] = revived
        if revived:
            backup = svg_path.with_name(svg_path.stem + ".pre-revival.svg")
            if not backup.exists():
                backup.write_bytes(original_bytes)
            _write_svg(svg_path, root)
            report["backup"] = backup.name
            report["warnings"].append(f"已复活 {revived} 行转曲文字")
        else:
            report["warnings"].append("没有发现可安全复活的转曲文字，原文件未修改")
    return report
