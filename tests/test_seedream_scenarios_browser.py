"""Seedream 工作台：渠道联动尺寸、AI 帮写与撤回、风格库热刷新。"""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from app.services import seedream_prompts as sp
from app.services.svg_browser import _launch
from test_poster_workbench_browser import STATIC
from test_seedream_browser import PREVIEW_SVG, SCHEMA, open_workbench, png_bytes, state

CONFIG_CORE = {
    "enabled": True,
    "model": "doubao-seedream-5-5-261028",
    "has_api_key": True,
    "enable_ai_svg": False,
    "max_references": 10,
    "llm_configured": True,
}


def catalog_payload():
    body = sp.catalog()
    return {
        **CONFIG_CORE,
        "channels": body["channels"],
        "intents": body["intents"],
        "styles": body["styles"],
        "prompts_dir": body["prompts_dir"],
        "warnings": [],
    }


@pytest.fixture
def scenarios():
    shot = png_bytes()
    calls = {"generate": [], "optimize": [], "decompose": [], "background": [],
             "config_update": {}, "verify": {}}
    config = {"payload": catalog_payload()}
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={'width': 1550, 'height': 950})
            page.set_default_timeout(9000)
            page.set_default_navigation_timeout(12000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            versions = [{"id": "v1", "index": 1, "url": "http://poster.test/mock.png",
                         "width": 1880, "height": 800, "kind": "generate", "prompt": "p"}]

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
                    if route.request.method == 'PUT':
                        update = route.request.post_data_json
                        calls['config_update'] = update
                        if update.get('clear_api_key'):
                            config['payload'] = {**config['payload'], 'enabled': False, 'has_api_key': False}
                        else:
                            config['payload'] = {**config['payload'], 'enabled': True, 'has_api_key': True}
                        route.fulfill(json=config['payload'])
                    else:
                        route.fulfill(json=config["payload"])
                elif path == '/api/seedream/generate':
                    payload = route.request.post_data_json
                    calls['generate'].append(payload)
                    route.fulfill(json={'project': {'id': 'p1', 'versions': versions},
                                        'version': versions[0]})
                elif path == '/api/seedream/decompose':
                    calls['decompose'].append(route.request.post_data_json)
                    route.fulfill(json={'decomposition': {'template': {'name': '拆层模板'},
                                                          'layers': [{'index': 1}]}})
                elif path == '/api/seedream/background-template':
                    calls['background'].append(route.request.post_data_json)
                    route.fulfill(json={'template': {'name': '底图模板', 'analyzed': True},
                                        'canvas': {'width': 600, 'height': 400}})
                elif path == '/api/seedream/verify':
                    calls['verify'] = route.request.post_data_json
                    route.fulfill(json={'ok': True, 'status': 400,
                                        'message': '密钥可用（模型探测返回 400，说明鉴权已通过）'})
                elif path == '/api/seedream/optimize-prompt':
                    payload = route.request.post_data_json
                    calls['optimize'].append(payload)
                    channel = next((item for item in config["payload"]["channels"]
                                    if item["id"] == payload["channel_id"]), None)
                    route.fulfill(json={
                        "prompt": "画面为克制网格排版的秋季研学营海报，主标题 “秋季研学营”，"
                                  "副标题 “11月25日 19:00”，细线分隔，米白底深墨蓝字。",
                        "used_files": ["system_assembler.md", f"channels.json#{payload['channel_id']}",
                                       "copywriting.md", f"intents/{payload['intent_id']}.md"],
                        "warnings": [],
                        "channel": {"id": payload["channel_id"], "name": channel["name"],
                                    "ratio": channel["ratio"], "size": channel["size"]} if channel else None,
                        "intent": {"id": payload["intent_id"], "name": payload["intent_id"]},
                        "style": None,
                    })
                elif path == '/api/ai-svg/config':
                    route.fulfill(json={'has_api_key': False, 'model': 'm', 'svg_model': 's', 'provider': 'apimart'})
                elif path == '/api/ai-svg/history':
                    route.fulfill(json={'items': []})
                elif path.endswith('/preview'):
                    route.fulfill(body=PREVIEW_SVG, content_type='image/svg+xml')
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
            page.locator('.poster-stage > svg').wait_for()
            yield page, calls, config
            assert not errors
        finally:
            browser.close()


def _fill_prompt(page, text='秋季研学营招生'):
    page.get_by_label('生图提示词').fill(text)


def test_channel_presets_drive_generate_size(scenarios):
    page, calls, _ = scenarios
    open_workbench(page)
    _fill_prompt(page)

    page.get_by_role('button', name='公众号头图 · 横幅').click()
    expect(page.get_by_text('2.35:1 · 1880x800')).to_be_visible()
    expect(page.get_by_label('生图尺寸')).to_have_count(0)
    page.get_by_role('button', name='生成图片').click()
    assert calls['generate'][-1]['size'] == '1880x800'

    page.get_by_role('button', name='小红书封面').click()
    expect(page.get_by_text('3:4 · 1242x1656')).to_be_visible()
    page.get_by_role('button', name='生成图片').click()
    assert calls['generate'][-1]['size'] == '1242x1656'

    page.get_by_role('button', name='视频号封面 · 竖版').click()
    expect(page.get_by_text('6:7 · 1200x1400')).to_be_visible()
    page.get_by_role('button', name='生成图片').click()
    assert calls['generate'][-1]['size'] == '1200x1400'

    # 切回自定义尺寸后恢复手动尺寸下拉
    page.get_by_role('button', name='自定义尺寸').click()
    expect(page.get_by_label('生图尺寸')).to_be_visible()
    assert state(page, 's.sd.channelId') == ''


def test_ai_optimize_fills_prompt_and_reverts(scenarios):
    page, calls, _ = scenarios
    open_workbench(page)
    page.get_by_role('button', name='公众号头图 · 横幅').click()
    page.get_by_role('button', name='成品直出').click()
    page.get_by_role('button', name='克制网格干货风').click()
    _fill_prompt(page, '秋季研学营 11月25日 19:00 林晓娟老师')

    page.get_by_role('button', name='AI 帮写提示词').click()
    expect(page.get_by_label('生图提示词')).to_have_value(
        "画面为克制网格排版的秋季研学营海报，主标题 “秋季研学营”，"
        "副标题 “11月25日 19:00”，细线分隔，米白底深墨蓝字。"
    )
    request = calls['optimize'][-1]
    assert request['raw_input'] == '秋季研学营 11月25日 19:00 林晓娟老师'
    assert request['channel_id'] == 'wechat_banner'
    assert request['intent_id'] == 'direct'
    assert request['style_id'] == 'editorial_grid'
    assert request['has_ref_images'] is False

    expect(page.get_by_text('已组合：公众号头图 · 横幅 + 成品直出 + 克制网格干货风')).to_be_visible()
    expect(page.get_by_text('channels.json#wechat_banner')).to_be_visible()

    page.get_by_role('button', name='撤回原话').click()
    expect(page.get_by_label('生图提示词')).to_have_value('秋季研学营 11月25日 19:00 林晓娟老师')
    expect(page.get_by_role('button', name='撤回原话')).to_be_hidden()
    assert state(page, 's.sd.promptSnapshot') is None


def test_second_optimize_uses_original_snapshot_not_expanded_text(scenarios):
    page, calls, _ = scenarios
    open_workbench(page)
    page.get_by_role('button', name='公众号头图 · 横幅').click()
    _fill_prompt(page, '原始需求：秋季研学营')
    page.get_by_role('button', name='AI 帮写提示词').click()
    page.wait_for_function(
        "() => document.querySelector('[aria-label=\"生图提示词\"]').value.includes('克制网格')"
    )

    # 换风格再帮写：仍以原始需求为原话，不套娃
    page.get_by_role('button', name='喜庆红金冲刺风').click()
    page.get_by_role('button', name='AI 帮写提示词').click()
    page.wait_for_function(
        "() => document.querySelector('[aria-label=\"生图提示词\"]').value.includes('克制网格')"
    )
    assert calls['optimize'][-1]['raw_input'] == '原始需求：秋季研学营'
    assert calls['optimize'][-1]['style_id'] == 'festive_red_gold'


def test_style_library_refreshes_without_reload(scenarios):
    page, _, config = scenarios
    open_workbench(page)
    expect(page.get_by_role('button', name='克制网格干货风')).to_be_visible()
    expect(page.get_by_role('button', name='第三方新风格')).to_have_count(0)

    payload = catalog_payload()
    payload['styles'] = payload['styles'] + [{
        "id": "third_party", "name": "第三方新风格", "summary": "刚拖进来的 Skill",
        "recommended_channels": [],
    }]
    config['payload'] = payload

    page.get_by_role('button', name='刷新风格库').click()
    expect(page.get_by_role('button', name='第三方新风格')).to_be_visible()
    assert state(page, 's.sd.catalog.styles.length') == 5  # noqa: PLR2004


def test_clean_bg_goes_straight_to_background_template(scenarios):
    page, calls, _ = scenarios
    open_workbench(page)
    page.get_by_role('button', name='公众号头图 · 横幅').click()
    _fill_prompt(page, '秋季研学营底图')
    page.get_by_role('button', name='生成图片').click()

    expect(page.get_by_role('button', name='✨ 拆图层并进编辑器')).to_be_visible()
    page.get_by_role('button', name='无字留白底图').click()
    expect(page.get_by_role('button', name='作为底图进编辑器')).to_be_visible()
    page.get_by_role('button', name='作为底图进编辑器').click()

    page.wait_for_function("() => Alpine.$data(document.body).mode === 'editor'")
    assert len(calls['background']) == 1
    assert calls['decompose'] == []          # 严禁再调拆图层接口
    assert calls['background'][0]['image'] == 'http://poster.test/mock.png'


def test_right_toolbar_can_decompose_without_scrolling_left_panel(scenarios):
    page, calls, _ = scenarios
    open_workbench(page)
    page.get_by_role('button', name='公众号头图 · 横幅').click()
    _fill_prompt(page, '秋季研学营')
    page.get_by_role('button', name='生成图片').click()

    toolbar = page.locator('section').filter(has=page.get_by_role('button', name='圈画局改'))
    button = toolbar.get_by_role('button', name='拆层进编辑器').first
    expect(button).to_be_visible()
    button.click()
    page.wait_for_function("() => Alpine.$data(document.body).mode === 'editor'")
    assert len(calls['decompose']) == 1


def test_ark_key_saved_from_settings_dialog(scenarios):
    page, calls, _ = scenarios
    open_workbench(page)
    page.evaluate("() => { Alpine.$data(document.body).sd.enabled = false; }")
    page.get_by_role('button', name='API 设置').click()
    dialog = page.locator('dialog[open]')
    expect(dialog.get_by_label('ARK_API_KEY')).to_be_visible()
    dialog.get_by_label('ARK_API_KEY').fill('ark-ui-saved-key-9876')
    dialog.get_by_role('button', name='保存 ARK 密钥').click()

    page.wait_for_function("() => Alpine.$data(document.body).sd.enabled === true")
    assert calls['config_update'] == {'api_key': 'ark-ui-saved-key-9876'}
    expect(page.get_by_text('已保存并立即生效')).to_be_visible()
    expect(page.get_by_text('ARK 已连接')).to_be_visible()


def test_verify_button_reports_key_status(scenarios):
    page, calls, _ = scenarios
    open_workbench(page)
    page.get_by_role('button', name='API 设置').click()
    dialog = page.locator('dialog[open]')
    dialog.get_by_label('ARK_API_KEY').fill('ark-typed-but-not-saved')
    dialog.get_by_role('button', name='测试连接').click()

    expect(page.get_by_text('✅ 密钥可用', exact=False)).to_be_visible()
    assert calls['verify'] == {'api_key': 'ark-typed-but-not-saved'}


def test_empty_state_can_click_and_drop_to_upload(scenarios):
    page, _, _ = scenarios
    open_workbench(page)
    empty = page.get_by_role('button', name='点击或拖拽上传海报')
    expect(empty).to_be_visible()

    with page.expect_file_chooser() as chooser:
        empty.click()
    assert chooser.value is not None


def test_download_current_image_button(scenarios):
    page, _, _ = scenarios
    open_workbench(page)
    _fill_prompt(page, '秋季研学营')
    page.get_by_role('button', name='生成图片').click()
    page.get_by_role('button', name='版本 v1').wait_for()

    captured = page.evaluate("""() => {
        window.__downloads = [];
        const original = HTMLAnchorElement.prototype.click;
        HTMLAnchorElement.prototype.click = function () {
            window.__downloads.push({download: this.download, href: this.href.slice(0, 5)});
        };
        window.__restoreAnchor = () => { HTMLAnchorElement.prototype.click = original; };
        return true;
    }""")
    assert captured is True
    page.get_by_role('button', name='下载原图').click()
    page.wait_for_function('() => window.__downloads.length === 1')
    downloads = page.evaluate('() => window.__downloads')
    assert downloads[0]['download'] == 'seedream-v1.png'
    assert downloads[0]['href'] == 'blob:'
