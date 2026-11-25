"""头像替换链路回归：上传 / 历史素材秒选后，<image> 必须贴合圆框 clipPath。

背景：Illustrator 导出的头像 <image> 常带 `width=734 height=612 transform="translate() scale()"`，
并且在头像外面再套一层大矩形 clip。替换图片时如果只改 href（或取错 clip 层），
新图会按大框铺满，圆框里只剩放大的五官。
"""
from __future__ import annotations

import base64
import contextlib
import io
import re
from urllib.parse import urlsplit

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from app.config import settings
from app.services.svg_browser import _launch
from test_poster_workbench_browser import STATIC

FRAME = {"x": 123.04, "y": 856.06, "width": 308.04, "height": 308.04}
IMAGE_NODE = (
    '<image id="image-004" width="734" height="612" '
    'transform="translate(-19.99 718.54) scale(0.91)" xlink:href="data:image/png;base64,{payload}"/>'
)
CIRCLE_CLIP = '<clipPath id="avatar-clip"><circle cx="277.06" cy="1010.08" r="154.02"/></clipPath>'
OUTER_CLIP = '<clipPath id="page-clip"><rect x="0" y="0" width="1242" height="2208"/></clipPath>'


def build_svg(payload: str, *, nested: bool) -> str:
    image = IMAGE_NODE.format(payload=payload)
    if nested:
        body = (
            '<g clip-path="url(#page-clip)">'
            '<g clip-path="url(#avatar-clip)">' + image + "</g></g>"
        )
        defs = CIRCLE_CLIP + OUTER_CLIP
    else:
        body = '<g clip-path="url(#avatar-clip)">' + image + "</g>"
        defs = CIRCLE_CLIP
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'viewBox="0 0 1242 2208" width="1242" height="2208">'
        '<rect width="1242" height="2208" fill="#f3efe6"/>'
        f"<defs>{defs}</defs>{body}</svg>"
    )


SCHEMA = {
    "version": 15,
    "template_id": "avatar-fixture",
    "name": "avatar-fixture",
    "svg_path": "avatar-fixture.svg",
    "canvas": {"width": 1242, "height": 2208},
    "texts": [],
    "images": [{
        "id": "image-004",
        "key": "portrait_1",
        "field": "portrait_frame",
        "slot": 0,
        "x": FRAME["x"], "y": FRAME["y"], "width": FRAME["width"], "height": FRAME["height"],
        "frame": dict(FRAME),
        "local_frame": dict(FRAME),
        "mode": "framed_circle",
    }],
}


def png_bytes(width: int = 2000, height: int = 2600) -> bytes:
    image = Image.new("RGB", (width, height), (230, 222, 210))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def read_image_tag(svg: str, image_id: str = "image-004") -> dict[str, str]:
    match = re.search(r'<image[^>]*id="' + re.escape(image_id) + r'"[^>]*>', svg)
    assert match, svg[:400]
    return dict(re.findall(r'([\w:-]+)="([^"]*)"', match.group(0)))


