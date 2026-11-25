from __future__ import annotations

import asyncio
import base64
import json
import socket
import time

import httpx
import pytest

from app.api import ai_svg as routes
from app.services import ai_svg as service

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="64"><script>bad()</script><path d="M1 2" onclick="bad()"/><text>Draft</text></svg>'
KEY = 'mock-only-credential'


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, 'data_dir', tmp_path)
    service.save_config({'api_key': KEY})


def payload(data):
    return 200, json.dumps(data).encode()


def wait_for_result(client, job):
    for _ in range(100):
        result = client.get(f"/api/ai-svg/jobs/{job['id']}").json()
        if result['status'] in {'completed', 'failed'}:
            return result
        time.sleep(.02)
    pytest.fail('AI SVG job did not complete')


def test_config_roundtrip_and_secret_free_errors(client):
    url = '/api/ai-svg/config'
    result = client.get(url).json()
    assert result['has_api_key'] is True and 'api_key' not in result
    assert KEY not in client.put(url, json={'api_key': {'bad': KEY}}).text
    assert KEY not in client.put(url, json={'api_key': KEY, 'base_url': f'https://{KEY}@example.com'}).text
    saved = client.put(url, json={'base_url': 'https://Example.com/proxy/v1/', 'api_key': 'different-key',
                                  'model': 'custom-image', 'provider': 'compatible'}).json()
    assert saved['base_url'] == 'https://example.com/proxy/v1'
    assert saved['model'] == 'custom-image' and saved['has_api_key'] is True
    assert 'different-key' not in str(saved)
    assert client.put(url, headers={'Origin': 'https://evil.example'}, json={}).status_code == 403
    assert not client.put(url, json={'base_url': 'https://new.example/'}).json()['has_api_key']


@pytest.mark.parametrize('variant', ['b64', 'url', 'task'])
def test_generation_modes_save_original_and_load_safe_svg(client, monkeypatch, variant):
    calls = []
    monkeypatch.setattr(service, 'POLL_INTERVAL', 0)

    async def fake(method, url, **options):
        calls.append((method, url, options))
        if url.endswith('/images/generations'):
            result = {'b64': {'b64_json': base64.b64encode(SVG).decode()},
                      'url': {'url': 'https://cdn.example/generated.svg'},
                      'task': {'task_id': 'task_mock'}}[variant]
            return payload({'data': [result], 'usage': {'output_tokens': 33}})
        if url.endswith('/tasks/task_mock'):
            persisted = service.list_jobs()[0]
            assert persisted['task_id'] == 'task_mock' and persisted['phase'] == 'poll'
            return payload({'data': {'status': 'completed', 'usage': {'svg_usage': {'input_tokens': 42}},
                                     'result': {'images': [{'url': ['https://cdn.example/generated.svg']}]}}})
        assert url == 'https://cdn.example/generated.svg' and options == {'artifact': True}
        return 200, SVG

    monkeypatch.setattr(service, '_request', fake)
    response = client.post('/api/ai-svg/generate', json={'prompt': '矢量海报', 'background': 'transparent'})
    assert response.status_code == 202
    job = wait_for_result(client, response.json())
    assert job['status'] == 'completed', job
    assert job['path_count'] == job['text_count'] == 1 and job['byte_size'] == len(SVG)
    assert job['view_box'].split()[2:] in (['128', '64'], ['128.0', '64.0'])
    request = calls[0][2]['body']
    assert request == {'model': 'gpt-image-2.6-flare', 'prompt': '矢量海报', 'output_format': 'svg', 'n': 1,
                       'size': '1024x1536', 'quality': 'auto', 'complexity': 'high', 'background': 'transparent',
                       'svg': {'model': 'gpt-5.4-mini'}}
    if variant == 'task':
        assert job['usage']['svg_usage']['input_tokens'] == 42
    history = client.get('/api/ai-svg/history').json()['items']
    assert history[0]['id'] == job['id'] and KEY not in str(history)
    original = client.get(job['download_url'])
    assert original.content == SVG and 'attachment' in original.headers['content-disposition']
    preview = client.get(job['preview_url'])
    assert '<script>' not in preview.text and 'onclick' not in preview.text
    imported = []
    monkeypatch.setattr(routes, 'import_template', lambda raw, filename: imported.append(raw) or {'name': 'imported'})
    assert client.post(job['import_url']).json()['name'] == 'imported'
    assert imported == [preview.content]


def test_compatible_mode_redacts_relay_errors(client, monkeypatch):
    client.put('/api/ai-svg/config', json={'provider': 'compatible', 'compatibility_mode': True})

    async def fake(method, url, *, key, body):
        assert 'complexity' not in body and 'svg' not in body
        return 418, json.dumps({'error': f'Authorization: Bearer {key}'}).encode()

    monkeypatch.setattr(service, '_request', fake)
    response = client.post('/api/ai-svg/generate', json={'prompt': 'x'})
    job = wait_for_result(client, response.json())
    assert job['status'] == 'failed' and job['error']['status_code'] == 418
    assert KEY not in str(job) and '[redacted]' in job['error']['summary']


