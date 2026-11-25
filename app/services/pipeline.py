"""业务流水线：抠图 → 人脸检测 → 按版式裁切，与 Web 框架解耦。

接口层只负责收发表单和存文件，测试可以直接调用 :func:`run_pipeline`。

三种版式
--------
* ``head_shoulder``：人脸锚点的半身头肩裁切（默认，兼容旧行为）。
* ``framed_circle``：正方形裁切 + 抗锯齿圆形蒙版，可选内侧描边。
* ``full_body``：强制抠图，按 alpha 外轮廓裁切全身，底部对齐目标画布。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from app.config import settings
from app.errors import BackgroundRemovalUnavailable, FaceDetectionUnavailable
from app.services import image_processor as ip
from app.services.face_detector import FaceDetector


@dataclass
class PipelineResult:
    image: np.ndarray
    crop: ip.CropResult
    face: ip.FaceBox | None
    face_backend: str
    cutout: np.ndarray | None
    mode: str = ip.MODE_HEAD_SHOULDER
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _resolve_mode(mode: str) -> str:
    normalized = (mode or ip.MODE_HEAD_SHOULDER).strip().lower()
    if normalized not in ip.PORTRAIT_MODES:
        raise ValueError(
            f"未知的排版模式：{mode!r}，可选：{', '.join(ip.PORTRAIT_MODES)}"
        )
    return normalized


def _resolve_ratios(mode: str, top_ratio: float | None, shoulder_ratio: float | None):
    """未显式传参时按模式取默认构图比例。"""
    resolved_top = (
        ip.MODE_TOP_RATIO.get(mode, ip.DEFAULT_TOP_RATIO)
        if top_ratio is None
        else float(top_ratio)
    )
    resolved_shoulder = (
        ip.MODE_SHOULDER_RATIO.get(mode, ip.DEFAULT_SHOULDER_RATIO)
        if shoulder_ratio is None
        else float(shoulder_ratio)
    )
    return resolved_top, resolved_shoulder


def _resolve_remove_bg(mode: str, remove_bg: bool | None, default_remove_bg: bool) -> bool:
    """各模式的背景策略。

    * full_body 强制抠图（规则要求，忽略调用方传 false）；
    * framed_circle 未显式传参时默认不抠图；
    * head_shoulder 沿用接口默认值（process 为 True，crop 为 False）。
    """
    if mode == ip.MODE_FULL_BODY:
        return True
    if remove_bg is not None:
        return bool(remove_bg)
    if mode == ip.MODE_FRAMED_CIRCLE:
        return False
    return default_remove_bg


def run_pipeline(
    image: np.ndarray,
    *,
    mode: str = ip.MODE_HEAD_SHOULDER,
    target_size: tuple[int, int],
    top_ratio: float | None = None,
    shoulder_ratio: float | None = None,
    remove_bg: bool | None = None,
    default_remove_bg: bool = True,
    face: ip.FaceBox | None = None,
    detector: FaceDetector | None = None,
    border_width: int = 0,
    border_color: str = "#FFFFFF",
    rembg_model: str | None = None,
    alpha_matting: bool | None = None,
) -> PipelineResult:
    """跑完整条人像处理链路。

    检测在**原图**上做（保留皮肤与背景对比度），裁切在**抠图结果**上做，
    这样透明背景会原样保留到成品里。``full_body`` 不需要人脸框，会跳过检测。
    """
    mode = _resolve_mode(mode)
    target_width, target_height = int(target_size[0]), int(target_size[1])
    if target_width <= 0 or target_height <= 0:
        raise ValueError("target_size 必须为正整数")

    warnings: list[str] = []
    timings: dict[str, float] = {}
    face_backend = "manual" if face is not None else "none"

    if mode != ip.MODE_FULL_BODY and face is None and detector is not None:
        started = time.perf_counter()
        try:
            face = detector.detect(image)
            face_backend = detector.name
        except FaceDetectionUnavailable as exc:
            warnings.append(f"人脸检测不可用，改用顶部居中裁剪：{exc}")
            face_backend = "unavailable"
        except Exception as exc:  # 检测器内部异常不应阻断出图
            warnings.append(f"人脸检测失败，改用顶部居中裁剪：{exc}")
            face_backend = "error"
        timings["detect_ms"] = _ms(started)
        if face is None and face_backend not in {"unavailable", "error"}:
            warnings.append("未检测到人脸，已退化为顶部居中裁剪")

    want_cutout = _resolve_remove_bg(mode, remove_bg, default_remove_bg)
    work = image
    cutout: np.ndarray | None = None
    if want_cutout:
        started = time.perf_counter()
        try:
            cutout = ip.remove_background(
                image,
                model=rembg_model or settings.rembg_model,
                alpha_matting=settings.rembg_alpha_matting if alpha_matting is None else alpha_matting,
            )
            work = cutout
        except BackgroundRemovalUnavailable as exc:
            # full_body 依赖 alpha，退化成原图会裁错；其余模式允许降级
            if settings.rembg_required or mode == ip.MODE_FULL_BODY:
                raise
            warnings.append(f"抠图已跳过，使用原图裁切：{exc}")
        timings["cutout_ms"] = _ms(started)

    top, shoulder = _resolve_ratios(mode, top_ratio, shoulder_ratio)
    started = time.perf_counter()

    if mode == ip.MODE_FULL_BODY:
        if cutout is None:
            raise BackgroundRemovalUnavailable("全身立绘模式需要先抠图（rembg 不可用）")
        crop = ip.crop_to_alpha_bbox(cutout, target_size=(target_width, target_height))
    else:
        if mode == ip.MODE_FRAMED_CIRCLE:
            side = min(target_width, target_height)
            if target_width != target_height:
                warnings.append(f"圆框头像按正方形输出 {side}×{side}")
            target: tuple[int, int] = (side, side)
            aspect = 1.0
        else:
            target = (target_width, target_height)
            aspect = target_width / target_height

        crop = ip.crop_head_and_shoulders(
            work,
            aspect,
            face=face,
            target_size=target,
            top_ratio=top,
            shoulder_ratio=shoulder,
        )

        if mode == ip.MODE_FRAMED_CIRCLE:
            started_mask = time.perf_counter()
            crop.image = ip.apply_circle_mask(
                crop.image,
                border_width=border_width,
                border_color=border_color,
            )
            crop.strategy = ip.MODE_FRAMED_CIRCLE
            timings["mask_ms"] = _ms(started_mask)

    timings["crop_ms"] = _ms(started)

    # 避免重复提示：未检测到人脸时 compute_crop_box 也会加一条，这里去重但保留顺序
    for message in crop.warnings:
        if message not in warnings:
            warnings.append(message)

    return PipelineResult(
        image=crop.image,
        crop=crop,
        face=face,
        face_backend=face_backend,
        cutout=cutout,
        mode=mode,
        warnings=warnings,
        timings=timings,
    )
