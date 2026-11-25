from lxml import etree
import pytest

from app.api import posters
from app.errors import BrowserUnavailable
from app.services import template_analyzer as analyzer, template_renderer as renderer
from app.services import svg_browser


SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 360">
<rect x="30" y="25" width="540" height="80" rx="12"/>
<rect x="90" y="120" width="300" height="54" rx="25"/>
<text id="topic" font-size="32" x="95" y="80"><tspan id="big">数学 | 寻根</tspan><tspan id="small" x="280" font-size="20">——专题</tspan></text>
<text id="time" x="145" y="152" font-size="20">本周六晚8点</text>
</svg>'''.encode()


def test_layout_preserves_siblings_and_centers():
    schema = analyzer.analyze_template(SVG, use_browser=False, use_llm=False, force=True)
    rendered, _ = renderer.build_svg(schema, {'big': '数学 | 新课', 'time': '20:00'})
    root = etree.fromstring(rendered.encode())
    topic, big, small, time = (root.xpath(f'//*[@id="{key}"]')[0] for key in ('topic', 'big', 'small', 'time'))
    assert topic.get('x') == '300'
    assert topic.get('text-anchor') == 'middle'
    assert big.text == '数学 | 新课'
    assert small.text == '——专题'
    assert small.get('font-size') == '20'
    assert small.get('x') is None and small.get('dx') == '12'
    assert time.text == '20:00'
    assert time.get('x') == '240' and time.get('text-anchor') == 'middle'


def test_explicit_empty_clears_only_target():
    root = analyzer._parse_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><text><tspan id="a">old</tspan><tspan id="b">keep</tspan></text></svg>')
    schema = {'texts': [{'id': 'a', 'kind': 'tspan', 'role': 'teacher_bio_3'}]}
    renderer._apply_text_values(root, schema, {'a': ''})
    assert root.xpath('//*[@id="a"]')[0].text is None
    assert root.xpath('//*[@id="b"]')[0].text == 'keep'


def test_real_browser_measures_transformed_clip_and_exports(tmp_path):
    from PIL import Image

    source = tmp_path / 'clip.svg'
    source.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">
        <defs><clipPath id="clip" transform="translate(10 5)"><circle cx="50" cy="50" r="20"/><circle cx="75" cy="50" r="20"/></clipPath></defs>
        <g transform="translate(100 50) scale(2)" clip-path="url(#clip)"><image id="avatar" width="300" height="250" transform="matrix(3 0 0 3 0 0)"/></g>
    </svg>''', encoding='utf-8')
    try:
        result = svg_browser.measure_svg(source, (400, 300), ['avatar'])
    except BrowserUnavailable as exc:
        pytest.skip(str(exc))
    box = result['avatar']['clip_boxes']
    assert box['canvas'] == [{'x': 180, 'y': 120, 'w': 130, 'h': 80}]
    assert box['local'] == [{'x': 40, 'y': 35, 'w': 65, 'h': 40}]
    output = tmp_path / 'poster.png'
    assert svg_browser.screenshot_svg(source, (400, 300), output, scale=2) == (800, 600)
    with Image.open(output) as png:
        assert png.size == (800, 600)


def test_group_font_fit_in_playwright_export(tmp_path):
    source = tmp_path / 'font-fit.svg'
    source.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 300">
      <text id="a" x="20" y="60" font-size="43" font-family="monospace">short
        <tspan id="b" x="20" y="120">long long long long long long</tspan>
        <tspan id="c" x="20" y="180">locked</tspan>
      </text>
      <text id="tolerance" x="20" y="240" font-size="43" font-family="monospace">mmmm</text>
    </svg>''', encoding='utf-8')
    entries = [
        {'id': id_, 'text': 'old', 'font_fit_group': 'teacher_bio:0', 'base_font_size': 43,
         'max_width': 400, 'targets': [{'id': id_, 'preserve_children': id_ == 'a'}]}
        for id_ in ('a', 'b', 'c')
    ]
    entries.append({'id': 'tolerance', 'text': 'old', 'base_font_size': 43, 'max_width': 101})
    schema = {'texts': entries}
    assert renderer._font_fit_entries(schema, {}) == []
    selected = renderer._font_fit_entries(schema, {'b': 'changed', 'tolerance': 'mmmm'})
    requests = renderer._font_fit_requests(selected, {'c': {'manual_font_size': 55}})
    out = tmp_path / 'poster.png'
    try:
        fits = svg_browser.fit_and_export(source, (600, 300), requests, out_png=out)
    except BrowserUnavailable as exc:
        pytest.skip(str(exc))
    assert fits['a']['font_size'] == fits['b']['font_size'] < 43
    assert fits['b']['length'] <= 412 and 'c' not in fits
    assert fits['tolerance']['font_size'] == 43
    root = etree.parse(str(source)).getroot()
    renderer.apply_text_styles(root, schema, {'c': {'manual_font_size': True, 'font_size': 55}})
    renderer._apply_font_sizes(root, selected, fits)
    assert root.xpath('//*[@id="a"]')[0].get('font-size') == root.xpath('//*[@id="b"]')[0].get('font-size')
    assert root.xpath('//*[@id="c"]')[0].get('font-size') == '55'
    assert out.is_file()


def test_avatar_matching_is_exact_per_teacher_and_mode(monkeypatch):
    monkeypatch.setattr(posters.asset_store, 'list_assets', lambda *args, **kwargs: [
        {'id': '11111111', 'label': '台商一中 曾芳芳.png', 'mode': 'framed_circle'},
        {'id': '22222222', 'label': '曾芳芳.png', 'mode': 'head_shoulder'},
        {'id': '33333333', 'label': '欧阳娜娜.png', 'mode': 'head_shoulder'},
    ])
    schema = {
        'texts': [{'id': 'a', 'role': 'teacher_name', 'teacher_slot': 0}, {'id': 'b', 'role': 'teacher_name', 'teacher_slot': 1}],
        'portraits': [{'slot': 0, 'key': 'portrait_1', 'mode': 'framed_circle'}, {'slot': 1, 'key': 'portrait_2'}],
    }
    images, unmatched = posters._match_teacher_assets(schema, {'a': '台商一中 曾芳芳', 'b': '王小明'})
    assert images == {'portrait_1': '/api/v1/posters/assets/file/11111111'}
    assert unmatched == ['portrait_2']
