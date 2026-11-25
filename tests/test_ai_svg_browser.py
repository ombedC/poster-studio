"""Browser coverage with mocked local AI SVG endpoints."""
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from app.services.svg_browser import _launch


STATIC = Path(__file__).resolve().parents[1] / 'static'
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 360"><text id="title" x="70" y="100" font-size="30">Poster title</text></svg>'
SCHEMA = {'canvas': {'width': 600, 'height': 360}, 'images': [], 'texts': [
    {'id': 'title', 'kind': 'text', 'text': 'Poster title'},
]}


def test_ai_svg_mode_preserves_editor_and_imports_generation():
    config = {'base_url': 'https://example.test/v1', 'model': 'gpt-image-2.6-flare',
              'svg_model': 'gpt-5.4-mini', 'provider': 'apimart', 'compatibility_mode': False, 'has_api_key': True}
    item = {'id': 'sample', 'status': 'completed', 'view_box': '0 0 600 360', 'path_count': 0, 'text_count': 1,
            'byte_size': len(SVG), 'preview_url': '/api/ai-svg/history/sample/preview',
            'download_url': '/api/ai-svg/history/sample/download', 'request': {'prompt': 'Draft'}, 'usage': {'total_tokens': 12}}
    requests, exports = [], []
    history_reads = 0
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={'width': 1550, 'height': 950})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def handle(route):
                nonlocal history_reads
                path = urlsplit(route.request.url).path
                if path.startswith('/static/'):
                    route.fulfill(path=STATIC / path.removeprefix('/static/'))
                    return
                if path == '/api/v1/health':
                    route.fulfill(json={'llm': {'configured': True, 'model': 'demo'},
                                        'features': {'ai_svg': True, 'seedream': False}})
                elif path == '/api/seedream/config':
                    route.fulfill(json={'enabled': False, 'model': 'doubao-seedream-5-5-261028',
                                        'enable_ai_svg': True, 'max_references': 10})
                elif path == '/api/ai-svg/config':
                    if route.request.method == 'PUT':
                        config.update(route.request.post_data_json)
                    route.fulfill(json=config)
                elif path == '/api/ai-svg/history':
                    history_reads += 1
                    history_item = {**item, 'status': 'processing', 'preview_url': None}
                    route.fulfill(json={'items': [history_item if history_reads < 3 else item]})
                elif path == '/api/ai-svg/generate':
                    requests.append(route.request.post_data_json)
                    route.fulfill(status=202, json={'id': 'sample', 'status': 'queued'})
                elif path == '/api/ai-svg/jobs/sample':
                    route.fulfill(json=item)
                elif path == item['preview_url'] or path == '/api/v1/posters/preview':
                    route.fulfill(body=SVG, content_type='image/svg+xml')
                elif path.endswith('/sample/import'):
                    route.fulfill(json={'name': 'generated', 'analyzed': True, 'warnings': []})
                elif path == '/api/v1/posters/export-svg':
                    exports.append(route.request.post_data_json['svg'])
                    route.fulfill(json={'url': '/blank', 'width': 1200, 'height': 720})
                elif path.endswith('/templates'):
                    route.fulfill(json=[{'name': 'demo', 'analyzed': True}])
                elif path.endswith('/schema'):
                    route.fulfill(json=SCHEMA)
                elif path.endswith('/assets'):
                    route.fulfill(json=[])
                else:
                    route.fulfill(json={})

            page.route('http://poster.test/**', handle)
            page.goto('http://poster.test/static/poster.html')
            title = page.locator('.poster-stage #title')
            title.wait_for()
            title.dblclick()
            page.locator('.editing-proxy').fill('Edited before switching')
            page.locator('.editing-proxy').press('Enter')
            page.keyboard.press('Control+1')
            page.get_by_role('button', name='放大', exact=True).click()
            page.get_by_role('button', name='AI 矢量生图').click()
            expect(title).to_be_hidden()
            page.keyboard.press('Control+z')
            page.get_by_role('button', name='SVG 模板编辑器').click()
            expect(title).to_have_text('Edited before switching')
            expect(page.locator('.canvas-viewport output')).to_have_text('120%')
            page.get_by_role('button', name='API 设置').click()
            expect(page.get_by_label('默认模型')).to_have_value('gpt-image-2.6-flare')
            expect(page.get_by_label('API Key')).to_have_attribute('type', 'password')
            page.get_by_role('button', name='显示', exact=True).click()
            expect(page.get_by_label('API Key')).to_have_attribute('type', 'text')
            page.get_by_label('默认模型').fill('custom-image-model')
            page.get_by_role('button', name='保存设置').click()
            expect(page.get_by_role('dialog')).to_be_hidden()
            page.get_by_role('button', name='AI 矢量生图').click()
            page.get_by_label('提示词', exact=True).fill('设计一张贴纸')
            page.get_by_label('透明底图标/贴纸').check()
            page.get_by_role('button', name='生成 SVG', exact=True).click()
            expect(page.get_by_alt_text('生成的 SVG')).to_have_attribute('src', item['preview_url'], timeout=12000)
            assert requests[0]['background'] == 'transparent'
            assert requests[0]['model'] == 'custom-image-model'
            assert '居中' in requests[0]['prompt']
            page.get_by_role('button', name='一键载入编辑器').click()
            expect(title).to_have_text('Poster title')
            expect(page.get_by_role('button', name='SVG 模板编辑器')).to_have_attribute('aria-pressed', 'true')
            title.dblclick()
            page.locator('.editing-proxy').fill('Edited after import')
            page.locator('.editing-proxy').press('Enter')
            page.get_by_role('button', name='导出高清 PNG').click()
            expect(page.locator('#dynamic-island-root .di-shell[data-island-state="success"]')).to_be_visible()
            assert 'Edited after import' in exports[0]
            assert not errors
        finally:
            browser.close()
