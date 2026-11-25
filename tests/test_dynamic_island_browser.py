"""灵动岛：展开、悬停暂停、后台提醒与导出洁净度。"""
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest
from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright

from app.services.svg_browser import _launch, screenshot_svg
from test_poster_canvas_browser import SCHEMA, SVG
from test_poster_workbench_browser import STATIC

AI_ITEM = {
    'id': 'job1', 'status': 'completed', 'view_box': '0 0 600 360', 'path_count': 0,
    'text_count': 1, 'byte_size': len(SVG), 'preview_url': '/api/ai-svg/history/job1/preview',
    'download_url': '/api/ai-svg/history/job1/download', 'request': {'prompt': 'p'},
}
CONFIG = {'base_url': 'https://example.test/v1', 'model': 'm', 'svg_model': 's',
          'provider': 'apimart', 'compatibility_mode': False, 'has_api_key': True}


@pytest.fixture
def island():
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={'width': 1550, 'height': 950})
            page.set_default_timeout(8000)
            page.set_default_navigation_timeout(12000)
            errors, exports = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def handle(route):
                path = urlsplit(route.request.url).path
                if path.startswith('/static/'):
                    route.fulfill(path=STATIC / path.removeprefix('/static/'))
                elif path == '/api/ai-svg/config':
                    route.fulfill(json=CONFIG)
                elif path == '/api/ai-svg/history':
                    route.fulfill(json={'items': [AI_ITEM]})
                elif path == '/api/ai-svg/generate':
                    route.fulfill(status=202, json={'id': 'job1', 'status': 'completed', 'item': AI_ITEM})
                elif path.endswith('/history/job1/import'):
                    route.fulfill(json={'name': 'generated', 'analyzed': True, 'warnings': []})
                elif path.endswith('/job1/preview') or path.endswith('/job1/download'):
                    route.fulfill(body=SVG, content_type='image/svg+xml')
                elif path.endswith('/export-svg'):
                    exports.append(route.request.post_data_json)
                    route.fulfill(json={'url': '/blank', 'width': 1200, 'height': 720})
                elif path.endswith('/preview'):
                    route.fulfill(body=SVG, content_type='image/svg+xml')
                elif path.endswith('/templates'):
                    route.fulfill(json=[{'name': 'demo', 'analyzed': True}])
                elif path.endswith('/schema'):
                    route.fulfill(json=SCHEMA)
                elif path.endswith('/health'):
                    route.fulfill(json={'llm': {'configured': True, 'model': 'demo'},
                                        'features': {'ai_svg': True, 'seedream': False}})
                elif path == '/api/seedream/config':
                    route.fulfill(json={'enabled': False, 'model': 'doubao-seedream-5-5-261028',
                                        'enable_ai_svg': True, 'max_references': 10})
                else:
                    route.fulfill(json=[] if path.endswith('/assets') else {})

            page.route('http://poster.test/**', handle)
            page.goto('http://poster.test/static/poster.html')
            page.locator('.poster-stage > svg').wait_for()
            yield page, exports
            assert not errors
        finally:
            browser.close()


SHELL = '#dynamic-island-root .di-shell'
ROOT = '#dynamic-island-root'


def state(page):
    return page.evaluate('''() => {
        const root = document.querySelector('#dynamic-island-root');
        const shell = root && root.querySelector('.di-shell');
        const expanded = root && root.querySelector('.di-expanded');
        if (!root || !shell) return null;
        const cs = getComputedStyle(expanded || shell);
        return {hidden: root.hidden, state: shell.dataset.islandState,
                count: shell.dataset.islandCount, radius: getComputedStyle(shell).borderTopLeftRadius,
                overflow: getComputedStyle(shell).overflow + '/' + (expanded ? getComputedStyle(expanded).overflow : ''),
                fits: expanded ? expanded.scrollWidth <= expanded.clientWidth + 1 &&
                                 expanded.scrollHeight <= expanded.clientHeight + 1 : true,
                text: root.innerText};
    }''')


def test_island_runs_ai_task_and_expands_with_actions(island):
    page, _ = island
    page.get_by_role('button', name='AI 矢量生图').click()
    page.get_by_label('提示词', exact=True).fill('生成一张海报')
    page.get_by_role('button', name='生成 SVG', exact=True).click()
    page.wait_for_selector(f'{SHELL}[data-island-state="success"]')
    before = state(page)
    assert before['hidden'] is False and before['count'] == '1'
    assert '已完成' in before['text']

    page.locator(SHELL).hover()
    page.wait_for_function("getComputedStyle(document.querySelector('#dynamic-island-root .di-shell')).borderTopLeftRadius === '24px'")
    opened = state(page)
    assert opened['radius'] == '24px'
    assert opened['overflow'] == 'hidden/hidden'
    assert opened['fits'] is True
    panel = page.locator('#dynamic-island-root')
    expect(panel.get_by_role('button', name='一键载入编辑器')).to_be_visible()
    expect(panel.get_by_role('button', name='查看结果')).to_be_visible()
    expect(panel.get_by_role('button', name='系统通知开关')).to_be_visible()
    expect(panel.get_by_role('button', name='提示音开关')).to_be_visible()

    panel.get_by_role('button', name='一键载入编辑器').click()
    page.wait_for_function("!document.querySelector('#dynamic-island-root [data-task-id=\"ai-generate\"]')")


