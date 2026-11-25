"""Seedream 5.5 工作台：入口切换、圈画松手弹框、Enter/Esc、拆层进编辑器。"""
import io
import json
from urllib.parse import urlsplit

import pytest
from PIL import Image
from playwright.sync_api import expect, sync_playwright

from app.services.svg_browser import _launch
from test_poster_workbench_browser import STATIC

PREVIEW_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200">
<image data-layout-role="bg" x="0" y="0" width="300" height="200" href="/mock.png"/>
<image id="layer-01" data-decompose-layer="1" x="150" y="20" width="60" height="50" href="/mock.png"/>
<text id="title" x="20" y="72" font-size="30" fill="#111">秋季研学营</text>
</svg>'''
SCHEMA = {'canvas': {'width': 300, 'height': 200}, 'images': [],
          'texts': [{'id': 'title', 'kind': 'text', 'text': '秋季研学营'}]}


def png_bytes() -> bytes:
    image = Image.new("RGB", (400, 300), (250, 248, 245))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def seedream(tmp_path):
    shot = png_bytes()
    calls = {"generate": [], "edit": [], "decompose": []}
    versions = [{"id": "v1", "index": 1, "url": "http://poster.test/mock.png",
                 "width": 400, "height": 300, "kind": "generate", "prompt": "p"}]
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={'width': 1550, 'height': 950})
            page.set_default_timeout(9000)
            page.set_default_navigation_timeout(12000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def handle(route):
                path = urlsplit(route.request.url).path
                if path.startswith('/static/'):
                    route.fulfill(path=STATIC / path.removeprefix('/static/'))
                elif path == '/mock.png':
                    route.fulfill(body=shot, content_type='image/png')
                elif path == '/api/v1/health':
                    route.fulfill(json={'llm': {'configured': True, 'model': 'demo'},
                                        'features': {'ai_svg': False, 'seedream': True}})
                elif path == '/api/seedream/config':
                    route.fulfill(json={'enabled': True, 'model': 'doubao-seedream-5-5-261028',
                                        'has_api_key': True, 'enable_ai_svg': False,
                                        'max_references': 10, 'sizes': ['1K', '2K']})
                elif path == '/api/ai-svg/config':
                    route.fulfill(json={'has_api_key': False, 'model': 'm', 'svg_model': 's',
                                        'provider': 'apimart', 'compatibility_mode': False})
                elif path == '/api/ai-svg/history':
                    route.fulfill(json={'items': []})
                elif path == '/api/seedream/generate':
                    calls['generate'].append(route.request.post_data_json)
                    route.fulfill(json={'project': {'id': 'p1', 'versions': versions}, 'version': versions[0]})
                elif path == '/api/seedream/edit':
                    calls['edit'].append(route.request.post_data_json)
                    index = len(versions) + 1
                    version = {"id": f"v{index}", "index": index, "url": "http://poster.test/mock.png",
                               "width": 400, "height": 300, "kind": "region", "prompt": "edit"}
                    versions.append(version)
                    route.fulfill(json={'project': {'id': 'p1', 'versions': list(versions)}, 'version': version})
                elif path == '/api/seedream/decompose':
                    calls['decompose'].append(route.request.post_data_json)
                    route.fulfill(json={
                        'project': {'id': 'p1', 'versions': versions},
                        'decomposition': {
                            'job_id': 'p1',
                            'base_url': '/api/seedream/files/p1/layers/base.png',
                            'layers': [
                                {'index': 1, 'url': '/api/seedream/files/p1/layers/layer-01.png',
                                 'text_recognition': None},
                                {'index': 2, 'url': '/api/seedream/files/p1/layers/layer-02.png',
                                 'text_recognition': {'lines': [{'text': '秋季研学营'}]}},
                            ],
                            'template': {'name': '拆层模板', 'analyzed': True,
                                         'canvas': {'width': 300, 'height': 200}},
                        },
                    })
                elif path.endswith('/export-svg'):
                    route.fulfill(json={'url': '/blank', 'width': 1200, 'height': 720})
                elif path.endswith('/preview'):
                    route.fulfill(body=PREVIEW_SVG, content_type='image/svg+xml')
                elif path.endswith('/templates'):
                    route.fulfill(json=[{'name': 'demo', 'analyzed': True}])
                elif path.endswith('/schema'):
                    route.fulfill(json=SCHEMA)
                elif path.endswith('/health'):
                    route.fulfill(json={'llm': {'configured': True}})
                else:
                    route.fulfill(json=[] if path.endswith('/assets') else {})

            page.route('http://poster.test/**', handle)
            page.goto('http://poster.test/static/poster.html')
            page.locator('.poster-stage > svg').wait_for()
            yield page, calls, tmp_path
            assert not errors
        finally:
            browser.close()


def open_workbench(page):
    page.get_by_role('button', name='Seedream 生图').click()
    expect(page.get_by_role('button', name='生成图片')).to_be_visible()


def state(page, expression):
    return page.evaluate(f'() => {{ const s = Alpine.$data(document.body); return {expression}; }}')


def test_old_ai_entry_is_hidden_and_seedream_shell_replaces_it(seedream):
    page, _, _ = seedream
    expect(page.get_by_role('button', name='Seedream 生图')).to_be_visible()
    assert state(page, 's.features.ai_svg') is False
    open_workbench(page)
    expect(page.get_by_role('button', name='生成 SVG', exact=True)).to_be_hidden()
    expect(page.get_by_role('button', name='✨ 拆图层并进编辑器')).to_be_visible()
    expect(page.get_by_label('最大图层数')).to_have_value('20')
    expect(page.get_by_label('生图尺寸')).to_have_value('2K')


def test_generate_then_marquee_popover_enter_and_escape(seedream):
    page, calls, _ = seedream
    open_workbench(page)
    page.get_by_label('生图提示词').fill('秋季研学营海报')
    page.get_by_role('button', name='生成图片').click()
    expect(page.get_by_role('button', name='版本 v1')).to_be_visible()
    assert calls['generate'][0]['size'] == '2K'
    assert calls['generate'][0]['background'] == 'opaque'

    page.keyboard.press('e')
    expect(page.get_by_role('button', name='圈画局改')).to_have_attribute('aria-pressed', 'true')
    canvas = page.locator('[x-ref="sdMask"]')
    box = canvas.bounding_box()
    start = (box['x'] + 30, box['y'] + 30)
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(box['x'] + 190, box['y'] + 120, steps=8)
    page.mouse.up()

    popover = page.get_by_label('局部修改说明')
    expect(popover).to_be_visible()
    expect(page.get_by_label('局部修改说明')).to_be_focused()
    panel = popover.bounding_box()
    assert panel['y'] >= box['y'] + 100 - 6          # 弹框落在选区下方
    assert panel['x'] >= box['x'] - 2
    assert panel['x'] + panel['width'] <= box['x'] + box['width'] + 12

    send = page.get_by_role('button', name='提交局部修改')
    expect(send).to_be_enabled()
    popover.fill('把标题改成秋季班')
    expect(send).to_be_enabled()
    send.click()                       # 必须真实点击，而不是只测 Enter
    expect(page.get_by_role('button', name='版本 v2')).to_be_visible()
    assert len(calls['edit']) == 1
    edit = calls['edit'][0]
    assert edit['instruction'] == '把标题改成秋季班'
    assert edit['mask_image'].startswith('data:image/png;base64,')
    assert 'mask' not in edit
    assert edit['base_image'].startswith('http://poster.test/mock.png')
    expect(popover).to_be_hidden()

    # Enter 仍然可用（第二条用键盘提交）
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(box['x'] + 170, box['y'] + 120, steps=6)
    page.mouse.up()
    expect(popover).to_be_visible()
    popover.fill('再把颜色改成暖橙')
    popover.press('Enter')
    page.wait_for_function('() => document.querySelectorAll(\'[aria-label^="版本 v"]\').length >= 3')
    assert calls['edit'][1]['instruction'] == '再把颜色改成暖橙'

    # Esc 清空圈选并退出圈画模式
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(box['x'] + 150, box['y'] + 110, steps=6)
    page.mouse.up()
    expect(popover).to_be_visible()
    popover.press('Escape')
    expect(popover).to_be_hidden()
    # Esc 只清空当前圈选并关闭弹框，工具保持激活；再按一次才退出圈画模式
    expect(page.get_by_role('button', name='圈画局改')).to_have_attribute('aria-pressed', 'true')
    assert page.evaluate('''() => {
        const c = document.querySelector('[x-ref="sdMask"]');
        const data = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        return !data.some((value, index) => index % 4 === 3 && value > 0);
    }''')
    page.keyboard.press('Escape')
    expect(page.get_by_role('button', name='圈画局改')).to_have_attribute('aria-pressed', 'false')
    assert state(page, 's.sd.drawMode') is False


def test_upload_external_poster_then_decompose_into_editor(seedream):
    page, calls, tmp_path = seedream
    open_workbench(page)
    uploaded = tmp_path / "poster.png"
    uploaded.write_bytes(png_bytes())
    page.get_by_label('上传已有图片').set_input_files(str(uploaded))
    expect(page.get_by_text('本地海报', exact=True)).to_be_visible()
    expect(page.get_by_text('已载入本地海报', exact=False)).to_be_visible()

    page.get_by_label('最大图层数').fill('24')
    page.get_by_role('button', name='✨ 拆图层并进编辑器').click()
    page.locator('.poster-stage #title').wait_for()
    assert calls['decompose'][0]['max_images'] == 24
    assert calls['decompose'][0]['image'].startswith('data:image/png;base64,')

    expect(page.locator('[data-canvas-tools]')).to_be_visible()
    stage = page.locator('.poster-stage')
    assert stage.locator('image[data-layout-role="bg"]').count() == 1
    assert stage.locator('[data-decompose-layer]').count() >= 1
    expect(stage.locator('#title')).to_have_text('秋季研学营')
    assert state(page, "s.mode") == 'editor'
