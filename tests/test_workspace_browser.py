"""工作区名片、`?ws=` 恢复链接与生图历史抽屉的页面级回归。"""
from __future__ import annotations

import io
import json
from urllib.parse import urlsplit

import pytest
from PIL import Image
from playwright.sync_api import expect, sync_playwright

from app.services.svg_browser import _launch
from test_poster_workbench_browser import STATIC
from test_seedream_browser import PREVIEW_SVG, SCHEMA


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (120, 90), (240, 238, 232)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeBackend:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.teams_of: dict[str, str] = {}
        self.teams: list[dict] = [
            {"id": "t_alpha01", "name": "秋季教研团队", "created_by": "u_alpha", "created_at": "2026-01-01",
             "generations": 1, "members": 1},
        ]
        self.items: list[dict] = [{
            "generation_id": "gen_alpha_1",
            "title": "公开作品：秋季研学营",
            "prompt": "一张秋季研学营海报，米白底",
            "raw_input": "秋季研学营",
            "channel_id": "xhs_cover",
            "intent_id": "direct",
            "style_id": "editorial_grid",
            "size": "1242x1656",
            "scope": "public",
            "owner_id": "u_alpha",
            "owner_name": "甲老师",
            "team_id": "t_alpha01",
            "cover_url": "http://poster.test/mock.png",
            "versions": [
                {"version": 1, "url": "http://poster.test/mock.png", "action": "generate",
                 "instruction": "秋季研学营", "width": 120, "height": 90},
                {"version": 2, "url": "http://poster.test/mock.png", "action": "region",
                 "instruction": "改成篮球", "width": 120, "height": 90},
            ],
            "version_count": 2,
            "created_at": "2026-01-01", "updated_at": "2026-01-02",
        }]

    def me(self, user: str) -> dict:
        return {
            "user_id": user,
            "name": self.names.get(user, ""),
            "team_id": self.teams_of.get(user, ""),
            "teams": self.teams,
            "is_guest": user == "u_guest",
        }

    def public_items(self) -> list[dict]:
        return [dict(item, owned=False) for item in self.items]

    def list(self, user: str, view: str, query: str) -> list[dict]:
        if view == "mine":
            rows = [item for item in self.items if item["owner_id"] == user]
        elif view == "team":
            team = self.teams_of.get(user, "")
            rows = [] if not team else [item for item in self.items
                                        if item["team_id"] == team and item["scope"] in ("team", "public")]
        else:
            rows = [item for item in self.items if item["scope"] == "public"]
        if query:
            rows = [item for item in rows if query in item["title"] or query in item["prompt"]]
        return [dict(item, owned=item["owner_id"] == user) for item in rows]


