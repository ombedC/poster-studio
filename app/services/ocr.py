"""本地 OCR 封装（RapidOCR），用于识别预览图中的视觉文字行。

RapidOCR 是可选依赖：未安装时返回空列表，上传流程继续，不会阻断模板。
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_ENGINE = None
_ENGINE_FAILED = False


def _load_engine():
    global _ENGINE, _ENGINE_FAILED
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_FAILED or not settings.ocr_enabled:
        return None
    try:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
        logger.info("RapidOCR 已加载")
    except Exception as exc:  # 未安装 / 模型不可用
        _ENGINE_FAILED = True
        logger.warning("RapidOCR 不可用：%s", exc)
        _ENGINE = None
    return _ENGINE


def ocr_available() -> bool:
    return _load_engine() is not None


def _box_to_bbox(points: Any, image_size: tuple[int, int], canvas: tuple[float, float]):
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    img_w, img_h = image_size
    scale_x = canvas[0] / max(1, img_w)
    scale_y = canvas[1] / max(1, img_h)
    x = min(xs) * scale_x
    y = min(ys) * scale_y
    w = (max(xs) - min(xs)) * scale_x
    h = (max(ys) - min(ys)) * scale_y
    return (x, y, w, h)


def _merge_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把同一水平线上的 OCR 片段合并成一行。"""
    if not lines:
        return []
    ordered = sorted(lines, key=lambda item: (item["bbox"][1], item["bbox"][0]))
    groups: list[list[dict[str, Any]]] = []
    for line in ordered:
        x, y, w, h = line["bbox"]
        center = y + h / 2
        placed = False
        for group in groups:
            gx, gy, gw, gh = group[0]["bbox"]
            gcenter = gy + gh / 2
            if abs(center - gcenter) > max(h, gh) * 0.45:
                continue
            right = max(gx + gw, x + w)
            left = min(gx, x)
            gap = max(gx - (x + w), x - (gx + gw), 0)
            if gap > max(h, gh) * 1.4:
                continue
            if right - left > max(gw, w) * 2.8:
                continue
            group.append(line)
            placed = True
            break
        if not placed:
            groups.append([line])

    merged: list[dict[str, Any]] = []
    for group in groups:
        group.sort(key=lambda item: item["bbox"][0])
        x0 = min(item["bbox"][0] for item in group)
        y0 = min(item["bbox"][1] for item in group)
        x1 = max(item["bbox"][0] + item["bbox"][2] for item in group)
        y1 = max(item["bbox"][1] + item["bbox"][3] for item in group)
        text = ""
        for item in group:
            value = item["text"]
            if text and text[-1:].isascii() and value[:1].isascii():
                text += " "
            text += value
        merged.append(
            {
                "text": text.strip(),
                "bbox": (x0, y0, x1 - x0, y1 - y0),
                "confidence": min(item["confidence"] for item in group),
            }
        )
    return merged


def detect_text_lines(
    image_png: bytes,
    canvas: tuple[float, float],
) -> list[dict[str, Any]]:
    """识别图中的文字行，返回 ``text/bbox/confidence``（画布坐标）。"""
    engine = _load_engine()
    if engine is None or not image_png:
        return []
    try:
        from PIL import Image
        import numpy as np
    except Exception:
        return []
    try:
        image = Image.open(BytesIO(image_png)).convert("RGB")
        image_size = image.size
        result, _ = engine(np.array(image))
    except Exception as exc:
        logger.warning("OCR 识别失败：%s", exc)
        return []
    if not result:
        return []
    lines: list[dict[str, Any]] = []
    for item in result:
        try:
            points, text, score = item[0], str(item[1]).strip(), float(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        if not text or score < settings.ocr_min_confidence:
            continue
        bbox = _box_to_bbox(points, image_size, canvas)
        if bbox[3] < settings.revival_min_line_height:
            continue
        lines.append({"text": text, "bbox": bbox, "confidence": score})
    return _merge_lines(lines)
