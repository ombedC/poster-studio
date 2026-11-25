# -*- coding: utf-8 -*-
"""批量清洗、动静分离并拍平海报模板（.eps / .ai / .svg）。

背景
----
设计师给的 .eps / .ai 或 Illustrator 导出的 .svg 直接接进工作台，会稳定触发三类问题：

1. XML 语法错误：``font-family="A, "B""`` 这种嵌套双引号，以及 ``data-name`` 里
   未转义的孤立 ``&``，会让浏览器直接中断解析；
2. 背景不稳：复杂 EPS 背景里的渐变蒙版在浏览器里会缩水，画板外的装饰横条还会把
   SVG 撑大，导出时左右出现白边；
3. 中文路径：项目本身就在中文目录里，临时文件很容易因为路径编码而打不开。

本脚本把整条流水线自动化：

1. **格式转换**：``.eps`` / ``.ai`` 调本地 Inkscape 命令行静默导出为原始 ``.svg``；
   ``.svg`` 直接进入下一步。``.ai`` 会优先直接导入，失败后按内部容器
   （PDF 兼容层 / PostScript）改扩展名重试。
2. **XML 预清洗**：正则修复 ``font-family`` 里的嵌套双引号（内层改单引号）、
   把孤立 ``&`` 转成 ``&amp;``（已知 HTML 实体则解码成字符）、清理非法控制字符。
3. **动静分离**：提取根节点 viewBox 尺寸，强制根节点 ``width/height/overflow``；
   把 ``<text>`` / ``<tspan>`` / ``<image>`` 留作动态层，其余色块、渐变、蒙版、
   装饰线条全部留作纯背景层。动态节点所在的 ``<g>`` 与 ``<defs>`` 依赖会被保留。
   默认严格按“图片全部动态”处理；若模板的整张背景本身就是一张大图，可加
   ``--images auto``（覆盖画板 ≥60% 的图片留在背景层）或 ``--images static``。
4. **背景拍平**：Playwright 无头浏览器以 viewBox 尺寸、2 倍 ``deviceScaleFactor``
   渲染纯背景层，并按 viewBox 精确裁切，画板外内容不会再撑出白边。
5. **重新合成**：背景 PNG 转 Base64，作为 ``<image x/y/width/height>`` 插到动态层
   最底部（第一个子节点），输出 ``data/templates/{原文件名}.fixed.svg``。

用法（Windows PowerShell）::

    # 处理整个文件夹（不含子目录）
    .\\.venv\\Scripts\\python.exe .\\scripts\\batch_prepare_templates.py "D:\\海报源文件"

    # 递归处理，覆盖已有输出
    .\\.venv\\Scripts\\python.exe .\\scripts\\batch_prepare_templates.py "D:\\海报源文件" `
        --recursive --force

    # EPS 背景本身就是整张位图时，把图片留在背景层一起拍平
    .\\.venv\\Scripts\\python.exe .\\scripts\\batch_prepare_templates.py "D:\\海报源文件" `
        --images static

注意事项：
- ``.ai`` 需在 Illustrator 保存时勾选“创建 PDF 兼容文件”（默认勾选）；没有兼容层的
  AI 无法可靠转换，建议让设计师改存 EPS/PDF/SVG；
- 删掉所有 ``<script>`` 与 ``on*`` 事件属性，避免本地 SVG 在无头浏览器里执行脚本；
- 动态层的外部相对路径图片会内嵌为 data URI，输出模板可独立分发；
- 临时文件优先放在纯 ASCII 的可写目录（``%TEMP%`` 等），可用
  ``POSTER_TEMP_DIR`` 或 ``--keep-temp`` 控制；
- Inkscape 路径可用 ``--inkscape`` 或环境变量 ``INKSCAPE_PATH`` 指定。
"""

from __future__ import annotations

import argparse
import base64
import copy
import html.entities
import io
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# 复用项目配置（模板目录、Playwright 渠道、默认倍率），失败时退回本脚本默认值
try:  # pragma: no cover - 独立运行时允许没有 app 包
    from app.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None

try:
    from lxml import etree
except ImportError as exc:  # pragma: no cover - 依赖缺失
    raise SystemExit("缺少 lxml：请先执行 `.venv\\Scripts\\pip install lxml`") from exc


SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
SUPPORTED_SUFFIXES = {".eps", ".ai", ".svg"}
INKSCAPE_INPUT_SUFFIXES = {".eps", ".ai"}

# 背景渲染的最大像素数，避免超大画板把浏览器拖死（2 倍图约 8000x10000）
MAX_PIXELS = 80_000_000
MAX_SIDE = 20_000
# 单个外部图片内嵌上限，超过则保留绝对路径
MAX_EMBED_BYTES = 40 * 1024 * 1024
# --images auto 时，渲染包围盒覆盖画板超过该比例的图片视为背景纹理
BACKDROP_COVERAGE_RATIO = 0.6

DYNAMIC_TAGS = {"text", "tspan", "image"}
TEXT_TAGS = {"text", "tspan"}

# 定义类节点不参与“背景/动态”二分：两边都原样保留，避免 mask / clipPath / 渐变丢失
DEFINITION_TAGS = {
    "defs",
    "clipPath",
    "mask",
    "pattern",
    "marker",
    "symbol",
    "filter",
    "linearGradient",
    "radialGradient",
}
KEEP_WHOLE_TAGS = DEFINITION_TAGS | {"style", "title", "desc", "font", "font-face", "view"}

# 需要原样跳过、不执行的正则区间：CDATA / 注释 / DOCTYPE 内部子集
_PROTECTED_BLOCKS_RE = re.compile(
    r"(<!\[CDATA\[.*?\]\]>|<!--.*?-->|<!DOCTYPE(?:[^>\[]|\[.*?\])*>)", re.S
)


class PrepareError(RuntimeError):
    """单个模板处理失败。"""


class BrowserUnavailable(PrepareError):
    """Playwright / 浏览器不可用。"""