@pytest.fixture
def backend():
    state = FakeBackend()

    def serve(page):
        def handle(route):
            url = route.request.url
            path = urlsplit(url).path
            headers = {key.lower(): value for key, value in route.request.headers.items()}
            user = headers.get("x-user-id", "u_guest")
            method = route.request.method

            if path.startswith('/static/'):
                route.fulfill(path=STATIC / path.removeprefix('/static/'))
            elif path == '/mock.png':
                route.fulfill(body=png_bytes(), content_type='image/png')
            elif path == '/api/v1/health':
                route.fulfill(json={'llm': {'configured': False},
                                    'features': {'ai_svg': False, 'seedream': True}})
            elif path == '/api/seedream/config':
                route.fulfill(json={'enabled': False, 'model': 'm', 'enable_ai_svg': False,
                                    'max_references': 10, 'llm_configured': False,
                                    'channels': [], 'intents': [], 'styles': []})
            elif path == '/api/workspace/me':
                if method == 'PUT':
                    body = route.request.post_data_json or {}
                    if 'name' in body:
                        state.names[user] = body['name'] or ''
                    if 'team_id' in body:
                        state.teams_of[user] = body['team_id'] or ''
                route.fulfill(json=state.me(user))
            elif path == '/api/workspace/teams' and method == 'POST':
                body = route.request.post_data_json or {}
                name = (body.get('name') or '').strip()
                if any(team['name'] == name for team in state.teams):
                    route.fulfill(status=409, json={'detail': '团队已存在'})
                else:
                    team = {'id': 't_new01', 'name': name, 'created_by': user,
                            'created_at': '2026-01-03', 'generations': 0, 'members': 1}
                    state.teams.append(team)
                    state.teams_of[user] = team['id']
                    payload = state.me(user)
                    payload['team_id'] = team['id']
                    payload['team'] = team
                    route.fulfill(status=201, json=payload)
            elif path == '/api/workspace/teams':
                route.fulfill(json={'teams': state.teams})
            elif path == '/api/seedream/history':
                query = route.request.url.split('q=', 1)[1].split('&')[0] if 'q=' in route.request.url else ''
                view = route.request.url.split('view=', 1)[1].split('&')[0] if 'view=' in route.request.url else 'mine'
                route.fulfill(json={'items': state.list(user, view, query), 'view': view,
                                    'team_id': state.teams_of.get(user, ''), 'user_id': user})
            elif path.startswith('/api/seedream/history/'):
                generation_id = path.rsplit('/', 1)[-1]
                item = next((entry for entry in state.items if entry['generation_id'] == generation_id), None)
                if item is None:
                    route.fulfill(status=404, json={'detail': '记录不存在'})
                elif method == 'PATCH':
                    body = route.request.post_data_json or {}
                    if item['owner_id'] != user:
                        route.fulfill(status=403, json={'detail': '只有作者可以操作'})
                    else:
                        item['scope'] = body.get('scope', item['scope'])
                        route.fulfill(json=dict(item, owned=True))
                elif method == 'DELETE':
                    if item['owner_id'] != user:
                        route.fulfill(status=403, json={'detail': '只有作者可以操作'})
                    else:
                        state.items.remove(item)
                        route.fulfill(status=204)
                else:
                    route.fulfill(json=dict(item, owned=item['owner_id'] == user))
            elif path == '/api/seedream/generate':
                token = route.request.post_data_json or {}
                item = dict(state.items[0])
                item.update({
                    'generation_id': f'gen_{user}_{len(state.items)}',
                    'title': (token.get('prompt') or '未命名')[:24],
                    'prompt': token.get('prompt') or '',
                    'owner_id': user,
                    'owner_name': state.names.get(user, ''),
                    'team_id': state.teams_of.get(user, ''),
                    'versions': [{'version': 1, 'url': 'http://poster.test/mock.png',
                                  'action': 'generate', 'instruction': token.get('prompt', ''),
                                  'width': 120, 'height': 90}],
                    'version_count': 1,
                })
                state.items.insert(0, item)
                route.fulfill(json={'project': {'id': item['generation_id'], 'versions': item['versions']},
                                    'version': item['versions'][0], 'generation_id': item['generation_id']})
            elif path == '/api/ai-svg/config':
                route.fulfill(json={'has_api_key': False, 'model': 'm', 'svg_model': 's', 'provider': 'apimart'})
            elif path == '/api/ai-svg/history':
                route.fulfill(json={'items': []})
            elif path.endswith('/preview'):
                route.fulfill(body=PREVIEW_SVG, content_type='image/svg+xml')
            elif path.endswith('/templates'):
                route.fulfill(json=[])
            elif path.endswith('/schema'):
                route.fulfill(json=SCHEMA)
            else:
                route.fulfill(json=[] if path.endswith('/assets') else {})

        page.route('http://poster.test/**', handle)
        return page

    return state, serve


def _open(playwright, backend, url='http://poster.test/static/poster.html', storage=None):
    serve = backend[1]
    browser = _launch(playwright)
    context = browser.new_context(viewport={'width': 1550, 'height': 950})
    page = context.new_page()
    page.set_default_timeout(9000)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    serve(page)
    page.goto(url)
    page.wait_for_load_state('load')
    return browser, context, page, errors


