"""Viewport, document geometry and export through the actual browser UI."""
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest
from lxml import etree
from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright

from app.services.svg_browser import _launch, screenshot_svg
from test_poster_workbench_browser import STATIC

SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="10 20 600 360">
<rect data-layout-role="bg" x="10" y="20" width="600" height="360" fill="#eee"/>
<text id="title" x="200" y="165" font-size="24">Poster<tspan x="202" dy="30">第二行</tspan><tspan x="202" dy="1.25em">第三行</tspan></text>
<image id="photo" x="350" y="150" width="75" height="60" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1 1'%3E%3Crect width='1' height='1' fill='red'/%3E%3C/svg%3E"/>
<rect id="guide" data-decompose-layer="2" x="280" y="235" width="40" height="26" fill="#094"/>
</svg>'''
SCHEMA = {'canvas': {'width': 600, 'height': 360},
          'images': [{'id': 'photo', 'key': 'photo', 'field': 'qr_frame'}],
          'texts': [{'id': 'title', 'kind': 'text', 'text': 'Poster'}]}


@pytest.fixture
def canvas(request):
    svg, schema = getattr(request, 'param', (SVG, SCHEMA))
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_page(viewport={'width': 1550, 'height': 950})
            page.set_default_timeout(6500)
            page.set_default_navigation_timeout(10000)
            errors, exports = [], []
            page.on('pageerror', lambda err: errors.append(str(err)))

            def handle(route):
                path = urlsplit(route.request.url).path
                if path.startswith('/static/'):
                    route.fulfill(path=STATIC / path.removeprefix('/static/'))
                elif path.endswith('/preview'):
                    body = svg(route.request.post_data_json) if callable(svg) else svg
                    route.fulfill(body=body, content_type='image/svg+xml')
                elif path.endswith('/assets/upload'):
                    route.fulfill(json={'asset': {'url': '/replacement.png'}, 'warnings': []})
                elif path.endswith('/export-svg'):
                    exports.append(route.request.post_data_json)
                    route.fulfill(json={'url': '/blank', 'width': 1200, 'height': 720})
                elif path.endswith('/templates'):
                    route.fulfill(json=[{'name': 'demo', 'analyzed': True}])
                elif path.endswith('/schema'):
                    route.fulfill(json=schema)
                else:
                    route.fulfill(json=[] if path.endswith('/assets') else {})

            page.route('http://poster.test/**', handle)
            page.goto('http://poster.test/static/poster.html')
            page.locator('.poster-stage #title').wait_for()
            expect(page.locator('[data-canvas-tools]')).to_be_visible()
            yield page, exports
            assert not errors
        finally:
            browser.close()


def center(locator):
    box = locator.bounding_box()
    return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2


def frame(page):
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def assert_aligned(node, box):
    expected = node.bounding_box()
    assert center(box.locator('.moveable-control.moveable-nw')) == pytest.approx((expected['x'], expected['y']), abs=2)
    assert center(box.locator('.moveable-control.moveable-se')) == pytest.approx((expected['x'] + expected['width'], expected['y'] + expected['height']), abs=2)


@pytest.mark.parametrize('canvas', [(
    '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 360">
    <g transform="translate(35 20) scale(.8)">
      <text id="title" x="30" y="105" font-size="26">福建省名优骨干教师点拨学科重难点</text>
      <text x="30" y="180" font-size="26"><tspan id="line">福建省名优骨干教师点拨学科重难点</tspan></text>
    </g></svg>''',
    {'canvas': {'width': 600, 'height': 360}, 'images': [], 'texts': [
        {'id': field, 'kind': kind, 'text': '福建省名优骨干教师点拨学科重难点',
         'base_font_size': 26, 'max_width': 265, 'font_fit_group': 'teacher_bio:0'}
        for field, kind in [('title', 'text'), ('line', 'tspan')]
    ]},
)], indirect=True)
def test_inline_long_single_line_commit_preserves_font_size(canvas):
    page, _ = canvas
    page.keyboard.press('Control+1')
    page.get_by_role('button', name='放大', exact=True).click()
    proxy = page.locator('.editing-proxy')

    def typography(node):
        return node.evaluate('el => [getComputedStyle(el).fontSize, el.getAttribute("font-size"), el.style.fontSize]')

    nodes = [page.locator('#title'), page.locator('#line')]
    before = [typography(node) for node in nodes]
    for node, key in zip(nodes, ('Enter', 'Escape')):
        node.dblclick()
        value = '福建省名优骨干教师精准点拨学科重难点'
        proxy.fill(value)
        proxy.press(key)
        expect(proxy).to_be_hidden()
        expect(node).to_have_text(value)
        area = page.locator('.poster-paper').bounding_box()
        rect = node.bounding_box()
        assert rect['x'] + rect['width'] < area['x'] + area['width']
        assert [typography(item) for item in nodes] == before

    page.locator('[data-inspector] input[type=number]').fill('28')
    page.locator('[data-inspector] input[type=number]').press('Tab')
    line = nodes[1]
    manual = typography(line)
    assert manual == ['28px', '28', '28px']
    line.dblclick()
    proxy.fill('福建省名优骨干教师专题点拨学科重难点')
    proxy.press('Escape')
    assert typography(line) == manual
    assert page.evaluate('() => Alpine.$data(document.body).manualFontSizes.line') == 28