class InkscapeUnavailable(PrepareError):
    """需要转换 EPS，但找不到 Inkscape。"""


@dataclass
class RepairStats:
    font_family: int = 0
    ampersand: int = 0
    control_chars: int = 0
    encoding: str = "utf-8"


@dataclass
class ViewBox:
    min_x: float
    min_y: float
    width: float
    height: float


@dataclass
class LayerStats:
    background_removed: int = 0
    dynamic_kept: int = 0
    texts: int = 0
    images: int = 0
    backdrop_images: int = 0


@dataclass
class FileResult:
    source: Path
    target: Path
    viewbox: ViewBox
    png_size: tuple[int, int]
    stats: RepairStats
    layers: LayerStats
    warnings: list[str]
    seconds: float
    output_bytes: int


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def configure_stdio() -> None:
    """中文 Windows 控制台可能不是 UTF-8，至少保证不会因编码异常中断。

    重定向到管道 / 被 GUI 捕获时，Python 默认按 ANSI 代码页输出，调用方若按
    UTF-8 解码就会乱码，因此非终端场景强制 UTF-8；真实终端保留系统编码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():  # type: ignore[union-attr]
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def fmt_num(value: float) -> str:
    """把 1242.190000 这类数字写成 1242.19，整数不带小数点。"""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def mb(size: int) -> str:
    return f"{size / (1024 * 1024):.2f} MB"


def local_name(element) -> str:
    if not isinstance(element.tag, str):
        return ""
    try:
        return etree.QName(element).localname
    except ValueError:
        return ""


def in_definition(element) -> bool:
    for ancestor in element.iterancestors():
        if local_name(ancestor) in DEFINITION_TAGS:
            return True
    return False


# ---------------------------------------------------------------------------
# 第一步：读取与 XML 预清洗
# ---------------------------------------------------------------------------


def decode_svg_file(path: Path) -> tuple[str, str]:
    """按 XML 声明 / BOM 猜编码读取，兼容 UTF-8、GB18030 与 UTF-16。"""
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe\x00\x00"):
        declared = "utf-32-le"
    elif raw.startswith(b"\x00\x00\xfe\xff"):
        declared = "utf-32-be"
    elif raw.startswith(b"\xff\xfe"):
        declared = "utf-16-le"
    elif raw.startswith(b"\xfe\xff"):
        declared = "utf-16-be"
    elif raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        declared = "utf-8"
    else:
        match = re.match(
            rb"<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']+)[\"']", raw[:512], re.I
        )
        declared = match.group(1).decode("ascii", "ignore").strip() if match else "utf-8"

    for candidate in dict.fromkeys([declared, "utf-8", "gb18030", "latin-1"]):
        try:
            return raw.decode(candidate), candidate
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def strip_control_chars(text: str) -> tuple[str, int]:
    """删除 XML 1.0 非法控制字符（保留 \\t \\n \\r）。"""
    pattern = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    return pattern.subn("", text)


# 匹配 font-family="..."，一直吞到“后面紧跟属性名或标签结束”的那个引号。
# 这样既能处理 font-family="A, "B""，也能处理 font-family="A, "B" letter-spacing=...。
_FONT_FAMILY_RE = re.compile(
    r'(?P<head>\bfont-family\s*=\s*")(?P<body>[^<>]*?)(?P<tail>")'
    r"(?=\s*(?:/?>|[A-Za-z_:][\w:.\-]*\s*=))",
    re.S,
)


def fix_font_family_quotes(text: str) -> tuple[str, int]:
    """把 font-family 属性值里的嵌套双引号改成单引号。"""
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        body = match.group("body")
        if '"' not in body:
            return match.group(0)
        count += 1
        return match.group("head") + body.replace('"', "'") + match.group("tail")

    return _FONT_FAMILY_RE.sub(repl, text), count


# & 后面跟实体名（或数字引用），分号可选；没有实体名时匹配裸 &
_AMP_RE = re.compile(r"&(#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9._-]*)?;?")
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


def _escape_text_char(char: str) -> str:
    if char == "&":
        return "&amp;"
    if char == "<":
        return "&lt;"
    if char == ">":
        return "&gt;"
    return char


def _fix_ampersands_in_segment(segment: str) -> tuple[str, int]:
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        raw = match.group(0)
        name = match.group(1)
        if name is None:
            count += 1
            return "&amp;"
        if raw.endswith(";"):
            if name.startswith("#") or name in _XML_ENTITIES:
                return raw
            decoded = html.entities.html5.get(name + ";")
            if decoded is not None:
                count += 1
                return "".join(_escape_text_char(char) for char in decoded)
        count += 1
        return "&amp;" + raw[1:]

    return _AMP_RE.sub(repl, segment), count


def fix_ampersands(text: str) -> tuple[str, int]:
    """转义孤立 &，同时保留合法实体；CDATA / 注释 / DOCTYPE 内不动。"""
    parts = _PROTECTED_BLOCKS_RE.split(text)
    total = 0
    for index in range(0, len(parts), 2):  # 偶数下标才是普通文本
        fixed, count = _fix_ampersands_in_segment(parts[index])
        parts[index] = fixed
        total += count
    return "".join(parts), total


def repair_xml_text(text: str) -> tuple[str, RepairStats]:
    """Illustrator 常见语法错误的统一修复。"""
    stats = RepairStats()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text, stats.control_chars = strip_control_chars(text)
    text = text.lstrip("\ufeff")
    # 统一按 UTF-8 重新解析，去掉可能对不上的原始 encoding 声明
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1, flags=re.I)
    text, stats.font_family = fix_font_family_quotes(text)
    text, stats.ampersand = fix_ampersands(text)
    return text, stats


def parse_svg(text: str) -> etree._Element:
    parser = etree.XMLParser(
        huge_tree=True,
        resolve_entities="internal",
        no_network=True,
        recover=False,
        remove_blank_text=False,
        strip_cdata=False,
    )
    try:
        root = etree.fromstring(text.encode("utf-8"), parser)
    except etree.XMLSyntaxError as exc:
        line = column = 0
        position = getattr(exc, "position", None)
        if position:
            line, column = position
        snippet = ""
        lines = text.splitlines()
        if 0 < line <= len(lines):
            raw = lines[line - 1]
            start = max(0, column - 80)
            snippet = raw[start : column + 80]
        raise PrepareError(
            f"XML 仍无法解析：{exc}（第 {line} 行第 {column} 列）"
            + (f"\n  片段：{snippet}" if snippet else "")
        ) from exc
    if local_name(root) != "svg":
        raise PrepareError(f"根节点不是 <svg>，而是 <{local_name(root) or root.tag}>")
    return root


# ---------------------------------------------------------------------------
# 第二步：画板尺寸与动静分离
# ---------------------------------------------------------------------------


_LENGTH_RE = re.compile(
    r"^\s*([-+]?(?:\d+\.?\d*|\.\d+))\s*(px|pt|pc|mm|cm|in|%)?\s*$", re.I
)
_UNIT_TO_PX = {
    "px": 1.0,
    "pt": 96.0 / 72.0,
    "pc": 16.0,
    "mm": 96.0 / 25.4,
    "cm": 96.0 / 2.54,
    "in": 96.0,
}


def parse_length_px(value: str | None) -> float | None:
    if not value:
        return None
    match = _LENGTH_RE.match(value)
    if not match:
        return None
    unit = (match.group(2) or "px").lower()
    if unit == "%":
        return None
    return float(match.group(1)) * _UNIT_TO_PX[unit]


def parse_viewbox(root: etree._Element) -> ViewBox:
    raw = root.get("viewBox") or root.get("viewbox")
    if raw:
        numbers = re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", raw)
        if len(numbers) >= 4:
            min_x, min_y, width, height = (float(number) for number in numbers[:4])
            if width > 0 and height > 0:
                return ViewBox(min_x, min_y, width, height)
    width = parse_length_px(root.get("width"))
    height = parse_length_px(root.get("height"))
    if width and height and width > 0 and height > 0:
        return ViewBox(0.0, 0.0, width, height)
    raise PrepareError(
        "无法从根节点读取画板尺寸：viewBox 缺失或非法，width/height 也不是可用长度"
    )


def guard_canvas(viewbox: ViewBox, scale: int) -> None:
    values = (viewbox.min_x, viewbox.min_y, viewbox.width, viewbox.height)
    if not all(math.isfinite(value) for value in values):
        raise PrepareError("viewBox 含非法数值")
    if viewbox.width > MAX_SIDE or viewbox.height > MAX_SIDE:
        raise PrepareError(f"画板过大：{viewbox.width}x{viewbox.height}（上限 {MAX_SIDE}）")
    pixels = viewbox.width * viewbox.height * scale * scale
    if pixels > MAX_PIXELS:
        raise PrepareError(
            f"背景图预计 {pixels / 1e6:.1f} 百万像素，超过上限 "
            f"{MAX_PIXELS / 1e6:.0f}；请降低 --scale"
        )


def force_canvas(root: etree._Element, viewbox: ViewBox) -> None:
    """强制根节点按 viewBox 定尺寸，并裁掉画板外内容。"""
    width = fmt_num(viewbox.width)
    height = fmt_num(viewbox.height)
    root.set("width", width)
    root.set("height", height)
    root.set("overflow", "hidden")
    root.set(
        "viewBox",
        f"{fmt_num(viewbox.min_x)} {fmt_num(viewbox.min_y)} {width} {height}",
    )
    style = (root.get("style") or "").strip().rstrip(";")
    extra = f"width:{width}px;height:{height}px;overflow:hidden"
    root.set("style", f"{style};{extra}" if style else extra)


def strip_active_content(root: etree._Element) -> None:
    """移除 script 和 on* 事件属性，防止本地 SVG 在无头浏览器里执行脚本。"""
    for element in list(root.iter()):
        if not isinstance(element.tag, str):
            continue
        if local_name(element) == "script":
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
            continue
        for name in list(element.attrib):
            value = element.attrib.get(name, "")
            lowered = name.lower()
            if not name.startswith("{") and lowered.startswith("on"):
                del element.attrib[name]
            elif "javascript:" in value.lower():
                del element.attrib[name]


_TRANSFORM_RE = re.compile(r"([A-Za-z]+)\s*\(([^)]*)\)")


def multiply_matrix(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    """2D 仿射矩阵相乘（SVG matrix(a b c d e f) 顺序）。"""
    a, b, c, d, e, f = left
    other_a, other_b, other_c, other_d, other_e, other_f = right
    return (
        a * other_a + c * other_b,
        b * other_a + d * other_b,
        a * other_c + c * other_d,
        b * other_c + d * other_d,
        a * other_e + c * other_f + e,
        b * other_e + d * other_f + f,
    )


def parse_transform(value: str | None) -> tuple[float, ...]:
    """解析 SVG transform 列表；无法识别时跳过该项。"""
    matrix: tuple[float, ...] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    if not value:
        return matrix
    for name, arguments in _TRANSFORM_RE.findall(value):
        numbers = [
            float(number)
            for number in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", arguments)
        ]
        lowered = name.lower()
        step: tuple[float, ...] | None = None
        if lowered == "matrix" and len(numbers) >= 6:
            step = tuple(numbers[:6])
        elif lowered == "translate" and numbers:
            step = (1.0, 0.0, 0.0, 1.0, numbers[0], numbers[1] if len(numbers) > 1 else 0.0)
        elif lowered == "scale" and numbers:
            scale_x = numbers[0]
            scale_y = numbers[1] if len(numbers) > 1 else scale_x
            step = (scale_x, 0.0, 0.0, scale_y, 0.0, 0.0)
        elif lowered == "rotate" and numbers:
            angle = math.radians(numbers[0])
            cos = math.cos(angle)
            sin = math.sin(angle)
            if len(numbers) >= 3:
                center_x, center_y = numbers[1], numbers[2]
                step = (
                    cos,
                    sin,
                    -sin,
                    cos,
                    center_x - cos * center_x + sin * center_y,
                    center_y - sin * center_x - cos * center_y,
                )
            else:
                step = (cos, sin, -sin, cos, 0.0, 0.0)
        elif lowered == "skewx" and numbers:
            step = (1.0, 0.0, math.tan(math.radians(numbers[0])), 1.0, 0.0, 0.0)
        elif lowered == "skewy" and numbers:
            step = (1.0, math.tan(math.radians(numbers[0])), 0.0, 1.0, 0.0, 0.0)
        if step is not None:
            matrix = multiply_matrix(matrix, step)
    return matrix


def combined_transform(element: etree._Element) -> tuple[float, ...]:
    chain: list[str] = []
    node: etree._Element | None = element
    while node is not None:
        if isinstance(node.tag, str):
            value = node.get("transform")
            if value:
                chain.append(value)
        node = node.getparent()
    matrix: tuple[float, ...] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for value in reversed(chain):
        matrix = multiply_matrix(matrix, parse_transform(value))
    return matrix


def image_coverage_ratio(image: etree._Element, viewbox: ViewBox) -> float:
    """估算 <image> 渲染后覆盖画板的比例（忽略 clip/mask）。"""
    try:
        x = float(image.get("x") or 0.0)
        y = float(image.get("y") or 0.0)
        width = float(image.get("width") or 0.0)
        height = float(image.get("height") or 0.0)
    except ValueError:
        return 0.0
    if width <= 0 or height <= 0:
        return 0.0
    a, b, c, d, e, f = combined_transform(image)
    points = [
        (a * px + c * py + e, b * px + d * py + f)
        for px, py in ((x, y), (x + width, y), (x, y + height), (x + width, y + height))
    ]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    left = max(min(xs), viewbox.min_x)
    right = min(max(xs), viewbox.min_x + viewbox.width)
    top = max(min(ys), viewbox.min_y)
    bottom = min(max(ys), viewbox.min_y + viewbox.height)
    if right <= left or bottom <= top:
        return 0.0
    return ((right - left) * (bottom - top)) / (viewbox.width * viewbox.height)


def element_path(root: etree._Element, element: etree._Element) -> tuple[int, ...]:
    """用子节点下标序列定位元素，便于在深拷贝之间对应同一节点。"""
    path: list[int] = []
    node = element
    while node is not root:
        parent = node.getparent()
        if parent is None:
            return ()
        path.append(parent.index(node))
        node = parent
    return tuple(reversed(path))


def find_backdrop_images(
    root: etree._Element, viewbox: ViewBox
) -> list[tuple[etree._Element, float]]:
    """找出覆盖画板达到 BACKDROP_COVERAGE_RATIO 的图片（通常是背景纹理）。"""
    found: list[tuple[etree._Element, float]] = []
    for element in root.iter():
        if not isinstance(element.tag, str) or local_name(element) != "image":
            continue
        if in_definition(element):
            continue
        ratio = image_coverage_ratio(element, viewbox)
        if ratio >= BACKDROP_COVERAGE_RATIO:
            found.append((element, ratio))
    return found


def split_layers(
    root: etree._Element, image_mode: str, viewbox: ViewBox
) -> tuple[etree._Element, etree._Element, LayerStats]:
    """返回 (纯背景层, 动态层, 统计)。两个返回值都是独立深拷贝。

    image_mode:
      - dynamic：所有 image 归动态层（默认，适合人物占位图）；
      - static：所有 image 留在背景层一起拍平；
      - auto：覆盖画板 ≥60% 的图片视为背景纹理，其余归动态层。
    """
    background = copy.deepcopy(root)
    dynamic = copy.deepcopy(root)
    strip_active_content(background)
    strip_active_content(dynamic)

    # 必须在清洗之后建路径索引：删除 <script> 会改变子节点下标
    backdrop_elements = (
        find_backdrop_images(background, viewbox) if image_mode == "auto" else []
    )
    backdrop_paths = {
        element_path(background, element) for element, _ in backdrop_elements
    }

    def is_dynamic(element: etree._Element, tree: etree._Element) -> bool:
        tag = local_name(element)
        if tag in TEXT_TAGS:
            return not in_definition(element)
        if tag == "image":
            if in_definition(element) or image_mode == "static":
                return False
            if image_mode == "auto" and element_path(tree, element) in backdrop_paths:
                return False
            return True
        return False

    removed = 0
    for element in list(background.iter()):
        if not isinstance(element.tag, str):
            continue
        if is_dynamic(element, background):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
                removed += 1

    # 动态层：只留 text/tspan/image 及其祖先、后代和定义类节点
    keep: set[etree._Element] = set()
    for element in dynamic.iter():
        if not isinstance(element.tag, str):
            continue
        tag = local_name(element)
        if is_dynamic(element, dynamic):
            keep.add(element)
            keep.update(element.iterdescendants())
            keep.update(element.iterancestors())
        elif tag in KEEP_WHOLE_TAGS:
            keep.add(element)
            keep.update(element.iterdescendants())
            keep.update(element.iterancestors())

    for element in list(dynamic.iter()):
        if element is dynamic or element in keep:
            continue
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)

    stats = LayerStats(background_removed=removed, backdrop_images=len(backdrop_paths))
    for element in dynamic.iter():
        if not isinstance(element.tag, str):
            continue
        tag = local_name(element)
        if tag in TEXT_TAGS or tag == "image":
            stats.dynamic_kept += 1
        if tag == "text":
            stats.texts += 1
        if tag == "image":
            stats.images += 1
    return background, dynamic, stats


# ---------------------------------------------------------------------------
# 外部资源：背景用绝对路径，动态层内嵌成 data URI
# ---------------------------------------------------------------------------


def resolve_local_reference(
    value: str, base_dirs: list[Path]
) -> tuple[Path | None, str]:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https", "data", "ftp"}:
        return None, ""
    fragment = parsed.fragment
    if parsed.scheme == "file":
        return Path(url2pathname(unquote(parsed.path))), fragment
    if parsed.scheme:
        return None, ""
    relative = unquote(parsed.path)
    if not relative:
        return None, ""
    candidates = [(base / relative).resolve() for base in base_dirs]
    for candidate in candidates:
        if candidate.is_file():
            return candidate, fragment
    return (candidates[0] if candidates else None), fragment


def file_to_data_uri(path: Path, cache: dict[Path, str | None]) -> str | None:
    if path in cache:
        return cache[path]
    uri: str | None = None
    try:
        if path.stat().st_size <= MAX_EMBED_BYTES:
            mime = mimetypes.guess_type(path.name)[0] or ""
            if mime.startswith("image/"):
                payload = base64.b64encode(path.read_bytes()).decode("ascii")
                uri = f"data:{mime};base64,{payload}"
    except OSError:
        uri = None
    cache[path] = uri
    return uri


def rewrite_external_hrefs(
    root: etree._Element,
    base_dirs: list[Path],
    embed_images: bool,
    warnings: list[str],
) -> None:
    """把相对引用改成绝对 file URI；动态层图片尽量内嵌为 data URI。"""
    cache: dict[Path, str | None] = {}
    for element in root.iter():
        if not isinstance(element.tag, str) or local_name(element) not in {"image", "use"}:
            continue
        for key in (f"{{{XLINK_NS}}}href", "href"):
            value = element.get(key)
            if not value:
                continue
            lowered = value.strip().lower()
            if lowered.startswith(("data:", "#", "http://", "https://", "javascript:")):
                continue
            path, fragment = resolve_local_reference(value, base_dirs)
            if path is None:
                continue
            if not path.is_file():
                warnings.append(f"引用的外部资源不存在：{value}")
                continue
            if embed_images and local_name(element) == "image":
                uri = file_to_data_uri(path, cache)
                if uri:
                    element.set(key, uri)
                    continue
                warnings.append(f"外部图片过大或类型不支持内嵌，保留绝对路径：{path.name}")
            element.set(key, path.as_uri() + (f"#{fragment}" if fragment else ""))


# ---------------------------------------------------------------------------
# 临时目录：优先纯 ASCII 路径，规避中文路径编码问题
# ---------------------------------------------------------------------------


def pick_temp_base() -> Path:
    candidates: list[Path] = []
    env_dir = os.getenv("POSTER_TEMP_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path(tempfile.gettempdir()))
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Temp")
    candidates.append(Path("C:/Windows/Temp"))
    candidates.append(BASE_DIR / "data" / ".cache" / "tmp")

    fallback: Path | None = None
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".poster-write-probe"
            probe.write_bytes(b"ok")
            probe.unlink()
            resolved = candidate.resolve()
        except Exception:  # noqa: BLE001 - 逐个尝试
            continue
        if fallback is None:
            fallback = resolved
        if is_ascii(str(resolved)):
            return resolved
    if fallback is not None:
        return fallback
    return Path(tempfile.gettempdir())


@contextmanager
def work_directory(keep: bool, verbose: bool):
    base = pick_temp_base()
    path = Path(tempfile.mkdtemp(prefix="poster-templates-", dir=str(base)))
    if verbose:
        print(f"临时目录：{path}")
    try:
        yield path
    finally:
        if keep:
            print(f"临时目录已保留：{path}")
        else:
            shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 第三步：EPS / AI -> SVG（Inkscape）
# ---------------------------------------------------------------------------


def find_inkscape(explicit: str | None) -> str:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_path = os.getenv("INKSCAPE_PATH")
    if env_path:
        candidates.append(Path(env_path))
    for name in ("inkscape", "inkscape.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    roots: list[Path] = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = os.getenv(variable)
        if value:
            roots.append(Path(value))
    for root in roots:
        candidates.extend(
            [
                root / "Inkscape" / "bin" / "inkscape.exe",
                root / "Inkscape" / "inkscape.exe",
                root / "Programs" / "Inkscape" / "bin" / "inkscape.exe",
            ]
        )

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise InkscapeUnavailable(
        "找不到 Inkscape，无法转换 .eps / .ai。请安装 Inkscape 1.x，或用 "
        "`--inkscape \"C:\\Program Files\\Inkscape\\bin\\inkscape.exe\"` / "
        "环境变量 INKSCAPE_PATH 指定路径；也可以先让设计师另存为 SVG。"
    )


def run_command(command: list[str], timeout_s: int, cwd: Path) -> tuple[int, str, str]:
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout_s,
        "cwd": str(cwd),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.run(command, **kwargs)
    return (
        process.returncode,
        process.stdout.decode("utf-8", "replace"),
        process.stderr.decode("utf-8", "replace"),
    )


def inkscape_attempts(exe: str, source: Path, target: Path) -> list[tuple[str, list[str]]]:
    return [
        (
            "Inkscape 1.x",
            [
                exe,
                str(source),
                "--export-type=svg",
                "--export-plain-svg",
                "--export-area-page",
                f"--export-filename={target}",
            ],
        ),
        (
            "Inkscape 0.92",
            [exe, "-z", "-f", str(source), "-l", str(target)],
        ),
    ]


def sniff_ai_flavor(path: Path) -> str:
    """判断 .ai 的底层容器：PDF 兼容层 / PostScript / 未知。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return "unknown"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"%!PS"):
        return "ps"
    return "unknown"


