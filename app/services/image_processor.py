"""核心图像处理模块。

职责
----
1. **解码 / 编码**：上传字节流 <-> RGBA numpy 数组 <-> PNG。
2. **本地抠图**：调用 rembg（u2net / isnet 等 ONNX 模型）输出透明底人像。
3. **智能头肩裁切**：以人脸框为锚点，按「头顶 0.45h + 下巴下方 0.75h」构图，
   自动裁掉腰部以下，再缩放到海报需要的目标尺寸。
4. **高质量缩放**：在预乘 alpha 空间重采样，半透明边缘不会出现黑边/灰边。

约定
----
* 数组形状 ``(H, W, 3|4)``，``dtype=uint8``，通道顺序 **RGB / RGBA**
  （不是 OpenCV 默认的 BGR，只有内部调用 cv2 时才临时转换）。
* 坐标系：原点在左上角，x 向右，y 向下，单位为像素。
* 任何对外函数都不修改入参数组。
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import cv2
import numpy as np
from PIL import Image, ImageOps

from app.errors import BackgroundRemovalUnavailable, InvalidImage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 构图常量：与需求文档保持一致，可在接口层按需覆盖
# ---------------------------------------------------------------------------
DEFAULT_TOP_RATIO = 0.45       # 头顶预留空间 = 0.45 * 人脸框高度
DEFAULT_SHOULDER_RATIO = 0.75  # 下巴下方延伸 = 0.75 * 人脸框高度（到肩膀上沿）
MIN_CROP_SIDE = 8              # 裁切框最小边，防止退化图片
UPSCALE_WARN_FACTOR = 3.0      # 放大超过 3 倍时提示"原图分辨率偏低"

# ---------------------------------------------------------------------------
# 三种人像排版模式
# ---------------------------------------------------------------------------
MODE_HEAD_SHOULDER = "head_shoulder"  # 半身头肩：卡片半身位
MODE_FRAMED_CIRCLE = "framed_circle"  # 圆框头像：正方形 + 圆形蒙版
MODE_FULL_BODY = "full_body"          # 全身立绘：按 alpha 外轮廓裁切
PORTRAIT_MODES = (MODE_HEAD_SHOULDER, MODE_FRAMED_CIRCLE, MODE_FULL_BODY)

# 各模式的默认构图比例（接口未显式传参时使用）
MODE_TOP_RATIO = {
    MODE_HEAD_SHOULDER: DEFAULT_TOP_RATIO,
    MODE_FRAMED_CIRCLE: 0.50,
}
MODE_SHOULDER_RATIO = {
    MODE_HEAD_SHOULDER: DEFAULT_SHOULDER_RATIO,
    MODE_FRAMED_CIRCLE: 0.85,
}

# 全身模式：alpha 阈值与四周安全边距
ALPHA_THRESHOLD = 10
ALPHA_MARGIN_RATIO = 0.03

_HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FaceBox:
    """人脸框（像素坐标，左上角 + 宽高）。"""

    x: int
    y: int
    w: int
    h: int
    confidence: float | None = None
    backend: str = "unknown"

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"人脸框宽高必须为正数: w={self.w}, h={self.h}")

    @property
    def center_x(self) -> float:
        return self.x + self.w / 2.0

    @property
    def center_y(self) -> float:
        return self.y + self.h / 2.0

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return self.w * self.h

    def clamp(self, width: int, height: int) -> "FaceBox":
        """把检测框夹回图像范围内（MediaPipe 的相对坐标可能略越界）。"""
        if width <= 0 or height <= 0:
            raise ValueError("图像尺寸必须为正数")
        x1 = max(0, min(int(self.x), width - 1))
        y1 = max(0, min(int(self.y), height - 1))
        x2 = max(x1 + 1, min(int(self.x + self.w), width))
        y2 = max(y1 + 1, min(int(self.y + self.h), height))
        return FaceBox(x1, y1, x2 - x1, y2 - y1, confidence=self.confidence, backend=self.backend)

    def as_dict(self) -> dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "confidence": self.confidence,
            "backend": self.backend,
        }


@dataclass(frozen=True)
class CropBox:
    """裁切框（像素坐标，位于原图坐标系内）。"""

    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("裁切框宽高必须为正数")

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height

    def as_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class CropResult:
    """一次头肩裁切的完整结果，便于接口层透出调试信息。"""

    image: np.ndarray
    box: CropBox
    face: FaceBox | None
    strategy: str
    warnings: list[str] = field(default_factory=list)
    upscale_factor: float = 1.0
    padded: bool = False
    pad: tuple[int, int, int, int] = (0, 0, 0, 0)
    output_size: tuple[int, int] = (0, 0)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def ensure_rgba(image: np.ndarray) -> np.ndarray:
    """把灰度 / RGB / RGBA 数组统一成连续的 uint8 RGBA。"""
    array = np.asarray(image)
    if array.dtype != np.uint8:
        if array.dtype.kind == "f":
            array = np.clip(array, 0, 255)
        array = array.astype(np.uint8)

    if array.ndim == 2:
        array = np.stack([array, array, array], axis=-1)
    if array.ndim != 3 or array.shape[2] not in (3, 4):
        raise ValueError(f"不支持的图像形状: {array.shape}")

    if array.shape[2] == 3:
        alpha = np.full((*array.shape[:2], 1), 255, dtype=np.uint8)
        array = np.concatenate([array, alpha], axis=2)
    return np.ascontiguousarray(array)


def to_gray(image: np.ndarray) -> np.ndarray:
    """RGB(A) -> 灰度图。"""
    rgba = ensure_rgba(image)
    return cv2.cvtColor(rgba[..., :3], cv2.COLOR_RGB2GRAY)


def decode_image(data: bytes) -> np.ndarray:
    """把上传的图片字节解码成 RGBA 数组，并按 EXIF 方向自动摆正。"""
    if not data:
        raise InvalidImage("上传内容为空")

    try:
        with Image.open(io.BytesIO(data)) as opening:
            opening.load()
            image = ImageOps.exif_transpose(opening)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA")
            array = np.array(image, dtype=np.uint8)
    except Image.DecompressionBombError as exc:  # 超大图保护
        raise InvalidImage("图片像素尺寸过大，已拒绝解码") from exc
    except Exception as exc:  # PIL 的异常类型很杂，统一转成可读错误
        raise InvalidImage(f"无法解码图片，请确认文件是 JPG/PNG/WebP/BMP：{exc}") from exc

    if array.size == 0:
        raise InvalidImage("图片内容为空")
    return ensure_rgba(array)


def encode_png(image: np.ndarray) -> bytes:
    """RGBA 数组 -> PNG 字节（保留透明通道）。"""
    rgba = ensure_rgba(image)
    buffer = io.BytesIO()
    Image.fromarray(rgba).save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 抠图
# ---------------------------------------------------------------------------
_REMBG_SESSIONS: dict[str, Any] = {}


def rembg_available() -> bool:
    """rembg 是否已安装（不触发模型加载）。"""
    try:
        import rembg  # noqa: F401
    except Exception:
        return False
    return True


def _get_rembg_session(model: str) -> Any:
    from rembg import new_session

    session = _REMBG_SESSIONS.get(model)
    if session is None:
        session = new_session(model)
        _REMBG_SESSIONS[model] = session
    return session


def remove_background(
    image: np.ndarray,
    *,
    model: str = "u2net",
    alpha_matting: bool = False,
) -> np.ndarray:
    """用本地 rembg 模型去背景，返回 RGBA（alpha 即人像蒙版）。

    首次调用会下载模型（默认 u2net，约 170 MB），之后完全离线。
    未安装或模型不可用时抛 :class:`BackgroundRemovalUnavailable`。
    """
    rgba = ensure_rgba(image)
    try:
        from rembg import remove
    except Exception as exc:
        raise BackgroundRemovalUnavailable(
            "未安装 rembg，抠图不可用。请执行 `pip install rembg[cpu]` 后重启服务。"
        ) from exc

    try:
        session = _get_rembg_session(model)
        output = remove(
            np.ascontiguousarray(rgba[..., :3]),  # rembg 只接受 RGB
            session=session,
            alpha_matting=alpha_matting,
            post_process_mask=True,
        )
    except Exception as exc:
        model_home = os.getenv("U2NET_HOME", "~/.u2net")
        raise BackgroundRemovalUnavailable(
            f"rembg 抠图失败（模型 {model}）：{exc}。"
            f"首次运行需要联网下载约 170 MB 模型；离线时可手动下载 u2net.onnx 放到 {model_home}。"
        ) from exc

    result = np.asarray(output)
    if result.ndim != 3 or result.shape[2] not in (3, 4):
        raise BackgroundRemovalUnavailable(f"rembg 返回了异常结果: shape={result.shape}")
    return ensure_rgba(result)


# ---------------------------------------------------------------------------
# 缩放与比例
# ---------------------------------------------------------------------------
def resize_rgba(
    image: np.ndarray,
    size: tuple[int, int],
    *,
    interpolation: int | None = None,
) -> np.ndarray:
    """缩放到 ``(width, height)``，在预乘 alpha 空间重采样，避免边缘发黑。"""
    rgba = ensure_rgba(image)
    target_width, target_height = int(size[0]), int(size[1])
    if target_width <= 0 or target_height <= 0:
        raise ValueError("目标尺寸必须为正整数")

    height, width = rgba.shape[:2]
    if (width, height) == (target_width, target_height):
        return rgba.copy()

    if interpolation is None:
        interpolation = (
            cv2.INTER_AREA
            if target_width * target_height < width * height
            else cv2.INTER_LANCZOS4
        )

    alpha = rgba[..., 3].astype(np.float32) / 255.0
    premultiplied = np.dstack(
        (rgba[..., :3].astype(np.float32) * alpha[..., None], alpha * 255.0)
    )
    resized = cv2.resize(
        np.ascontiguousarray(premultiplied),
        (target_width, target_height),
        interpolation=interpolation,
    )
    out_alpha = np.clip(np.rint(resized[..., 3]), 0.0, 255.0)
    scale = np.maximum(out_alpha / 255.0, 1e-6)[..., None]
    out_rgb = np.clip(np.rint(resized[..., :3] / scale), 0.0, 255.0)
    return np.dstack((out_rgb, out_alpha)).astype(np.uint8)


def fit_aspect(
    image: np.ndarray,
    aspect_ratio: float,
    *,
    anchor: tuple[float, float] = (0.5, 0.5),
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """把图像补成精确比例：不足的部分填透明像素（不拉伸、不变形）。

    返回 ``(新图像, (左, 上, 右, 下) 补边像素)``。
    """
    rgba = ensure_rgba(image)
    if not math.isfinite(aspect_ratio) or aspect_ratio <= 0:
        raise ValueError("aspect_ratio 必须为正的有限数值")

    height, width = rgba.shape[:2]
    if height == 0 or width == 0:
        raise ValueError("图像尺寸不能为 0")

    if abs(width / height - aspect_ratio) <= 1e-6:
        return rgba.copy(), (0, 0, 0, 0)

    if width / height < aspect_ratio:
        new_width = max(width, int(round(height * aspect_ratio)))
        new_height = height
    else:
        new_width = width
        new_height = max(height, int(round(width / aspect_ratio)))

    if (new_width, new_height) == (width, height):
        return rgba.copy(), (0, 0, 0, 0)

    canvas = np.zeros((new_height, new_width, 4), dtype=np.uint8)
    anchor_x, anchor_y = anchor
    offset_x = int(round((new_width - width) * anchor_x))
    offset_y = int(round((new_height - height) * anchor_y))
    offset_x = max(0, min(offset_x, new_width - width))
    offset_y = max(0, min(offset_y, new_height - height))
    canvas[offset_y : offset_y + height, offset_x : offset_x + width] = rgba

    pad = (
        offset_x,
        offset_y,
        new_width - width - offset_x,
        new_height - height - offset_y,
    )
    return canvas, pad


# ---------------------------------------------------------------------------
# 圆框头像：圆形蒙版 + 内侧描边
# ---------------------------------------------------------------------------
def parse_border_color(value: str | Iterable[int]) -> tuple[int, int, int]:
    """解析描边颜色：``#RRGGBB`` 字符串，或 ``(r, g, b)``。"""
    if isinstance(value, str):
        match = _HEX_COLOR_RE.match(value.strip())
        if not match:
            raise ValueError(f"描边颜色需为 #RRGGBB 格式，收到：{value!r}")
        digits = match.group(1)
        return tuple(int(digits[index : index + 2], 16) for index in (0, 2, 4))

    channels = tuple(int(channel) for channel in value)
    if len(channels) != 3 or any(channel < 0 or channel > 255 for channel in channels):
        raise ValueError("描边颜色需为 0-255 范围内的 (r, g, b)")
    return channels  # type: ignore[return-value]


