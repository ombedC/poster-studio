"""Playwright 无头浏览器辅助。

三件事：
1. 量出 SVG 里文字/图片在画布坐标系里的包围盒；
2. 文字超出原文本框时按比例缩小 font-size（不低于原字号 85%）；
3. 以 ``deviceScaleFactor`` 导出高清 PNG。

默认使用本机安装的 Edge（``PLAYWRIGHT_CHANNEL=msedge``），这样不必额外下载
Chromium；找不到时自动退回 Playwright 自带内核。
"""

from __future__ import annotations

import atexit
import logging
import math
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.config import settings
from app.errors import BrowserUnavailable
from app.services import fonts, svg_inline

logger = logging.getLogger(__name__)

# Windows 下浏览器可能仍持有临时 SVG 句柄，删除失败时留到进程退出再清。
_TEMP_RENDERS: list[Path] = []


def _cleanup_temp_renders() -> None:
    for path in _TEMP_RENDERS:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


atexit.register(_cleanup_temp_renders)

MEASURE_JS = r"""
(ids) => {
  const out = {};
  const svg = document.querySelector('svg');
  const rootInverse = svg.getScreenCTM().inverse();
  const ns = svg.namespaceURI;
  const bounds = (box, m) => {
    const pts = [[box.x, box.y], [box.x + box.width, box.y],
                 [box.x, box.y + box.height], [box.x + box.width, box.y + box.height]]
      .map(([x, y]) => new DOMPoint(x, y).matrixTransform(m));
    const x = Math.min(...pts.map(p => p.x)), y = Math.min(...pts.map(p => p.y));
    return {x, y, w: Math.max(...pts.map(p => p.x)) - x, h: Math.max(...pts.map(p => p.y)) - y};
  };
  const clipBoxes = (image) => {
    const canvas = [], local = [];
    const parent = image.parentElement.getScreenCTM();
    for (let owner = image; owner && owner !== svg.parentElement; owner = owner.parentElement) {
      const match = (owner.getAttribute('clip-path') || getComputedStyle(owner).clipPath || '').match(/url\(["']?[^)]*#([^"')]+)["']?\)/);
      if (!match) continue;
      const clip = document.getElementById(match[1]);
      if (!clip || clip.localName !== 'clipPath') continue;
      const probe = document.createElementNS(ns, 'g');
      probe.setAttribute('visibility', 'hidden');
      const geometry = document.createElementNS(ns, 'g');
      if (clip.hasAttribute('transform')) geometry.setAttribute('transform', clip.getAttribute('transform'));
      for (const child of clip.children) {
        const copy = child.cloneNode(true);
        copy.removeAttribute('id');
        copy.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
        geometry.append(copy);
      }
      probe.append(geometry);
      svg.append(probe);
      try {
        const box = probe.getBBox();
        const screen = owner === image ? parent : owner.getScreenCTM();
        let m = screen;
        if (clip.getAttribute('clipPathUnits') === 'objectBoundingBox') {
          const b = owner.getBBox();
          if (owner === image) {
            // A normalized image-local clip continues to fit its new image box.
            continue;
          }
          m = m.translate(b.x, b.y).scale(b.width, b.height);
        }
        if (box.width > 0 && box.height > 0) {
          canvas.push(bounds(box, rootInverse.multiply(m)));
          local.push(bounds(box, parent.inverse().multiply(m)));
        }
      } finally { probe.remove(); }
    }
    return {canvas, local};
  };
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    const style = getComputedStyle(el);
    // <text> 的直接文字（可能还有子 tspan）用 Range 只量这一段的宽度
    if (el.tagName && el.tagName.toLowerCase() === "text" && el.firstChild && el.firstChild.nodeType === 3 && el.firstChild.textContent.trim()) {
      const range = document.createRange();
      range.setStart(el.firstChild, 0);
      range.setEnd(el.firstChild, el.firstChild.textContent.length);
      const rect = range.getBoundingClientRect();
      if (rect.width > 0) {
        out[id] = {
          ...bounds({x: rect.left, y: rect.top, width: rect.width, height: rect.height}, rootInverse),
          font_size: parseFloat(style.fontSize) || 0,
          anchor: style.textAnchor || "start",
          baseline: el.getStartPositionOfChar(0).matrixTransform(rootInverse.multiply(el.getScreenCTM())).y,
        };
        continue;
      }
    }
    let box;
    try { box = el.getBBox(); } catch (err) { continue; }
    const screen = el.getScreenCTM();
    if (!screen) continue;
    const m = rootInverse.multiply(screen);
    out[id] = {
      ...bounds(box, m),
      ...(el.localName === 'image' ? {clip_boxes: clipBoxes(el)} : {}),
      ...(el.getNumberOfChars?.() ? {baseline: el.getStartPositionOfChar(0).matrixTransform(m).y} : {}),
      font_size: parseFloat(style.fontSize) || 0,
      anchor: style.textAnchor || "start",
    };
  }
  return out;
}
"""

