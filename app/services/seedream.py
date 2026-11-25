"""火山方舟 Seedream 5.5：文生图 / 圈画局改 / 一键拆图层。

设计要点（务必保持）：
* 只走 ``/images/generations``，不发送方舟未定义的 ``mask`` 字段；
* 生图与局改显式传 ``background``；拆图层模式严禁传 ``background``；
* 方舟返回的图片 URL 只有 24 小时有效期，拿到即下载落盘；
* 编辑态 SVG 只引用本地静态 URL，导出时再按需内联（见 ``svg_inline``）。
"""

from __future__ import annotations

import base64
import io
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
from PIL import Image, ImageFilter

from app.config import settings
from app.errors import AppError, InvalidImage, PayloadTooLarge, ValidationFailed
from app.services import net, svg_compose
from app.services.template_import import import_template

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MIN_PIXELS = 921_600
MAX_PIXELS = 4_624_220
CONFIG_NAME = "seedream.json"
FILE_PREFIX = "/api/seedream/files"
SIZE_PATTERN = re.compile(r"^(auto|1K|1\.5K|2K|\d{2,5}x\d{2,5})$", re.IGNORECASE)
DEFAULT_PROMPT = "生成一张高质量教培课程海报，构图干净，留出文字排版空间。"

EDIT_PROMPT = (
    "图1为干净原图，图2中红色标注区域为唯一需要修改的范围。"
    "请基于图1仅修改该标注区域内的内容为：{instruction}。"
    "严格保持图1标注区域外的构图、文字、人物和配色不变，"
    "最终输出中严禁保留任何红色标注线框或涂抹痕迹。\n\n"
    "默认保持原标注区域的元素形态不变：若原标注区域内为文字，除非用户明确要求画成图标或插画，"
    "否则必须仍以文字形式呈现，仅替换对应文案内容，并严格沿用原有的字体、字号、颜色及前后装饰符号/标点；"
    "若原区域为图形或图标，则按同画风替换图形。"
)
DECOMPOSE_PROMPT = "将图片中的文字、主体和装饰元素拆分成独立图层，保留位置与样式，自然补全底图"


class SeedreamError(AppError):
    status_code = 502
    code = "seedream_error"


# ---------------------------------------------------------------------------
# 配置 / 目录
# ---------------------------------------------------------------------------
def configured() -> bool:
    return bool(settings.seedream_enabled and settings.ark_api_key)


def public_config() -> dict[str, Any]:
    return {
        "enabled": configured(),
        "model": settings.seedream_model,
        "has_api_key": bool(settings.ark_api_key),
        "base_url": settings.ark_base_url,
        "max_references": settings.seedream_max_references,
        "enable_ai_svg": settings.enable_ai_svg,
        "sizes": ["1K", "1.5K", "2K", "1242x2208", "auto"],
    }


def runtime_config_path() -> Path:
    path = settings.data_dir / CONFIG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_saved_config() -> dict[str, Any]:
    """启动时应用界面上保存过的密钥 / 模型，免去改 .env 再重启。"""
    path = runtime_config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 配置文件坏了就用环境变量
        logger.warning("Seedream 本地配置无法解析，已忽略：%s", path)
        return {}
    if not isinstance(data, dict):
        return {}
    key = str(data.get("api_key") or "").strip()
    if key:
        settings.ark_api_key = key
    if data.get("model"):
        settings.seedream_model = str(data["model"])
    if data.get("base_url"):
        settings.ark_base_url = str(data["base_url"]).rstrip("/")
    return data


def _write_runtime_config(values: dict[str, Any]) -> None:
    path = runtime_config_path()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:  # noqa: BLE001
            data = {}
    data.update(values)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def save_api_key(key: str) -> None:
    cleaned = (key or "").strip()
    if not 8 <= len(cleaned) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in cleaned):
        raise ValidationFailed("ARK_API_KEY 格式不合法（需 8-4096 位可见 ASCII 字符）")
    settings.ark_api_key = cleaned
    _write_runtime_config({"api_key": cleaned})


