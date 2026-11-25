#!/usr/bin/env python
"""离线校验 ``scripts/decompose_smoke.py`` 的纯逻辑（不需要 torch / LayerD / PaddleOCR）。

用一张构造的「背景 + 文字层 + 图形层」模拟 LayerD 输出，验证：
按层归属文字行、覆盖率判定、文字像素擦除、残余图形保留、SVG 组装与报告。

    .\\.venv\\Scripts\\python.exe scripts\\dev\\check_decompose_smoke.py
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from scripts import decompose_smoke as smoke  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"[ok] {name}")
    else:
        FAILURES.append(name)
        print(f"[FAIL] {name} {detail}")


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeResult:
    def __init__(self, layers):
        self.layers = layers
        self.elements = []
        self.canvas_size = layers[0].size


def make_layers():
    size = (400, 600)
    background = Image.new("RGBA", size, (60, 60, 60, 255))
    foreground = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(foreground)
    # 文字区域（会被 OCR 命中并转成 text）：高度控制在画布 4% 以内
    draw.rectangle((80, 100, 320, 120), fill=(200, 30, 40, 255))
    # 同一层里的非文字图形（应保留为 PNG）
    draw.rectangle((60, 300, 120, 360), fill=(20, 80, 200, 255))
    return size, FakeResult([background, foreground])


def run_build(keep_png_text=False):
    size, result = make_layers()
    resized_rgb = Image.new("RGB", size, "white")
    args = smoke.parse_args(["--keep-png-text"] if keep_png_text else [])
    smoke.read_lines = lambda engine, image: [
        smoke.OcrLine("暑期招生", 0.95, [(80, 100), (320, 100), (320, 120), (80, 120)])
    ]
    layers, warnings, background = smoke.build_layers(
        result, 1.0, args, resized_rgb, ("stub", None)
    )
    return size, layers, warnings, background


def main() -> int:
    smoke.setup_console()
    args = smoke.parse_args([])

    size, layers, warnings, background = run_build()
    check("图层数", len(layers) == 1, len(layers))
    layer = layers[0]
    check("文字层判定", layer.action == "text", layer.reason)
    check("每行一个 text 节点", len(layer.line_nodes) == 1, layer.line_nodes)
    check("文字内容", layer.text == "暑期招生", layer.text)
    check("覆盖率达标", layer.coverage >= 0.5, layer.coverage)
    check("残余图形保留 PNG", layer.keep_image and len(layer.png) > 0, layer.keep_image)
    check("文字颜色取自图层像素", str(layer.fill).startswith("#c"), layer.fill)

    # 同一份结果，若显式保留 PNG，则不应转文字
    _, layers_png, _, _ = run_build(keep_png_text=True)
    check("keep-png-text 时不转文字", layers_png[0].action == "png", layers_png[0].reason)

    # SVG 组装：背景 + 残余图层 + text；base64 内嵌；背景有锁定标记
    svg = smoke.build_svg(background, layers, size, args)
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    images = root.findall(f"{ns}image")
    texts = root.findall(f"{ns}text")
    check("SVG 含背景与残余图层", len(images) == 2, len(images))
    check("图片用 data URI", all(el.get("href", "").startswith("data:image/png;base64,") for el in images))
    check("背景层有 bg 标记", images[0].get("data-decompose-layer") == "bg")
    check("SVG 含 text 节点", len(texts) == 1)
    check("text 内容", texts[0].text == "暑期招生", texts[0].text)
    check("text 字号由行高推得", float(texts[0].get("font-size")) > 10, texts[0].get("font-size"))
    check("text 带原框宽", abs(float(texts[0].get("data-box-w")) - 240) <= 2, texts[0].get("data-box-w"))
    check("viewBox 为原图尺寸", root.get("viewBox") == "0 0 400 600")

    # 报告生成
    summary = {
        "image": "x.png", "original_size": (400, 600), "inference_size": (400, 600),
        "scale": 1.0, "max_side": 1024, "max_iterations": 3, "elapsed_s": 12.3,
        "peak_rss_mb": 1234.5, "layers": 1, "text_layers": 1, "text_nodes": 1,
        "image_layers": 1,
        "svg_bytes": len(svg), "timings": [{"stage": "decompose", "ms": 12000}],
        "warnings": warnings, "env": {"HF_HOME": "x"},
    }
    details = smoke._layer_details(layers)
    markdown = smoke._markdown(summary, details)
    check("报告表格", "| 图层 |" in markdown and "暑期招生" in markdown and "12.3" in markdown)

    if FAILURES:
        print(f"\n失败：{FAILURES}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
