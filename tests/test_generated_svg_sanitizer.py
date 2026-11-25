from lxml import etree
import pytest

from app.errors import TemplateInvalid
from app.services.svg_sanitizer import sanitize_generated_svg


def test_generated_svg_preserves_vector_art_and_scopes_styles():
    original = b'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="200" onload="bad()" x-init="bad()">
      <style>@import 'https://example.test/a.css'; .shape, text {fill:url(#paint); stroke:url(https://example.test/paint);}</style>
      <defs><linearGradient id="paint"><stop offset="0" stop-color="red"/></linearGradient></defs>
      <script>bad()</script><foreignObject/><image href="https://example.test/raster"/>
      <path class="shape" d="M0 0L10 10"/>
      <text x="10" y="20">Editable</text>
      <use href="#paint"/><use href="https://example.test/a.svg#paint"/>
    </svg>'''
    result, _ = sanitize_generated_svg(original)
    svg = etree.fromstring(result)
    assert svg.get('viewBox') == '0 0 100.0 200.0'
    assert not svg.get('onload') and not svg.get('x-init')
    assert 'https://' not in result.decode()
    assert svg.xpath('count(//*[local-name()="path" or local-name()="text"])') == 2
    assert svg.xpath('count(//*[local-name()="script" or local-name()="foreignObject" or local-name()="image"])') == 0
    assert '[data-generated-svg=' in result.decode()
    assert 'fill:url(#paint)' in result.decode()
    assert 'href="#paint"' in result.decode()


@pytest.mark.parametrize('payload', [b'{"error":"not SVG"}', b'<svg><g></svg>', b'<!DOCTYPE svg><svg width="1" height="1"/>', b'<wrapper><svg/></wrapper>'])
def test_generated_svg_requires_a_complete_safe_document(payload):
    with pytest.raises(TemplateInvalid):
        sanitize_generated_svg(payload)
