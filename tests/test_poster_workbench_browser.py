"""Exercise the real workbench in Chromium with deterministic API responses."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from app.errors import BrowserUnavailable
from app.services.svg_browser import _launch


STATIC = Path(__file__).resolve().parents[1] / 'static'
SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 360">
<text id="title" x="70" y="105" font-size="36">First title</text>
<text id="row" x="70" y="190" font-size="32">Honor line<tspan id="big" x="70" y="230">Second title</tspan><tspan id="small" x="320" y="270" font-size="22">Subtitle</tspan></text>
</svg>'''
SCHEMA = {'canvas': {'width': 600, 'height': 360}, 'images': [], 'texts': [
    {'id': 'title', 'kind': 'text', 'text': 'First title'},
    {'id': 'row', 'kind': 'text', 'text': 'Honor line', 'base_font_size': 32, 'max_width': 200, 'font_fit_group': 'teacher_bio:0'},
    {'id': 'big', 'kind': 'tspan', 'text': 'Second title', 'base_font_size': 32, 'max_width': 200, 'font_fit_group': 'teacher_bio:0'},
    {'id': 'small', 'kind': 'tspan', 'text': 'Subtitle'},
]}


def test_viewport_drag_and_inline_edit_do_not_replace_svg():
    with sync_playwright() as p:
        try:
            browser = _launch(p)
        except BrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = browser.new_page(viewport={'width': 1550, 'height': 950})
            errors, exports, previews = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def handle(route):
                path = route.request.url.split('poster.test', 1)[1].split('?', 1)[0]
                if path.startswith('/static/'):
                    route.fulfill(path=STATIC / path.removeprefix('/static/'))
                    return
                if path.endswith('/preview'):
                    previews.append(path)
                    text = route.request.post_data_json['data'].get('big', 'Second title')
                    route.fulfill(body=SVG.replace('Second title', text), content_type='image/svg+xml')
                    return
                body = None
                if path.endswith('/templates'):
                    body = [{'name': 'demo', 'analyzed': True, 'thumbnail_url': '/blank'}]
                elif path.endswith('/schema'):
                    body = SCHEMA
                elif path.endswith('/parse-text'):
                    body = {'values': {'big': route.request.post_data_json['raw_text']}}
                elif path.endswith('/assets'):
                    body = []
                elif path.endswith('/export-svg'):
                    exports.append(route.request.post_data_json['svg'])
                    body = {'url': '/blank', 'width': 1200, 'height': 720}
                route.fulfill(body=json.dumps(body or (body if isinstance(body, list) else {})), content_type='application/json')

            page.route('http://poster.test/**', handle)
            page.goto('http://poster.test/static/poster.html')
            stage = page.locator('.poster-stage svg')
            stage.wait_for()
            title, row, big, small = (stage.locator(f'#{key}') for key in ('title', 'row', 'big', 'small'))
            def size(node):
                return node.evaluate('(el) => parseFloat(getComputedStyle(el).fontSize)')

            def parse(value):
                assert page.evaluate('''async (value) => {
                    const studio = Alpine.$data(document.body);
                    studio.rawText = value;
                    await studio.parseText();
                    return studio.values.big === value;
                }''', value)

            assert size(row) == size(big) == 32
            long_text = 'An exceptionally long biography line for a narrow field'
            parse(long_text)
            assert size(row) == size(big) < 32
            parse('Tiny')
            assert size(row) == size(big) == 32
            big.dblclick()
            page.locator('.editing-proxy').fill(long_text)
            page.locator('.editing-proxy').press('Enter')
            assert size(row) == size(big) == 32
            big.evaluate('(el) => el.dispatchEvent(new MouseEvent("dblclick", {bubbles: true}))')
            page.locator('.editing-proxy').fill('Second title')
            page.locator('.editing-proxy').press('Enter')
            assert size(row) == size(big) == 32
            big.click()
            page.evaluate("() => Alpine.$data(document.body).updateTextProperty('font_size', '28')")
            assert size(big) == 28 and size(row) == 32
            page.evaluate('() => Alpine.$data(document.body).undo()')
            assert size(big) == 32
            page.evaluate('() => Alpine.$data(document.body).redo()')
            assert size(big) == 28
            parse(long_text)
            assert size(big) == 28
            assert page.evaluate('() => Alpine.$data(document.body).preview()')
            assert size(big) == 28
            parse('Second title')
            assert size(big) == 28 and size(row) == 32
            page.evaluate("() => { const a = Alpine.$data(document.body); delete a.manualFontSizes.big; a.fitPreviewText(document.querySelector('.poster-stage svg'), new Set(['big'])); }")
            page.get_by_role('button', name='放大', exact=True).click()
            page.locator('.poster-paper').evaluate('(el) => el.dataset.zoomed = el.getBoundingClientRect().width')
            point = title.bounding_box()
            x, y = point['x'] + 15, point['y'] + point['height'] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 70, y + 30, steps=5)
            page.wait_for_function("document.querySelector('#title').getBoundingClientRect().x > " + str(point['x'] + 60))
            page.mouse.up()
            assert title.bounding_box()['x'] - point['x'] == pytest.approx(70, abs=2)
            page.keyboard.down('Space')
            page.mouse.move(900, 520)
            page.mouse.down()
            page.mouse.move(965, 560, steps=5)
            page.mouse.up()
            page.keyboard.up('Space')
            assert title.bounding_box()['x'] - point['x'] == pytest.approx(135, abs=2)
            page.keyboard.press('Control+0')
            selection = page.locator('.text-selection-box')

            def assert_selection(node, direct=False):
                rect = node.evaluate('''(el, direct) => {
                    if (!direct) return el.getBoundingClientRect().toJSON();
                    const range = document.createRange();
                    range.selectNodeContents(el.firstChild);
                    return range.getBoundingClientRect().toJSON();
                }''', direct)
                box = selection.bounding_box()
                for key in ('x', 'y', 'width', 'height'):
                    assert box[key] == pytest.approx(rect[key], abs=1)

            row = stage.locator('#row')
            row.evaluate('(el) => el.dispatchEvent(new MouseEvent("click", {bubbles: true}))')
            assert_selection(row, direct=True)
            big.click()
            assert_selection(big)
            page.get_by_role('button', name='放大', exact=True).click()
            page.wait_for_function('''() => Math.abs(document.querySelector('.text-selection-box').getBoundingClientRect().top - document.querySelector('#big').getBoundingClientRect().top) < 1''')
            assert_selection(big)
            page.keyboard.press('Control+0')
            assert_selection(big)
            handle = selection.locator('[data-scale-direction="se"]').bounding_box()
            page.mouse.move(handle['x'] + handle['width'] / 2, handle['y'] + handle['height'] / 2)
            page.mouse.down()
            page.mouse.move(handle['x'] + 18, handle['y'] + 18)
            page.mouse.up()
            assert float(big.get_attribute('font-size')) > 32
            assert_selection(big)
            big.dblclick()
            editor = page.locator('.editing-proxy')
            editor.fill('改好的标题')
            small.click()
            assert big.text_content() == '改好的标题'
            assert small.text_content() == 'Subtitle'
            small.dblclick()
            editor.fill('新副标题')
            editor.press('Enter')
            page.get_by_role('button', name='导出高清 PNG').click()
            page.wait_for_selector('#dynamic-island-root .di-shell[data-island-state="success"]')
            assert len(previews) == 6 and not errors
            assert '改好的标题' in exports[0] and '新副标题' in exports[0]
            assert 'poster-paper' not in exports[0] and 'translate(' in exports[0]
        finally:
            browser.close()