def test_non_svg_responses_are_explicit(client, monkeypatch):
    async def fake(method, url, **options):
        return payload({'data': [{'b64_json': base64.b64encode(b'\x89PNG\r\n').decode()}]})

    monkeypatch.setattr(service, '_request', fake)
    response = client.post('/api/ai-svg/generate', json={'prompt': 'test'})
    job = wait_for_result(client, response.json())
    assert job['status'] == 'failed' and job['error']['status_code'] == 200
    assert 'SVG' in job['error']['message'] and 'PNG' in job['error']['summary']


def test_persisted_job_resumes_at_lifespan_and_does_not_resubmit(client, monkeypatch):
    values = routes.GenerateRequest(prompt='Resume a poster').model_dump()
    job = service.create_job(values)
    job.update(status='processing', phase='poll', task_id='task_existing')
    service._save(job)
    ambiguous = service.create_job(values)
    ambiguous.update(status='processing', phase='submit')
    service._save(ambiguous)

    async def fake(method, url, **options):
        assert method == 'GET'
        if url.endswith('/tasks/task_existing'):
            return payload({'data': {'status': 'completed', 'result': {'images': [{'url': 'https://cdn.example/s.svg'}]}}})
        return 200, SVG

    monkeypatch.setattr(service, '_request', fake)
    assert wait_for_result(client, job)['status'] == 'completed'
    uncertain = wait_for_result(client, ambiguous)
    assert uncertain['status'] == 'failed' and '重复扣费' in uncertain['error']['message']


def test_artifact_destination_must_resolve_public_and_pins_tls_host(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop()

        async def public(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 443))]

        monkeypatch.setattr(loop, 'getaddrinfo', public)
        target, headers, extensions = await service._pinned_url('https://cdn.example/a.svg?sig=x')
        assert target.host == '93.184.215.14' and target.query == b'sig=x'
        assert headers['Host'] == 'cdn.example' and extensions['sni_hostname'] == 'cdn.example'
        with pytest.raises(service.RelayError):
            await service._pinned_url('http://cdn.example/a.svg')

        async def mixed(host, port, **kwargs):
            return await public(host, port) + [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]

        monkeypatch.setattr(loop, 'getaddrinfo', mixed)
        with pytest.raises(service.RelayError):
            await service._pinned_url('https://cdn.example/a.svg')

    asyncio.run(check())


@pytest.mark.parametrize(('url', 'artifact', 'expected_proxy'), [
    ('https://api.apimart.ai/v1/models', False, 'http://127.0.0.1:1234'),
    ('https://upload.apimart.ai/result.svg', True, 'http://127.0.0.1:1234'),
    ('https://relay.example/v1/models', False, None),
])
def test_proxy_retains_apimart_hostname_and_limits_scope(monkeypatch, url, artifact, expected_proxy):
    async def pin(value):
        original = httpx.URL(value)
        return original.copy_with(host='93.184.215.14'), {'Host': original.host}, {'sni_hostname': original.host}

    def reply(request):
        assert request.url.host == (httpx.URL(url).host if expected_proxy else '93.184.215.14')
        assert ('Authorization' in request.headers) == (not artifact)
        return httpx.Response(200, content=b'ok')

    real_client = httpx.AsyncClient

    def client_for_test(**kwargs):
        assert kwargs['proxy'] == expected_proxy
        assert kwargs['trust_env'] is False
        return real_client(transport=httpx.MockTransport(reply))

    monkeypatch.setattr(service, '_pinned_url', pin)
    monkeypatch.setattr(service, 'getproxies', lambda: {'https': 'http://127.0.0.1:1234'})
    monkeypatch.setattr(service.httpx, 'AsyncClient', client_for_test)
    assert asyncio.run(service._request('GET', url, key='test-key', artifact=artifact)) == (200, b'ok')


def test_artifact_redirect_checks_new_host_and_sends_no_auth(monkeypatch):
    calls = []

    async def pin(url):
        calls.append(url)
        if url.startswith('https://private.example'):
            raise service.RelayError('non-public')
        return httpx.URL(url), {}, {}

    def reply(request):
        assert 'Authorization' not in request.headers
        return httpx.Response(302, headers={'location': 'https://private.example/x'})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(service, '_pinned_url', pin)
    monkeypatch.setattr(service.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(reply)))
    with pytest.raises(service.RelayError, match='non-public'):
        asyncio.run(service._request('GET', 'https://cdn.example/first', artifact=True))
    assert calls == ['https://cdn.example/first', 'https://private.example/x']