INLINE_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 360">
  <g id="inline-transform" transform="matrix(.8 0 0 .8 35 20)">
    <text id="title" x="100" y="110" font-size="12"><tspan id="outer"><tspan id="large" font-size="38" style="font-size: 40px; fill: #123456">福建省名优骨干教师</tspan></tspan></text>
    <text id="multiline" x="100" y="190" font-size="25"><tspan x="100">ABCDEFGHIJ</tspan><tspan x="100" dy="36">KLMNOPQRST</tspan></text>
  </g></svg>'''
INLINE_SCHEMA = {'canvas': {'width': 600, 'height': 360}, 'images': [], 'texts': [
    {'id': 'wrapped', 'kind': 'tspan', 'text': '福建省名优骨干教师', 'base_font_size': 12,
     'targets': [{'id': 'outer', 'kind': 'tspan', 'member_ids': ['outer', 'large']}]},
    {'id': 'multiline', 'kind': 'text', 'text': 'ABCDEFGHIJ\nKLMNOPQRST'},
]}


@pytest.mark.parametrize('canvas', [(INLINE_SVG, INLINE_SCHEMA)], indirect=True)
def test_inline_preserves_nested_styles_and_height_on_commit(canvas):
    page, _ = canvas
    page.keyboard.press('Control+1')
    page.get_by_role('button', name='放大', exact=True).click()
    title = page.locator('#title')
    proxy = page.locator('.editing-proxy')

    def snapshot():
        return title.evaluate('''el => {
            const child = el.querySelector('#large');
            const studio = Alpine.$data(document.body);
            return {parentSize: el.getAttribute('font-size'), parentStyle: el.getAttribute('style'),
                parentTransform: el.getAttribute('transform'), ancestor: el.parentElement.getAttribute('transform'),
                childSize: child?.getAttribute('font-size'), childStyle: child?.getAttribute('style'),
                computed: getComputedStyle(child || el).fontSize, height: el.getBoundingClientRect().height,
                manual: {...studio.manualFontSizes}, value: studio.values.wrapped};
        }''')

    for scale in (.5, 1, 2):
        page.evaluate('(scale) => Alpine.$data(document.body).centerView(scale)', scale)
        frame(page)
        before = snapshot()
        for key, value in [('Enter', None), ('Enter', '福建名师'), ('Escape', '骨干教师')]:
            title.dblclick()
            if value is not None:
                proxy.fill(value)
            proxy.press(key)
            expect(proxy).to_be_hidden()
            expected_value = value or before['value']
            assert snapshot() == {**before, 'value': expected_value}
            expect(page.locator('#large')).to_have_text(expected_value)


@pytest.mark.parametrize('canvas', [(INLINE_SVG, INLINE_SCHEMA)], indirect=True)
def test_inline_double_click_and_drag_place_caret_on_visual_line(canvas):
    page, _ = canvas
    page.keyboard.press('Control+1')
    page.get_by_role('button', name='放大', exact=True).click()
    node = page.locator('#multiline tspan').nth(1)
    proxy = page.locator('.editing-proxy')

    def point(index, fraction):
        return node.evaluate('''(el, [index, fraction]) => {
            const b = el.getExtentOfChar(index);
            const p = new DOMPoint(b.x + fraction * b.width, b.y + b.height / 2).matrixTransform(el.getScreenCTM());
            return [p.x, p.y];
        }''', [index, fraction])

    page.mouse.dblclick(*point(4, .25))
    expect(proxy).to_have_value('ABCDEFGHIJ\nKLMNOPQRST')
    assert proxy.evaluate('el => [el.selectionStart, el.selectionEnd]') == [15, 15]
    page.mouse.click(*point(6, .75))
    assert proxy.evaluate('el => [el.selectionStart, el.selectionEnd]') == [18, 18]
    page.mouse.move(*point(1, .25))
    page.mouse.down()
    page.mouse.move(*point(5, .75), steps=5)
    page.mouse.up()
    assert proxy.evaluate('el => [el.selectionStart, el.selectionEnd]') == [12, 17]
    proxy.fill('ABCD\nWXYZ')
    proxy.press('Escape')
    expect(page.locator('#multiline tspan').first).to_have_text('ABCD')
    expect(node).to_have_text('WXYZ')
    expect(node).to_have_attribute('dy', '36')


@pytest.mark.parametrize('canvas', [(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1242 2243"><text id="title" x="80" y="150">Tall poster</text></svg>',
    {'canvas': {'width': 1241.95, 'height': 2201.3}, 'images': [], 'texts': []},
)], indirect=True)
def test_tall_template_fits_on_first_mount(canvas):
    page, _ = canvas
    def measure():
        return page.locator('.poster-stage svg').evaluate('''svg => {
            const rect = svg.getBoundingClientRect();
            const area = document.querySelector('.canvas-viewport').getBoundingClientRect();
            return {width: rect.width, height: rect.height, x: rect.left, y: rect.top,
                    zoom: +document.querySelector('.canvas-viewport output').value.slice(0, -1) / 100,
                    margins: [rect.left - area.left, area.right - rect.right, rect.top - area.top, area.bottom - rect.bottom]};
        }''')
    first = measure()
    assert all(margin >= 31 for margin in first['margins']), first
    assert first['width'] == pytest.approx(1242 * first['zoom'], abs=2)
    assert first['height'] == pytest.approx(2243 * first['zoom'], abs=2)
    assert first['margins'][0] == pytest.approx(first['margins'][1], abs=1)
    page.keyboard.press('Control+0')
    frame(page)
    assert measure() == first


WHITESPACE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 360"><text id="title" x="120" y="150" font-size="32" xml:space="preserve"><tspan id="spaces">  </tspan><tspan>老师</tspan></text></svg>'
WHITESPACE_SCHEMA = {'canvas': {'width': 600, 'height': 360}, 'images': [],
                     'texts': [{'id': 'title', 'kind': 'text', 'text': '老师'}]}


def render_whitespace_preview(payload):
    from app.services.template_renderer import _apply_text_values

    root = etree.fromstring(WHITESPACE_SVG.encode())
    _apply_text_values(root, WHITESPACE_SCHEMA, payload['data'], frozenset(payload.get('exact_text_ids', [])))
    return etree.tostring(root, encoding='unicode')


@pytest.mark.parametrize('canvas', [(render_whitespace_preview, WHITESPACE_SCHEMA)], indirect=True)
def test_inline_whitespace_survives_deselection_preview_and_export(canvas):
    page, exports = canvas
    title = page.locator('.poster-stage #title')
    proxy = page.locator('.editing-proxy')
    title.dblclick()
    expect(proxy).to_have_value('  老师')
    proxy.fill('老师')
    proxy.press('Enter')
    assert title.text_content() == '老师'
    page.keyboard.press('Control+z')
    assert title.text_content() == '  老师'
    page.keyboard.press('Control+Shift+z')
    assert title.text_content() == '老师'
    for key, value in [('Escape', ' 老师'), ('Enter', '\u00a0老师\u3000'), ('Escape', '老师')]:
        title.dblclick()
        proxy.fill(value)
        proxy.press(key)
        page.locator('.canvas-viewport').click(position={'x': 10, 'y': 10})
        expect(page.locator('.text-selection-box')).to_be_hidden()
        assert title.text_content() == value
        assert page.evaluate('() => Alpine.$data(document.body).values.title') == value
        if value.startswith(' '):
            assert title.evaluate('el => el.getNumberOfChars()') == 3
        assert page.evaluate('() => Alpine.$data(document.body).preview()')
        assert title.text_content() == value
        if value.startswith(' '):
            assert title.evaluate('el => el.getNumberOfChars()') == 3
        page.get_by_role('button', name='导出高清 PNG').click()
        saved = etree.fromstring(exports[-1]['svg'].encode())
        assert ''.join(saved.xpath('//*[@id="title"]')[0].itertext()) == value


def test_viewport_anchor_pan_reset_and_selection(canvas):
    page, _ = canvas
    photo = page.locator('#photo')
    photo.click()
    box = page.locator('.moveable-control-box')
    page.keyboard.press('Control+1')
    expect(page.locator('[data-canvas-tools] output')).to_have_text('100%')
    frame(page)
    assert_aligned(photo, box)
    x, y = center(photo)
    for modifier in ('Alt', 'Control'):
        page.mouse.move(x, y)
        page.keyboard.down(modifier)
        page.mouse.wheel(0, -90)
        page.keyboard.up(modifier)
        frame(page)
        assert center(photo) == pytest.approx((x, y), abs=1)
        assert_aligned(photo, box)
    page.mouse.down(button='middle')
    page.mouse.move(x + 32, y - 19, steps=4)
    page.mouse.up(button='middle')
    frame(page)
    assert center(photo) == pytest.approx((x + 32, y - 19), abs=1)
    assert_aligned(photo, box)
    page.keyboard.press('Control+0')
    frame(page)
    assert_aligned(photo, box)
    assert center(page.locator('.poster-paper')) == pytest.approx(center(page.locator('.canvas-viewport')), abs=1)
    page.keyboard.press('Control+1')
    page.keyboard.press('Control+u')
    frame(page)
    x, y = center(photo)
    page.mouse.move(x, y)
    page.keyboard.down('Control')
    page.mouse.wheel(0, -347)
    page.keyboard.up('Control')
    page.wait_for_function("Math.abs(parseFloat(document.querySelector('[data-canvas-tools] output').value) - 200) < .3")
    start = root_x(photo)
    page.mouse.down()
    page.mouse.move(x - 10, y, steps=3)
    page.mouse.up()
    frame(page)
    assert center(photo)[0] == pytest.approx(x - 10, abs=1)
    assert root_x(photo) == pytest.approx(start - 5, abs=.1)
    assert_aligned(photo, box)


def root_x(node):
    return node.evaluate('el => {const b=el.getBoundingClientRect(); return new DOMPoint(b.left,b.top).matrixTransform(el.ownerSVGElement.getScreenCTM().inverse()).x}')


def test_nudge_history_focus_and_escape(canvas):
    page, _ = canvas
    title = page.locator('#title')
    title.click()
    page.keyboard.press('Control+1')
    frame(page)
    start = root_x(title)
    page.keyboard.press('ArrowRight')
    assert root_x(title) == pytest.approx(start + 1, abs=.05)
    page.keyboard.press('Shift+ArrowRight')
    assert root_x(title) == pytest.approx(start + 11, abs=.05)
    page.keyboard.press('Control+z')
    assert root_x(title) == pytest.approx(start, abs=.05)
    page.keyboard.press('Control+y')
    assert root_x(title) == pytest.approx(start + 11, abs=.05)
    page.keyboard.press('Control+z')
    page.keyboard.press('Control+Shift+z')
    assert root_x(title) == pytest.approx(start + 11, abs=.05)
    title.click()
    before = root_x(title)
    input_box = page.locator('[data-inspector] input[type=number]').first
    input_box.click()
    expect(input_box).to_be_focused()
    page.keyboard.press('ArrowRight')
    assert root_x(title) == before
    page.keyboard.press('Escape')
    expect(input_box).not_to_be_focused()
    expect(page.locator('.text-selection-box')).to_be_visible()
    page.evaluate('''() => { const el = document.createElement('div'); el.contentEditable = 'true';
                      el.innerHTML = '<span>typing</span>'; document.body.append(el); el.focus(); }''')
    page.keyboard.press('ArrowRight')
    assert root_x(title) == before
    # Edit a simple label to exercise the normal proxy Esc and keyboard handoff.
    title.dblclick()
    proxy = page.locator('.editing-proxy')
    proxy.fill('Edited')
    proxy.press('Escape')
    expect(proxy).to_be_hidden()
    expect(title).to_contain_text('Edited')
    before = root_x(title)
    page.keyboard.press('ArrowRight')
    assert root_x(title) == pytest.approx(before + 1, abs=.05)
    page.keyboard.press('Escape')
    expect(page.locator('.text-selection-box')).to_be_hidden()
    photo = page.locator('#photo')
    photo.click()
    before = root_x(photo)
    page.get_by_role('button', name='→', exact=True).click()
    assert root_x(photo) == pytest.approx(before + 2, abs=.05)


def test_text_corner_real_font_and_dy(canvas):
    page, _ = canvas
    page.keyboard.press('Control+1')
    title = page.locator('#title')
    title.click()
    page.keyboard.press('Control+u')
    handle = page.locator('[data-scale-direction=se]')
    x, y = center(handle)
    before = title.bounding_box()
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 28, y + 24, steps=5)
    expect(page.locator('.canvas-hud')).to_contain_text('字号')
    page.mouse.up()
    expect(page.locator('.canvas-hud')).to_be_hidden()
    size = float(title.get_attribute('font-size'))
    assert size > 24
    assert float(title.locator('tspan').first.get_attribute('dy')) == pytest.approx(30 * size / 24, abs=.02)
    assert title.locator('tspan').last.get_attribute('dy') == '1.25em'
    assert 'scale(' not in (title.get_attribute('transform') or '')
    assert title.bounding_box()['x'] == pytest.approx(before['x'], abs=1)
    assert title.bounding_box()['y'] == pytest.approx(before['y'], abs=1)
    expect(page.locator('[data-inspector] input[type=number]')).to_have_value(str(size).rstrip('0').rstrip('.'))
    page.keyboard.press('Control+z')
    assert title.get_attribute('font-size') == '24'
    assert title.locator('tspan').first.get_attribute('dy') == '30'


PASTEBOARD_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200" overflow="visible">
  <rect width="300" height="200" fill="white"/>
  <text id="title" x="180" y="95" fill="red" font-size="30">Poster</text>
</svg>'''
PASTEBOARD_SCHEMA = {'canvas': {'width': 300, 'height': 200}, 'images': [],
                     'texts': [{'id': 'title', 'kind': 'text', 'text': 'Poster'}]}