def _launch(p):
    channel = (settings.playwright_channel or "").strip()
    if channel:
        try:
            return p.chromium.launch(channel=channel, headless=settings.playwright_headless)
        except Exception as exc:  # 没装 Edge / 渠道名不对，退回自带内核
            logger.warning("Playwright 渠道 %s 启动失败（%s），尝试自带 Chromium", channel, exc)
    try:
        return p.chromium.launch(headless=settings.playwright_headless)
    except Exception as exc:
        raise BrowserUnavailable(
            "无法启动无头浏览器：请安装 Edge，或执行 `python -m playwright install chromium`"
        ) from exc


def _inline_assets(svg_path: Path) -> Path:
    """把本地资源图片内联后写入临时文件供文件协议页面使用。

    ``file://`` 页面无法解析站内绝对路径，导出/量尺寸前必须先自包含。
    """
    try:
        raw = svg_path.read_bytes()
    except OSError:
        return svg_path
    if b"/api/seedream/files/" not in raw and b"/api/v1/posters/templates/" not in raw:
        return svg_path
    inlined = svg_inline.inline_local_images(raw)
    if inlined is raw or inlined == raw:
        return svg_path
    tmp_dir = settings.cache_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkstemp(suffix=".svg", dir=str(tmp_dir))[1])
    target.write_bytes(inlined)
    _TEMP_RENDERS.append(target)
    return target


@contextmanager
def _page(
    svg_path: Path,
    canvas: tuple[float, float],
    scale: int = 1,
    render_size: tuple[float, float] | None = None,
):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - 依赖缺失
        raise BrowserUnavailable("未安装 playwright，请执行 `pip install playwright`") from exc

    root_width, root_height = render_size or canvas
    width = max(1, math.ceil(root_width))
    height = max(1, math.ceil(root_height))
    with sync_playwright() as p:
        browser = _launch(p)
        render_path = Path(svg_path)
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=max(1, int(scale)),
            )
            page = context.new_page()
            render_path = _inline_assets(Path(svg_path))
            page.goto(render_path.resolve().as_uri(), wait_until="load", timeout=120000)
            page.evaluate(
                "([w, h]) => { const s = document.documentElement;"
                " s.setAttribute('width', w); s.setAttribute('height', h);"
                " s.style.width = w + 'px'; s.style.height = h + 'px';"
                " if (document.body) { document.body.style.margin = '0'; } }",
                [root_width, root_height],
            )
            css = fonts.font_css()
            if css:
                page.evaluate(
                    "(css) => { const style = document.createElementNS("
                    "'http://www.w3.org/2000/svg', 'style');"
                    " style.setAttribute('type', 'text/css');"
                    " style.textContent = css;"
                    " document.documentElement.appendChild(style); }",
                    css,
                )
            page.evaluate("async () => { if (document.fonts) { await document.fonts.ready; } }")
            page.wait_for_timeout(250)
            yield page
        finally:
            browser.close()
            if render_path != Path(svg_path):
                try:
                    render_path.unlink(missing_ok=True)
                except OSError:
                    pass  # 句柄仍被浏览器占用，交给 atexit 兜底


