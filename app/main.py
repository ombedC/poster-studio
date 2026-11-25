"""FastAPI 入口：``uvicorn app.main:app --reload``。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import re

from app.api import ai_svg, images, posters, seedream, workspace
from app.config import settings
from app.errors import AppError
from app.schemas import HealthResponse
from app.services import db, fonts, seedream as seedream_service, llm_client
from app.services.image_processor import rembg_available

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    for directory in (
        settings.output_dir,
        settings.model_dir,
        settings.template_dir,
        settings.data_dir / "assets",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    seedream_service.load_saved_config()
    db.init()
    template_count = len(list(settings.template_dir.glob("*.fixed.svg")))
    logger.info("数据目录：%s", settings.data_dir)
    logger.info("海报模板：%s（%d 个）", settings.template_dir, template_count)
    logger.info(
        "模板上传限制：MAX_TEMPLATE_UPLOAD_MB=%s（%s）",
        settings.max_template_upload_mb,
        "不限制" if not settings.max_template_upload_bytes else f"{settings.max_template_upload_bytes} 字节",
    )
    await ai_svg.start()
    try:
        yield
    finally:
        await ai_svg.stop()


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "本地运行的人像素材工作台：rembg 去背景 → MediaPipe/OpenCV 人脸检测 → "
        "智能头肩裁切 → 输出海报尺寸透明底 PNG。"
    ),
    lifespan=lifespan,
)

if settings.enable_cors:
    # 仅本机使用的开发工具，放开跨域方便前端原型联调
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(images.router)
app.include_router(posters.router)
app.include_router(ai_svg.router)
app.include_router(seedream.router)
app.include_router(workspace.router)


@app.exception_handler(AppError)
async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message, "detail": exc.detail},
    )


@app.exception_handler(RequestValidationError)
async def handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    """把 FastAPI 的参数校验错误统一成前端的 {error, message, detail} 结构。"""
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', '')}"
        for error in exc.errors()
    )
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": f"请求参数不合法：{problems}" if problems else "请求参数不合法",
            "detail": jsonable_encoder(exc.errors()),
        },
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html")


@app.get("/api/v1/health", response_model=HealthResponse, tags=["system"], summary="健康检查")
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=settings.version,
        rembg={
            "available": rembg_available(),
            "model": settings.rembg_model,
            "required": settings.rembg_required,
        },
        data_dir=str(settings.data_dir),
        fonts=fonts.font_summary(),
        llm={
            "configured": llm_client.llm_configured(),
            "model": settings.llm_model,
            "base_url": settings.llm_base_url,
            "env_file": str(settings.base_dir / ".env"),
        },
        features={
            "ai_svg": settings.enable_ai_svg,
            "seedream": bool(settings.seedream_enabled and settings.ark_api_key),
        },
        limits={
            "max_template_upload_mb": settings.max_template_upload_mb,
            "max_template_upload_bytes": settings.max_template_upload_bytes,
        },
    )


@app.get("/api/v1/posters/templates/{name}/resources/{filename}", include_in_schema=False)
def template_resource(name: str, filename: str) -> FileResponse:
    from app.services.template_analyzer import _safe_name
    safe_name = _safe_name(name)
    if filename != Path(filename).name or not re.fullmatch(r"image-[0-9a-f]{32}\.(?:png|jpg)", filename):
        raise posters.TemplateNotFound("模板图片资源不存在")
    from app.services import template_resources
    root = template_resources.resource_directory(safe_name).resolve()
    path = (root / filename).resolve()
    if root not in path.parents or not path.is_file():
        raise posters.TemplateNotFound("模板图片资源不存在")
    return FileResponse(path, media_type="image/png" if path.suffix == ".png" else "image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


# 前端静态资源（Tailwind / Alpine 走 CDN，这里只托管本地页面）
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
