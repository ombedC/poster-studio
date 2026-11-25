"""底部二维码引导语必须保持原 SVG 坐标，不能因字体/合并改写挤进二维码框。"""
import pytest
from playwright.sync_api import sync_playwright

from app.services import template_analyzer as analyzer
from app.services import template_renderer as renderer
from app.services.svg_browser import _launch

QR_LEFT = 744
SOURCE = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 300">
  <rect id="qr" x="{QR_LEFT}" y="40" width="180" height="180" fill="#222"/>
  <text transform="translate(87.72 190.42)" font-size="68.8" fill="#fff">
    <tspan letter-spacing="0.05em">长按识别二维</tspan><tspan x="426.82" y="0">码</tspan>
  </text>
</svg>'''.encode("utf-8")


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("qrtext")
    schema = analyzer.analyze_template(SOURCE, name="二维码引导语", use_llm=False, use_browser=False, force=True)
    svg, _warnings = renderer.build_svg(schema, {}, {}, {})
    path = tmp / "poster.svg"
    path.write_bytes(svg if isinstance(svg, bytes) else svg.encode("utf-8"))
    return path, schema


def test_qr_tip_keeps_original_origin_and_never_overlaps_qr(rendered):
    path, schema = rendered
    entry = next(item for item in schema["texts"] if "长按" in item["text"])
    # 归一化后整个字段仍从原始起点开始（87.72），不会被“码”的绝对 x 拽走
    assert float(entry["x"]) < 120
    assert entry["width"] < QR_LEFT - 87.72
    # 合并逻辑不得把整句搬到 426.82 这个末字起点
    assert '>长按识别二维码<' not in path.read_text(encoding="utf-8") or 'x="426.82"' not in path.read_text(encoding="utf-8")

    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={'width': 1000, 'height': 300})
            page.goto(path.resolve().as_uri(), wait_until="load")
            page.wait_for_timeout(250)
            boxes = page.evaluate('''() => {
                const svg = document.querySelector('svg');
                const qr = document.querySelector('#qr') || [...svg.querySelectorAll('rect')][0];
                const texts = [...svg.querySelectorAll('text')];
                return {
                    right: Math.max(...texts.map(t => t.getBoundingClientRect().right)),
                    left: Math.min(...texts.map(t => t.getBoundingClientRect().left)),
                    qrLeft: qr.getBoundingClientRect().left,
                    hasAbsoluteCode: texts.some(t => Math.abs(t.getBoundingClientRect().left - 426.82) < 4
                        && t.textContent.trim().length > 1),
                };
            }''')
        finally:
            browser.close()
    assert boxes["right"] < boxes["qrLeft"] - 20, boxes
    assert boxes["left"] < 140, boxes
    assert boxes["hasAbsoluteCode"] is False, boxes
