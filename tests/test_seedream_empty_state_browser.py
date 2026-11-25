"""Seedream 工作台空状态与配置错误：不可静默空白，需给出可操作提示。"""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from app.services.svg_browser import _launch
from test_poster_workbench_browser import STATIC
from test_seedream_browser import PREVIEW_SVG, SCHEMA


def _boot(config_mode):
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={'width': 1550, 'height': 950})
            page.set_default_timeout(8000)
            page.set_default_navigation_timeout(12000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def handle(route):
                path = urlsplit(route.request.url).path
                if path.startswith('/static/'):
                    route.fulfill(path=STATIC / path.removeprefix('/static/'))
                elif path == '/api/v1/health':
                    route.fulfill(json={'llm': {'configured': True, 'model': 'demo'},
                                        'features': {'ai_svg': False, 'seedream': True}})
                elif path == '/api/seedream/config':
                    if config_mode == '404':
                        route.fulfill(status=404, json={'detail': 'Not Found'})
                    else:
                        route.fulfill(json={'enabled': True, 'model': 'm', 'max_references': 10,
                                            'enable_ai_svg': False, 'llm_configured': True,
                                            'channels': [], 'intents': [], 'styles': [],
                                            'prompts_dir': '/tmp/prompts', 'warnings': []})
                elif path == '/api/ai-svg/config':
                    route.fulfill(json={'has_api_key': False, 'model': 'm', 'svg_model': 's', 'provider': 'apimart'})
                elif path == '/api/ai-svg/history':
                    route.fulfill(json={'items': []})
                elif path.endswith('/preview'):
                    route.fulfill(body=PREVIEW_SVG, content_type='image/svg+xml')
                elif path.endswith('/templates'):
                    route.fulfill(json=[{'name': 'demo', 'analyzed': True}])
                elif path.endswith('/schema'):
                    route.fulfill(json=SCHEMA)
                else:
                    route.fulfill(json=[] if path.endswith('/assets') else {})

            page.route('http://poster.test/**', handle)
            page.goto('http://poster.test/static/poster.html')
            page.locator('.poster-stage > svg').wait_for()
            page.get_by_role('button', name='Seedream 生图').click()
            yield page
            assert not errors
        finally:
            browser.close()


@pytest.fixture
def broken_backend():
    yield from _boot('404')


@pytest.fixture
def empty_catalog():
    yield from _boot('empty')


def test_missing_backend_route_shows_actionable_banner(broken_backend):
    page = broken_backend
    banner = page.get_by_role('alert')
    expect(banner).to_be_visible()
    expect(banner).to_contain_text('404')
    expect(banner).to_contain_text('重启后端')
    expect(page.get_by_role('button', name='重试')).to_be_visible()
    # 空列表不再是无声空白
    expect(page.get_by_text('没有读到渠道预设')).to_be_visible()
    expect(page.get_by_text('没有读到产出意图')).to_be_visible()
    # 未配置时帮写按钮禁用且给出原因
    expect(page.get_by_role('button', name='AI 帮写提示词')).to_be_disabled()


def test_empty_state_never_shows_applied_row(empty_catalog):
    page = empty_catalog
    expect(page.get_by_role('alert')).to_be_hidden()
    row = page.get_by_text('已组合：', exact=False)
    expect(row).to_be_hidden()
    expect(page.get_by_role('button', name='撤回原话')).to_be_hidden()
    expect(page.get_by_label('生图提示词')).to_have_value('')