INSPECT_PATHS_JS = r"""
() => {
  const svg = document.querySelector('svg');
  if (!svg) return [];
  const out = [];
  const excluded = new Set(['defs', 'clippath', 'mask', 'pattern', 'marker', 'symbol']);
  const local = (el) => (el && el.tagName ? el.tagName.toLowerCase() : '');
  for (const el of svg.querySelectorAll('path')) {
    let blocked = false;
    let ancestor = el.parentElement;
    while (ancestor) {
      if (excluded.has(local(ancestor))) { blocked = true; break; }
      ancestor = ancestor.parentElement;
    }
    if (blocked) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
    if (!style.fill || style.fill === 'none' || style.fill === 'transparent') continue;
    let box;
    try { box = el.getBBox(); } catch (err) { continue; }
    if (!box || box.width < 1 || box.height < 1) continue;
    const m = svg.getScreenCTM()?.inverse().multiply(el.getScreenCTM());
    if (!m) continue;
    const corners = [
      [box.x, box.y], [box.x + box.width, box.y],
      [box.x, box.y + box.height], [box.x + box.width, box.y + box.height],
    ];
    const xs = [], ys = [];
    for (const [x, y] of corners) {
      xs.push(m.a * x + m.c * y + m.e);
      ys.push(m.b * x + m.d * y + m.f);
    }
    const parent = el.parentElement;
    let transformedAncestor = parent;
    while (transformedAncestor && transformedAncestor !== svg) {
      const s = getComputedStyle(transformedAncestor);
      if (s.transform && s.transform !== 'none') break;
      transformedAncestor = transformedAncestor.parentElement;
    }
    out.push({
      id: el.id || '',
      tag: 'path',
      x: Math.min(...xs),
      y: Math.min(...ys),
      w: Math.max(...xs) - Math.min(...xs),
      h: Math.max(...ys) - Math.min(...ys),
      fill: style.fill,
      fillOpacity: style.fillOpacity,
      parentId: parent ? (parent.id || '') : '',
      parentTag: local(parent),
      transformedAncestorId: transformedAncestor && transformedAncestor !== svg ? (transformedAncestor.id || '') : '',
      clipped: Boolean(el.closest('[clip-path]')),
      d: (el.getAttribute('d') || '').slice(0, 200),
    });
  }
  return out;
}
"""


def inspect_paths(
    svg_path: Path, canvas: tuple[float, float]
) -> list[dict[str, Any]]:
    """返回所有可见 `<path>` 的画布坐标包围盒与填充信息。"""
    with _page(svg_path, canvas, scale=1) as page:
        return page.evaluate(INSPECT_PATHS_JS)


def measure_svg(
    svg_path: Path, canvas: tuple[float, float], ids: list[str]
) -> dict[str, dict[str, Any]]:
    """返回 ``{id: {x, y, w, h, font_size, anchor}}``（画布坐标系）。"""
    if not ids:
        return {}
    with _page(svg_path, canvas, scale=1) as page:
        return page.evaluate(MEASURE_JS, list(ids))