def test_workspace_card_creates_team_and_survives_reload(backend):
    state, _ = backend
    with sync_playwright() as p:
        browser, context, page, errors = _open(p, backend)
        try:
            expect(page.get_by_label('工作区名片', exact=True)).to_be_visible()
            page.get_by_label('工作区名片', exact=True).click()
            expect(page.get_by_label('显示昵称')).to_be_visible()

            page.get_by_label('显示昵称').fill('林老师')
            page.get_by_role('button', name='保存昵称').click()
            expect(page.get_by_text('昵称已保存')).to_be_visible()

            page.get_by_label('新团队名称').fill('漳州教研组')
            page.get_by_role('button', name='新建团队').click()
            expect(page.get_by_text('团队已创建并加入')).to_be_visible()
            page.get_by_label('关闭工作区名片').click()

            # 生成一条作品，验证刷新后历史还在
            page.get_by_role('button', name='Seedream 生图').click()
            page.get_by_label('生图提示词').fill('秋季研学营')
            page.get_by_role('button', name='生成图片').click()
            page.wait_for_function("() => Alpine.$data(document.body).sd.versions.length === 1")

            page.reload()
            page.wait_for_load_state('load')
            expect(page.get_by_label('工作区名片', exact=True)).to_contain_text('林老师')
            expect(page.get_by_role('button', name='Seedream 生图')).to_be_visible()
            assert state.names, '昵称应写入后端'
            page.get_by_role('button', name='Seedream 生图').click()
            page.get_by_label('历史记录', exact=True).click()
            expect(page.get_by_text('秋季研学营', exact=False).first).to_be_visible()
            assert not errors
        finally:
            browser.close()


def test_recovery_link_switches_identity_and_isolates_history(backend):
    with sync_playwright() as p:
        browser, context, page, errors = _open(p, backend)
        try:
            page.get_by_label('工作区名片', exact=True).click()
            page.get_by_label('个人口令').fill('test_user_2')
            page.get_by_role('button', name='切换口令').click()
            expect(page.get_by_text('已切换到 test_user_2')).to_be_visible()
            token = page.evaluate('() => localStorage.getItem("poster.ws.user")')
            assert token == 'test_user_2'
            assert not errors
        finally:
            browser.close()

    with sync_playwright() as p:
        browser, context, page, errors = _open(
            p, backend, url='http://poster.test/static/poster.html?ws=test_user_2'
        )
        try:
            assert page.evaluate('() => localStorage.getItem("poster.ws.user")') == 'test_user_2'
            page.get_by_role('button', name='Seedream 生图').click()
            page.get_by_label('历史记录', exact=True).click()
            expect(page.get_by_text('这里还没有作品')).to_be_visible()
            page.get_by_role('button', name='共享全部').click()
            expect(page.get_by_text('公开作品：秋季研学营')).to_be_visible()
            assert not errors
        finally:
            browser.close()


def test_history_drawer_tabs_load_and_reuse_prompt(backend):
    state, _ = backend
    state.names['u_alpha'] = '甲老师'
    state.teams_of['u_alpha'] = 't_alpha01'
    with sync_playwright() as p:
        browser, context, page, errors = _open(p, backend)
        try:
            page.evaluate("() => localStorage.setItem('poster.ws.user', 'u_alpha')")
            page.reload()
            page.wait_for_load_state('load')
            page.get_by_role('button', name='Seedream 生图').click()
            page.get_by_label('历史记录', exact=True).click()

            expect(page.get_by_text('公开作品：秋季研学营')).to_be_visible()
            page.get_by_role('button', name='团队').click()
            expect(page.get_by_text('公开作品：秋季研学营')).to_be_visible()

            page.get_by_role('button', name='加载到画布 公开作品：秋季研学营').click()
            page.wait_for_function(
                "() => Alpine.$data(document.body).sd.versions.length === 2"
            )
            assert page.evaluate('() => Alpine.$data(document.body).sd.current.url') == 'http://poster.test/mock.png'

            page.get_by_label('历史记录', exact=True).click()
            page.get_by_role('button', name='复刻提示词 公开作品：秋季研学营').click()
            value = page.get_by_label('生图提示词').input_value()
            assert '秋季研学营' in value
            assert page.evaluate('() => Alpine.$data(document.body).sd.styleId') == 'editorial_grid'
            assert not errors
        finally:
            browser.close()
