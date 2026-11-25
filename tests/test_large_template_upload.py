"""Configured large template staging and lightweight external raster assets."""
from __future__ import annotations

import base64
import io
from pathlib import Path

from fastapi.testclient import TestClient
from lxml import etree
from PIL import Image

from app.config import settings
from app.errors import PayloadTooLarge
from app.services import template_resources, template_upload


class ChunkOnlyReader:
    def __init__(self, payload: bytes):
        self.source = io.BytesIO(payload)
        self.calls: list[int] = []

    def seek(self, position: int):
        self.source.seek(position)

    def read(self, amount=-1):
        assert amount >= 0, 'upload code must never request an unbounded read'
        self.calls.append(amount)
        return self.source.read(amount)


def test_upload_is_chunked_and_default_limit_is_unlimited(monkeypatch, tmp_path):
    payload = b'%PDF-1.4\n' + b'x' * (5 * 1024 * 1024) + b'\n%%EOF'
    reader = ChunkOnlyReader(payload)
    monkeypatch.setattr(settings, 'cache_dir', tmp_path)
    monkeypatch.setattr(settings, 'max_template_upload_bytes', 0)
    path, size = template_upload.stage_upload(reader, 'large.ai')
    try:
        assert size == len(payload)
        assert path.stat().st_size == len(payload)
        assert max(reader.calls) == template_upload.CHUNK_BYTES
        assert reader.calls.count(template_upload.CHUNK_BYTES) >= 2
        assert template_upload.upload_limit_error(size, 'large.ai') is None
    finally:
        template_upload.remove_staged(path)


def test_configured_limit_error_reports_actual_size(monkeypatch):
    monkeypatch.setattr(settings, 'max_template_upload_bytes', 1024)
    error = template_upload.upload_limit_error(2048, 'print.ai')
    assert isinstance(error, PayloadTooLarge)
    assert 'print.ai' in error.message and '0.0MB' in error.message and 'MAX_TEMPLATE_UPLOAD_MB' in error.message


def test_inspect_route_stages_large_file_in_chunks_and_cleans_up(client, monkeypatch, tmp_path):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((30, 50), 'Upload', fontsize=20)
    pdf = doc.tobytes()
    monkeypatch.setattr(settings, 'cache_dir', tmp_path)
    monkeypatch.setattr(settings, 'max_template_upload_bytes', 0)
    monkeypatch.setattr('app.services.vector_importer.inspect_pages', lambda path, filename: {
        'kind': 'pdf', 'total': 1, 'pages': [{'index': 1, 'blank': False, 'width': 300, 'height': 400}],
        'non_blank': [1], 'auto_page': 1, 'needs_choice': False, 'truncated': False})
    staged_path = None
    original_stage = template_upload.stage_upload
    def track(source, filename):
        nonlocal staged_path
        staged_path, size = original_stage(source, filename)
        return staged_path, size
    monkeypatch.setattr(template_upload, 'stage_upload', track)
    upload = ChunkOnlyReader(pdf)
    response = client.post('/api/v1/posters/templates/inspect', files={'file': ('route.ai', upload, 'application/pdf')})
    assert response.status_code == 200, response.text
    assert max(upload.calls) <= template_upload.CHUNK_BYTES
    if response.json().get('staged_token'):
        assert staged_path is not None and staged_path.exists()
        template_upload.remove_staged(staged_path)
    else:
        assert staged_path is not None and not staged_path.exists()


def test_raster_externalized_and_downsampled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'template_dir', tmp_path)
    image = Image.new('RGB', (5000, 3000), (220, 30, 40))
    data = io.BytesIO()
    image.save(data, 'PNG')
    encoded = base64.b64encode(data.getvalue()).decode()
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           f'width="600" height="400" viewBox="0 0 600 400"><image id="bg" x="0" y="0" '
           f'width="600" height="400" xlink:href="data:image/png;base64,{encoded}"/></svg>').encode()
    fixed, assets = template_resources.externalize_svg_images(svg, 'fixture', (600, 400))
    root = etree.fromstring(fixed)
    href = root.xpath('string(//*[local-name()="image"]/@*[local-name()="href"])')
    assert href.startswith('/api/v1/posters/templates/fixture/resources/image-') and href.endswith('.jpg')
    assert assets and assets[0].is_file() and assets[0].stat().st_size < 2_000_000
    assert len(fixed) < 2000
    with Image.open(assets[0]) as result:
        assert max(result.size) <= 1200


def test_template_resource_route_returns_external_raster(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'template_dir', tmp_path)
    directory = template_resources.resource_directory('fixture')
    directory.mkdir()
    path = directory / ('image-' + 'a' * 32 + '.jpg')
    Image.new('RGB', (2, 2), 'red').save(path, 'JPEG')
    response = client.get('/api/v1/posters/templates/fixture/resources/' + path.name)
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/jpeg'
    assert response.content == path.read_bytes()
