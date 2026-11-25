"""AI SVG generation and history routes for the local poster workbench."""
from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool
from typing_extensions import Annotated

from app.services import ai_svg
from app.services.ai_svg import start, stop
from app.services.template_import import import_template

router = APIRouter(prefix=ai_svg.PREFIX, tags=['AI SVG'])
ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
_NO_STORE = {'Cache-Control': 'no-store'}


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str | None = Field(default=None, max_length=2048)
    model: ModelName | None = None
    svg_model: ModelName | None = None
    provider: Literal['apimart', 'compatible'] | None = None
    compatibility_mode: bool | None = None
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)]
    model: ModelName | None = None
    size: Literal['1024x1536', '1536x1024', '1024x1024', 'auto'] = '1024x1536'
    quality: Literal['high', 'medium', 'low', 'auto'] = 'auto'
    complexity: Literal['high', 'medium', 'low'] = 'high'
    background: Literal['opaque', 'transparent', 'auto'] = 'auto'


def _same_origin(request: Request):
    origin = request.headers.get('origin')
    if origin:
        origin_url = urlsplit(origin)
        if (origin_url.scheme, origin_url.netloc) != (request.url.scheme, request.url.netloc):
            raise HTTPException(403, detail='AI SVG 接口仅接受同源页面请求')


async def _body(request, model):
    try:
        return model.model_validate(await request.json())
    except (ValidationError, ValueError):
        # The application's global validation handler includes raw input.
        raise HTTPException(422, detail='请求参数不合法') from None


def _job(job_id):
    try:
        return ai_svg.get_job(job_id)
    except FileNotFoundError:
        raise HTTPException(404, detail='作品或任务不存在') from None


def _artifact(job_id, filename):
    job = _job(job_id)
    if job['status'] != 'completed':
        raise HTTPException(409, detail='SVG 尚未生成完成')
    path = ai_svg.job_dir(job_id) / filename
    if not path.is_file():
        raise HTTPException(404, detail='SVG 文件不存在')
    return path


@router.get('/config')
async def get_config(request: Request):
    _same_origin(request)
    return ai_svg.public_config()


@router.put('/config')
async def set_config(request: Request):
    _same_origin(request)
    update = await _body(request, ConfigUpdate)
    values = update.model_dump(exclude_none=True, exclude={'api_key'})
    if update.api_key is not None:
        key = update.api_key.get_secret_value().strip()
        if any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise HTTPException(422, detail='密钥格式不合法')
        values['api_key'] = key
    try:
        return ai_svg.save_config(values)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None


@router.post('/generate', status_code=202)
async def generate(request: Request):
    _same_origin(request)
    payload = await _body(request, GenerateRequest)
    try:
        job = ai_svg.create_job(payload.model_dump())
    except ai_svg.RelayError as exc:
        raise HTTPException(400, detail=exc.detail['message']) from None
    ai_svg.schedule(job)
    return ai_svg.public_job(job)


@router.get('/jobs/{job_id}')
async def job_status(request: Request, job_id: str):
    _same_origin(request)
    job = _job(job_id)
    ai_svg.schedule(job)
    return ai_svg.public_job(job)


@router.get('/history')
async def history(request: Request):
    _same_origin(request)
    jobs = ai_svg.list_jobs()
    for job in jobs:
        ai_svg.schedule(job)
    return {'items': [ai_svg.public_job(job) for job in jobs]}


@router.get('/history/{job_id}/preview')
async def preview(request: Request, job_id: str):
    _same_origin(request)
    path = _artifact(job_id, 'preview.svg')
    return FileResponse(path, media_type='image/svg+xml', headers={
        **_NO_STORE,
        'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:",
    })


@router.get('/history/{job_id}/download')
async def download(request: Request, job_id: str):
    _same_origin(request)
    path = _artifact(job_id, 'original.svg')
    return FileResponse(path, media_type='application/octet-stream', filename=f'AI-{job_id[:12]}.svg',
                        headers={**_NO_STORE, 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "sandbox; default-src 'none'"})


@router.post('/history/{job_id}/import')
async def import_history(request: Request, job_id: str):
    _same_origin(request)
    path = _artifact(job_id, 'preview.svg')
    filename = f'AI-{job_id[:12]}.svg'
    return await run_in_threadpool(import_template, path.read_bytes(), filename)
