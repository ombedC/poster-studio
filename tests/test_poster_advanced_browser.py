from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageChops

import pytest
from app.services.svg_browser import screenshot_svg
from playwright.sync_api import expect

from test_poster_canvas_browser import canvas, center, frame

SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 360">
<defs><linearGradient id="gradient"><stop stop-color="red"/></linearGradient></defs>
<image id="bg" data-layout-role="bg" width="600" height="360"/>
<text id="title" x="90" y="100" font-size="30" style="fill:url(#gradient)">First label</text>
<text id="second" x="145" y="160" font-size="30">Second label</text>
<rect id="swatch" data-layout-role="decoration" x="350" y="100" width="50" height="50" fill="#2277aa"/>
</svg>'''
SCHEMA = {'canvas': {'width': 600, 'height': 360}, 'images': [], 'texts': [
    {'id': 'title', 'kind': 'text', 'text': 'First label'},
    {'id': 'second', 'kind': 'text', 'text': 'Second label'},
]}


def drag(page, start, end):
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=8)
    page.mouse.up()
    frame(page)


def state(page, expression):
    return page.evaluate(f'() => {{ const s = Alpine.$data(document.body); return {expression}; }}')


@pytest.mark.parametrize('canvas', [(SVG, SCHEMA)], indirect=True)
def test_align_panel_single_selection_icons_and_no_overflow(canvas):
    page, _ = canvas
    title = page.locator('#title')
    title.click()
    panel = page.locator('[data-inspector] [data-align-panel]')
    expect(panel).to_be_visible()
    expect(page.locator('[data-inspector]')).to_contain_text('文字属性')
    expect(page.locator('[data-align-basis="selection"]')).to_be_disabled()
    expect(page.locator('[data-align-basis="board"]')).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('[data-align="distribute-x"]')).to_be_disabled()
    expect(page.locator('[data-align]')).to_have_count(8)
    assert page.evaluate('''() => {
        const panel = document.querySelector('[data-align-panel]');
        const box = panel.getBoundingClientRect();
        const card = panel.closest('[data-inspector]').getBoundingClientRect();
        const grid = panel.querySelector('.align-grid');
        return panel.scrollWidth <= panel.clientWidth + 1 &&
            grid.scrollWidth <= grid.clientWidth + 1 &&
            box.left >= card.left && box.right <= card.right;
    }''')
    original = title.bounding_box()
    page.get_by_role('button', name='水平居中', exact=True).click()
    paper = page.locator('.poster-stage > svg').bounding_box()
    box = title.bounding_box()
    assert box['x'] + box['width'] / 2 == pytest.approx(paper['x'] + paper['width'] / 2, abs=1)
    page.keyboard.press('Control+z')
    assert title.bounding_box()['x'] == pytest.approx(original['x'], abs=1)


@pytest.mark.parametrize('canvas', [(SVG, SCHEMA)], indirect=True)
def test_marquee_align_group_drag_and_undo(canvas):
    page, _ = canvas
    a, b = page.locator('#title'), page.locator('#second')
    x, y = center(a)
    bx, by = center(b)
    drag(page, (x - 120, y - 35), (bx + 120, by + 20))
    expect(page.locator('.canvas-group-box')).to_be_visible()
    assert state(page, 's.selection.length') == 2
    assert state(page, "s.alignTo") == 'selection'
    panel = page.locator('[data-inspector] [data-align-panel]')
    expect(panel).to_be_visible()
    expect(page.locator('[data-inspector] h2')).to_have_text('多选操作')
    expect(page.locator('[data-inspector]').get_by_text('已选 2 个')).to_be_visible()
    assert page.locator('.canvas-viewport [data-canvas-command]').count() == 0
    assert page.evaluate('''() => { const g = document.querySelector('.align-grid');
        return g.scrollWidth <= g.clientWidth + 1 && g.getBoundingClientRect().height < 40; }''')
    page.get_by_role('button', name='左对齐', exact=True).click()
    assert a.bounding_box()['x'] == pytest.approx(b.bounding_box()['x'], abs=.5)
    before = [node.bounding_box() for node in (a, b)]
    x, y = center(a)
    assert page.evaluate('([x, y]) => document.elementFromPoint(x, y)?.id', [x, y]) == 'title'
    drag(page, (x, y), (x + 41, y + 31))
    shifts = [node.bounding_box()['x'] - box['x'] for node, box in zip((a, b), before)]
    assert shifts[0] > 25 and shifts[0] == pytest.approx(shifts[1], abs=.5)
    page.keyboard.press('Control+z')
    for node, box in zip((a, b), before):
        assert node.bounding_box()['x'] == pytest.approx(box['x'], abs=.5)
        assert node.bounding_box()['y'] == pytest.approx(box['y'], abs=.5)
    assert state(page, 's.history.every(entry => !entry.html.includes("canvas-marquee"))')


@pytest.mark.parametrize('canvas', [(SVG, SCHEMA)], indirect=True)
def test_alt_shift_copy_edit_delete_and_preview(canvas):
    page, _ = canvas
    a = page.locator('#title')
    x, y = center(a)
    page.keyboard.down('Alt')
    page.keyboard.down('Shift')
    drag(page, (x, y), (x + 230, y + 9))
    page.keyboard.up('Shift')
    page.keyboard.up('Alt')
    duplicate = page.locator('text[data-free-entry]')
    expect(duplicate).to_have_count(1)
    assert duplicate.get_attribute('id') != 'title'
    assert duplicate.bounding_box()['y'] == pytest.approx(a.bounding_box()['y'], abs=.5)
    assert center(a) == pytest.approx((x, y), abs=.5)
    assert 'url(#gradient)' in duplicate.get_attribute('style')
    duplicate.dblclick()
    proxy = page.locator('.editing-proxy')
    proxy.fill('Independent copy')
    proxy.press('Enter')
    assert a.text_content() == 'First label'
    assert state(page, 's.values.title') == 'First label'
    assert state(page, 's.preview()')
    expect(duplicate).to_have_text('Independent copy')
    assert state(page, "s.preview({values:{second:'Updated second'},images:{}})")
    expect(duplicate).to_have_text('Independent copy')
    duplicate.click()
    page.keyboard.press('Delete')
    expect(duplicate).to_have_count(0)
    assert state(page, 's.preview()')
    expect(duplicate).to_have_count(0)
    expect(page.locator('#gradient')).to_have_count(1)
    page.keyboard.press('Control+z')
    expect(duplicate).to_have_text('Independent copy')


@pytest.mark.parametrize('canvas', [(SVG, SCHEMA)], indirect=True)
def test_lock_order_noop_and_color_picker(canvas):
    page, _ = canvas
    a = page.locator('#title')
    x, y = center(a)
    a.click()
    history = state(page, 's.historyIndex')
    page.keyboard.press('Control+Shift+BracketLeft')
    assert state(page, 's.historyIndex') == history
    assert a.evaluate('el => el.previousElementSibling.id') == 'bg'
    page.keyboard.press('Control+BracketRight')
    assert a.evaluate('el => el.previousElementSibling.id') == 'second'
    page.keyboard.press('Control+Shift+BracketLeft')
    assert a.evaluate('el => el.previousElementSibling.id') == 'bg'
    page.keyboard.press('Control+Shift+l')
    expect(a).to_have_attribute('data-locked', '1')
    page.mouse.click(x, y)
    drag(page, (x - 110, y - 29), (x + 110, y + 20))
    assert state(page, 's.selection.length') == 0
    page.get_by_role('button', name='解锁全部').click()
    a.click()
    page.evaluate("() => Object.defineProperty(window, 'EyeDropper', {value: undefined, configurable: true})")
    page.keyboard.press('i')
    page.locator('#swatch').click()
    assert a.evaluate('el => getComputedStyle(el).fill') == 'rgb(34, 119, 170)'
    expect(page.locator('[data-inspector] input[type=color]')).to_have_value('#2277aa')
    page.keyboard.press('Control+z')
    assert a.evaluate('el => getComputedStyle(el).fill').startswith('url(')


IMAGE = 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Crect width=%22100%22 height=%22100%22 fill=%22red%22/%3E%3C/svg%3E'
PORTRAIT = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 360">
<defs><clipPath id="circle"><circle cx="195" cy="160" r="55"/></clipPath></defs>
<rect width="600" height="360" fill="white"/>
<text id="title" x="60" y="55">Poster</text>
<g id="frame" clip-path="url(#circle)"><image id="photo" href="{IMAGE}" x="135" y="100" width="120" height="120"/></g>
<image id="qr" href="{IMAGE}" transform="translate(350 100)" width="80" height="80"/>
</svg>'''
PORTRAIT_SCHEMA = {**SCHEMA, 'texts': [SCHEMA['texts'][0]], 'images': [
    {'id': 'photo', 'key': 'portrait', 'field': 'portrait_frame', 'frame': {'x': 140, 'y': 105, 'width': 110, 'height': 110}},
    {'id': 'qr', 'key': 'qr', 'field': 'qr_frame', 'frame': {'x': 350, 'y': 100, 'width': 80, 'height': 80}},
]}


