"""AI 智能填表只做增量补丁：未提及字段必须保留画布原值。"""
import json
from datetime import date
from html import escape
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from app.services.poster_semantics import build_values
from app.services.svg_browser import _launch
from test_poster_workbench_browser import STATIC

TODAY = date(2026, 11, 22)  # 周日
INITIAL = {
    "title": "语文期末冲刺课",
    "subtitle": "2026漳州九年级",
    "teacher": "林晓娟",
    "school": "福建师范大学",
    "bio": "省级骨干教师",
    "schedule": "11月20日（周四）19:00",
}
SCHEMA = {
    "canvas": {"width": 600, "height": 400},
    "images": [],
    "texts": [
        {"id": "title", "kind": "text", "field": "main_title", "role": "main_title", "text": INITIAL["title"]},
        {"id": "subtitle", "kind": "text", "field": "sub_title", "role": "sub_title", "text": INITIAL["subtitle"]},
        {"id": "teacher", "kind": "text", "field": "teacher_name", "role": "teacher_name",
         "text": INITIAL["teacher"], "teacher_slot": 0},
        {"id": "school", "kind": "text", "field": "teacher_org", "role": "teacher_org",
         "text": INITIAL["school"], "teacher_slot": 0},
        {"id": "bio", "kind": "text", "field": "teacher_bio_1", "role": "teacher_bio_1",
         "text": INITIAL["bio"], "teacher_slot": 0, "bio_index": 0},
        {"id": "schedule", "kind": "text", "field": "schedule", "role": "schedule",
         "text": INITIAL["schedule"], "course_slot": 0},
    ],
}


def render_svg(values: dict) -> str:
    rows = [
        (20, 30, 22, "subtitle"),
        (20, 90, 34, "title"),
        (20, 140, 28, "teacher"),
        (20, 200, 22, "school"),
        (20, 260, 20, "bio"),
        (20, 330, 24, "schedule"),
    ]
    body = "".join(
        f'<text id="{key}" x="{x}" y="{y}" font-size="{size}" fill="#111">{escape(values.get(key, ""))}</text>'
        for x, y, size, key in rows
    )
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 400">{body}</svg>'


@pytest.fixture
def merge():
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={'width': 1550, 'height': 950})
            page.set_default_timeout(8000)
            page.set_default_navigation_timeout(12000)
            errors, calls = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def handle(route):
                path = urlsplit(route.request.url).path
                if path.startswith('/static/'):
                    route.fulfill(path=STATIC / path.removeprefix('/static/'))
                elif path == '/api/v1/health':
                    route.fulfill(json={'llm': {'configured': True, 'model': 'demo'},
                                        'features': {'ai_svg': False, 'seedream': False}})
                elif path == '/api/seedream/config':
                    route.fulfill(json={'enabled': True, 'model': 'm', 'enable_ai_svg': False})
                elif path == '/api/ai-svg/config':
                    route.fulfill(json={'has_api_key': False, 'model': 'm', 'svg_model': 's', 'provider': 'apimart'})
                elif path == '/api/ai-svg/history':
                    route.fulfill(json={'items': []})
                elif path.endswith('/parse-text'):
                    calls.append(route.request.post_data_json)
                    # The browser fixture exercises merge behavior; the API has its own parser tests.
                    body = route.request.post_data_json
                    current = body.get('current_values') or {}
                    if body['raw_text'] == '周日晚19:00':
                      values = {'schedule': '周日晚 19:00'}
                    elif body['raw_text'] == '厦门':
                      values = {'subtitle': '2026厦门九年级', 'bio': '省级骨干教师'}
                    else:
                      values = build_values(SCHEMA, body['raw_text'], TODAY, current_values=current)
                    values.update({'teacher': '', 'school': '', 'bio': ''})
                    route.fulfill(json={'values': values, 'images': {}, 'unmatched_avatar_keys': [],
                                        'warnings': [], 'engine': 'heuristic'})
                elif path.endswith('/preview'):
                    data = route.request.post_data_json.get('data') or {}
                    route.fulfill(body=render_svg(data), content_type='image/svg+xml')
                elif path.endswith('/export-svg'):
                    route.fulfill(json={'url': '/blank', 'width': 600, 'height': 400})
                elif path.endswith('/templates'):
                    route.fulfill(json=[{'name': 'demo', 'analyzed': True}])
                elif path.endswith('/schema'):
                    route.fulfill(json=SCHEMA)
                else:
                    route.fulfill(json=[] if path.endswith('/assets') else {})

            page.route('http://poster.test/**', handle)
            page.goto('http://poster.test/static/poster.html')
            page.locator('.poster-stage #teacher').wait_for()
            yield page, calls
            assert not errors
        finally:
            browser.close()