# ---------------------------------------------------------------------------
# 后端：预览套版时按 local_frame 写入 x/y/width/height，并清掉旧 transform
# ---------------------------------------------------------------------------
def test_preview_normalizes_avatar_geometry(client, monkeypatch):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "avatar-fixture.svg").write_text(
        build_svg(base64.b64encode(png_bytes(120, 160)).decode(), nested=True),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.api.posters._ensure_schema", lambda name: SCHEMA)

    response = client.post(
        "/api/v1/posters/preview",
        json={"template_name": "avatar-fixture", "data": {},
              "images": {"portrait_1": data_url(png_bytes())}},
    )
    assert response.status_code == 200, response.text
    attrs = read_image_tag(response.text)
    assert "transform" not in attrs
    assert float(attrs["x"]) == pytest.approx(FRAME["x"], abs=0.5)
    assert float(attrs["y"]) == pytest.approx(FRAME["y"], abs=0.5)
    assert float(attrs["width"]) == pytest.approx(FRAME["width"], abs=0.5)
    assert float(attrs["height"]) == pytest.approx(FRAME["height"], abs=0.5)
    assert attrs["preserveAspectRatio"] == "xMidYMid slice"


# ---------------------------------------------------------------------------
# 前端：上传 / 素材库秒选后，DOM 里的 <image> 与圆框一致
# ---------------------------------------------------------------------------
PROBE = """() => {
    const d = Alpine.$data(document.body);
    const svg = document.querySelector('.poster-stage svg');
    const slot = d.imageSlots().find(s => s.kind === 'teacher');
    const image = svg.getElementById(slot.image_id || slot.id);
    const m = image.getScreenCTM();
    const box = image.getBBox();
    const pts = [[box.x, box.y], [box.x + box.width, box.y],
                 [box.x, box.y + box.height], [box.x + box.width, box.y + box.height]]
        .map(([x, y]) => new DOMPoint(x, y).matrixTransform(m));
    const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
    return {
        x: image.getAttribute('x'), y: image.getAttribute('y'),
        width: image.getAttribute('width'), height: image.getAttribute('height'),
        transform: image.getAttribute('transform'),
        par: image.getAttribute('preserveAspectRatio'),
        renderedW: Math.max(...xs) - Math.min(...xs),
        renderedH: Math.max(...ys) - Math.min(...ys),
        frame: slot.frame,
    };
}"""


@contextlib.contextmanager
def avatar_page(*, nested: bool):
    """打开只含一个头像坑位的工作台，上传/素材接口都走假后端。"""
    state: dict = {"assets": [], "uploads": 0}
    svg = build_svg(base64.b64encode(png_bytes(60, 80)).decode(), nested=nested)

    def handle(route):
        path = urlsplit(route.request.url).path
        if path.startswith('/static/'):
            route.fulfill(path=STATIC / path.removeprefix('/static/'))
        elif path == '/api/v1/health':
            route.fulfill(json={'llm': {'configured': False},
                                'features': {'ai_svg': False, 'seedream': False}})
        elif path.endswith('/templates'):
            route.fulfill(json=[{'name': 'avatar-fixture', 'display_name': '头像模板',
                                 'analyzed': True, 'thumbnail_url': '',
                                 'scope': 'public', 'owner_id': '', 'team_id': '', 'owned': False}])
        elif path.endswith('/schema'):
            route.fulfill(json=SCHEMA)
        elif path.endswith('/preview'):
            route.fulfill(body=svg, content_type='image/svg+xml')
        elif path.endswith('/assets/upload'):
            state['uploads'] += 1
            route.fulfill(json={'asset': {'id': 'asset-new', 'kind': 'teacher', 'label': 'portrait.png',
                                          'url': 'http://poster.test/uploaded.png',
                                          'thumbnail_url': 'http://poster.test/uploaded.png'},
                                'warnings': []})
        elif path.endswith('/assets'):
            route.fulfill(json=state['assets'])
        elif path == '/uploaded.png':
            route.fulfill(body=png_bytes(2000, 2600), content_type='image/png')
        elif path.endswith('/ai-svg/history'):
            route.fulfill(json={'items': []})
        elif path.endswith('/ai-svg/config'):
            route.fulfill(json={'has_api_key': False, 'model': 'm', 'svg_model': 's', 'provider': 'p'})
        else:
            route.fulfill(json={})

    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_context(viewport={'width': 1550, 'height': 950}).new_page()
            page.set_default_timeout(20000)
            errors: list[str] = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('http://poster.test/**', handle)
            page.goto('http://poster.test/static/poster.html')
            page.wait_for_load_state('load')
            page.wait_for_selector('.poster-stage svg image#image-004')
            yield page, state, errors
        finally:
            browser.close()


@pytest.mark.parametrize("nested", [False, True], ids=["single-clip", "nested-clip"])
def test_upload_replaces_avatar_without_zoom(nested, tmp_path):
    path = tmp_path / "portrait-2000x2600.png"
    path.write_bytes(png_bytes(2000, 2600))

    with avatar_page(nested=nested) as (page, state, errors):
        before = page.evaluate(PROBE)
        assert before["transform"], "模板应带 Illustrator matrix，作为回归前提"
        assert float(before["width"]) == pytest.approx(734)

        page.locator("input[type=file][accept='image/*']").first.set_input_files(str(path))
        page.wait_for_function("() => Alpine.$data(document.body).uploading.portrait_1 === false")
        page.wait_for_timeout(300)

        after = page.evaluate(PROBE)
        assert state["uploads"] == 1
        assert after["transform"] is None
        assert float(after["width"]) == pytest.approx(FRAME["width"], abs=0.5)
        assert float(after["height"]) == pytest.approx(FRAME["height"], abs=0.5)
        assert float(after["x"]) == pytest.approx(FRAME["x"], abs=0.5)
        assert float(after["y"]) == pytest.approx(FRAME["y"], abs=0.5)
        assert after["par"] == "xMidYMid slice"
        # 嵌套大矩形 clip 时，绝不能把图片撑成外层矩形
        assert float(after["width"]) < 1000
        assert after["renderedW"] == pytest.approx(after["renderedH"], rel=0.01)
        assert after["renderedW"] <= float(FRAME["width"]) * 1.1
        assert not errors


def test_history_asset_click_also_normalizes():
    with avatar_page(nested=True) as (page, state, errors):
        state["assets"] = [{'id': 'asset-old', 'kind': 'teacher', 'label': '旧头像',
                            'url': 'http://poster.test/uploaded.png',
                            'thumbnail_url': 'http://poster.test/uploaded.png'}]

        page.evaluate("() => Alpine.$data(document.body).loadAssets('teacher')")
        page.wait_for_selector("img[src='http://poster.test/uploaded.png']")
        page.locator("img[src='http://poster.test/uploaded.png']").first.click()
        page.wait_for_timeout(300)

        after = page.evaluate(PROBE)
        assert after["transform"] is None
        assert float(after["width"]) == pytest.approx(FRAME["width"], abs=0.5)
        assert after["renderedW"] == pytest.approx(after["renderedH"], rel=0.01)
        assert not errors