@pytest.mark.parametrize('canvas', [(PORTRAIT, PORTRAIT_SCHEMA)], indirect=True)
def test_portrait_isolation_commits_once_and_exports_clean_pixels(canvas, tmp_path):
    page, exports = canvas
    photo = page.locator('#photo')
    frame_node = page.locator('#frame')
    clip = page.locator('#circle').evaluate('el => el.outerHTML')
    outer_transform = frame_node.get_attribute('transform')
    before = state(page, 's.historyIndex')
    photo.dblclick()
    surface = page.locator('[data-image-isolation-surface]')
    expect(surface).to_be_visible()
    assert surface.evaluate('el => !el.closest("svg")')
    x, y = center(surface)
    drag(page, (x, y), (x + 30, y + 10))
    assert photo.get_attribute('transform')
    assert state(page, 's.historyIndex') == before
    page.keyboard.press('Escape')
    expect(surface).to_have_count(0)
    assert state(page, 's.historyIndex') == before + 1
    assert frame_node.get_attribute('transform') == outer_transform
    assert page.locator('#circle').evaluate('el => el.outerHTML') == clip
    assert state(page, 's.history.every(e => !e.html.includes("data-image-isolation-surface"))')
    page.get_by_role('button', name='导出高清 PNG').click()
    markup = exports[-1]['svg']
    assert 'data-image-isolation-surface' not in markup and 'moveable-control' not in markup
    assert exports[-1]['scale'] == 2
    svg_path, png_path = tmp_path / 'art.svg', tmp_path / 'art.png'
    svg_path.write_text(markup, encoding='utf-8')
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(screenshot_svg, svg_path, (600, 360), png_path, 2).result(timeout=45) == (1200, 720)
    with Image.open(png_path) as image:
        image = image.convert('RGB')
        assert image.size == (1200, 720)
        assert image.getpixel((1000, 600)) == (255, 255, 255)
        assert image.getpixel((390, 320)) == (255, 0, 0)
    page.keyboard.press('Control+z')
    assert photo.get_attribute('transform') is None
    photo.click()
    handle = page.locator('.text-selection-box [data-scale-direction=se]')
    x, y = center(handle)
    drag(page, (x, y), (x + 25, y + 25))
    assert frame_node.get_attribute('transform')
    assert photo.get_attribute('transform') is None