def clear_api_key() -> None:
    settings.ark_api_key = ""
    _write_runtime_config({"api_key": ""})


def save_model(model: str) -> None:
    cleaned = (model or "").strip()
    if not cleaned:
        raise ValidationFailed("模型 ID 不能为空")
    settings.seedream_model = cleaned
    _write_runtime_config({"model": cleaned})


def project_root() -> Path:
    root = settings.seedream_project_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def project_dir(project_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,40}", project_id or ""):
        raise ValidationFailed("项目 ID 不合法")
    path = project_root() / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_file(project_id: str, name: str) -> Path | None:
    """把 ``/api/seedream/files/...`` 的剩余部分解析成磁盘路径（防目录穿越）。"""
    root = project_root().resolve()
    parts = [part for part in str(name).replace("\\", "/").split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    path = (root / project_id).joinpath(*parts).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


def file_url(project_id: str, relative: str) -> str:
    safe = "/".join(str(relative).replace("\\", "/").split("/"))
    return f"{FILE_PREFIX}/{project_id}/{safe}"


def _meta_path(project_id: str) -> Path:
    return project_dir(project_id) / "meta.json"


def fork_project(source_id: str) -> dict[str, Any]:
    """在他人作品上继续修改时，复制版本链到新项目，绝不污染原作者记录。"""
    source = load_project(source_id)
    meta = _new_project()
    inherited = [dict(version) for version in source.get("versions", [])]
    meta["versions"] = inherited
    meta["forked_from"] = source_id
    save_project(meta)
    return meta


def load_project(project_id: str) -> dict[str, Any]:
    path = _meta_path(project_id)
    if not path.is_file():
        return {"id": project_id, "versions": [], "created_at": _now()}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"id": project_id, "versions": [], "created_at": _now()}