def test_partial_parse_text_keeps_teacher_school_and_bio(merge):
    page, calls = merge
    expect(page.locator('.poster-stage #teacher')).to_have_text('林晓娟')
    expect(page.locator('.poster-stage #school')).to_have_text('福建师范大学')

    page.locator('textarea[placeholder*="整段排课文案"]').fill('周日晚19:00')
    page.get_by_role('button', name='✨ AI 智能解析并填充').click()

    expect(page.locator('.poster-stage #schedule')).to_have_text('周日晚 19:00')
    # 未提及字段必须原样保留
    expect(page.locator('.poster-stage #teacher')).to_have_text('林晓娟')
    expect(page.locator('.poster-stage #school')).to_have_text('福建师范大学')
    expect(page.locator('.poster-stage #bio')).to_have_text('省级骨干教师')
    expect(page.locator('.poster-stage #title')).to_have_text('语文期末冲刺课')
    expect(page.locator('.poster-stage #subtitle')).to_have_text('2026漳州九年级')

    values = page.evaluate('() => Alpine.$data(document.body).values')
    assert values['teacher'] == '林晓娟' and values['school'] == '福建师范大学'
    assert values['bio'] == '省级骨干教师' and values['schedule'] == '周日晚 19:00'
    assert calls[0]['raw_text'] == '周日晚19:00'


def test_clear_unmatched_option_is_opt_in(merge):
    page, _ = merge
    box = page.get_by_label('清空未提及字段')
    expect(box).not_to_be_checked()
    page.locator('textarea[placeholder*="整段排课文案"]').fill('周日晚19:00')
    page.get_by_role('button', name='✨ AI 智能解析并填充').click()
    expect(page.locator('.poster-stage #teacher')).to_have_text('林晓娟')

    box.check()
    page.get_by_role('button', name='✨ AI 智能解析并填充').click()
    expect(page.locator('.poster-stage #teacher')).to_have_text('')
    expect(page.locator('.poster-stage #schedule')).to_have_text('周日晚 19:00')


def test_title_patch_lands_on_matching_slot_not_first_title(merge):
    """只改“2026漳州九年级 → 2026厦门九年级”时，不能把主标题覆盖掉。"""
    page, _ = merge
    expect(page.locator('.poster-stage #subtitle')).to_have_text('2026漳州九年级')
    expect(page.locator('.poster-stage #title')).to_have_text('语文期末冲刺课')

    page.locator('textarea[placeholder*="整段排课文案"]').fill('2026厦门九年级')
    page.get_by_role('button', name='✨ AI 智能解析并填充').click()

    expect(page.locator('.poster-stage #subtitle')).to_have_text('2026厦门九年级')
    expect(page.locator('.poster-stage #title')).to_have_text('语文期末冲刺课')
    values = page.evaluate('() => Alpine.$data(document.body).values')
    assert values['subtitle'] == '2026厦门九年级'
    assert values['title'] == '语文期末冲刺课'


def test_bare_city_patch_never_overwrites_course_title(merge):
    page, calls = merge
    page.locator('textarea[placeholder*="整段排课文案"]').fill('厦门')
    page.get_by_role('button', name='✨ AI 智能解析并填充').click()
    expect(page.locator('.poster-stage #subtitle')).to_have_text('2026厦门九年级')
    expect(page.locator('.poster-stage #title')).to_have_text('语文期末冲刺课')
    assert calls[0]['current_values']['subtitle'] == '2026漳州九年级'
    assert calls[0]['current_values']['title'] == '语文期末冲刺课'
