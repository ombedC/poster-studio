"""多画板 .ai 上传：前端弹窗选择画板并把 page 传给后端。"""
from __future__ import annotations

import base64
import io
import re
from urllib.parse import urlsplit

import pytest
from PIL import Image
from playwright.sync_api import expect, sync_playwright

from app.services.svg_browser import _launch
from test_poster_workbench_browser import STATIC

SCHEMA = {
    "version": 15,
    "template_id": "multi",
    "name": "多画板测试",
    "canvas": {"width": 600, "height": 800},
    "texts": [],
    "images": [],
    "svg_path": "multi.svg",
}

PREVIEW = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 800" width="600" height="800">'
    '<rect width="600" height="800" fill="#f2efe8"/></svg>'
)


def thumb(color: tuple[int, int, int]) -> str:
    image = Image.new("RGB", (60, 80), (255, 255, 255))
    for x in range(10, 50):
        for y in range(10, 70):
            image.putpixel((x, y), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


INSPECT = {
    "kind": "pdf",
    "total": 3,
    "pages": [
        {"index": 1, "width": 600, "height": 800, "blank": True, "thumbnail": thumb((240, 240, 240))},
        {"index": 2, "width": 600, "height": 800, "blank": False, "thumbnail": thumb((200, 40, 40))},
        {"index": 3, "width": 600, "height": 800, "blank": False, "thumbnail": thumb((30, 60, 140))},
    ],
    "non_blank": [2, 3],
    "auto_page": 0,
    "needs_choice": True,
    "truncated": False,
}


@pytest.fixture
def workbench():
    state: dict = {"page": None, "uploaded": False}

    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_context(viewport={"width": 1550, "height": 950}).new_page()
            page.set_default_timeout(15000)

            def handle(route):
                path = urlsplit(route.request.url).path
                method = route.request.method
                if path.startswith('/static/'):
                    route.fulfill(path=STATIC / path.removeprefix('/static/'))
                elif path == '/api/v1/health':
                    route.fulfill(json={'llm': {'configured': False},
                                        'features': {'ai_svg': False, 'seedream': False}})
                elif path == '/api/v1/posters/templates/inspect':
                    state['source_upload_count'] = state.get('source_upload_count', 0) + 1
                    route.fulfill(json={**INSPECT, 'staged_token': 'upload-0123456789abcdef0123456789abcdef'})
                elif path == '/api/v1/posters/templates/upload':
                    state['confirm_count'] = state.get('confirm_count', 0) + 1
                    body = route.request.post_data or ''
                    assert 'name="staged_token"' in body
                    assert 'name="file"' not in body
                    state['staged_token_sent'] = 'upload-0123456789abcdef0123456789abcdef' in body
                    state['confirmed_filename'] = '多画板.ai' in body
                    match = re.search(r'name="page"\r\n\r\n(\d+)', body)
                    state['page'] = int(match.group(1)) if match else 0
                    state['uploaded'] = True
                    route.fulfill(json={
                        "name": "多画板测试", "display_name": "多画板测试", "analyzed": True,
                        "thumbnail_url": "", "canvas": {"width": 600, "height": 800},
                        "warnings": ["使用第 3 个画板（共 3 个）"], "revived_texts": 0,
                        "revival_report": {},
                    })
                elif path == '/api/v1/posters/templates/upload-staged':
                    body = route.request.post_data_json or {}
                    state['page'] = body.get('page', 0)
                    state['staged_token'] = body.get('staged_token')
                    state['uploaded'] = True
                    route.fulfill(json={
                        "name": "多画板测试", "display_name": "多画板测试", "analyzed": True,
                        "thumbnail_url": "", "canvas": {"width": 600, "height": 800},
                        "warnings": ["使用第 3 个画板（共 3 个）"], "revived_texts": 0,
                        "revival_report": {},
                    })
                elif path.endswith('/templates'):
                    route.fulfill(json=[{'name': '多画板测试', 'display_name': '多画板测试',
                                         'analyzed': True, 'thumbnail_url': '',
                                         'scope': 'public', 'owner_id': '', 'team_id': '',
                                         'owned': False}])
                elif path.endswith('/schema'):
                    route.fulfill(json=SCHEMA)
                elif path.endswith('/preview'):
                    route.fulfill(body=PREVIEW, content_type='image/svg+xml')
                elif path.endswith('/ai-svg/history'):
                    route.fulfill(json={'items': []})
                elif path.endswith('/ai-svg/config'):
                    route.fulfill(json={'has_api_key': False, 'model': 'm', 'svg_model': 's', 'provider': 'p'})
                else:
                    route.fulfill(json=[] if path.endswith('/assets') else {})

            page.route('http://poster.test/**', handle)
            page.goto('http://poster.test/static/poster.html')
            page.wait_for_load_state('load')
            yield page, state
        finally:
            browser.close()


def test_artboard_picker_imports_selected_board(workbench):
    page, state = workbench
    page.get_by_role('button', name='切换模板').click()
    expect(page.get_by_text('选择要导入的画板', exact=True)).to_be_hidden()

    page.locator("input[type=file][accept*='.ai']").first.set_input_files(
        files=[{"name": "多画板.ai", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n"}]
    )

    expect(page.get_by_text('选择要导入的画板', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='画板 1（空白）')).to_be_disabled()
    expect(page.get_by_role('button', name='画板 3')).to_be_enabled()

    page.get_by_role('button', name='画板 3').click()
    page.get_by_role('button', name='导入选中画板').click()

    expect(page.get_by_role('button', name='导入选中画板')).to_be_hidden()
    assert state['page'] == 3
    assert state['source_upload_count'] == 1
    assert state['confirm_count'] == 1
    assert state['staged_token_sent'] is True
    assert state['confirmed_filename'] is True
    expect(page.get_by_text('多画板测试').first).to_be_visible()


def test_blank_boards_are_disabled_and_not_selected(workbench):
    page, state = workbench
    page.get_by_role('button', name='切换模板').click()
    page.locator("input[type=file][accept*='.ai']").first.set_input_files(
        files=[{"name": "全空白.ai", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n"}]
    )
    expect(page.get_by_text('选择要导入的画板', exact=True)).to_be_visible()
    # 默认选中第一个非空画板（2），空白画板不可点
    assert page.get_by_role('button', name='导入选中画板').inner_text().strip().endswith('2 个画板')
    page.get_by_role('button', name='画板 1（空白）').click(force=True)
    assert page.get_by_role('button', name='导入选中画板').inner_text().strip().endswith('2 个画板')
    page.get_by_role('button', name='取消').click()
    expect(page.get_by_role('heading', name='选择要导入的画板')).to_be_hidden()
    assert state['page'] is None