def save_project(meta: dict[str, Any]) -> dict[str, Any]:
    path = _meta_path(meta["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return meta


def public_project(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": meta["id"],
        "versions": meta.get("versions", []),
        "decompositions": meta.get("decompositions", []),
        "created_at": meta.get("created_at"),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_project() -> dict[str, Any]:
    project_id = uuid.uuid4().hex[:12]
    project_dir(project_id)
    return save_project({"id": project_id, "versions": [], "decompositions": [], "created_at": _now()})


# ---------------------------------------------------------------------------
# 图片编解码
# ---------------------------------------------------------------------------
def _normalize_png(raw: bytes) -> tuple[bytes, int, int]:
    if not raw:
        raise InvalidImage("图片内容为空")
    if len(raw) > MAX_IMAGE_BYTES:
        raise PayloadTooLarge("图片超过 20MB")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:  # noqa: BLE001 - 交给统一错误
        raise InvalidImage(f"图片无法解析：{exc}") from exc
    image = image.convert("RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), image.width, image.height


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def decode_source(value: str | None) -> bytes:
    """接受 data URL、本地 /api 路径或 http(s) 地址，统一转成 PNG 字节。"""
    text = (value or "").strip()
    if not text:
        raise ValidationFailed("缺少图片内容")
    if text.startswith("data:"):
        _, _, payload = text.partition(",")
        try:
            return _normalize_png(base64.b64decode(payload, validate=False))[0]
        except (ValueError, base64.binascii.Error) as exc:
            raise InvalidImage("图片 Data URL 不合法") from exc
    if text.startswith(("http://", "https://")):
        try:
            with net.sync_client(timeout=60, follow_redirects=True) as client:
                response = client.get(text)
                response.raise_for_status()
                return _normalize_png(response.content)[0]
        except httpx.HTTPError as exc:
            raise SeedreamError(f"参考图下载失败：{exc}") from exc
    if text.startswith(FILE_PREFIX + "/"):
        rest = text[len(FILE_PREFIX) + 1:].split("?", 1)[0]
        project_id, _, name = rest.partition("/")
        path = project_file(project_id, name)
        if path is None:
            raise InvalidImage("本地图片不存在")
        return _normalize_png(path.read_bytes())[0]
    raise InvalidImage("不支持的图片来源")


def _annotation(base_png: bytes, mask_png: bytes) -> bytes:
    """把半透明红色标注画到原图副本上，作为方舟的第二张参考图。"""
    base = Image.open(io.BytesIO(base_png)).convert("RGBA")
    mask = Image.open(io.BytesIO(mask_png)).convert("RGBA")
    if mask.size != base.size:
        mask = mask.resize(base.size, Image.LANCZOS)
    red = Image.new("RGBA", base.size, (255, 45, 85, 0))
    alpha = mask.getchannel("A").point(lambda value: int(value * 0.55))
    red.putalpha(alpha)
    # 标注外沿描白线，保证在深色海报上依然清晰可辨
    edges = mask.getchannel("A").point(lambda value: 255 if value > 40 else 0)
    outline = Image.new("RGBA", base.size, (255, 255, 255, 0))
    outline.putalpha(edges.filter(ImageFilter.MaxFilter(5)))
    annotated = Image.alpha_composite(base, outline)
    annotated = Image.alpha_composite(annotated, red)
    buffer = io.BytesIO()
    annotated.convert("RGBA").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def candidate_bases() -> list[str]:
    """标准 ``/api/v3`` 与套餐 ``/api/plan/v3`` 互为兜底。

    “智能创作套餐”签发的 key 只在 plan 端点可用，标准 key 只在 ``/api/v3`` 可用，
    两边都试一次，成功的那个会被记下来复用，避免每次请求都多打一次。
    """
    order = [settings.ark_base_url.rstrip("/")]
    host = settings.ark_base_url.split("/api/")[0].rstrip("/")
    alternates = [f"{host}/api/plan/v3", f"{host}/api/v3"]
    for base in alternates:
        if base not in order:
            order.append(base)
    return order


async def _send(base: str, payload: dict[str, Any], *, timeout: float | None = None):
    url = f"{base}/images/generations"
    headers = {
        "Authorization": f"Bearer {settings.ark_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with net.async_client(timeout=timeout or settings.seedream_timeout) as client:
            return await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise SeedreamError(f"方舟请求失败：{exc}") from exc


def _remember_base(base: str) -> None:
    if base == settings.ark_base_url:
        return
    settings.ark_base_url = base
    try:
        _write_runtime_config({"base_url": base})
    except Exception:  # noqa: BLE001 - 记不住也不影响本次请求
        logger.warning("Seedream 端点偏好写入失败：%s", base)


def _error_message(response) -> str:
    try:
        body = response.json()
        return (
            (body.get("error") or {}).get("message")
            or (body.get("error") or {}).get("code")
            or body.get("message")
            or "方舟返回错误"
        )
    except ValueError:
        return response.text[:400] or "方舟返回错误"


async def _post(payload: dict[str, Any]) -> dict[str, Any]:
    if not configured():
        raise ValidationFailed("尚未配置 ARK_API_KEY，请先在 .env 中填写后重启服务")

    bases = candidate_bases()
    response = None
    used_base = bases[0]
    for index, base in enumerate(bases):
        response = await _send(base, payload)
        used_base = base
        # 只有鉴权失败才换端点；模型/参数错误说明端点是对的
        if response.status_code in {401, 403} and index + 1 < len(bases):
            continue
        break

    if response is None:  # pragma: no cover - 理论上不会发生
        raise SeedreamError("方舟请求失败：没有可用的接口地址")
    if response.status_code in {401, 403}:
        raise SeedreamError(
            "ARK_API_KEY 无效或未被授权（HTTP "
            f"{response.status_code}）：请到「API 设置 → 火山方舟 Seedream」重新粘贴密钥，"
            "或用「测试连接」确认"
        )
    if response.status_code >= 400:
        message = _error_message(response)
        if "UnsupportedModel" in message or "does not support" in message:
            message = (
                f"该接口只支持特定模型（当前 {settings.seedream_model}）：{message}。"
                "套餐端点目前仅支持 doubao-seedream-5-5-261028"
            )
        raise SeedreamError(f"方舟返回 {response.status_code}：{message}")

    _remember_base(used_base)
    try:
        return response.json()
    except ValueError as exc:
        raise SeedreamError("方舟返回内容不是合法 JSON") from exc


def _result_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容 data[] 结构与 layers[] 结构，返回 [{url, text_recognition}]。"""
    items: list[dict[str, Any]] = []
    for entry in body.get("data") or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url") or entry.get("image_url") or entry.get("image")
        if isinstance(url, dict):
            url = url.get("url")
        recognition = (
            entry.get("text_recognition")
            or entry.get("text_recognition_result")
            or entry.get("ocr")
        )
        if url:
            items.append({"url": url, "recognition": recognition})
    if not items:
        for entry in body.get("layers") or []:
            if isinstance(entry, dict) and entry.get("url"):
                items.append({"url": entry["url"], "recognition": entry.get("text_recognition")})
    return items


async def _download(url: str) -> bytes:
    if url.startswith("data:"):
        return decode_source(url)
    try:
        async with net.async_client(timeout=120, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return _normalize_png(response.content)[0]
    except httpx.HTTPError as exc:
        raise SeedreamError(f"结果图下载失败：{exc}") from exc


def _store_version(meta: dict[str, Any], png: bytes, *, prompt: str, kind: str, extra: dict | None = None) -> dict:
    index = len(meta.get("versions", [])) + 1
    name = f"v{index}.png"
    (project_dir(meta["id"]) / name).write_bytes(png)
    image = Image.open(io.BytesIO(png))
    version = {
        "id": f"v{index}",
        "index": index,
        "url": file_url(meta["id"], name),
        "width": image.width,
        "height": image.height,
        "prompt": prompt,
        "kind": kind,
        "created_at": _now(),
    }
    if extra:
        version.update(extra)
    meta.setdefault("versions", []).append(version)
    save_project(meta)
    return version


# ---------------------------------------------------------------------------
# 三个业务动作
# ---------------------------------------------------------------------------
async def generate(
    *,
    prompt: str,
    size: str = "2K",
    background: str = "opaque",
    images: Iterable[str] | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    text = (prompt or "").strip() or DEFAULT_PROMPT
    normalized_size = resolve_size(size)
    if not SIZE_PATTERN.match(normalized_size):
        raise ValidationFailed("尺寸只支持 1K / 1.5K / 2K / auto 或 WxH")
    if background not in {"opaque", "transparent"}:
        raise ValidationFailed("背景只支持 opaque / transparent")
    references = [ref for ref in (images or []) if str(ref).strip()]
    if len(references) > settings.seedream_max_references:
        raise ValidationFailed(f"参考图最多 {settings.seedream_max_references} 张")

    payload: dict[str, Any] = {
        "model": settings.seedream_model,
        "prompt": text,
        "size": normalized_size,
        "response_format": "url",
        "output_format": "png",
        "watermark": False,
        "background": background,
    }
    if references:
        payload["image"] = [_data_url(decode_source(ref)) for ref in references]

    body = await _post(payload)
    items = _result_items(body)
    if not items:
        raise SeedreamError("方舟未返回图片结果")
    png = await _download(items[0]["url"])

    meta = load_project(project_id) if project_id else _new_project()
    version = _store_version(meta, png, prompt=text, kind="generate")
    return {"project": public_project(meta), "version": version, "raw": body.get("usage") or {}}


async def edit(
    *,
    base_image: str,
    mask_image: str | None,
    instruction: str,
    size: str = "auto",
    project_id: str | None = None,
) -> dict[str, Any]:
    text = (instruction or "").strip()
    if not text:
        raise ValidationFailed("请填写修改说明")
    base_png = decode_source(base_image)
    normalized_size = resolve_size(size, base_png)
    if not SIZE_PATTERN.match(normalized_size):
        raise ValidationFailed("尺寸只支持 1K / 1.5K / 2K / auto 或 WxH")

    references = [_data_url(base_png)]
    if mask_image:
        references.append(_data_url(_annotation(base_png, decode_source(mask_image))))

    payload: dict[str, Any] = {
        "model": settings.seedream_model,
        "prompt": EDIT_PROMPT.format(instruction=text),
        "image": references,
        "size": normalized_size,
        "response_format": "url",
        "output_format": "png",
        "watermark": False,
        # 局改必须是实体底，透明底会导致边缘与标注区域失真
        "background": "opaque",
    }
    body = await _post(payload)
    items = _result_items(body)
    if not items:
        raise SeedreamError("方舟未返回修改结果")
    png = await _download(items[0]["url"])

    meta = load_project(project_id) if project_id else _new_project()
    version = _store_version(meta, png, prompt=text, kind="region" if mask_image else "global")
    return {"project": public_project(meta), "version": version, "raw": body.get("usage") or {}}


def upload_image(*, image: str, name: str = "", project_id: str | None = None) -> dict[str, Any]:
    """把用户直接上传的海报存成一条记录（版本链第一版）。"""
    png = decode_source(image)
    meta = load_project(project_id) if project_id else _new_project()
    version = _store_version(meta, png, prompt=name or "上传图片", kind="upload")
    return {"project": public_project(meta), "version": version}


async def decompose(
    *,
    image: str,
    max_images: int = 20,
    project_id: str | None = None,
    name: str = "",
    owner_id: str = "",
    team_id: str = "",
    scope: str = "public",
) -> dict[str, Any]:
    base_png = decode_source(image)
    limit = max(10, min(40, int(max_images or 20)))

    # 红线：拆图层模式严禁传 background
    payload: dict[str, Any] = {
        "model": settings.seedream_model,
        "prompt": DECOMPOSE_PROMPT,
        "image": _data_url(base_png),
        "layer_decomposition": True,
        "layer_decomposition_options": {
            "max_images": limit,
            "text_recognition": True,
        },
        "size": "auto",
        "response_format": "url",
        "output_format": "png",
        "watermark": False,
    }
    body = await _post(payload)
    items = _result_items(body)
    if not items:
        raise SeedreamError("方舟未返回图层结果")

    meta = load_project(project_id) if project_id else _new_project()
    job_dir = project_dir(meta["id"])
    layers_dir = job_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)

    base_name = "layers/base.png"
    (job_dir / base_name).write_bytes(_rasterize_base(base_png, await _download(items[0]["url"])))

    records: list[dict[str, Any]] = []
    for index, item in enumerate(items[1:], start=1):
        png = await _download(item["url"])
        filename = f"layers/layer-{index:02d}.png"
        (job_dir / filename).write_bytes(png)
        records.append(
            {
                "index": index,
                "url": file_url(meta["id"], filename),
                "path": filename,
                "text_recognition": item.get("recognition"),
            }
        )

    canvas = _image_size(base_png)
    for record in records:
        record["file"] = job_dir / record["path"]
    svg = svg_compose.compose(
        base_href=file_url(meta["id"], base_name),
        layers=records,
        canvas=canvas,
        job_dir=job_dir,
        asset_prefix=f"{FILE_PREFIX}/{meta['id']}",
    )
    for record in records:
        record.pop("file", None)
    template = import_template(
        svg,
        f"{name or 'seedream'}-拆层.svg",
        name=name or "",
        analyze=True,
        owner_id=owner_id,
        team_id=team_id,
        scope=scope,
    )
    record = {
        "id": uuid.uuid4().hex[:8],
        "job_id": meta["id"],
        "base_url": file_url(meta["id"], base_name),
        "layers": records,
        "template": template,
        "canvas": {"width": canvas[0], "height": canvas[1]},
        "created_at": _now(),
    }
    meta.setdefault("decompositions", []).append({
        "job_id": meta["id"],
        "template": template.get("name"),
        "layers": len(records),
        "created_at": record["created_at"],
    })
    save_project(meta)
    return {"project": public_project(meta), "decomposition": record}


def background_template(
    *,
    image: str,
    name: str = "",
    project_id: str | None = None,
    owner_id: str = "",
    team_id: str = "",
    scope: str = "public",
) -> dict[str, Any]:
    """无字底图直通编辑器：只封装一张受保护背景层，不调用拆图层接口。"""
    png = decode_source(image)
    meta = load_project(project_id) if project_id else _new_project()
    job_dir = project_dir(meta["id"])
    (job_dir / "bg.png").write_bytes(png)
    canvas = _image_size(png)
    svg = svg_compose.compose(
        base_href=file_url(meta["id"], "bg.png"),
        layers=[],
        canvas=canvas,
    )
    template = import_template(
        svg,
        f"{name or 'seedream'}-底图.svg",
        name=name or "",
        analyze=True,
        owner_id=owner_id,
        team_id=team_id,
        scope=scope,
    )
    return {"template": template, "canvas": {"width": canvas[0], "height": canvas[1]},
            "project": public_project(meta)}


async def verify_key(key: str | None = None) -> dict[str, Any]:
    """探测密钥是否可用：用不存在的模型发一次请求，401/403 才算密钥问题。"""
    target = (key or settings.ark_api_key or "").strip()
    if not target:
        return {"ok": False, "status": 0, "message": "还没有填写 ARK_API_KEY"}
    payload = {"model": "seedream-verify-probe-not-exists", "prompt": "ping", "size": "1K"}
    last_base, response = settings.ark_base_url, None
    for base in candidate_bases():
        try:
            async with net.async_client(timeout=20) as client:
                response = await client.post(
                    f"{base}/images/generations",
                    headers={"Authorization": f"Bearer {target}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            return {"ok": False, "status": 0, "message": f"网络不可达：{exc}"}
        last_base = base
        if response.status_code in {401, 403}:
            continue
        break
    if response is None:  # pragma: no cover
        return {"ok": False, "status": 0, "message": "没有可用的接口地址"}
    if response.status_code in {401, 403}:
        message = "密钥无效或没有权限"
        try:
            body = response.json()
            message = (body.get("error") or {}).get("message") or message
        except ValueError:
            pass
        return {"ok": False, "status": response.status_code, "message": message}
    _remember_base(last_base)
    return {
        "ok": True,
        "status": response.status_code,
        "message": f"密钥可用（{last_base} 返回 {response.status_code}，说明鉴权已通过）",
    }


def resolve_size(requested: str | None, base_png: bytes | None = None) -> str:
    """``size: auto`` 只允许配合 layer_decomposition。

    生图 / 局改必须给出具体尺寸：局改优先沿用原图尺寸（保持构图与像素一致），
    原图总像素不在方舟允许区间时退回 2K。
    """
    text = (requested or "").strip()
    if text and text.lower() != "auto":
        return text
    if base_png is not None:
        width, height = _image_size(base_png)
        if MIN_PIXELS <= width * height <= MAX_PIXELS:
            return f"{width}x{height}"
    return "2K"


def _image_size(png: bytes) -> tuple[int, int]:
    image = Image.open(io.BytesIO(png))
    return image.width, image.height


def _rasterize_base(original: bytes, returned: bytes) -> bytes:
    """底图以拆层接口返回为准，尺寸不一致时缩放回原图尺寸。"""
    base = Image.open(io.BytesIO(original)).convert("RGBA")
    layers = Image.open(io.BytesIO(returned)).convert("RGBA")
    if layers.size != base.size:
        layers = layers.resize(base.size, Image.LANCZOS)
    buffer = io.BytesIO()
    layers.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
