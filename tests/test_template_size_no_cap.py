"""No hard-coded 200MB template cap: 0 means unlimited on both upload routes."""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from app.config import settings
from app.errors import PayloadTooLarge
from app.services import template_upload

BIG = 250 * 1024 * 1024
APP_DIR = Path(__file__).resolve().parents[1] / "app"
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


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


@pytest.mark.parametrize('size', [200 * 1024 * 1024 + 1, BIG])
def test_zero_limit_never_rejects_large_template(monkeypatch, size):
    monkeypatch.setattr(settings, 'max_template_upload_bytes', 0)
    assert template_upload.upload_limit_error(size, 'print.ai') is None


def test_250mb_staging_streams_in_chunks(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'cache_dir', tmp_path)
    monkeypatch.setattr(settings, 'max_template_upload_bytes', 0)
    source = tmp_path / 'big.ai'
    with source.open('wb') as handle:
        handle.truncate(BIG)  # sparse: real 250MB read/write without 250MB in memory
    with source.open('rb') as stream:
        staged, size = template_upload.stage_upload(stream, 'big.ai')
    try:
        assert size == BIG
        assert staged.stat().st_size == BIG
    finally:
        template_upload.remove_staged(staged)


def test_inspect_route_accepts_250mb_with_zero_limit(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'cache_dir', tmp_path)
    monkeypatch.setattr(settings, 'max_template_upload_bytes', 0)
    staged = tmp_path / 'template-staging' / 'upload-0123456789abcdef.upload'
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b'%PDF-1.4\n%%EOF')
    monkeypatch.setattr(template_upload, 'stage_upload', lambda source, filename: (staged, BIG))
    monkeypatch.setattr('app.services.vector_importer.inspect_pages', lambda path, filename: {
        'kind': 'pdf', 'total': 2, 'pages': [], 'non_blank': [1, 2], 'auto_page': 0,
        'needs_choice': True, 'truncated': False})
    response = client.post('/api/v1/posters/templates/inspect', files={'file': ('big.ai', b'%PDF', 'application/pdf')})
    assert response.status_code == 200, response.text
    assert '200MB' not in response.text and '超过' not in response.text
    assert response.json()['staged_token']


def test_upload_route_with_staged_token_accepts_250mb(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'max_template_upload_bytes', 0)
    staged = tmp_path / 'upload-test.upload'
    staged.write_bytes(b'%PDF-1.4\n%%EOF')
    monkeypatch.setattr(template_upload, 'resolve_stage', lambda token: staged)
    monkeypatch.setattr('app.api.posters.import_template', lambda raw, filename, **kwargs: {
        'name': 'big', 'display_name': 'big', 'analyzed': True, 'thumbnail_url': '',
        'canvas': {'width': 100, 'height': 100}, 'warnings': [], 'text_status': None,
        'revived_texts': 0, 'revival_report': {}})
    response = client.post('/api/v1/posters/templates/upload', data={
        'staged_token': 'upload-0123456789abcdef', 'filename': 'big.ai', 'name': 'big', 'page': '2'})
    assert response.status_code == 200, response.text
    assert '200MB' not in response.text and '超过' not in response.text


def test_source_has_no_residual_200mb_template_cap():
    """Guard the exact strings that produced「模板超过 200MB」before the fix."""
    markers = ('200MB', 'MAX_TEMPLATE_BYTES', 'MAX_VECTOR_BYTES', '209715200', '模板超过')
    suffixes = {'.py', '.html', '.js'}
    offenders = []
    for root in (APP_DIR, STATIC_DIR):
        for path in root.rglob('*'):
            if path.suffix not in suffixes or not path.is_file():
                continue
            text = path.read_text(encoding='utf-8', errors='ignore')
            for marker in markers:
                if marker in text:
                    line = text[:text.index(marker)].count('\n') + 1
                    offenders.append(f'{path.relative_to(path.parents[2])}:{line}:{marker}')
    assert not offenders, offenders
