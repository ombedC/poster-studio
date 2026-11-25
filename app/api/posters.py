"""海报模板工作台 API。

模板以 ``data/templates/<name>.fixed.svg`` 为源文件：
- 列表 / 缩略图 / 解析（schema 落在同目录 ``<name>.schema.json``）
- 整段文案解析、纯 SVG 预览、高清 PNG 导出
- 老师头像 / 二维码素材库
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from lxml import etree
from PIL import Image

from app.config import settings
from app.errors import (
    InvalidImage,
    PayloadTooLarge,
    TemplateInvalid,
    TemplateNotFound,
    VectorArtboardChoice,
    UnsupportedMediaType,
    ValidationFailed,
)
from app.services import assets as asset_store, vector_importer, template_upload, template_resources
from app.api.workspace import current_workspace
from app.services import (
    fonts,
    image_processor,
    pipeline,
    poster_semantics,
    svg_browser,
    svg_inline,
    template_meta,
)
from app.services import template_analyzer as analyzer
from app.services import template_renderer as renderer
from app.services.template_import import import_template
from app.services.face_detector import get_detector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/posters", tags=["posters"])

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_EXPORT_SVG_BYTES = 50 * 1024 * 1024
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
_JOB_RE = re.compile(r"^[0-9a-f]{6,32}$")
_ASSET_FILE_RE = re.compile(r"^[0-9a-f]{8,32}$")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _template_url(name: str, suffix: str) -> str:
    return f"/api/v1/posters/templates/{quote(name, safe='')}/{suffix}"


def _asset_url(asset_id: str, *, thumbnail: bool = False) -> str:
    kind = "thumb" if thumbnail else "file"
    return f"/api/v1/posters/assets/{kind}/{asset_id}"


def _asset_payload(item: dict) -> dict:
    return {
        **item,
        "url": _asset_url(item["id"]),
        "thumbnail_url": _asset_url(item["id"], thumbnail=True),
    }


def _read_upload(raw: bytes, *, label: str = "图片") -> bytes:
    if not raw:
        raise InvalidImage(f"{label}内容为空")
    if len(raw) > MAX_IMAGE_BYTES:
        raise PayloadTooLarge(f"{label}超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB")
    return raw


def _load_image(raw: bytes):
    array = image_processor.decode_image(raw)
    height, width = array.shape[:2]
    if width < 8 or height < 8:
        raise ValidationFailed("图片尺寸过小")
    return array


def _resolve_image_value(value: str) -> bytes:
    """把 data URL / 素材 URL / 素材 ID / 本地路径 / 裸 base64 还原成字节。"""
    text = (value or "").strip()
    if not text:
        raise ValidationFailed("图片内容为空")

    if text.startswith("data:"):
        _, _, payload = text.partition(",")
        try:
            return base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise InvalidImage(f"data URL 解码失败：{exc}") from exc

    parsed = urlparse(text)
    path = parsed.path if parsed.scheme or parsed.netloc else text
    match = re.search(r"/assets/(?:file|thumb)/([0-9a-f]{8,32})$", path)
    if match:
        asset_path = asset_store.asset_path(match.group(1))
        if asset_path is None:
            raise ValidationFailed(f"素材不存在：{match.group(1)}")
        return asset_path.read_bytes()

    if _ASSET_FILE_RE.match(text):
        asset_path = asset_store.asset_path(text)
        if asset_path is not None:
            return asset_path.read_bytes()

    if text.startswith("data/assets/") or text.startswith("assets/"):
        relative = text[len("data/") :] if text.startswith("data/") else text
        candidate = (settings.data_dir / relative).resolve()
        if settings.data_dir.resolve() in candidate.parents and candidate.is_file():
            return candidate.read_bytes()
        raise ValidationFailed(f"素材路径不存在：{text}")

    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailed("无法识别的图片引用") from exc


def _resolve_images(images: dict[str, str] | None) -> tuple[dict[str, bytes], list[str]]:
    resolved: dict[str, bytes] = {}
    warnings: list[str] = []
    for key, value in (images or {}).items():
        if not value:
            continue
        try:
            resolved[key] = _resolve_image_value(value)
        except Exception as exc:
            warnings.append(f"图片 {key} 加载失败：{exc}")
    return resolved, warnings


def _ensure_schema(name: str) -> dict:
    if analyzer.is_named_template_analyzed(name):
        try:
            schema = analyzer.load_named_schema(name)
        except TemplateInvalid:
            pass
        else:
            if schema.get("version", 1) >= analyzer.SCHEMA_VERSION:
                return schema
    analyzer.analyze_named_template(name, force=True, use_llm=True)
    return analyzer.load_named_schema(name)


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------
@router.post("/templates/inspect", summary="探测 .ai/.eps 的画板列表（含空白判断与缩略图）")
def inspect_template_boards(file: UploadFile = File(...)) -> JSONResponse:
    filename = file.filename or "template.ai"
    staged = None
    try:
        staged, _ = template_upload.stage_upload(file.file, filename)
        info = vector_importer.inspect_pages(staged, filename)
        # Keep the staged source so the confirm step never re-uploads the whole file; TTL cleans it.
        info["staged_token"] = template_upload.stage_token(staged)
        staged = None
    except PayloadTooLarge as exc:
        raise HTTPException(413, detail=exc.message) from None
    except TemplateInvalid as exc:
        raise HTTPException(422, detail=exc.message) from None
    finally:
        if staged:
            template_upload.remove_staged(staged)
    info["filename"] = filename
    return JSONResponse(info, headers={"Cache-Control": "no-store"})


@router.post("/templates/upload", summary="上传 SVG / AI / EPS 模板并清洗、识别")
def upload_template(
    request: Request,
    file: UploadFile | None = File(None),
    staged_token: str = Form(""),
    filename: str = Form(""),
    name: str = Form(""),
    overwrite: bool = Form(False),
    analyze: bool = Form(True),
    scope: str = Form("public"),
    page: int = Form(0),
) -> JSONResponse:
    ws = current_workspace(request)
    if file is None and not staged_token:
        raise ValidationFailed("缺少模板文件或暂存模板令牌")
    filename = file.filename if file is not None else str(filename or "template.ai")
    staged = None
    owns_staged = False
    consume_staged = False
    try:
        if staged_token:
            if file is not None:
                raise ValidationFailed("不能同时提交文件与暂存令牌")
            staged = template_upload.resolve_stage(staged_token)
            filename = str(filename or "template.ai")
        else:
            staged, _ = template_upload.stage_upload(file.file, filename)
            owns_staged = True
        suffix = Path(filename).suffix.lower()
        if suffix in {".ai", ".eps"} and not staged_token:
            info = vector_importer.inspect_pages(staged, filename)
            if not info["non_blank"]:
                raise HTTPException(422, detail="这个文件的画板全是空白，无法导入")
            if info["needs_choice"] and not page:
                token = staged_token or template_upload.stage_token(staged)
                if not staged_token:
                    staged = None
                return JSONResponse(
                    {"status": "choose_artboard", **info, "staged_token": token, "filename": filename},
                    headers={"Cache-Control": "no-store"},
                )
        consume_staged = bool(staged_token)
        result = import_template(
            staged,
            filename,
            name=name,
            overwrite=overwrite,
            analyze=analyze,
            owner_id=ws.user_id,
            team_id=ws.team_id,
            scope=scope if scope in ("public", "team", "private") else "public",
            page=page,
        )
    except VectorArtboardChoice as exc:
        token = staged_token or template_upload.stage_token(staged)
        if staged_token:
            template_upload.remove_staged(staged)
            staged = None
            consume_staged = False
        else:
            staged = None
        return JSONResponse(
            {"status": "choose_artboard", **exc.detail, "staged_token": token, "filename": filename},
            headers={"Cache-Control": "no-store"},
        )
    except PayloadTooLarge as exc:
        raise HTTPException(413, detail=exc.message) from None
    except TemplateInvalid as exc:
        raise HTTPException(422, detail=exc.message) from None
    finally:
        if staged and (owns_staged or consume_staged):
            template_upload.remove_staged(staged)
    return JSONResponse(result)


@router.get("/templates", summary="模板画廊列表（按可见范围筛选）")
def list_templates(
    request: Request,
    view: Literal["public", "team", "mine"] = "public",
    q: str = "",
) -> JSONResponse:
    ws = current_workspace(request)
    metadata = template_meta.metadata_map()
    needle = q.strip().lower()
    items = []
    for item in analyzer.list_named_templates():
        meta = metadata.get(item["name"])
        scope = (meta or {}).get("scope") or "public"
        owner_id = (meta or {}).get("owner_id") or ""
        team_id = (meta or {}).get("team_id") or ""
        owned = bool(owner_id) and owner_id == ws.user_id
        if view == "mine" and not owned:
            continue
        if view == "team":
            in_team = bool(ws.team_id) and team_id == ws.team_id
            if not (owned or in_team or scope == "public"):
                continue
            if scope == "private" and not owned:
                continue
        if view == "public" and scope != "public":
            continue
        if needle and needle not in item["name"].lower() and needle not in item["display_name"].lower():
            continue
        items.append({
            **item,
            "thumbnail_url": _template_url(item["name"], "thumbnail")
            + f"?v={fonts.font_revision()}",
            "scope": scope,
            "owner_id": owner_id,
            "team_id": team_id,
            "owned": owned,
        })
    return JSONResponse(items, headers={"Cache-Control": "no-store"})


@router.patch("/templates/{name}/visibility", summary="切换模板可见范围（仅作者）")
def set_template_visibility(request: Request, name: str, payload: dict) -> JSONResponse:
    ws = current_workspace(request)
    if not analyzer.source_svg_path(name).is_file():
        raise TemplateNotFound(f"模板不存在：{name}")
    template_meta.ensure(name)
    scope = str(payload.get("scope") or "").strip()
    try:
        meta = template_meta.update_scope(name, ws, scope)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    return JSONResponse({"template_id": name, **(meta or {})}, headers={"Cache-Control": "no-store"})


@router.get("/templates/{name}/thumbnail", summary="模板缩略图（JPEG，320px 宽）")
def template_thumbnail(name: str) -> FileResponse:
    source = analyzer.source_svg_path(name)
    if not source.is_file():
        raise TemplateNotFound(f"模板文件不存在：{source.name}")
    thumb = settings.template_dir / "thumbs" / f"{analyzer._safe_name(name)}.jpg"
    revision = fonts.font_revision()
    revision_file = thumb.with_suffix(".rev")
    stale = (
        not thumb.is_file()
        or not revision_file.is_file()
        or revision_file.read_text(encoding="utf-8").strip() != revision
    )
    if stale:
        canvas = analyzer.svg_canvas(source)
        svg_browser.thumbnail_svg(source, canvas, thumb, width=320)
        revision_file.write_text(revision, encoding="utf-8")
    return FileResponse(
        thumb,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache"},
    )


def _delete_template_files(name: str) -> None:
    source = analyzer.source_svg_path(name)
    resource_dir = template_resources.resource_directory(name)
    if resource_dir.is_dir():
        import shutil
        shutil.rmtree(resource_dir, ignore_errors=True)
    for path in (
        source,
        analyzer.named_schema_path(name),
        analyzer.named_svg_path(name),
        analyzer.revival_backup_path(name),
        settings.template_dir / "thumbs" / f"{name}.jpg",
        settings.template_dir / "thumbs" / f"{name}.rev",
    ):
        if path.is_file():
            path.unlink()


@router.delete("/templates/{name}", summary="删除模板及其解析缓存")
def delete_template(request: Request, name: str) -> JSONResponse:
    ws = current_workspace(request)
    source = analyzer.source_svg_path(name)
    if not source.is_file():
        raise TemplateNotFound(f"模板不存在：{name}")
    template_meta.require_owner(name, ws, "只有模板作者可以删除该模板")
    _delete_template_files(name)
    with __import__("app.services.db", fromlist=["db"]).connect() as conn:
        conn.execute("DELETE FROM template_meta WHERE template_id = ?", (name,))
    return JSONResponse(
        {"deleted": True, "name": name, "display_name": analyzer.display_name_of(name)}
    )


@router.post("/templates/{name}/analyze", summary="解析模板并保存 schema.json")
def analyze_template(
    name: str,
    force: bool = Query(False, description="忽略缓存重新解析"),
) -> JSONResponse:
    revival = {"available": False, "revived": [], "skipped": [], "warnings": [], "revived_count": 0}
    if force:
        thumb = settings.template_dir / "thumbs" / f"{analyzer._safe_name(name)}.jpg"
        thumb.unlink(missing_ok=True)
        try:
            revival = analyzer.revive_named_template(name, use_llm=True, retry=True)
        except Exception as exc:
            revival["warnings"].append(f"转曲文字复活失败，已保留原路径：{exc}")
    schema = analyzer.analyze_named_template(name, force=force, use_llm=True)
    schema_warnings = list(schema.get("warnings") or [])
    schema_warnings.extend(revival.get("warnings") or [])
    return JSONResponse(
        {
            "name": schema.get("name", name),
            "display_name": schema.get("display_name", analyzer.display_name_of(name)),
            "analyzed": True,
            "canvas": schema["canvas"],
            "source": schema.get("source"),
            "text_count": len(schema.get("texts", [])),
            "image_count": len(schema.get("images", [])),
            "portrait_count": len(schema.get("portraits", [])),
            "warnings": schema_warnings,
            "revived_texts": revival.get("revived_count", 0),
            "revival_report": revival,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/templates/{name}/schema", summary="读取模板 schema")
def template_schema(name: str) -> JSONResponse:
    schema = _ensure_schema(name)
    return JSONResponse(
        {
            **schema,
            "display_name": schema.get("display_name", analyzer.display_name_of(name)),
        },
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# 文案 / 预览 / 导出
# ---------------------------------------------------------------------------
def _match_teacher_assets(schema: dict, values: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    names = {
        entry.get("teacher_slot", entry.get("slot", 0)): poster_semantics.extract_teacher_name(values.get(entry["id"], ""))
        for entry in schema.get("texts", [])
        if (entry.get("role") or entry.get("field")) == "teacher_name" and entry["id"] in values
    }
    if not names:
        return {}, []
    assets = asset_store.list_assets("teacher", limit=200)
    matched: dict[str, str] = {}
    unmatched: list[str] = []
    for entry in schema.get("portraits", []):
        name = names.get(entry["slot"])
        if not name:
            continue
        choices = [item for item in assets if poster_semantics.extract_teacher_name(Path(item.get("label") or "").stem) == name]
        preferred = [item for item in choices if item.get("mode") == entry.get("mode")]
        choices = preferred or choices
        if len(choices) == 1:
            matched[entry["key"]] = _asset_url(choices[0]["id"])
        else:
            unmatched.append(entry["key"])
    return matched, unmatched


@router.post("/parse-text", summary="整段排课文案 → 结构化表单值")
def parse_text(payload: dict) -> JSONResponse:
    template_name = str(payload.get("template_name") or "").strip()
    raw_text = str(payload.get("raw_text") or "").strip()
    if not template_name:
        raise ValidationFailed("缺少 template_name")
    schema = _ensure_schema(template_name)
    today = datetime.now(timezone(timedelta(hours=8))).date()
    current_values = payload.get("current_values")
    if not isinstance(current_values, dict):
        current_values = {}
    ids = {entry['id'] for entry in schema.get('texts', [])}
    current_values = {key: value for key, value in current_values.items() if key in ids and isinstance(value, str)}
    if len(raw_text) + sum(map(len, current_values.values())) > 120_000:
        raise PayloadTooLarge('排课输入及当前画布文案总长超过 12 万字符')
    patch = renderer.parse_text_patch(
        schema, raw_text, use_llm=True, today=today, current_values=current_values
    )
    values = patch['values']
    matched, unmatched = _match_teacher_assets(schema, values)
    return JSONResponse(
        {
            "template_name": template_name,
            "intent": patch['intent'],
            "patches": patch['patches'],
            "engine": patch['engine'],
            "values": values,
            "warnings": patch['warnings'],
            "images": matched,
            "unmatched_avatar_keys": unmatched,
        }
    )


@router.post("/preview", summary="快速套版（纯 SVG，不启动浏览器）")
def preview(payload: dict) -> Response:
    template_name = str(payload.get("template_name") or "").strip()
    if not template_name:
        raise ValidationFailed("缺少 template_name")
    schema = _ensure_schema(template_name)
    values = payload.get("data") or {}
    if not isinstance(values, dict):
        raise ValidationFailed("data 必须是 {节点ID: 文字} 对象")
    styles = payload.get("styles") or {}
    if not isinstance(styles, dict):
        styles = {}
    images, warnings = _resolve_images(payload.get("images") or {})
    exact_ids = payload.get("exact_text_ids")
    exact_text_ids = frozenset(
        field_id for field_id in exact_ids if isinstance(field_id, str)
    ) if isinstance(exact_ids, list) else frozenset()
    svg, svg_warnings = renderer.build_svg(schema, values, images, styles, exact_text_ids=exact_text_ids)
    warnings.extend(svg_warnings)
    if warnings:
        logger.info("预览告警 %s：%s", template_name, warnings)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/export", summary="高清 PNG 导出（Playwright, device_scale_factor=2）")
def export(payload: dict) -> JSONResponse:
    template_name = str(payload.get("template_name") or "").strip()
    if not template_name:
        raise ValidationFailed("缺少 template_name")
    schema = _ensure_schema(template_name)
    values = payload.get("data") or {}
    if not isinstance(values, dict):
        raise ValidationFailed("data 必须是 {节点ID: 文字} 对象")
    styles = payload.get("styles") or {}
    if not isinstance(styles, dict):
        styles = {}
    images, warnings = _resolve_images(payload.get("images") or {})
    try:
        scale = int(payload.get("scale") or settings.poster_render_scale)
    except (TypeError, ValueError):
        scale = settings.poster_render_scale

    result = renderer.export_poster(schema, values, images, scale=scale, styles=styles)
    warnings.extend(result.warnings)
    filename = f"{schema.get('display_name') or analyzer.display_name_of(template_name)}.png"
    filename = re.sub(r'[\\/:*?"<>|]', "_", filename)
    return JSONResponse(
        {
            "job_id": result.job_id,
            "url": f"/api/v1/posters/files/{result.job_id}/poster.png",
            "svg_url": f"/api/v1/posters/files/{result.job_id}/poster.svg",
            "filename": filename,
            "width": result.width,
            "height": result.height,
            "warnings": warnings,
            "font_adjustments": result.font_adjustments,
            "timings": result.timings,
        }
    )


@router.post("/export-svg", summary="导出当前画布 SVG（保留 Moveable/局部样式修改）")
def export_svg(payload: dict) -> JSONResponse:
    svg_text = payload.get("svg") or ""
    cleaned = _prepare_export_svg(str(svg_text))
    job_id = uuid.uuid4().hex[:12]
    job_dir = settings.output_dir / "posters" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    svg_path = job_dir / "poster.svg"
    svg_path.write_bytes(cleaned)
    canvas = analyzer.svg_canvas(svg_path)
    try:
        scale = int(payload.get("scale") or settings.poster_render_scale)
    except (TypeError, ValueError):
        scale = settings.poster_render_scale
    scale = max(1, min(settings.poster_max_scale, scale))
    png_path = job_dir / "poster.png"
    width, height = svg_browser.screenshot_svg(svg_path, canvas, png_path, scale=scale)
    filename = re.sub(r'[\\/:*?"<>|]', "_", str(payload.get("filename") or "poster.png"))
    return JSONResponse(
        {
            "job_id": job_id,
            "url": f"/api/v1/posters/files/{job_id}/poster.png",
            "svg_url": f"/api/v1/posters/files/{job_id}/poster.svg",
            "filename": filename,
            "width": width,
            "height": height,
        }
    )


@router.get("/files/{job_id}/{filename}", summary="读取导出产物")
def get_export_file(job_id: str, filename: str) -> FileResponse:
    if not _JOB_RE.match(job_id):
        raise TemplateNotFound("导出任务不存在")
    if filename not in {"poster.png", "poster.svg"}:
        raise TemplateNotFound("文件不存在")
    path = (settings.output_dir / "posters" / job_id / filename).resolve()
    if settings.output_dir.resolve() not in path.parents or not path.is_file():
        raise TemplateNotFound("导出文件不存在")
    media = "image/png" if filename.endswith(".png") else "image/svg+xml"
    return FileResponse(path, media_type=media, filename=f"poster-{job_id}{path.suffix}")


def _prepare_export_svg(svg_text: str) -> bytes:
    if not svg_text or not svg_text.strip():
        raise TemplateInvalid("SVG 内容为空")
    if len(svg_text.encode("utf-8")) > MAX_EXPORT_SVG_BYTES:
        raise PayloadTooLarge("SVG 超过 50MB")
    parser = etree.XMLParser(recover=True, huge_tree=True)
    try:
        root = etree.fromstring(svg_text.encode("utf-8"), parser=parser)
    except Exception as exc:
        raise TemplateInvalid(f"SVG 无法解析：{exc}") from exc
    if root is None or not isinstance(root.tag, str) or not root.tag.endswith("svg"):
        raise TemplateInvalid("导出内容不是有效的 SVG（根节点不是 <svg>）")
    if not root.tag.startswith(f"{{{SVG_NS}}}"):
        root.set("xmlns", SVG_NS)
    if not root.get("viewBox"):
        width = root.get("width")
        height = root.get("height")
        if width and height:
            try:
                root.set("viewBox", f"0 0 {float(width):g} {float(height):g}")
            except ValueError:
                pass
    cleaned = etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)
    text = cleaned.decode("utf-8")
    # lxml 不能在已有节点上直接补 xmlns:xlink，序列化后注入根节点
    match = re.search(r"<svg\b[^>]*>", text)
    if match:
        tag = match.group(0)
        additions = []
        if "xmlns=" not in tag:
            additions.append(f'xmlns="{SVG_NS}"')
        if "xmlns:xlink" not in tag:
            additions.append(f'xmlns:xlink="{XLINK_NS}"')
        if additions:
            text = text[: match.start()] + tag[:-1] + " " + " ".join(additions) + ">" + text[match.end() :]
    # 导出的 SVG 需要可移植：把站内图片引用内联成 data URI（编辑态 DOM 不受影响）
    return svg_inline.inline_local_images(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# 素材库
# ---------------------------------------------------------------------------
@router.post("/assets/upload", summary="上传并处理老师头像 / 二维码，留存到素材库")
async def upload_asset(
    file: UploadFile = File(...),
    kind: str = Form("teacher"),
    mode: str = Form("head_shoulder"),
    width: int = Form(0),
    height: int = Form(0),
    label: str = Form(""),
) -> JSONResponse:
    if kind not in {"teacher", "qrcode"}:
        raise ValidationFailed(f"不支持的素材类型：{kind}")
    raw = _read_upload(await file.read(), label="上传图片")
    image = _load_image(raw)
    warnings: list[str] = []

    if kind == "teacher":
        target_w = width if width >= 64 else 900
        target_h = height if height >= 64 else target_w
        result = pipeline.run_pipeline(
            image,
            mode=mode,
            target_size=(target_w, target_h),
            detector=get_detector(),
        )
        png = image_processor.encode_png(result.image)
        warnings.extend(result.warnings)
        width_out, height_out = result.image.shape[1], result.image.shape[0]
    else:
        qr = Image.fromarray(image, "RGBA").convert("RGB")
        qr.thumbnail((900, 900), Image.LANCZOS)
        buffer = io.BytesIO()
        qr.save(buffer, format="PNG")
        png = buffer.getvalue()
        width_out, height_out = qr.width, qr.height

    item = asset_store.add_asset(
        kind,
        png,
        mode=mode if kind == "teacher" else "",
        label=label or (file.filename or ""),
        width=width_out,
        height=height_out,
    )
    return JSONResponse(
        {
            "asset": _asset_payload(item),
            "warnings": warnings,
        }
    )


@router.get("/assets", summary="历史素材列表")
def list_assets(kind: str | None = Query(None), limit: int = Query(60, ge=1, le=200)) -> list[dict]:
    if kind and kind not in {"teacher", "qrcode"}:
        raise ValidationFailed(f"不支持的素材类型：{kind}")
    return [_asset_payload(item) for item in asset_store.list_assets(kind, limit=limit)]


@router.get("/assets/file/{asset_id}", summary="素材原图")
def asset_file(asset_id: str) -> FileResponse:
    path = asset_store.asset_path(asset_id)
    if path is None:
        raise TemplateNotFound("素材不存在")
    return FileResponse(path, media_type="image/png")


@router.get("/assets/thumb/{asset_id}", summary="素材缩略图")
def asset_thumb(asset_id: str) -> FileResponse:
    path = asset_store.asset_path(asset_id, thumbnail=True)
    if path is None:
        raise TemplateNotFound("素材不存在")
    return FileResponse(path, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
@router.get("/fonts", summary="可选字体与字重")
def available_fonts() -> JSONResponse:
    weights = sorted({face.weight for face in fonts.discover_fonts()})
    return JSONResponse(
        {
            "families": [
                {
                    "family": "Alibaba PuHuiTi 3.0",
                    "label": "阿里巴巴普惠体 3.0",
                    "weights": weights or [400, 500, 700, 800, 900],
                }
            ]
        }
    )


@router.get("/fonts.css", include_in_schema=False)
def fonts_css() -> Response:
    return Response(
        content=fonts.web_font_css(),
        media_type="text/css",
        headers={"Cache-Control": "no-store, max-age=0"},
    )
