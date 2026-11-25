"""Local AI SVG configuration, durable jobs and relay protocol."""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import getproxies

import httpx
from lxml import etree

from app.config import settings
from app.errors import TemplateInvalid
from app.services.svg_sanitizer import sanitize_generated_svg

logger = logging.getLogger(__name__)
PREFIX = '/api/ai-svg'
MAX_SVG = 16 * 1024 * 1024
MAX_JSON = MAX_SVG * 4 // 3 + 65536
REQUEST_TIMEOUT = 185
JOB_TIMEOUT = 22 * 60
POLL_INTERVAL = 3
ID = re.compile(r'[a-f0-9]{32}\Z')
TASK_ID = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
DEFAULTS = {
    'base_url': 'https://api.apimart.ai/v1',
    'model': 'gpt-image-2.6-flare',
    'svg_model': 'gpt-5.4-mini',
    'provider': 'apimart',
    'compatibility_mode': False,
}
_running: dict[str, asyncio.Task] = {}
_slots: asyncio.Semaphore | None = None


class RelayError(Exception):
    def __init__(self, message, status_code=None, summary=''):
        self.detail = {'message': message, 'status_code': status_code, 'summary': summary}
        super().__init__(message)


def _root() -> Path:
    return settings.data_dir / 'ai-svg'


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _put(path, data):
    _atomic(path, json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def _get(path):
    return json.loads(path.read_text(encoding='utf-8'))


def normalize_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme.lower() != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.port not in (None, 443) or '\\' in value
                or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname)
                or any(part in {'.', '..'} for part in parsed.path.split('/'))):
            raise ValueError
    except ValueError:
        raise ValueError('Base URL 必须是无凭据、无查询参数的 HTTPS API 地址') from None
    path = parsed.path.rstrip('/')
    if not path.endswith('/v1'):
        path += '/v1'
    return urlunsplit(('https', parsed.hostname, path, '', ''))


def config():
    path = _root() / 'config.json'
    return {**DEFAULTS, **(_get(path) if path.is_file() else {})}


def public_config():
    values = config()
    public = {key: values[key] for key in DEFAULTS}
    return {**_redact(public, values.get('api_key')), 'has_api_key': bool(values.get('api_key'))}


def save_config(values):
    current = config()
    data = {**current, **{key: value for key, value in values.items() if key in DEFAULTS}}
    data['base_url'] = normalize_base_url(data['base_url'])
    if values.get('clear_api_key'):
        data.pop('api_key', None)
    elif values.get('api_key'):
        data['api_key'] = values['api_key']
    elif data['base_url'] != current['base_url'] and current.get('api_key'):
        # Do not forward a previous destination's credential to a new relay automatically.
        data.pop('api_key', None)
    _put(_root() / 'config.json', data)
    return public_config()


def job_dir(job_id):
    if not ID.fullmatch(job_id):
        raise FileNotFoundError
    return _root() / 'jobs' / job_id


def get_job(job_id):
    return _get(job_dir(job_id) / 'job.json')


def _save(job):
    _put(job_dir(job['id']) / 'job.json', job)


def list_jobs():
    jobs = []
    for path in (_root() / 'jobs').glob('*/job.json'):
        try:
            jobs.append(get_job(path.parent.name))
        except (FileNotFoundError, ValueError):
            logger.warning('Skipping unreadable AI SVG job metadata')
    return sorted(jobs, key=lambda job: job['created_at'], reverse=True)


def public_job(job):
    data = {key: job.get(key) for key in ('id', 'status', 'progress', 'created_at', 'request',
            'usage', 'error', 'debug', 'view_box', 'path_count', 'text_count', 'byte_size')}
    if job['status'] == 'completed':
        base = f"{PREFIX}/history/{job['id']}"
        data.update(preview_url=f'{base}/preview', download_url=f'{base}/download', import_url=f'{base}/import')
    return data


def create_job(values):
    cfg = config()
    key = cfg.get('api_key')
    if not key:
        raise RelayError('请先在 API 设置中保存密钥', 400)
    body = {
        'model': values.get('model') or cfg['model'],
        'prompt': values['prompt'],
        'output_format': 'svg', 'n': 1,
        'size': values['size'], 'quality': values['quality'], 'background': values['background'],
    }
    if not cfg['compatibility_mode']:
        body['complexity'] = values['complexity']
    if cfg['provider'] == 'apimart':
        body['svg'] = {'model': cfg['svg_model']}
    now = time.time()
    job = {'id': uuid.uuid4().hex, 'created_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
           'deadline': now + JOB_TIMEOUT, 'status': 'queued', 'phase': 'queued', 'progress': 0,
           'base_url': cfg['base_url'], 'request': _redact(body, key), 'debug': {}, 'usage': None, 'error': None}
    if job['request'] != body:
        raise RelayError('请求内容包含 API 密钥，请从提示词或模型名中移除密钥', 400)
    _save(job)
    return job


