"""Seedream 5.5 生图 / 圈画局改 / 拆图层路由。"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool

from app.api.workspace import current_workspace
from app.errors import ValidationFailed
from app.services import generations, llm_client, seedream, seedream_prompts

router = APIRouter(prefix="/api/seedream", tags=["Seedream"])

_PROMPT = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
_IMAGE = Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=32_000_000)]
_PROJECT = Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=40)]
_NO_STORE = {"Cache-Control": "no-store"}


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: _PROMPT
    size: str = Field(default="2K", max_length=16)
    background: Literal["opaque", "transparent"] = "opaque"
    images: list[_IMAGE] = Field(default_factory=list, max_length=10)
    project_id: _PROJECT | None = None
    channel_id: str = Field(default="", max_length=64)
    intent_id: str = Field(default="", max_length=64)
    style_id: str = Field(default="", max_length=64)


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: _PROMPT
    base_image: _IMAGE
    mask_image: _IMAGE | None = None
    size: str = Field(default="auto", max_length=16)
    project_id: _PROJECT | None = None


class ArkConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    model: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: _IMAGE
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""
    project_id: _PROJECT | None = None


class HistoryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)] | None = None
    scope: Literal["public", "team", "private"] | None = None


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr | None = Field(default=None, max_length=4096)


class OptimizePromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_input: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    channel_id: str = Field(default="wechat_banner", max_length=64)
    intent_id: str = Field(default="direct", max_length=64)
    style_id: str | None = Field(default=None, max_length=64)
    has_ref_images: bool = False


class BackgroundTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: _IMAGE
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""
    project_id: _PROJECT | None = None


class DecomposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: _IMAGE
    max_images: int = Field(default=20, ge=10, le=40)
    project_id: _PROJECT | None = None
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""


def _same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    origin_url = urlsplit(origin)
    if (origin_url.scheme, origin_url.netloc) != (request.url.scheme, request.url.netloc):
        raise HTTPException(403, detail="Seedream 接口仅接受同源页面请求")


async def _body(request: Request, model):
    try:
        return model.model_validate(await request.json())
    except (ValidationError, ValueError):
        raise HTTPException(422, detail="请求参数不合法") from None


def _config_payload() -> dict:
    # 实时扫描 prompts/seedream/：新增或修改文件后刷新即生效
    return {
        **seedream.public_config(),
        **seedream_prompts.catalog(),
        "llm_configured": llm_client.llm_configured(),
    }


@router.get("/config")
async def get_config(request: Request):
    _same_origin(request)
    return _config_payload()


@router.put("/config")
async def set_config(request: Request):
    """在界面里直接填写 ARK_API_KEY，保存即生效，无需改 .env 或重启。"""
    _same_origin(request)
    payload = await _body(request, ArkConfigUpdate)
    try:
        if payload.clear_api_key:
            seedream.clear_api_key()
        if payload.api_key is not None:
            seedream.save_api_key(payload.api_key.get_secret_value())
        if payload.model:
            seedream.save_model(payload.model)
    except ValidationFailed as exc:
        raise HTTPException(422, detail=exc.message) from None
    return _config_payload()


@router.post("/generate")
async def generate(request: Request):
    _same_origin(request)
    ws = current_workspace(request)
    payload = await _body(request, GenerateRequest)
    result = await seedream.generate(
        prompt=payload.prompt,
        size=payload.size,
        background=payload.background,
        images=payload.images,
        project_id=payload.project_id,
    )
    generation = generations.record_project(
        generation_id=result["project"]["id"],
        ws=ws,
        versions=result["project"]["versions"],
        prompt=payload.prompt,
        raw_input=payload.prompt,
        channel_id=payload.channel_id,
        intent_id=payload.intent_id,
        style_id=payload.style_id,
        size=payload.size,
    )
    result["generation_id"] = generation["generation_id"]
    return result


@router.post("/edit")
async def edit(request: Request):
    _same_origin(request)
    ws = current_workspace(request)
    payload = await _body(request, EditRequest)

    project_id = payload.project_id
    forked_from = ""
    fork_title = ""
    if project_id:
        source = generations.get_row(project_id)
        if source is not None and source["owner_id"] != ws.user_id:
            # 在他人作品上继续改：复制版本链开新记录，绝不污染原作者
            forked = seedream.fork_project(project_id)
            project_id = forked["id"]
            forked_from = source["id"]
            fork_title = (source["title"] + " · 复刻")[:60]

    result = await seedream.edit(
        base_image=payload.base_image,
        mask_image=payload.mask_image,
        instruction=payload.instruction,
        size=payload.size,
        project_id=project_id,
    )
    generation = generations.record_project(
        generation_id=result["project"]["id"],
        ws=ws,
        versions=result["project"]["versions"],
        prompt=payload.instruction,
        raw_input=payload.instruction,
        size=payload.size,
        forked_from=forked_from,
        title=fork_title,
    )
    result["generation_id"] = generation["generation_id"]
    result["forked_from"] = forked_from
    return result


@router.post("/upload")
async def upload(request: Request):
    _same_origin(request)
    ws = current_workspace(request)
    payload = await _body(request, UploadRequest)
    result = seedream.upload_image(
        image=payload.image, name=payload.name, project_id=payload.project_id
    )
    generation = generations.record_project(
        generation_id=result["project"]["id"],
        ws=ws,
        versions=result["project"]["versions"],
        prompt=payload.name or "上传图片",
        raw_input=payload.name or "上传图片",
    )
    result["generation_id"] = generation["generation_id"]
    return result


@router.get("/history")
async def list_history(
    request: Request,
    view: Literal["mine", "team", "public"] = "mine",
    q: str = "",
    limit: int = 50,
    offset: int = 0,
):
    _same_origin(request)
    ws = current_workspace(request)
    items = generations.list_visible(ws, view=view, query=q, limit=limit, offset=offset)
    return {"items": items, "view": view, "team_id": ws.team_id, "user_id": ws.user_id}


@router.get("/history/{generation_id}")
async def get_history(request: Request, generation_id: str):
    _same_origin(request)
    ws = current_workspace(request)
    row = generations.get_visible(generation_id, ws)
    return generations.public_item(row, ws)


@router.patch("/history/{generation_id}")
async def update_history(request: Request, generation_id: str):
    _same_origin(request)
    ws = current_workspace(request)
    payload = await _body(request, HistoryUpdateRequest)
    try:
        return generations.update(
            generation_id, ws, title=payload.title, scope=payload.scope
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None


@router.delete("/history/{generation_id}", status_code=204)
async def delete_history(request: Request, generation_id: str):
    _same_origin(request)
    ws = current_workspace(request)
    generations.delete(generation_id, ws)
    return Response(status_code=204)


@router.post("/decompose")
async def decompose(request: Request):
    _same_origin(request)
    ws = current_workspace(request)
    payload = await _body(request, DecomposeRequest)
    return await seedream.decompose(
        image=payload.image,
        max_images=payload.max_images,
        project_id=payload.project_id,
        name=payload.name,
        owner_id=ws.user_id,
        team_id=ws.team_id,
        scope="public",
    )


@router.post("/verify")
async def verify(request: Request):
    _same_origin(request)
    payload = await _body(request, VerifyRequest)
    key = payload.api_key.get_secret_value().strip() if payload.api_key else None
    return await seedream.verify_key(key)


@router.post("/optimize-prompt")
async def optimize_prompt(request: Request):
    _same_origin(request)
    payload = await _body(request, OptimizePromptRequest)
    if not llm_client.llm_configured():
        raise HTTPException(503, detail="未配置大模型：请在 API 设置或 .env 中填写 LLM_API_KEY")
    try:
        assembled = seedream_prompts.assemble(
            channel_id=payload.channel_id,
            intent_id=payload.intent_id,
            style_id=payload.style_id,
            has_ref_images=payload.has_ref_images,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None

    channel = assembled["channel"]
    style_line = f"视觉风格：{assembled['style']['name']}" if assembled.get("style") else "视觉风格：不指定，按最合适的克制设计处理"
    user = "\n".join([
        "用户需求原文（只允许使用其中的事实信息）：",
        payload.raw_input,
        "",
        f"渠道：{channel['name']}（{channel['ratio']}，安全区见系统指令）",
        f"产出意图：{assembled['intent']['name']}",
        style_line,
        "参考图：" + ("已提供，按系统指令取用氛围但不得照抄文字" if payload.has_ref_images else "未提供"),
        "",
        "请严格按系统指令输出 JSON。",
    ])
    try:
        data = await run_in_threadpool(
            llm_client.chat_json,
            assembled["system"],
            user,
            temperature=0.7,
            max_tokens=2400,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        raise HTTPException(502, detail=f"大模型调用失败：{exc}") from None

    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(502, detail="大模型没有返回有效提示词，请重试或换个说法")
    warnings = list(assembled["warnings"])
    if assembled["intent"]["id"] == "clean_bg":
        prompt, extra = seedream_prompts.sanitize_clean_bg(prompt)
        warnings.extend(extra)
    return {
        "prompt": prompt,
        "notes": str(data.get("notes") or ""),
        "used_files": assembled["used_files"],
        "warnings": warnings,
        "channel": assembled["channel"],
        "intent": assembled["intent"],
        "style": assembled["style"],
    }


@router.post("/background-template")
async def background_template(request: Request):
    _same_origin(request)
    ws = current_workspace(request)
    payload = await _body(request, BackgroundTemplateRequest)
    return seedream.background_template(
        image=payload.image,
        name=payload.name,
        project_id=payload.project_id,
        owner_id=ws.user_id,
        team_id=ws.team_id,
        scope="public",
    )


@router.get("/projects/{project_id}")
async def get_project(request: Request, project_id: str):
    _same_origin(request)
    return seedream.public_project(seedream.load_project(project_id))


@router.get("/files/{project_id}/{name:path}")
def get_file(project_id: str, name: str) -> FileResponse:
    path = seedream.project_file(project_id, name)
    if path is None:
        raise HTTPException(404, detail="文件不存在")
    return FileResponse(path, media_type="image/png", headers=_NO_STORE)