def test_success_holds_while_hovered_then_auto_hides(island):
    page, _ = island
    page.get_by_role('button', name='导出高清 PNG').click()
    page.wait_for_selector(f'{SHELL}[data-island-state="success"]')
    page.locator(SHELL).hover()
    page.wait_for_timeout(4200)
    held = state(page)
    assert held['hidden'] is False and held['state'] == 'success'
    page.mouse.move(40, 500)
    page.wait_for_function("document.querySelector('#dynamic-island-root').hidden === true", timeout=6000)


def test_background_completion_updates_title_and_restores(island):
    page, _ = island
    original = page.evaluate('() => document.title')
    icon = page.evaluate('() => document.querySelector("link[rel=icon]")?.getAttribute("href")')
    page.evaluate('''() => {
        window.__notes = [];
        window.__tones = 0;
        class FakeNotification {
            static permission = 'granted';
            static requestPermission() { return Promise.resolve('granted'); }
            constructor(title, options) { window.__notes.push([title, options]); this.onclick = null; }
            close() {}
        }
        window.Notification = FakeNotification;
        window.AudioContext = class {
            constructor() { this.currentTime = 0; this.state = 'running'; this.destination = {}; }
            resume() { return Promise.resolve(); }
            createOscillator() { window.__tones += 1; return { type: '', frequency: { value: 0 }, connect: (n) => n, start() {}, stop() {} }; }
            createGain() { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect: (n) => n }; }
        };
    }''')
    page.get_by_role('button', name='导出高清 PNG').click()
    page.wait_for_selector(f'{SHELL}[data-island-state="success"]')
    page.locator(SHELL).hover()
    page.get_by_role('button', name='系统通知开关').click()
    assert page.evaluate('() => DynamicIsland.notify') is True

    page.evaluate('''() => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
        document.dispatchEvent(new Event('visibilitychange'));
    }''')
    page.get_by_role('button', name='导出高清 PNG').click()
    page.wait_for_function("document.title.startsWith('✅')")
    title = page.evaluate('() => document.title')
    assert '导出高清 PNG已完成' in title and title.endswith(original)
    page.wait_for_function('() => window.__notes.length === 1', timeout=4000)
    note = page.evaluate('() => window.__notes[0]')
    assert '导出高清 PNG' in note[0]
    assert page.evaluate('() => window.__tones >= 2') is True
    assert page.evaluate('() => document.querySelector("link[rel=icon]").href.startsWith("data:image/png")') is True

    page.evaluate('''() => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
        document.dispatchEvent(new Event('visibilitychange'));
    }''')
    assert page.evaluate('() => document.title') == original
    assert page.evaluate('() => document.querySelector("link[rel=icon]")?.getAttribute("href")') == icon


def test_favicon_fallback_round_trip(island):
    page, _ = island
    created = page.evaluate('''async () => {
        document.querySelectorAll('link[rel="icon"], link[rel="shortcut icon"]').forEach(link => link.remove());
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
        DynamicIsland.start('probe', {title: '回退测试'});
        DynamicIsland.succeed('probe', {detail: 'done'});
        await new Promise(resolve => setTimeout(resolve, 200));
        const links = document.querySelectorAll('link[rel="icon"]');
        return {count: links.length, href: links[0]?.href || ''};
    }''')
    assert created['count'] == 1 and created['href'].startswith('data:image/png')
    page.evaluate('''() => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
        document.dispatchEvent(new Event('visibilitychange'));
    }''')
    assert page.evaluate('() => document.querySelectorAll("link[rel=icon]").length') == 0


def test_export_png_unaffected_by_island(island, tmp_path):
    page, exports = island
    page.get_by_role('button', name='导出高清 PNG').click()
    page.wait_for_selector(f'{SHELL}[data-island-state="success"]')
    page.locator(SHELL).hover()
    page.wait_for_timeout(350)
    assert state(page)['hidden'] is False
    page.evaluate("() => DynamicIsland.dismiss('export')")
    page.get_by_role('button', name='导出高清 PNG').click()
    page.wait_for_selector(f'{SHELL}[data-island-state="success"]')
    assert len(exports) == 2
    first, second = exports[0]['svg'], exports[1]['svg']
    assert first == second
    for markup in (first, second):
        assert 'dynamic-island' not in markup and 'di-shell' not in markup and 'di-expanded' not in markup
    images = []
    for index, payload in enumerate(exports):
        svg_path, png_path = tmp_path / f'{index}.svg', tmp_path / f'{index}.png'
        svg_path.write_text(payload['svg'], encoding='utf-8')
        assert payload['scale'] == 2
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(screenshot_svg, svg_path, (600, 360), png_path, 2).result(timeout=45) == (1200, 720)
        with Image.open(png_path) as image:
            assert image.size == (1200, 720)
            images.append(image.convert('RGB'))
    assert ImageChops.difference(*images).getbbox() is None