def _redact(value, key):
    if isinstance(value, str):
        text = value.replace(key, '[redacted]') if key else value
        return re.sub(r'(?i)\bBearer\s+[\w.+/=-]+|\bsk-[A-Za-z0-9_-]{8,}', '[redacted]', text)
    if isinstance(value, dict):
        return {k: _redact(v, key) for k, v in value.items()
                if k.lower() not in {'api_key', 'authorization', 'access_token', 'token', 'b64_json'}}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value


async def _pinned_url(url: str) -> tuple[httpx.URL, dict, dict]:
    try:
        parsed = httpx.URL(url)
        host = parsed.host
        if (parsed.scheme != 'https' or not host or parsed.userinfo or parsed.fragment
                or (parsed.port is not None and parsed.port != 443)):
            raise ValueError
        addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM), timeout=10)
        ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
            raise ValueError
    except (ValueError, OSError, TimeoutError, httpx.InvalidURL):
        raise RelayError('上游地址必须指向可解析的公网 HTTPS 服务') from None
    # Pin the verified address for the connection and keep the original host for TLS and HTTP.
    # HTTPX/httpcore connection pools are keyed by IP, so use a dedicated client per request.
    return parsed.copy_with(host=str(ips[0])), {'Host': parsed.netloc.decode('ascii')}, {'sni_hostname': host}


async def _request(method, url, *, key='', body=None, artifact=False):
    limit = MAX_SVG if artifact else MAX_JSON
    for attempt in range(4):
        target, headers, extensions = await _pinned_url(url)
        # The local proxy routes APIMart by CONNECT hostname. Keep this exception
        # limited to its documented API and download hosts, including redirects.
        relay_host = 'upload.apimart.ai' if artifact else 'api.apimart.ai'
        original = httpx.URL(url)
        proxy = getproxies().get('https') if original.host == relay_host else None
        if proxy:
            target = original
            extensions = {}
        if key and not artifact:
            headers['Authorization'] = f'Bearer {key}'
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=REQUEST_TIMEOUT, trust_env=False, follow_redirects=False) as client:
                async with client.stream(method, target, headers=headers, extensions=extensions, json=body) as response:
                    if artifact and response.is_redirect and response.headers.get('location') and attempt < 3:
                        url = str(httpx.URL(url).join(response.headers['location']))
                        continue
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        chunks.extend(chunk)
                        if len(chunks) > limit:
                            raise RelayError('上游返回超过大小限制', response.status_code)
                    return response.status_code, bytes(chunks)
        except httpx.TimeoutException:
            raise RelayError('等待上游响应超时', 504) from None
        except httpx.HTTPError:
            raise RelayError('无法连接上游 HTTPS 服务', 502) from None
    raise RelayError('文件地址重定向次数过多', 502)


def _response(status, raw, key):
    summary = _redact(raw[:4096].decode('utf-8', 'replace'), key)[:1000]
    if not 200 <= status < 300:
        raise RelayError(f'上游返回 HTTP {status}', status, summary)
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RelayError('上游响应不是有效 JSON', status, summary) from None
    if not isinstance(document, dict) or document.get('error') or document.get('code', 200) not in (200, None):
        raise RelayError('上游返回错误', status, summary)
    return document


def _items(document):
    data = document.get('data')
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}


def _url(item):
    value = item.get('url')
    if isinstance(value, list):
        value = next((url for url in value if isinstance(url, str) and url), None)
    return value if isinstance(value, str) and value else None


def _usage(job, usage, key):
    if isinstance(usage, dict):
        job['usage'] = _redact(usage, key)


def _decode_base64(value, status):
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error, TypeError):
        raise RelayError('上游 b64_json 无法解码', status) from None


