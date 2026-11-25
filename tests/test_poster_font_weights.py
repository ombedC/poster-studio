"""Regression coverage for SVG font aliases and newly revived text."""
from lxml import etree
import pytest

from app.services import svg_sanitizer, text_revival


def _sanitize(fragment):
    raw = f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">{fragment}</svg>'
    return etree.fromstring(svg_sanitizer.sanitize_svg(raw.encode())[0])


@pytest.mark.parametrize(('family', 'expected'), [
    ('AlibabaPuHuiTiR', '400'),
    ('AlibabaPuHuiTiM GBpc-EUC-H', '500'),
    ('AlibabaPuHuiTiB-GBpc-EUC-H', '700'),
    ('AlibabaPuHuiTi_3_105_Heavy-GBpc-EUC-H', '800'),
    ('AlibabaPuHuiTiBL', '900'),
])
def test_font_suffix_wins_over_attribute_and_inline_weight(family, expected):
    root = _sanitize(f'<text font-family="{family}" font-weight="600" style="font-weight:200 !important;fill:red">sample</text>')
    assert root[0].get('font-family') == "'Alibaba PuHuiTi 3.0'"
    assert root[0].get('font-weight') == expected
    assert root[0].get('style') == f'fill:red;font-weight:{expected} !important'


def test_stylesheet_and_inline_font_declarations():
    root = _sanitize('''<style>.legacy{fill:red;font-family:AlibabaPuHuiTiH;font-weight:400}
        .conflict{font-weight:900 !important}
        @font-face{font-family:Original;src:url(example.ttf);font-weight:400}</style>
        <text class="conflict" font-family="AlibabaPuHuiTiR" style="font-family:serif;font-weight:900">sample</text>''')
    assert ".legacy{fill:red;font-family:'Alibaba PuHuiTi 3.0' !important;font-weight:800 !important}" in root[0].text
    assert '.conflict{font-weight:900 !important}' in root[0].text
    assert '@font-face{font-family:Original;src:url(example.ttf);font-weight:400}' in root[0].text
    assert root[1].get('font-weight') == '400'
    assert root[1].get('style').endswith('font-weight:400 !important')


def test_already_normalized_template_keeps_explicit_weights_and_sizes():
    root = _sanitize('''<text font-family="'Alibaba PuHuiTi 3.0'" font-weight="400" font-size="72">Title</text>
        <text font-size="43" style="font-family:'Alibaba PuHuiTi 3.0';font-weight:500">Honor</text>''')
    assert root[0].get('font-weight') == '400'
    assert root[0].get('font-size') == '72'
    assert root[1].get('font-size') == '43'
    assert root[1].get('style').endswith('font-weight:500')


def test_unsuffixed_font_uses_browser_weight_cascade():
    from playwright.sync_api import sync_playwright
    from app.errors import BrowserUnavailable
    from app.services.svg_browser import _launch

    raw = b'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">
        <style>
            .family {font-family:'Alibaba PuHuiTi 3.0';font-size:80px}
            .weighted {font-weight:bold}
            .forced {font-family:AlibabaPuHuiTiH;font-weight:700}
            .override {font-weight:900 !important}
        </style>
        <g class="weighted">
            <text id="inherited" class="family">sample</text>
            <text id="nested"><tspan id="child" font-family="Alibaba PuHuiTi 3.0">sample</tspan></text>
            <text id="explicit" font-family="Alibaba PuHuiTi 3.0" font-weight="600" style="font-weight:inherit">sample</text>
        </g>
        <text id="class" class="family weighted" style="font-family:'Alibaba PuHuiTi 3.0'">sample</text>
        <text id="default" class="family">sample</text>
        <text id="forced" class="forced" style="font-weight:400">sample</text>
        <text id="attribute" class="override" font-family="AlibabaPuHuiTiB">sample</text>
    </svg>'''
    cleaned, _ = svg_sanitizer.sanitize_svg(raw)
    root = etree.fromstring(cleaned)
    assert root.xpath('//*[@id="child"]')[0].get('font-weight') is None
    assert 'font-weight:700' in root[0].text
    with sync_playwright() as playwright:
        try:
            browser = _launch(playwright)
        except BrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = browser.new_page()
            page.set_content(etree.tostring(root, encoding='unicode'))
            weights = page.locator('text[id], tspan[id]').evaluate_all(
                'nodes => Object.fromEntries(nodes.map(node => [node.id, getComputedStyle(node).fontWeight]))'
            )
        finally:
            browser.close()
    assert weights == {
        'inherited': '700', 'nested': '700', 'child': '700', 'explicit': '700',
        'class': '700', 'default': '400', 'forced': '800', 'attribute': '700',
    }


@pytest.mark.parametrize(('height', 'expected'), [(47.99, 500), (48, 700), (54.99, 700), (55, 800), (80, 800)])
def test_newly_revived_text_weight_boundary(height, expected):
    root = etree.Element('svg')
    text = text_revival._make_text(root, '#fff', (10, 10, 100, height), 'sample', 1, (400, 300))
    assert text.get('font-weight') == str(expected)
    assert text.get('data-revived') == '1'