INK_JS = r"""
async ({id, sizes}) => {
  const el = document.getElementById(id);
  if (!el || el.localName !== 'text' || el.children.length || !el.textContent.trim()) return [];
  const svg = el.ownerSVGElement, style = getComputedStyle(el);
  if (style.visibility !== 'visible' || style.display === 'none' || style.writingMode !== 'horizontal-tb' ||
      el.hasAttribute('textLength') || el.hasAttribute('rotate') || Number(style.opacity) === 0) return [];
  const m = svg.getScreenCTM().inverse().multiply(el.getScreenCTM());
  if (Math.abs(m.b) > 1e-6 || Math.abs(m.c) > 1e-6 || m.a <= 0 || m.d <= 0) return [];
  const ctx = document.createElementNS('http://www.w3.org/1999/xhtml', 'canvas').getContext('2d');
  ctx.textAlign = {middle:'center', end:'end'}[style.textAnchor] || 'start';
  ctx.direction = style.direction;
  if ('letterSpacing' in ctx) ctx.letterSpacing = style.letterSpacing === 'normal' ? '0px' : style.letterSpacing;
  if ('wordSpacing' in ctx) ctx.wordSpacing = style.wordSpacing === 'normal' ? '0px' : style.wordSpacing;
  const text = style.whiteSpace === 'pre' || el.getAttribute('xml:space') === 'preserve'
    ? el.textContent : el.textContent.trim().replace(/\s+/g, ' ');
  const anchor = (name) => el[name].baseVal.numberOfItems ? el[name].baseVal.getItem(0).value : 0;
  if (['x','y','dx','dy'].some(name => el[name].baseVal.numberOfItems > 1)) return [];
  const x = anchor('x') + anchor('dx'), y = anchor('y') + anchor('dy');
  const out = [];
  for (const size of sizes ?? [parseFloat(style.fontSize)]) {
    const font = `${style.fontStyle} ${style.fontWeight} ${size}px ${style.fontFamily}`;
    await document.fonts.load(font, text);
    ctx.font = font;
    const ink = ctx.measureText(text);
    out.push({size, weight: +style.fontWeight, family: style.fontFamily, anchor: style.textAnchor, scale: [m.a, m.d],
              box: [(x-ink.actualBoundingBoxLeft)*m.a+m.e, (y-ink.actualBoundingBoxAscent)*m.d+m.f,
                    (ink.actualBoundingBoxLeft+ink.actualBoundingBoxRight)*m.a,
                    (ink.actualBoundingBoxAscent+ink.actualBoundingBoxDescent)*m.d]});
  }
  return out;
}
"""


@contextmanager
def text_ink_probe(svg_path: Path, canvas: tuple[float, float]):
    """Measure ink bounds, excluding advance and line-box space."""
    with _page(svg_path, canvas) as page:
        yield lambda id, sizes=None: page.evaluate(INK_JS, {"id": id, "sizes": sizes})


def screenshot_svg(
    svg_path: Path, canvas: tuple[float, float], out_path: Path, scale: int = 2
) -> tuple[int, int]:
    """以 ``scale`` 倍渲染 SVG，返回 PNG 的像素尺寸。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with _page(svg_path, canvas, scale=scale) as page:
        svg = page.locator("svg").first
        svg.evaluate("el => el.style.setProperty('overflow', 'hidden', 'important')")
        svg.screenshot(path=str(out_path), omit_background=False, timeout=180000)
    width = int(round(canvas[0] * scale))
    height = int(round(canvas[1] * scale))
    return width, height


def fit_and_export(
    svg_path: Path,
    canvas: tuple[float, float],
    requests: dict[str, dict[str, float]],
    out_png: Path | None = None,
    scale: int = 2,
) -> dict[str, dict[str, Any]]:
    """在浏览器里缩小超宽文字；可选地同时导出 PNG。

    ``requests``: ``{element_id: {"max_width": px, "font_size": px, "min_ratio": 0.85}}``
    """
    with _page(svg_path, canvas, scale=scale) as page:
        result = {}
        if requests:
            script = (settings.static_dir / "poster-font-fit.js").read_text(encoding="utf-8")
            page.evaluate(f"() => {{\n{script}\n}}")
            result = page.evaluate("requests => fitPosterText(document.querySelector('svg'), requests)", requests)
        if out_png is not None:
            out_png.parent.mkdir(parents=True, exist_ok=True)
            svg = page.locator("svg").first
            svg.evaluate("el => el.style.setProperty('overflow', 'hidden', 'important')")
            svg.screenshot(path=str(out_png), omit_background=False, timeout=180000)
        return result


def thumbnail_svg(
    svg_path: Path,
    canvas: tuple[float, float],
    out_path: Path,
    width: int = 320,
) -> tuple[int, int]:
    """把整张 SVG 以 ``width`` 像素宽渲染为 JPEG 缩略图。"""
    ratio = canvas[1] / max(1.0, canvas[0])
    height = max(1, int(round(width * ratio)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with _page(
        svg_path, canvas, scale=1, render_size=(width, height)
    ) as page:
        page.locator("svg").first.screenshot(
            path=str(out_path),
            type="jpeg",
            quality=86,
            omit_background=False,
            timeout=180000,
        )
    return width, height
