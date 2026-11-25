"""图像处理接口。

每个接口都只做三件事：校验入参、调用服务层、把结果落盘并返回元数据。
所有接口都写成同步函数（``def``），FastAPI 会把它们放进线程池，
抠图 / 检测这类 CPU 密集任务不会阻塞事件循环。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.config import settings
from app.errors import (
    FaceDetectionUnavailable,
    InvalidImage,
    PayloadTooLarge,
    UnsupportedMediaType,
    ValidationFailed,
)
from app.schemas import (
    AnalyzeResponse,
    ArtifactOut,
    CropBoxOut,
    CutoutResponse,
    FaceBoxOut,
    ProcessResponse,
    SizeOut,
)
from app.services import image_processor as ip
from app.services import pipeline
from app.services.face_detector import FaceDetector, get_detector

router = APIRouter(prefix="/api/v1/images", tags=["images"])


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _read_upload(file: UploadFile) -> bytes:
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type and content_type not in settings.allowed_mimes:
        raise UnsupportedMediaType(
            f"不支持的图片格式：{content_type}，请上传 JPG / PNG / WebP / BMP"
        )
    data = file.file.read(settings.max_upload_bytes + 1)
    if not data:
        raise InvalidImage("上传内容为空")
    if len(data) > settings.max_upload_bytes:
        raise PayloadTooLarge(f"图片超过 {settings.max_upload_mb} MB 上限")
    return data


def _validate_target(width: int, height: int) -> tuple[int, int]:
    if width < settings.min_output_side or height < settings.min_output_side:
        raise ValidationFailed(f"目标尺寸不能小于 {settings.min_output_side}px")
    if width > settings.max_output_side or height > settings.max_output_side:
        raise ValidationFailed(f"目标尺寸不能大于 {settings.max_output_side}px")
    return width, height


def _validate_ratios(top_ratio: float | None, shoulder_ratio: float | None) -> None:
    if top_ratio is not None and not (0.0 <= top_ratio <= 1.5):
        raise ValidationFailed("top_ratio 需在 0 ~ 1.5 之间")
    if shoulder_ratio is not None and not (0.0 <= shoulder_ratio <= 2.0):
        raise ValidationFailed("shoulder_ratio 需在 0 ~ 2.0 之间")


def _validate_mode(mode: str) -> str:
    normalized = (mode or "").strip().lower()
    if normalized not in ip.PORTRAIT_MODES:
        raise ValidationFailed(
            f"未知的排版模式：{mode!r}，可选：{', '.join(ip.PORTRAIT_MODES)}"
        )
    return normalized


def _validate_border(border_width: int, border_color: str) -> None:
    if not 0 <= border_width <= 512:
        raise ValidationFailed("border_width 需在 0 ~ 512 之间")
    try:
        ip.parse_border_color(border_color)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _face_out(face: ip.FaceBox | None) -> FaceBoxOut | None:
    if face is None:
        return None
    return FaceBoxOut(**face.as_dict())


def _save_png(image, request_id: str, suffix: str) -> ArtifactOut:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{request_id}_{suffix}.png"
    payload = ip.encode_png(image)
    (settings.output_dir / filename).write_bytes(payload)
    return ArtifactOut(
        url=f"{settings.api_prefix}/images/files/{filename}",
        filename=filename,
        width=int(image.shape[1]),
        height=int(image.shape[0]),
        bytes=len(payload),
    )


def _manual_face(
    face_x: int | None,
    face_y: int | None,
    face_w: int | None,
    face_h: int | None,
) -> ip.FaceBox | None:
    values = (face_x, face_y, face_w, face_h)
    if all(value is None for value in values):
        return None
    if not all(value is not None for value in values):
        raise ValidationFailed("手动人脸框需同时提供 face_x / face_y / face_w / face_h")
    try:
        return ip.FaceBox(
            x=int(face_x), y=int(face_y), w=int(face_w), h=int(face_h),
            confidence=1.0, backend="manual",
        )
    except ValueError as exc:
        raise ValidationFailed(f"手动人脸框无效：{exc}") from exc


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
@router.post("/analyze", response_model=AnalyzeResponse, summary="检测人脸并预览裁切框（不落盘）")
def analyze_image(
    file: UploadFile = File(...),
    target_width: int = Form(1080),
    target_height: int = Form(1440),
    top_ratio: float = Form(ip.DEFAULT_TOP_RATIO),
    shoulder_ratio: float = Form(ip.DEFAULT_SHOULDER_RATIO),
    detector: FaceDetector = Depends(get_detector),
) -> AnalyzeResponse:
    request_id = _new_request_id()
    _validate_target(target_width, target_height)
    _validate_ratios(top_ratio, shoulder_ratio)

    started = time.perf_counter()
    image = ip.decode_image(_read_upload(file))
    timings = {"decode_ms": round((time.perf_counter() - started) * 1000, 1)}

    started = time.perf_counter()
    try:
        face = detector.detect(image)
    except FaceDetectionUnavailable:
        raise
    except Exception as exc:
        raise FaceDetectionUnavailable(f"人脸检测失败：{exc}") from exc
    timings["detect_ms"] = round((time.perf_counter() - started) * 1000, 1)

    box, warnings, _ = ip.compute_crop_box(
        (image.shape[1], image.shape[0]),
        face,
        target_width / target_height,
        top_ratio,
        shoulder_ratio,
    )
    return AnalyzeResponse(
        request_id=request_id,
        source=SizeOut(width=int(image.shape[1]), height=int(image.shape[0])),
        face=_face_out(face),
        face_detected=face is not None,
        backend=detector.name,
        target=SizeOut(width=target_width, height=target_height),
        suggested_crop=CropBoxOut(**box.as_dict()),
        warnings=warnings,
        timings=timings,
    )


@router.post("/cutout", response_model=CutoutResponse, summary="仅去背景，输出透明底 PNG")
def cutout_image(file: UploadFile = File(...)) -> CutoutResponse:
    request_id = _new_request_id()
    started = time.perf_counter()
    image = ip.decode_image(_read_upload(file))
    timings = {"decode_ms": round((time.perf_counter() - started) * 1000, 1)}

    started = time.perf_counter()
    cutout = ip.remove_background(image, model=settings.rembg_model)
    timings["cutout_ms"] = round((time.perf_counter() - started) * 1000, 1)

    artifact = _save_png(cutout, request_id, "cutout")
    return CutoutResponse(
        request_id=request_id,
        source=SizeOut(width=int(image.shape[1]), height=int(image.shape[0])),
        cutout=artifact,
        timings=timings,
    )


def _execute_pipeline(
    *,
    file: UploadFile,
    mode: str,
    target_width: int,
    target_height: int,
    top_ratio: float | None,
    shoulder_ratio: float | None,
    remove_bg: bool | None,
    default_remove_bg: bool,
    border_width: int,
    border_color: str,
    face_overrides: tuple[int | None, int | None, int | None, int | None],
    detector: FaceDetector,
) -> ProcessResponse:
    request_id = _new_request_id()
    mode = _validate_mode(mode)
    _validate_target(target_width, target_height)
    _validate_ratios(top_ratio, shoulder_ratio)
    _validate_border(border_width, border_color)

    started = time.perf_counter()
    image = ip.decode_image(_read_upload(file))
    timings = {"decode_ms": round((time.perf_counter() - started) * 1000, 1)}

    manual_face = _manual_face(*face_overrides)

    started = time.perf_counter()
    result = pipeline.run_pipeline(
        image,
        mode=mode,
        target_size=(target_width, target_height),
        top_ratio=top_ratio,
        shoulder_ratio=shoulder_ratio,
        remove_bg=remove_bg,
        default_remove_bg=default_remove_bg,
        face=manual_face,
        detector=detector if manual_face is None else None,
        border_width=border_width,
        border_color=border_color,
    )
    timings.update(result.timings)
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 1)

    output = _save_png(result.image, request_id, "poster")
    cutout_artifact = (
        _save_png(result.cutout, request_id, "cutout") if result.cutout is not None else None
    )
    output_height, output_width = result.image.shape[:2]

    return ProcessResponse(
        request_id=request_id,
        source=SizeOut(width=int(image.shape[1]), height=int(image.shape[0])),
        # 圆框模式会按正方形输出，这里回传真实成品尺寸而不是请求值
        target=SizeOut(width=int(output_width), height=int(output_height)),
        output=output,
        cutout=cutout_artifact,
        face=_face_out(result.face),
        crop=CropBoxOut(**result.crop.box.as_dict()),
        strategy=result.crop.strategy,
        mode=result.mode,
        warnings=result.warnings,
        timings=timings,
    )


@router.post("/process", response_model=ProcessResponse, summary="完整流程：按版式抠图 + 检测 + 裁切")
def process_image(
    file: UploadFile = File(...),
    mode: str = Form(ip.MODE_HEAD_SHOULDER, description="head_shoulder | framed_circle | full_body"),
    target_width: int = Form(1080),
    target_height: int = Form(1440),
    top_ratio: float | None = Form(None, description="不传则按 mode 取默认值"),
    shoulder_ratio: float | None = Form(None, description="不传则按 mode 取默认值"),
    remove_bg: bool | None = Form(None, description="不传则按 mode 取默认值"),
    border_width: int = Form(0, description="圆框内侧描边宽度（px）"),
    border_color: str = Form("#FFFFFF", description="描边颜色 #RRGGBB"),
    face_x: int | None = Form(None),
    face_y: int | None = Form(None),
    face_w: int | None = Form(None),
    face_h: int | None = Form(None),
    detector: FaceDetector = Depends(get_detector),
) -> ProcessResponse:
    return _execute_pipeline(
        file=file,
        mode=mode,
        target_width=target_width,
        target_height=target_height,
        top_ratio=top_ratio,
        shoulder_ratio=shoulder_ratio,
        remove_bg=remove_bg,
        default_remove_bg=True,
        border_width=border_width,
        border_color=border_color,
        face_overrides=(face_x, face_y, face_w, face_h),
        detector=detector,
    )


@router.post("/crop", response_model=ProcessResponse, summary="只做裁切（头肩默认不抠图）")
def crop_image(
    file: UploadFile = File(...),
    mode: str = Form(ip.MODE_HEAD_SHOULDER, description="head_shoulder | framed_circle | full_body"),
    target_width: int = Form(1080),
    target_height: int = Form(1440),
    top_ratio: float | None = Form(None, description="不传则按 mode 取默认值"),
    shoulder_ratio: float | None = Form(None, description="不传则按 mode 取默认值"),
    remove_bg: bool | None = Form(None, description="不传则按 mode 取默认值"),
    border_width: int = Form(0, description="圆框内侧描边宽度（px）"),
    border_color: str = Form("#FFFFFF", description="描边颜色 #RRGGBB"),
    face_x: int | None = Form(None),
    face_y: int | None = Form(None),
    face_w: int | None = Form(None),
    face_h: int | None = Form(None),
    detector: FaceDetector = Depends(get_detector),
) -> ProcessResponse:
    return _execute_pipeline(
        file=file,
        mode=mode,
        target_width=target_width,
        target_height=target_height,
        top_ratio=top_ratio,
        shoulder_ratio=shoulder_ratio,
        remove_bg=remove_bg,
        # 旧 /crop 语义：head_shoulder 默认不抠图，保持向后兼容
        default_remove_bg=False,
        border_width=border_width,
        border_color=border_color,
        face_overrides=(face_x, face_y, face_w, face_h),
        detector=detector,
    )


@router.get("/files/{filename}", response_class=FileResponse, summary="读取生成的 PNG")
def get_output_file(filename: str) -> FileResponse:
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404, detail="文件不存在")
    path: Path = (settings.output_dir / filename).resolve()
    try:
        path.relative_to(settings.output_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="文件不存在") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path, media_type="image/png", filename=path.name)
