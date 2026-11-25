"""把 Seedream 拆层结果组装成可编辑 SVG。

* 底图：受保护背景 ``<image data-layout-role="bg">``，锁在最底层；
* 非文字层：先做紧身裁剪，再按裁剪后的 ``x/y/width/height`` 摆放；
* 文字层：按 ``text_recognition`` 重建原生 ``<text>`` / ``<tspan>``，不再叠光栅文字；
* 引用：SVG 里只写本地静态 URL（``/api/seedream/files/...``），导出时再内联。
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from lxml import etree
from PIL import Image

from app.services import fonts

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
FALLBACK_FAMILY = "'Alibaba PuHuiTi 3.0'"
_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([^)]+\)|[a-zA-Z]{3,20})$")


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _absolute_box(payload: Any) -> tuple[float, float, float, float] | None:
    """兼容 ``bounding_box.absolute`` / ``bbox`` / 直接 ``[l,t,r,b]``。"""
    if isinstance(payload, dict):
        box = (
            payload.get("absolute")
            or payload.get("abs")
            or payload.get("bbox")
            or payload.get("box")
        )
    else:
        box = payload
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        values = [_num(item) for item in box[:4]]
        if values[2] > values[0] and values[3] > values[1]:
            return values[0], values[1], values[2], values[3]
    return None


def _normalize_color(value: Any) -> str:
    text = str(value or "").strip()
    return text if text and _COLOR_RE.match(text) else "#111111"


def resolve_family(name: Any) -> str:
    raw = str(name or "").strip().strip("'\"")
    lowered = raw.lower()
    if not raw:
        return FALLBACK_FAMILY
    if any(token in lowered for token in ("puhui", "普惠", "alibaba")):
        return FALLBACK_FAMILY
    try:
        families = sorted({family for face in fonts.discover_fonts() for family in face.families})
    except Exception:  # noqa: BLE001 - 字体目录不可用时退回默认
        families = []
    for family in families:
        if lowered in family.lower() or family.lower() in lowered:
            return f"'{family}'"
    return FALLBACK_FAMILY


def _text_elements(recognition: dict[str, Any], counter: list[int]) -> list[etree._Element]:
    lines = recognition.get("lines") or recognition.get("paragraphs") or []
    nodes: list[etree._Element] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        runs = [run for run in (line.get("runs") or []) if isinstance(run, dict)]
        if not runs:
            runs = [{"text": line.get("text", ""), "style": line.get("style") or {},
                     "bounding_box": line.get("bounding_box") or {}}]
        runs = [run for run in runs if str(run.get("text") or "").strip()]
        if not runs:
            continue
        box = _absolute_box(line.get("bounding_box")) or _union_boxes(runs)
        if box is None:
            continue
        left, top, right, bottom = box
        height = max(1.0, bottom - top)
        width = max(1.0, right - left)
        text = "".join(str(run.get("text") or "") for run in runs)
        # 行高 ≈ 字号的 1.18 倍；0.85 倍行高作字号，再用行宽做防撑爆校正
        size = round(height * 0.85, 2)
        estimated = len(text) * size * 0.98
        if estimated > width * 1.12:
            size = round(max(6.0, size * (width * 1.12) / estimated), 2)
        baseline = round(top + height * 0.86, 2)

        counter[0] += 1
        element = etree.Element(f"{{{SVG_NS}}}text", nsmap={None: SVG_NS})
        element.set("id", f"seedream-text-{counter[0]:02d}")
        element.set("x", f"{left:.2f}")
        element.set("y", f"{baseline:.2f}")
        element.set("font-size", f"{size:g}")
        element.set("font-family", resolve_family((runs[0].get("style") or {}).get("similar_font_family")))
        element.set("fill", _normalize_color((runs[0].get("style") or {}).get("foreground_color")))
        weight = "700" if _is_bold(runs[0]) else "400"
        element.set("font-weight", weight)
        if _is_italic(runs[0]):
            element.set("font-style", "italic")

        positioned = [run for run in runs if _absolute_box(run.get("bounding_box"))]
        if len(runs) == 1:
            element.text = str(runs[0].get("text") or "")
        else:
            for run in runs:
                style = run.get("style") or {}
                span = etree.SubElement(element, f"{{{SVG_NS}}}tspan")
                span.text = str(run.get("text") or "")
                run_box = _absolute_box(run.get("bounding_box"))
                if len(positioned) == len(runs) and run_box:
                    span.set("x", f"{run_box[0]:.2f}")
                if style.get("foreground_color"):
                    span.set("fill", _normalize_color(style["foreground_color"]))
                if _is_bold(run):
                    span.set("font-weight", "700")
                if _is_italic(run):
                    span.set("font-style", "italic")
                if style.get("similar_font_family"):
                    span.set("font-family", resolve_family(style["similar_font_family"]))
        nodes.append(element)
    return nodes


def _union_boxes(runs: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    boxes = [box for box in (_absolute_box(run.get("bounding_box")) for run in runs) if box]
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _is_bold(run: dict[str, Any]) -> bool:
    style = run.get("style") or {}
    if style.get("bold") or style.get("font_weight") in {600, 700, 800, 900, "bold"}:
        return True
    return str(style.get("font_weight") or "").lower() == "bold"


def _is_italic(run: dict[str, Any]) -> bool:
    style = run.get("style") or {}
    return bool(style.get("italic")) or str(style.get("font_style") or "").lower() == "italic"


def tight_crop(path: Path) -> tuple[bytes, int, int, int, int] | None:
    """裁掉四周全透明像素，返回 (png, x, y, w, h)；空图层返回 None。"""
    raw = path.read_bytes()
    image = Image.open(io.BytesIO(raw)).convert("RGBA")
    box = image.getbbox()
    if box is None:
        return None
    left, top, right, bottom = box
    cropped = image.crop(box)
    buffer = io.BytesIO()
    cropped.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), left, top, cropped.width, cropped.height


def compose(
    *,
    base_href: str,
    layers: list[dict[str, Any]],
    canvas: tuple[int, int],
    job_dir: Path | None = None,
    asset_prefix: str = "",
) -> bytes:
    """组装最终 SVG。

    ``layers`` 每项支持 ``url`` / ``file``(绝对路径) / ``text_recognition``
    以及可选的 ``x/y/width/height``（接口若已返回裁切坐标则直接复用它）。
    """
    width, height = max(1, int(canvas[0])), max(1, int(canvas[1]))
    root = etree.Element(f"{{{SVG_NS}}}svg", nsmap={None: SVG_NS, "xlink": XLINK_NS})
    root.set("viewBox", f"0 0 {width} {height}")
    root.set("width", str(width))
    root.set("height", str(height))

    if base_href:
        base = etree.SubElement(root, f"{{{SVG_NS}}}image")
        base.set("id", "seedream-base")
        base.set("data-layout-role", "bg")
        base.set("x", "0")
        base.set("y", "0")
        base.set("width", str(width))
        base.set("height", str(height))
        base.set("preserveAspectRatio", "none")
        _set_href(base, base_href)

    counter = [0]
    for index, layer in enumerate(layers, start=1):
        recognition = layer.get("text_recognition") or layer.get("recognition")
        if isinstance(recognition, dict) and (recognition.get("lines") or recognition.get("paragraphs")):
            for element in _text_elements(recognition, counter):
                root.append(element)
            continue

        source = layer.get("file")
        href = layer.get("url") or ""
        box = None
        if isinstance(source, (str, Path)) and Path(source).is_file():
            path = Path(source)
            cropped = tight_crop(path)
            if cropped is None:
                continue
            png, left, top, crop_w, crop_h = cropped
            if job_dir is not None:
                name = f"layers/crop-{index:02d}.png"
                (Path(job_dir) / name).write_bytes(png)
                if asset_prefix:
                    href = f"{asset_prefix.rstrip('/')}/{name}"
            box = (
                _num(layer.get("x"), left),
                _num(layer.get("y"), top),
                _num(layer.get("width"), crop_w),
                _num(layer.get("height"), crop_h),
            )
        elif all(key in layer for key in ("x", "y", "width", "height")):
            box = (_num(layer["x"]), _num(layer["y"]), _num(layer["width"]), _num(layer["height"]))
        if not href or box is None:
            continue
        left, top, layer_w, layer_h = box
        if layer_w <= 0 or layer_h <= 0:
            continue
        image = etree.SubElement(root, f"{{{SVG_NS}}}image")
        image.set("id", f"seedream-layer-{index:02d}")
        image.set("data-decompose-layer", str(index))
        image.set("x", f"{left:.2f}")
        image.set("y", f"{top:.2f}")
        image.set("width", f"{layer_w:.2f}")
        image.set("height", f"{layer_h:.2f}")
        image.set("preserveAspectRatio", "none")
        _set_href(image, href)

    return etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)


def _set_href(element: etree._Element, href: str) -> None:
    element.set("href", href)
    element.set(f"{{{XLINK_NS}}}href", href)