@pytest.mark.parametrize('canvas', [(PASTEBOARD_SVG, PASTEBOARD_SCHEMA)], indirect=True)
def test_pasteboard_hit_testing_trim_bounds_and_png(canvas, tmp_path):
    page, exports = canvas
    page.keyboard.press('Control+1')
    page.keyboard.press('Control+u')
    title = page.locator('#title')
    paper = page.locator('.poster-stage > svg')
    shade = page.locator('.pasteboard-shade')
    trim = page.get_by_role('button', name='裁切视图')
    frame(page)
    # The inspector floats over the canvas's right edge, so pan left to keep the
    # far-right pasteboard band (where this drag clamps) reachable on screen.
    page.keyboard.down('Space')
    page.mouse.move(1000, 700)
    page.mouse.down()
    page.mouse.move(700, 700, steps=6)
    page.mouse.up()
    page.keyboard.up('Space')
    frame(page)
    initial = title.bounding_box()
    board = paper.bounding_box()

    def drag(start, dx):
        page.mouse.move(*start)
        page.mouse.down()
        page.mouse.move(start[0] + dx, start[1], steps=8)
        page.mouse.up()
        frame(page)

    def hit(point):
        return page.evaluate('([x, y]) => document.elementFromPoint(x, y)?.id', point)

    origin = center(title)
    expect(shade).to_be_visible()
    expect(trim).to_have_attribute('aria-pressed', 'false')
    drag(origin, 430)
    outside = title.bounding_box()
    assert outside['x'] > board['x'] + board['width']
    assert outside['x'] + outside['width'] == pytest.approx(board['x'] + 2 * board['width'], abs=1)
    outside_point = center(title)
    assert hit(outside_point) == 'title'
    page.keyboard.press('Escape')
    page.mouse.move(*outside_point)
    expect(page.locator('.canvas-hover')).to_be_visible()
    page.mouse.click(*outside_point)
    expect(page.locator('.text-selection-box')).to_be_visible()
    trim.click()
    expect(trim).to_have_attribute('aria-pressed', 'true')
    expect(shade).to_be_hidden()
    assert hit(outside_point) != 'title'
    trim.click()
    assert hit(outside_point) == 'title'

    page.get_by_role('button', name='导出高清 PNG').click()
    markup = exports[-1]['svg']
    exported = etree.fromstring(markup.encode())
    assert exported.get('viewBox') == '0 0 300 200'
    assert exported.get('overflow') == 'hidden'
    assert 'pasteboard-shade' not in markup and 'artboard-edge' not in markup
    svg_path, png_path = tmp_path / 'pasteboard.svg', tmp_path / 'pasteboard.png'
    svg_path.write_text(markup, encoding='utf-8')
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(screenshot_svg, svg_path, (300, 200), png_path, 2).result(timeout=45) == (600, 400)
    with Image.open(png_path) as png:
        assert png.size == (600, 400)
        assert png.convert('RGB').getextrema() == ((255, 255),) * 3

    drag(outside_point, initial['x'] - outside['x'])
    assert title.bounding_box()['x'] == pytest.approx(initial['x'], abs=1)
    assert hit(origin) == 'title'