def stage_copy(source: Path, target: Path, label: str, errors: list[str]) -> bool:
    try:
        shutil.copy2(source, target)
        return True
    except OSError as exc:
        errors.append(f"{label}失败：{exc}")
        return False


def try_inkscape(
    candidates: list[tuple[str, Path]],
    target: Path,
    inkscape: str,
    timeout_s: int,
    errors: list[str],
    verbose: bool,
) -> bool:
    for label, current in candidates:
        for version, command in inkscape_attempts(inkscape, current, target):
            if target.exists():
                try:
                    target.unlink()
                except OSError:
                    pass
            if verbose:
                print("  Inkscape:", subprocess.list2cmdline(command))
            try:
                code, stdout, stderr = run_command(command, timeout_s, target.parent)
            except subprocess.TimeoutExpired:
                errors.append(f"{version}（{label}）超时 {timeout_s}s")
                continue
            except OSError as exc:
                errors.append(f"{version}（{label}）无法启动：{exc}")
                continue
            if code == 0 and target.is_file() and target.stat().st_size > 0:
                return True
            detail = (stderr or stdout).strip().replace("\n", " ")[:400]
            errors.append(f"{version}（{label}）exit={code} {detail}")
    return False


def convert_with_inkscape(
    source: Path, target: Path, inkscape: str, timeout_s: int, verbose: bool
) -> Path:
    """静默调用 Inkscape 把 EPS / AI 导出为原始 SVG。

    .ai 本质是 Illustrator 私有容器：勾选“创建 PDF 兼容文件”时内部是 PDF，
    老文件可能是 PostScript。直接导入失败后会按容器类型临时改成 .pdf/.eps 重试。
    中文路径失败时也会用 ASCII 副本重试。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    suffix = source.suffix.lower()

    candidates: list[tuple[str, Path]] = [("原始文件", source)]
    if not is_ascii(str(source)) and suffix != ".ai":
        staged = target.with_name("source" + suffix)
        if stage_copy(source, staged, "复制到 ASCII 临时目录", errors):
            candidates.append(("ASCII 临时副本", staged))

    if try_inkscape(candidates, target, inkscape, timeout_s, errors, verbose):
        return target

    if suffix == ".ai":
        fallback = {"pdf": ".pdf", "ps": ".eps"}.get(sniff_ai_flavor(source))
        if fallback:
            staged = target.with_name("source" + fallback)
            label = f"AI 的 {fallback.upper()} 兼容层"
            if stage_copy(source, staged, f"复制{label}", errors):
                if try_inkscape(
                    [(label, staged)], target, inkscape, timeout_s, errors, verbose
                ):
                    return target

    hint = ""
    if suffix == ".ai":
        hint = (
            "（AI 需在 Illustrator 保存时勾选「创建 PDF 兼容文件」，"
            "或改存为 EPS/PDF/SVG）"
        )
    raise PrepareError(
        f"Inkscape 导出 {suffix.upper().lstrip('.')} 失败{hint}：" + "；".join(errors)
    )


# ---------------------------------------------------------------------------
# 第四步：Playwright 无头浏览器拍摄纯背景层
# ---------------------------------------------------------------------------


class BrowserRenderer:
    """一个浏览器实例处理整批文件，每个文件开一个独立 context。"""

    def __init__(
        self,
        channel: str = "",
        headless: bool = True,
        scale: int = 2,
        timeout_s: int = 120,
        transparent: bool = False,
    ) -> None:
        self.channel = channel
        self.headless = headless
        self.scale = max(1, int(scale))
        self.timeout_s = max(10, int(timeout_s))
        self.transparent = transparent
        self.description = ""
        self.warnings: list[str] = []
        self._pw = None
        self._browser = None

    def __enter__(self) -> "BrowserRenderer":
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            raise BrowserUnavailable(
                "未安装 playwright：请执行 `.venv\\Scripts\\pip install playwright`"
            ) from exc
        self._pw = sync_playwright().start()
        try:
            self._browser = self._launch(self._pw)
        except Exception:
            try:
                self._pw.stop()
            finally:
                self._pw = None
            raise
        return self

    def _launch(self, playwright):
        args = ["--force-color-profile=srgb"]
        channel = (self.channel or "").strip()
        if channel:
            try:
                browser = playwright.chromium.launch(
                    channel=channel, headless=self.headless, args=args
                )
                self.description = f"Playwright channel={channel}"
                return browser
            except Exception as exc:  # noqa: BLE001 - 没装 Edge 就退回自带内核
                self.warnings.append(
                    f"浏览器渠道 {channel} 启动失败（{exc}），改用 Playwright 自带 Chromium"
                )
        try:
            browser = playwright.chromium.launch(headless=self.headless, args=args)
            self.description = "Playwright Chromium"
            return browser
        except Exception as exc:  # noqa: BLE001
            raise BrowserUnavailable(
                "无法启动无头浏览器：请安装 Edge，或执行 "
                "`.venv\\Scripts\\python -m playwright install chromium`"
            ) from exc

    def render(self, svg_path: Path, width: float, height: float) -> bytes:
        width = max(1.0, float(width))
        height = max(1.0, float(height))
        viewport = {"width": max(1, math.ceil(width)), "height": max(1, math.ceil(height))}
        context = self._browser.new_context(
            viewport=viewport, device_scale_factor=self.scale
        )
        try:
            page = context.new_page()
            page.goto(
                svg_path.resolve().as_uri(),
                wait_until="load",
                timeout=self.timeout_s * 1000,
            )
            page.wait_for_timeout(120)
            page.evaluate(
                """async () => {
                    if (document.fonts && document.fonts.ready) {
                        try { await document.fonts.ready; } catch (err) {}
                    }
                    await new Promise((resolve) =>
                        requestAnimationFrame(() => requestAnimationFrame(resolve)));
                }"""
            )
            return page.screenshot(
                clip={"x": 0.0, "y": 0.0, "width": width, "height": height},
                omit_background=self.transparent,
                timeout=self.timeout_s * 1000,
            )
        except PrepareError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PrepareError(f"背景层渲染失败：{exc}") from exc
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._browser = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None
        return False


def normalize_png(
    png: bytes, expected: tuple[int, int], warnings: list[str]
) -> tuple[bytes, tuple[int, int]]:
    """校验 / 校正像素尺寸，保证 2 倍图与 viewBox 严格对应。"""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - Pillow 缺失时直接使用浏览器输出
        return png, expected
    try:
        with Image.open(io.BytesIO(png)) as image:
            image.load()
            actual = image.size
            if actual == expected:
                return png, actual
            if abs(actual[0] - expected[0]) > 2 or abs(actual[1] - expected[1]) > 2:
                warnings.append(
                    f"截图尺寸 {actual[0]}x{actual[1]} 与预期 "
                    f"{expected[0]}x{expected[1]} 相差较大，已缩放校正"
                )
            resized = image.resize(expected, Image.LANCZOS)
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG")
            return buffer.getvalue(), expected
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"PNG 校验失败（{exc}），直接使用浏览器输出")
        return png, expected


# ---------------------------------------------------------------------------
# 第五步：合成输出
# ---------------------------------------------------------------------------


def attach_background(root: etree._Element, png: bytes, viewbox: ViewBox) -> None:
    """把拍平背景作为最底层 <image> 插到动态层第一个子节点。"""
    image = etree.Element(f"{{{SVG_NS}}}image")
    image.set("id", "flattened-background")
    image.set("x", fmt_num(viewbox.min_x))
    image.set("y", fmt_num(viewbox.min_y))
    image.set("width", fmt_num(viewbox.width))
    image.set("height", fmt_num(viewbox.height))
    image.set("preserveAspectRatio", "none")
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    image.set("href", uri)
    if XLINK_NS in {value for value in root.nsmap.values() if value}:
        image.set(f"{{{XLINK_NS}}}href", uri)
    root.insert(0, image)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def serialize_svg(root: etree._Element) -> bytes:
    return etree.tostring(
        root, xml_declaration=True, encoding="utf-8", pretty_print=False
    )


# ---------------------------------------------------------------------------
# 单文件流水线
# ---------------------------------------------------------------------------


def process_file(
    source: Path,
    target: Path,
    args: argparse.Namespace,
    renderer: BrowserRenderer,
    work: Path,
    inkscape: str | None,
) -> FileResult:
    started = time.perf_counter()
    warnings: list[str] = []
    work.mkdir(parents=True, exist_ok=True)

    if source.suffix.lower() in INKSCAPE_INPUT_SUFFIXES:
        if not inkscape:
            raise InkscapeUnavailable(
                f"处理 {source.suffix.upper()} 需要 Inkscape，但未提供可执行文件路径"
            )
        source_svg = work / "converted.svg"
        convert_with_inkscape(
            source, source_svg, inkscape, args.inkscape_timeout, args.verbose
        )
    else:
        source_svg = source

    text, encoding = decode_svg_file(source_svg)
    text, stats = repair_xml_text(text)
    stats.encoding = encoding
    root = parse_svg(text)
    viewbox = parse_viewbox(root)
    guard_canvas(viewbox, args.scale)

    base_dirs: list[Path] = []
    for directory in (source_svg.parent, source.parent):
        if directory not in base_dirs:
            base_dirs.append(directory)

    backdrop = find_backdrop_images(root, viewbox)
    if args.images == "dynamic" and backdrop:
        warnings.append(
            f"检测到 {len(backdrop)} 张覆盖画板 ≥"
            f"{int(BACKDROP_COVERAGE_RATIO * 100)}% 的图片按动态层处理，"
            "拍平背景不会包含它们；若它们是背景纹理，请加 --images auto"
        )
    elif args.images == "auto" and backdrop:
        warnings.append(
            f"自动判定 {len(backdrop)} 张画板级图片留在背景层："
            + "、".join(f"{image.get('id') or 'image'}（{ratio:.0%}）" for image, ratio in backdrop)
        )

    background, dynamic, layers = split_layers(root, args.images, viewbox)
    rewrite_external_hrefs(dynamic, base_dirs, embed_images=True, warnings=warnings)
    rewrite_external_hrefs(background, base_dirs, embed_images=False, warnings=warnings)
    force_canvas(background, viewbox)
    force_canvas(dynamic, viewbox)

    background_svg = work / "background.svg"
    background_svg.write_bytes(serialize_svg(background))

    png = renderer.render(background_svg, viewbox.width, viewbox.height)
    expected = (
        max(1, round(viewbox.width * renderer.scale)),
        max(1, round(viewbox.height * renderer.scale)),
    )
    png, png_size = normalize_png(png, expected, warnings)
    (work / "background.png").write_bytes(png)

    attach_background(dynamic, png, viewbox)
    data = serialize_svg(dynamic)
    atomic_write(target, data)

    return FileResult(
        source=source,
        target=target,
        viewbox=viewbox,
        png_size=png_size,
        stats=stats,
        layers=layers,
        warnings=warnings,
        seconds=time.perf_counter() - started,
        output_bytes=len(data),
    )


# ---------------------------------------------------------------------------
# 批处理入口
# ---------------------------------------------------------------------------


def collect_inputs(items: list[str], recursive: bool) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            return
        seen.add(resolved)
        files.append(resolved)

    for item in items:
        path = Path(item).expanduser()
        if path.is_dir():
            iterator = path.rglob("*") if recursive else path.iterdir()
            for candidate in iterator:
                if not candidate.is_file() or candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                if candidate.name.lower().endswith((".fixed.svg", ".bak.svg")):
                    continue
                add(candidate)
        elif path.is_file():
            if path.suffix.lower() in SUPPORTED_SUFFIXES:
                add(path)
            else:
                print(f"! 跳过不支持的文件：{path}")
        else:
            print(f"! 路径不存在：{item}")

    return sorted(files, key=lambda item: str(item).lower())


def plan_output(
    out_dir: Path, source: Path, force: bool, taken: set[str]
) -> tuple[Path | None, str]:
    stem = source.stem
    name = f"{stem}.fixed.svg"
    index = 2
    while os.path.normcase(name) in taken:
        name = f"{stem}-{index}.fixed.svg"
        index += 1
    target = out_dir / name
    if target.exists() and not force:
        return None, f"已存在 {target.name}（加 --force 覆盖）"
    taken.add(os.path.normcase(name))
    return target, ""


def build_parser() -> argparse.ArgumentParser:
    default_output = (
        Path(_settings.template_dir)
        if _settings is not None
        else BASE_DIR / "data" / "templates"
    )
    default_scale = getattr(_settings, "poster_render_scale", 2)
    default_channel = getattr(_settings, "playwright_channel", "msedge")
    default_headless = getattr(_settings, "playwright_headless", True)

    parser = argparse.ArgumentParser(
        description="批量清洗 .eps/.ai/.svg 海报模板：修 XML、动静分离、背景拍平、输出 .fixed.svg",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "PowerShell 示例：\n"
            '  .\\.venv\\Scripts\\python.exe .\\scripts\\batch_prepare_templates.py "D:\\海报源文件"\n'
            '  .\\.venv\\Scripts\\python.exe .\\scripts\\batch_prepare_templates.py "D:\\海报源文件" '
            "--recursive --force\n"
            '  .\\.venv\\Scripts\\python.exe .\\scripts\\batch_prepare_templates.py "D:\\海报源文件" '
            "--images static\n"
        ),
    )
    parser.add_argument("inputs", nargs="+", help="EPS/AI/SVG 文件或文件夹（可多个）")
    parser.add_argument(
        "--out", default=str(default_output), help=f"输出目录（默认 {default_output}）"
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=int(default_scale),
        help=f"背景渲染倍率（默认 {default_scale}，建议 2）",
    )
    parser.add_argument(
        "--images",
        choices=("dynamic", "auto", "static"),
        default="dynamic",
        help=(
            "image 节点归属：dynamic=全部动态（默认，适合人物占位图）；"
            "auto=覆盖画板 ≥60%% 的图片算背景纹理，其余动态；static=全部留在背景层"
        ),
    )
    parser.add_argument(
        "--transparent",
        action="store_true",
        help="背景 PNG 保留透明通道（默认铺白底，避免半透明模板发灰）",
    )
    parser.add_argument("--recursive", action="store_true", help="递归处理子目录")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时目录便于排查")
    parser.add_argument("--verbose", action="store_true", help="打印命令、临时路径与完整堆栈")
    parser.add_argument(
        "--inkscape",
        default=None,
        help="Inkscape 可执行文件路径（默认自动查找，或读 INKSCAPE_PATH）",
    )
    parser.add_argument(
        "--channel",
        default=default_channel,
        help=f"Playwright 浏览器渠道（默认 {default_channel or '自带 Chromium'}）",
    )
    parser.add_argument(
        "--timeout", type=int, default=120, help="单个背景渲染超时秒数（默认 120）"
    )
    parser.add_argument(
        "--inkscape-timeout", type=int, default=300, help="单个 EPS 转换超时秒数（默认 300）"
    )
    parser.set_defaults(headless=default_headless)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)

    if not 1 <= args.scale <= 4:
        print("错误：--scale 只支持 1~4")
        return 2

    files = collect_inputs(args.inputs, args.recursive)
    if not files:
        print("没有找到可处理的 .eps / .ai / .svg 文件")
        return 1

    out_dir = Path(args.out).expanduser()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"错误：无法创建输出目录 {out_dir}：{exc}")
        return 2

    inkscape: str | None = None
    if any(item.suffix.lower() in INKSCAPE_INPUT_SUFFIXES for item in files):
        try:
            inkscape = find_inkscape(args.inkscape)
        except InkscapeUnavailable as exc:
            print(f"错误：{exc}")
            return 2

    print(f"待处理 {len(files)} 个文件，输出到 {out_dir.resolve()}")
    if inkscape:
        print(f"Inkscape：{inkscape}")

    succeeded: list[FileResult] = []
    skipped: list[tuple[Path, str]] = []
    failed: list[tuple[Path, Exception]] = []
    taken: set[str] = set()

    try:
        with BrowserRenderer(
            channel=args.channel,
            headless=args.headless,
            scale=args.scale,
            timeout_s=args.timeout,
            transparent=args.transparent,
        ) as renderer, work_directory(args.keep_temp, args.verbose) as work:
            for warning in renderer.warnings:
                print(f"! {warning}")
            print(f"背景渲染：{renderer.description}，{args.scale}x")

            for index, source in enumerate(files, start=1):
                target, reason = plan_output(out_dir, source, args.force, taken)
                if target is None:
                    skipped.append((source, reason))
                    print(f"[{index}/{len(files)}] {source.name} 跳过：{reason}")
                    continue

                file_work = work / f"tpl_{index:04d}"
                try:
                    result = process_file(
                        source, target, args, renderer, file_work, inkscape
                    )
                except Exception as exc:  # noqa: BLE001 - 单个失败不阻断批处理
                    failed.append((source, exc))
                    print(f"[{index}/{len(files)}] {source.name} 失败：{exc}")
                    if args.verbose:
                        traceback.print_exc()
                    continue

                succeeded.append(result)
                viewbox = result.viewbox
                print(
                    f"[{index}/{len(files)}] {source.name} -> {target.name}"
                    f"（{result.seconds:.1f}s）"
                )
                backdrop_note = (
                    f"（另 {result.layers.backdrop_images} 张背景图已拍平）"
                    if result.layers.backdrop_images
                    else ""
                )
                print(
                    f"      viewBox {fmt_num(viewbox.width)}x{fmt_num(viewbox.height)}"
                    f" -> PNG {result.png_size[0]}x{result.png_size[1]}"
                    f" | 修 font-family {result.stats.font_family}"
                    f"、& {result.stats.ampersand}"
                    f" | 动态 {result.layers.texts} 文本 / {result.layers.images} 图片"
                    f"{backdrop_note}"
                    f" | {mb(result.output_bytes)}"
                )
                for warning in result.warnings:
                    print(f"      ! {warning}")
    except (BrowserUnavailable, PrepareError) as exc:
        print(f"错误：{exc}")
        if args.verbose:
            traceback.print_exc()
        return 2

    print(
        f"完成：成功 {len(succeeded)}，跳过 {len(skipped)}，失败 {len(failed)}，"
        f"共 {len(files)} 个"
    )
    if succeeded:
        print(f"输出目录：{out_dir.resolve()}")
    for source, exc in failed:
        print(f"  - {source.name}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