def _accept(job, original, status, key):
    if not original or len(original) > MAX_SVG:
        raise RelayError('SVG 文件为空或超过大小限制', status)
    try:
        content = original.decode('utf-8-sig')
        if not re.search(r'<(?:[\w.-]+:)?svg(?=[\s>])', content):
            raise ValueError('没有 SVG 根元素')
        if key and key in content:
            raise ValueError('SVG 包含凭据信息')
        cleaned, notes = sanitize_generated_svg(original)
    except (ValueError, UnicodeError, TemplateInvalid) as exc:
        # Binary content gets a bounded textual summary suitable for display.
        summary = _redact(original[:300].decode('utf-8', 'replace'), key)
        raise RelayError('上游返回内容无法作为 SVG 使用', status, _redact(str(exc), key) + ' | ' + summary) from None
    root = etree.fromstring(cleaned, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    job.update(view_box=root.get('viewBox'), path_count=sum(1 for _ in root.iter('{*}path')),
               text_count=sum(1 for _ in root.iter('{*}text')), byte_size=len(original))
    directory = job_dir(job['id'])
    _atomic(directory / 'original.svg', original)
    _atomic(directory / 'preview.svg', cleaned)
    job['debug']['sanitizer_notes'] = notes
    job.update(status='completed', progress=100, phase='done')
    job.pop('artifact_url', None)
    _save(job)


async def _poll(job, key):
    while time.time() < job['deadline']:
        try:
            status, raw = await _request('GET', f"{job['base_url']}/tasks/{job['task_id']}", key=key)
        except RelayError as exc:
            status = exc.detail['status_code']
            if status not in (502, 504):
                raise
            raw = b''
        job['debug']['poll_status'] = status
        if status in (429, 502, 503, 504):
            _save(job)
            await asyncio.sleep(POLL_INTERVAL)
            continue
        result = _response(status, raw, key).get('data')
        if not isinstance(result, dict):
            raise RelayError('上游任务响应缺少 data', status)
        _usage(job, result.get('usage'), key)
        state = result.get('status')
        if state in {'failed', 'cancelled'}:
            error = _redact(result.get('error'), key)
            raise RelayError('上游生成任务失败或已取消', status, json.dumps(error, ensure_ascii=False)[:1000])
        progress = result.get('progress')
        if isinstance(progress, (int, float)) and 0 <= progress <= 100:
            job['progress'] = min(99, int(progress))
        if state == 'completed':
            output = result.get('result') or {}
            images = output.get('images') or [] if isinstance(output, dict) else []
            item = images[0] if isinstance(images, list) and images and isinstance(images[0], dict) else {}
            url = _url(item)
            if not url:
                raise RelayError('上游任务完成但未返回文件链接', status)
            job.update(phase='download', artifact_url=url)
            _save(job)
            return
        if state not in {'submitted', 'pending', 'processing', 'queued', 'running'}:
            raise RelayError('上游返回未知任务状态', status, str(_redact(state, key))[:300])
        _save(job)
        await asyncio.sleep(POLL_INTERVAL)
    raise RelayError('等待上游任务超时', 504)


async def _process(job_id):
    job = get_job(job_id)
    key = ''
    try:
        if job['phase'] == 'submit':
            raise RelayError('服务中断时已开始提交，无法确认上游是否已受理。为避免重复扣费，未重新提交')
        cfg = config()
        key = cfg.get('api_key', '')
        if job['phase'] in {'queued', 'poll'} and (not key or cfg['base_url'] != job['base_url']):
            raise RelayError('任务使用的中转站配置已经变更，无法继续查询')
        async with asyncio.timeout(max(0, job['deadline'] - time.time())):
            if job['phase'] == 'queued':
                job.update(status='processing', phase='submit')
                _save(job)
                status, raw = await _request('POST', job['base_url'] + '/images/generations', key=key, body=job['request'])
                job['debug']['submit_status'] = status
                document = _response(status, raw, key)
                item = _items(document)
                _usage(job, document.get('usage') or item.get('usage'), key)
                if item.get('b64_json') is not None:
                    original = _decode_base64(item['b64_json'], status)
                    _accept(job, original, status, key)
                    return
                if url := _url(item):
                    job.update(phase='download', artifact_url=url)
                elif isinstance(task_id := item.get('task_id'), str) and TASK_ID.fullmatch(task_id):
                    job.update(phase='poll', task_id=task_id)
                    job['debug']['task_id'] = task_id
                else:
                    raise RelayError('上游未返回 b64_json、文件链接或任务 ID', status)
                _save(job)
            if job['phase'] == 'poll':
                await _poll(job, key)
            if job['phase'] == 'download':
                if key and key in job['artifact_url']:
                    raise RelayError('上游文件地址包含凭据信息')
                status, original = await _request('GET', job['artifact_url'], artifact=True)
                job['debug']['download_status'] = status
                if not 200 <= status < 300:
                    raise RelayError(f'下载 SVG 返回 HTTP {status}', status,
                                     _redact(original[:1000].decode('utf-8', 'replace'), key))
                _accept(job, original, status, key)
                return
            raise RelayError('无法识别本地任务状态')
    except TimeoutError:
        error = RelayError('等待上游任务超时', 504).detail
    except RelayError as exc:
        error = _redact(exc.detail, key)
    except Exception:
        logger.error('AI SVG job %s failed during local processing', job_id)
        error = RelayError('本地处理 SVG 失败').detail
    job.update(status='failed', phase='done', error=error)
    job.pop('artifact_url', None)
    _save(job)


async def _run_queued(job_id, slots):
    async with slots:
        await _process(job_id)


def schedule(job):
    global _slots
    if job['status'] not in {'queued', 'processing'} or job['id'] in _running:
        return
    if _slots is None:
        _slots = asyncio.Semaphore(2)
    task = asyncio.create_task(_run_queued(job['id'], _slots))
    _running[job['id']] = task

    def done(future):
        _running.pop(job['id'], None)
        if not future.cancelled() and future.exception():
            logger.error('AI SVG job %s failed to persist state', job['id'])

    task.add_done_callback(done)


async def start():
    for job in list_jobs():
        schedule(job)


async def stop():
    global _slots
    tasks = list(_running.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _running.clear()
    _slots = None