def test_guides_hover_and_png_clean(canvas, tmp_path):
    page, exports = canvas
    page.keyboard.press('Control+1')
    guide = page.locator('#guide')
    guide.hover()
    expect(page.locator('.canvas-hover')).to_be_visible()
    background = page.locator('[data-layout-role=bg]')
    background.hover(position={'x': 30, 'y': 30})
    expect(page.locator('.canvas-hover')).to_be_hidden()
    photo = page.locator('#photo')
    x, y = center(photo)
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 27, y + 21, steps=5)
    expect(page.locator('.canvas-hud')).to_contain_text('X')
    expect(page.locator('.canvas-guide').first).to_be_visible()
    page.mouse.up()
    expect(page.locator('.canvas-hud')).to_be_hidden()
    assert page.locator('.canvas-guide').count() == 0
    assert root_x(photo) == pytest.approx(320, abs=.1)
    for show in (True, False):
        if show:
            guide.hover()
            expect(page.locator('.canvas-hover')).to_be_visible()
        else:
            page.keyboard.press('Escape')
        page.evaluate('() => Alpine.$data(document.body).exportPng()')
        markup = exports[-1]['svg']
        assert all(token not in markup for token in ('canvas-guide', 'canvas-hud', 'canvas-hover', 'poster-paper', 'text-selected', 'data-entry-id'))
    images = []
    for idx, payload in enumerate(exports):
        svg = tmp_path / f'{idx}.svg'
        png = tmp_path / f'{idx}.png'
        svg.write_text(payload['svg'], encoding='utf-8')
        assert payload['scale'] == 2
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(screenshot_svg, svg, (600, 360), png, 2).result(timeout=45) == (1200, 720)
        with Image.open(png) as image:
            assert image.size == (1200, 720)
            images.append(image.convert('RGB'))
    assert len(images) == 2
    assert ImageChops.difference(*images).getbbox() is None
