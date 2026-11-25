"""A real multipart AI import opens editable CJK text in the workbench."""
import io
from pathlib import Path
from urllib.parse import urlsplit

import pymupdf
import pytest
from lxml import etree
from PIL import Image
from playwright.sync_api import expect, sync_playwright

from app.config import settings
from app.services import template_analyzer
from app.services.svg_browser import _launch


@pytest.mark.parametrize('live', [True, False])
def test_ai_upload_then_edit_or_acknowledge(client, monkeypatch, live):
    monkeypatch.setattr(template_analyzer, 'revive_named_template', lambda *a, **kw: {})
    with pymupdf.open() as doc:
        doc.new_page(width=600, height=400)  # skipped blank artboard
        board = doc.new_page(width=600, height=400)
        if live:
            board.insert_font(fontname='cn', fontfile=str(settings.base_dir / 'static/Fonts/AlibabaPuHuiTi-3-65-Medium.ttf'))
            board.insert_text((60, 100), '厦门 2026', fontname='cn', fontsize=38)
        png = io.BytesIO()
        Image.new('RGB', (8, 8), 'red').save(png, format='PNG')
        board.insert_image(pymupdf.Rect(350, 180, 420, 250), stream=png.getvalue())
        data = doc.tobytes()

    uploads = []
    tokens = []
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            page = browser.new_page(viewport={'width': 1500, 'height': 920})

            def forward(route):
                request = route.request
                path = urlsplit(request.url).path
                if path.startswith('/static/'):
                    route.fulfill(path=settings.base_dir / Path(path.lstrip('/')))
                    return
                if path.endswith('/api/v1/posters/templates'):
                    response = client.get('/api/v1/posters/templates')
                elif path.startswith('/api/workspace/'): 
                    response = client.request(request.method, request.url.removeprefix('http://poster.test'),
                                              content=request.post_data_buffer, headers=request.headers)
                elif path.endswith('/templates/inspect'):
                    response = client.post('/api/v1/posters/templates/inspect',
                                           files={'file': ('实测导入.ai', data, 'application/pdf')})
                    assert response.is_success, response.text
                    tokens.append(response.json().get('staged_token'))
                elif path.endswith('/templates/upload'):
                    import re
                    body = request.post_data or ''
                    token_match = re.search(r'name="staged_token"\r\n\r\n([^\r\n]+)', body)
                    if token_match:
                        page_match = re.search(r'name="page"\r\n\r\n(\d+)', body)
                        response = client.post('/api/v1/posters/templates/upload', data={
                            'staged_token': token_match.group(1), 'filename': '实测导入.ai',
                            'name': '实测导入', 'page': page_match.group(1) if page_match else '0'})
                    else:
                        response = client.post('/api/v1/posters/templates/upload', content=request.post_data_buffer,
                                               headers=request.headers)
                    assert response.is_success, response.text
                    uploads.append(response.json())
                else:
                    response = client.request(request.method, request.url.removeprefix('http://poster.test'),
                                              content=request.post_data_buffer, headers=request.headers)
                route.fulfill(status=response.status_code, body=response.content,
                              content_type=response.headers.get('content-type', 'application/json'))

            page.route('http://poster.test/**', forward)
            page.goto('http://poster.test/static/poster.html')
            page.locator("input[type=file][accept*='.ai']").first.set_input_files(
                {'name': '实测导入.ai', 'mimeType': 'application/pdf', 'buffer': data})
            if not live:
                expect(page.get_by_text('未检测到可编辑文字', exact=True)).to_be_visible(timeout=30000)
                expect(page.get_by_text('Seedream 拆图层 OCR', exact=False)).to_be_visible()
                page.get_by_role('button', name='继续使用模板').click()
                expect(page.locator('.poster-stage svg image')).to_be_visible(timeout=15000)
                assert uploads[0]['text_status']['status'] == 'no_text'
                return

            text = page.locator('.poster-stage svg text').filter(has_text='厦门 2026')
            expect(text).to_be_visible(timeout=30000)
            fixed = etree.parse(str(template_analyzer.source_svg_path(uploads[0]['name'])))
            assert fixed.xpath('string(//*[local-name()="text"])') == '厦门 2026'
            assert len(fixed.xpath('//*[local-name()="image" and not(ancestor::*[local-name()="defs"])]')) == 1
            assert uploads[0]['text_status']['status'] == 'editable'
            text.click()
            text.dblclick()
            editor = page.locator('.editing-proxy')
            editor.fill('泉州 2027')
            editor.press('Enter')
            expect(page.locator('.poster-stage svg text')).to_have_text('泉州 2027')
        finally:
            browser.close()