def apply_circle_mask(
    image: np.ndarray,
    *,
    border_width: int = 0,
    border_color: str | Iterable[int] = "#FFFFFF",
    supersample: int = 4,
) -> np.ndarray:
    """把图像裁成抗锯齿圆形 RGBA，四角透明。

    先在 ``supersample`` 倍尺寸上画圆再用 ``INTER_AREA`` 缩回，得到平滑边缘；
    描边画在圆周**内侧**，不会超出圆形轮廓。

    Parameters
    ----------
    border_width:
        最终输出像素单位的描边宽度，0 表示不描边。
    border_color:
        ``#RRGGBB`` 或 ``(r, g, b)``。
    """
    rgba = ensure_rgba(image)
    height, width = rgba.shape[:2]
    size = min(height, width)
    if size < 8:
        raise ValueError("图像太小，无法生成圆形头像")
    if border_width < 0:
        raise ValueError("border_width 不能为负数")

    factor = max(2, int(supersample))
    big_width, big_height = width * factor, height * factor
    center = (big_width // 2, big_height // 2)
    # 留 1px 边距，避免抗锯齿被画布边缘裁掉
    radius = max(1, size * factor // 2 - factor)

    mask_big = np.zeros((big_height, big_width), dtype=np.uint8)
    cv2.circle(mask_big, center, radius, 255, thickness=-1, lineType=cv2.LINE_AA)
    mask = cv2.resize(mask_big, (width, height), interpolation=cv2.INTER_AREA)

    output = rgba.copy()
    output[..., 3] = (
        output[..., 3].astype(np.uint16) * mask.astype(np.uint16) // 255
    ).astype(np.uint8)

    if border_width > 0 and border_color is not None:
        color = np.array(parse_border_color(border_color), dtype=np.float32)
        thickness_big = min(radius, max(1, int(round(border_width * factor))))
        ring_big = np.zeros((big_height, big_width), dtype=np.uint8)
        ring_radius = max(1, radius - thickness_big // 2)
        cv2.circle(
            ring_big,
            center,
            ring_radius,
            255,
            thickness=thickness_big,
            lineType=cv2.LINE_AA,
        )
        ring = cv2.resize(ring_big, (width, height), interpolation=cv2.INTER_AREA)
        coverage = (ring.astype(np.float32) / 255.0)[..., None]
        rgb = output[..., :3].astype(np.float32)
        output[..., :3] = np.clip(rgb * (1.0 - coverage) + color * coverage, 0, 255).astype(
            np.uint8
        )
        output[..., 3] = np.maximum(output[..., 3], ring)

    return output


# ---------------------------------------------------------------------------
# 智能头肩裁切
# ---------------------------------------------------------------------------
def compute_crop_box(
    image_size: tuple[int, int],
    face: FaceBox | None,
    target_aspect_ratio: float,
    top_ratio: float = DEFAULT_TOP_RATIO,
    shoulder_ratio: float = DEFAULT_SHOULDER_RATIO,
) -> tuple[CropBox, list[str], str]:
    """纯几何计算：给定原图尺寸与人脸框，返回裁切框。

    构图公式（face 为检测框）::

        裁切高度 = h * (1 + top_ratio + shoulder_ratio)   # 默认 2.2h
        裁切顶部 = face.y - top_ratio * h                 # 头顶留白
        裁切底部 = face.y + h + shoulder_ratio * h        # 下巴向下到肩膀
        裁切宽度 = 裁切高度 * 目标宽高比                   # 水平以人脸中心对齐

    全身照的人脸通常很小，2.2h 的窗口自然把腰部以下裁掉；
    当窗口越出原图时优先平移贴边，仍放不下才缩小窗口，并给出 warning。

    返回 ``(裁切框, 警告列表, 策略)``，策略为 ``"face"`` 或 ``"fallback_top_center"``。
    """
    width, height = int(image_size[0]), int(image_size[1])
    if width <= 0 or height <= 0:
        raise ValueError(f"无效的图像尺寸: {image_size}")

    aspect = float(target_aspect_ratio)
    if not math.isfinite(aspect) or aspect <= 0:
        raise ValueError("target_aspect_ratio 必须为正的有限数值")
    if not math.isfinite(top_ratio) or top_ratio < 0:
        raise ValueError("top_ratio 不能为负数")
    if not math.isfinite(shoulder_ratio) or shoulder_ratio < 0:
        raise ValueError("shoulder_ratio 不能为负数")

    warnings: list[str] = []

    if face is None:
        # 退化策略：顶部居中裁剪
        crop_width = min(float(width), height * aspect)
        crop_height = crop_width / aspect
        left = (width - crop_width) / 2.0
        top = 0.0
        strategy = "fallback_top_center"
        warnings.append("未检测到人脸，已退化为顶部居中裁剪")
    else:
        face = face.clamp(width, height)
        crop_height = face.h * (1.0 + top_ratio + shoulder_ratio)
        crop_width = crop_height * aspect

        if crop_width > width:
            crop_width = float(width)
            crop_height = crop_width / aspect
            warnings.append("目标比例偏宽，裁切宽度已收窄到原图宽度")
        if crop_height > height:
            crop_height = float(height)
            crop_width = min(float(width), crop_height * aspect)
            warnings.append("目标比例偏高，裁切高度已限制为原图高度")

        left = face.center_x - crop_width / 2.0
        top = float(face.y) - top_ratio * face.h

        if top < 0:
            warnings.append("头顶预留空间不足，裁切框已贴住上边缘")
        if top + crop_height > height:
            warnings.append("肩膀下方空间不足，裁切框已贴住下边缘")

        left = min(max(left, 0.0), max(0.0, width - crop_width))
        top = min(max(top, 0.0), max(0.0, height - crop_height))
        strategy = "face"

    box_width = max(1, min(width, int(round(crop_width))))
    box_height = max(1, min(height, int(round(crop_height))))
    box_left = max(0, min(int(round(left)), width - box_width))
    box_top = max(0, min(int(round(top)), height - box_height))
    box = CropBox(box_left, box_top, box_width, box_height)

    if face is not None and (
        face.x < box.left
        or face.y < box.top
        or face.right > box.right
        or face.bottom > box.bottom
    ):
        warnings.append("人脸框未完全包含在裁切区域内，请检查人脸检测结果或手动传入人脸框")

    return box, warnings, strategy


def crop_head_and_shoulders(
    image: np.ndarray,
    target_aspect_ratio: float,
    *,
    face: FaceBox | None = None,
    target_size: tuple[int, int] | None = None,
    top_ratio: float = DEFAULT_TOP_RATIO,
    shoulder_ratio: float = DEFAULT_SHOULDER_RATIO,
) -> CropResult:
    """智能头肩裁切 + 缩放到目标尺寸。

    Parameters
    ----------
    image:
        RGB / RGBA numpy 数组（通常来自 :func:`remove_background` 的透明底图）。
    target_aspect_ratio:
        目标宽高比 = 宽 / 高。例如 3:4 海报传 ``0.75``，1:1 传 ``1.0``。
        传入 ``target_size`` 时以目标尺寸为准。
    face:
        人脸框；``None`` 时自动退化为顶部居中裁剪。
    target_size:
        ``(width, height)``，最终输出精确尺寸；``None`` 则保持裁切后的原生分辨率。
    top_ratio, shoulder_ratio:
        构图比例，默认 0.45 / 0.75。

    Returns
    -------
    CropResult
        包含成品图、实际裁切框、策略与告警信息。

    Notes
    -----
    若原图是全身照，人脸框很小，2.2h 的窗口会把腰部以下裁掉并放大到目标尺寸，
    因此输出分辨率不足时会在 ``warnings`` 里提示。
    """
    rgba = ensure_rgba(image)
    height, width = rgba.shape[:2]

    if target_size is not None:
        target_width, target_height = int(target_size[0]), int(target_size[1])
        if target_width <= 0 or target_height <= 0:
            raise ValueError("target_size 必须为正整数")
        aspect = target_width / target_height
        target: tuple[int, int] | None = (target_width, target_height)
    else:
        aspect = float(target_aspect_ratio)
        target = None

    box, warnings, strategy = compute_crop_box(
        (width, height), face, aspect, top_ratio, shoulder_ratio
    )

    native = rgba[box.top : box.bottom, box.left : box.right]
    fitted, pad = fit_aspect(native, aspect)
    padded = any(pad)
    # 取整误差通常只有 1~2 px，透明补边看不出来，不必打扰用户
    if padded and max(pad) > 2:
        warnings.append("为保证目标比例，裁切边缘补入了透明像素")

    if target is None:
        output = fitted
        upscale_factor = 1.0
    else:
        output = resize_rgba(fitted, target)
        upscale_factor = target[0] / max(1, box.width)
        if upscale_factor >= UPSCALE_WARN_FACTOR:
            warnings.append(
                f"人脸在原图中较小，已放大约 {upscale_factor:.1f} 倍，成品可能偏软；"
                "建议换用更高分辨率的原图"
            )

    return CropResult(
        image=output,
        box=box,
        face=face,
        strategy=strategy,
        warnings=warnings,
        upscale_factor=round(upscale_factor, 3),
        padded=padded,
        pad=pad,
        output_size=(output.shape[1], output.shape[0]),
    )


def crop_to_alpha_bbox(
    image: np.ndarray,
    target_aspect_ratio: float | None = None,
    *,
    target_size: tuple[int, int] | None = None,
    alpha_threshold: int = ALPHA_THRESHOLD,
    margin_ratio: float = ALPHA_MARGIN_RATIO,
) -> CropResult:
    """全身立绘：按 alpha 外轮廓裁切，保留完整人物（头顶到鞋底）。

    1. 取 ``alpha > alpha_threshold`` 的像素最小外接矩形；
    2. 四周外扩 ``margin_ratio``（默认 3%）安全边距；
    3. 用 :func:`fit_aspect` 补到目标比例（**底部对齐**，人物贴近画布底边）；
    4. 用 :func:`resize_rgba` 缩放到目标尺寸。

    没有可用 alpha（全透明或全不透明背景）时退化为整图，并在 warnings 里说明。
    """
    rgba = ensure_rgba(image)
    height, width = rgba.shape[:2]

    if target_size is not None:
        target_width, target_height = int(target_size[0]), int(target_size[1])
        if target_width <= 0 or target_height <= 0:
            raise ValueError("target_size 必须为正整数")
        aspect = target_width / target_height
        target: tuple[int, int] | None = (target_width, target_height)
    else:
        if target_aspect_ratio is None:
            raise ValueError("target_aspect_ratio 与 target_size 至少提供一个")
        aspect = float(target_aspect_ratio)
        target = None

    if not 0 <= alpha_threshold <= 255:
        raise ValueError("alpha_threshold 需在 0-255 之间")
    if margin_ratio < 0:
        raise ValueError("margin_ratio 不能为负数")

    warnings: list[str] = []
    rows, columns = np.where(rgba[..., 3] > alpha_threshold)

    if rows.size == 0:
        box = CropBox(0, 0, width, height)
        warnings.append("没有检测到透明底人像（alpha 全为 0），已使用整张图片")
        strategy = "alpha_bbox_fallback"
    else:
        left = int(columns.min())
        top = int(rows.min())
        right = int(columns.max()) + 1
        bottom = int(rows.max()) + 1
        margin_x = int(round((right - left) * margin_ratio))
        margin_y = int(round((bottom - top) * margin_ratio))
        left = max(0, left - margin_x)
        top = max(0, top - margin_y)
        right = min(width, right + margin_x)
        bottom = min(height, bottom + margin_y)
        box = CropBox(left, top, max(1, right - left), max(1, bottom - top))
        strategy = "alpha_bbox"

    native = rgba[box.top : box.bottom, box.left : box.right]
    # 底部对齐：比例不足时只在顶部补透明像素，人物始终贴近画布底边
    fitted, pad = fit_aspect(native, aspect, anchor=(0.5, 1.0))
    padded = any(pad)
    if padded and max(pad) > 2:
        warnings.append("为保持目标比例，人物四周补入了透明像素")

    if target is None:
        output = fitted
        upscale_factor = 1.0
    else:
        output = resize_rgba(fitted, target)
        upscale_factor = target[0] / max(1, box.width)
        if upscale_factor >= UPSCALE_WARN_FACTOR:
            warnings.append(
                f"人像在原图中较小，已放大约 {upscale_factor:.1f} 倍，成品可能偏软；"
                "建议换用更高分辨率的原图"
            )

    return CropResult(
        image=output,
        box=box,
        face=None,
        strategy=strategy,
        warnings=warnings,
        upscale_factor=round(upscale_factor, 3),
        padded=padded,
        pad=pad,
        output_size=(output.shape[1], output.shape[0]),
    )


def average_face_boxes(boxes: Iterable[FaceBox]) -> FaceBox | None:
    """多帧/多模型结果取中位数，减少抖动（预留：视频帧批量处理时可用）。"""
    box_list = list(boxes)
    if not box_list:
        return None
    xs = sorted(b.x for b in box_list)
    ys = sorted(b.y for b in box_list)
    ws = sorted(b.w for b in box_list)
    hs = sorted(b.h for b in box_list)
    middle = len(box_list) // 2
    return FaceBox(xs[middle], ys[middle], ws[middle], hs[middle], backend=box_list[0].backend)