@pytest.mark.parametrize('canvas', [(SVG, SCHEMA)], indirect=True)
def test_advanced_shortcuts_respect_focus_and_dialog(canvas):
    page, _ = canvas
    title = page.locator('#title')
    title.click()
    field = page.locator('textarea:visible').first
    field.focus()
    expect(field).to_be_focused()
    page.keyboard.press('Delete')
    page.keyboard.press('Control+Shift+l')
    assert title.get_attribute('data-locked') is None
    expect(title).to_have_count(1)
    page.get_by_role('button', name='API 设置', exact=True).click()
    page.keyboard.press('Delete')
    expect(title).to_have_count(1)
    assert title.get_attribute('data-locked') is None


@pytest.mark.parametrize('canvas', [(PORTRAIT, PORTRAIT_SCHEMA)], indirect=True)
def test_file_drop_blocks_navigation_and_preserves_qr_geometry(canvas):
    page, _ = canvas
    qr = page.locator('#qr')
    before = qr.get_attribute('transform')
    def dispatch(target, event):
        return target.evaluate('''(el, event) => {
            const dt = new DataTransfer();
            dt.items.add(new File(['image'], 'drop.png', {type:'image/png'}));
            return !el.dispatchEvent(new DragEvent(event, {dataTransfer:dt, bubbles:true, cancelable:true}));
        }''', event)
    assert dispatch(page.locator('#title'), 'drop')
    expect(page.get_by_text('请将图片拖到头像或二维码上', exact=True)).to_be_visible()
    assert dispatch(qr, 'dragover')
    expect(page.locator('[data-image-drop-outline]')).to_be_visible()
    assert dispatch(qr, 'drop')
    expect(qr).to_have_attribute('href', 'http://poster.test/replacement.png')
    expect(qr).to_have_attribute('preserveAspectRatio', 'xMidYMid meet')
    assert qr.get_attribute('transform') == before
    expect(page.locator('[data-image-drop-outline]')).to_be_hidden()
