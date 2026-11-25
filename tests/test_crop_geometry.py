"""头肩裁切几何算法测试（不需要 mediapipe / rembg）。"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.image_processor import (
    CropBox,
    FaceBox,
    apply_circle_mask,
    compute_crop_box,
    crop_head_and_shoulders,
    crop_to_alpha_bbox,
    parse_border_color,
)


def _canvas(width: int = 1200, height: int = 1600) -> np.ndarray:
    return np.full((height, width, 4), 255, dtype=np.uint8)


def test_face_anchored_crop_uses_045_top_and_075_shoulder():
    """2.2h 的窗口 + 以人脸中心水平对齐，是完全可预测的几何。"""
    image = _canvas()
    face = FaceBox(500, 300, 200, 240)

    result = crop_head_and_shoulders(image, 3 / 4, face=face)

    # 高 = 240 * (1 + 0.45 + 0.75) = 528；宽 = 528 * 0.75 = 396
    # 顶 = 300 - 0.45 * 240 = 192；左 = (500 + 100) - 396 / 2 = 402
    assert result.box == CropBox(left=402, top=192, width=396, height=528)
    assert result.image.shape == (528, 396, 4)
    assert result.strategy == "face"
    assert result.warnings == []
    assert result.upscale_factor == 1.0


def test_output_matches_source_region_without_resize():
    image = _canvas()
    image[600:700, 500:600] = (10, 20, 30, 255)
    face = FaceBox(500, 300, 200, 240)

    result = crop_head_and_shoulders(image, 0.75, face=face)

    assert np.array_equal(result.image, image[192:720, 402:798])


def test_top_overflow_shifts_crop_down_and_keeps_height():
    image = _canvas()
    face = FaceBox(500, 10, 200, 240)

    result = crop_head_and_shoulders(image, 0.75, face=face)

    assert result.box.top == 0
    assert result.box.height == 528
    assert any("上边缘" in warning for warning in result.warnings)


def test_bottom_overflow_shifts_crop_up():
    image = _canvas()
    face = FaceBox(500, 1200, 200, 240)

    result = crop_head_and_shoulders(image, 0.75, face=face)

    assert result.box.bottom == 1600
    assert result.box.top == 1072
    assert any("下边缘" in warning for warning in result.warnings)


def test_full_body_photo_zooms_in_and_cuts_waist():
    """全身照：人脸很小，2.2h 的窗口只保留头肩，且放大到海报尺寸。"""
    image = _canvas(1200, 2400)
    face = FaceBox(540, 400, 120, 150)

    result = crop_head_and_shoulders(image, 0.75, face=face, target_size=(1080, 1440))

    assert result.box.height == pytest.approx(330)
    assert result.box.bottom <= 730  # 原图 2400 高，腰部以下被裁掉
    assert result.image.shape == (1440, 1080, 4)
    assert result.upscale_factor > 3
    assert any("放大" in warning for warning in result.warnings)


def test_no_face_falls_back_to_top_center_crop():
    tall = crop_head_and_shoulders(_canvas(1000, 2000), 1.0)
    assert tall.strategy == "fallback_top_center"
    assert tall.box == CropBox(left=0, top=0, width=1000, height=1000)
    assert any("未检测到人脸" in warning for warning in tall.warnings)

    wide = crop_head_and_shoulders(_canvas(2000, 1000), 1.0)
    assert wide.box == CropBox(left=500, top=0, width=1000, height=1000)


@pytest.mark.parametrize(
    ("aspect", "target"),
    [
        (3 / 4, (600, 800)),
        (4 / 5, (600, 750)),
        (1.0, (600, 600)),
        (9 / 16, (540, 960)),
    ],
)
def test_target_size_is_exact(aspect: float, target: tuple[int, int]):
    result = crop_head_and_shoulders(
        _canvas(), aspect, face=FaceBox(500, 300, 200, 240), target_size=target
    )
    assert result.image.shape == (target[1], target[0], 4)
    assert result.image.shape[1] / result.image.shape[0] == pytest.approx(aspect, abs=1e-3)


def test_transparent_alpha_survives_the_crop():
    image = np.zeros((1600, 1200, 4), dtype=np.uint8)
    image[300:540, 400:600] = (240, 200, 170, 255)

    result = crop_head_and_shoulders(image, 0.75, face=FaceBox(400, 300, 200, 240))

    assert result.image.shape == (528, 396, 4)
    assert result.image[0, 0, 3] == 0
    assert result.image[-1, -1, 3] == 0
    assert result.image[200, 200, 3] == 255  # 脸中央


def test_width_limited_by_portrait_image():
    box, warnings, strategy = compute_crop_box((400, 1600), FaceBox(100, 300, 80, 100), 2.0)
    assert strategy == "face"
    assert box == CropBox(left=0, top=255, width=400, height=200)
    assert any("偏宽" in warning for warning in warnings)


def test_height_limited_by_landscape_image():
    box, warnings, _ = compute_crop_box((1200, 800), FaceBox(500, 100, 200, 400), 0.75)
    assert box == CropBox(left=300, top=0, width=600, height=800)
    assert any("偏高" in warning for warning in warnings)


def test_zero_top_ratio_puts_crop_on_face_top():
    box, _, _ = compute_crop_box((1200, 1600), FaceBox(500, 300, 200, 240), 0.75, top_ratio=0.0)
    assert box.top == 300


def test_face_box_clamps_into_image():
    clamped = FaceBox(-20, -10, 100, 100).clamp(1200, 1600)
    assert (clamped.x, clamped.y, clamped.w, clamped.h) == (0, 0, 80, 90)


@pytest.mark.parametrize(
    ("aspect", "top_ratio", "shoulder_ratio"),
    [(0, 0.45, 0.75), (-1, 0.45, 0.75), (0.75, -0.1, 0.75), (0.75, 0.45, -0.5)],
)
def test_invalid_parameters_raise(aspect: float, top_ratio: float, shoulder_ratio: float):
    with pytest.raises(ValueError):
        compute_crop_box((1200, 1600), FaceBox(500, 300, 200, 240), aspect, top_ratio, shoulder_ratio)


def test_invalid_target_size_raises():
    with pytest.raises(ValueError):
        crop_head_and_shoulders(_canvas(), 0.75, face=FaceBox(500, 300, 200, 240), target_size=(0, 720))


# ---------------------------------------------------------------------------
# 圆框头像：圆形蒙版 + 内侧描边
# ---------------------------------------------------------------------------
def test_circle_mask_makes_corners_transparent():
    image = np.full((400, 400, 4), (10, 20, 30, 255), dtype=np.uint8)

    masked = apply_circle_mask(image)

    assert masked.shape == (400, 400, 4)
    assert masked[0, 0, 3] == 0
    assert masked[-1, -1, 3] == 0
    assert masked[200, 200, 3] == 255
    assert tuple(masked[200, 200, :3]) == (10, 20, 30)


def test_circle_mask_edge_is_antialiased():
    image = np.full((400, 400, 4), (10, 20, 30, 255), dtype=np.uint8)

    masked = apply_circle_mask(image)

    partial = ((masked[..., 3] > 0) & (masked[..., 3] < 255)).sum()
    assert partial > 50, "圆边应存在半透明过渡像素"


def test_circle_mask_draws_border_inside_perimeter():
    image = np.full((400, 400, 4), (10, 20, 30, 255), dtype=np.uint8)
    plain = apply_circle_mask(image)

    bordered = apply_circle_mask(image, border_width=12, border_color="#FF00FF")

    magenta = (
        (bordered[..., 0] > 240)
        & (bordered[..., 1] < 40)
        & (bordered[..., 2] > 240)
        & (bordered[..., 3] > 200)
    )
    assert magenta.sum() > 100
    # 描边不能溢出圆形：不描边时为透明的区域，描边后也不能有颜色
    assert not (magenta & (plain[..., 3] == 0)).any()
    # 圆心保持原色
    assert tuple(bordered[200, 200, :3]) == (10, 20, 30)
    # 四角仍然透明
    assert bordered[0, 0, 3] == 0


def test_parse_border_color_accepts_hex_and_rgb():
    assert parse_border_color("#FFFFFF") == (255, 255, 255)
    assert parse_border_color("ff0000") == (255, 0, 0)
    assert parse_border_color((1, 2, 3)) == (1, 2, 3)
    with pytest.raises(ValueError):
        parse_border_color("white")


# ---------------------------------------------------------------------------
# 全身立绘：alpha 外轮廓裁切
# ---------------------------------------------------------------------------
def test_alpha_bbox_crops_person_with_three_percent_margin():
    image = np.zeros((1000, 600, 4), dtype=np.uint8)
    image[200:800, 250:350] = (200, 150, 120, 255)  # 100 × 600 的人像

    result = crop_to_alpha_bbox(image, target_size=(300, 500))

    # 外扩 3%：宽 100→106、高 600→636
    assert result.strategy == "alpha_bbox"
    assert result.box == CropBox(left=247, top=182, width=106, height=636)
    assert result.image.shape == (500, 300, 4)
    # 人物没有被截断：四角透明，中间不透明
    assert result.image[0, 0, 3] == 0
    assert result.image[250, 150, 3] == 255


def test_alpha_bbox_aligns_person_to_canvas_bottom_when_padding():
    image = np.zeros((400, 600, 4), dtype=np.uint8)
    image[100:300, 50:550] = (220, 180, 150, 255)  # 宽扁人像 → 需要上下补边

    result = crop_to_alpha_bbox(image, target_size=(300, 400))

    alpha = result.image[..., 3]
    rows = np.where(alpha.max(axis=1) > 10)[0]
    assert rows.size > 0
    assert rows.max() >= 390, "人物底部应贴近画布底边"
    assert alpha[0].max() == 0, "补边应在人物上方"


def test_alpha_bbox_falls_back_to_whole_image_without_alpha():
    image = np.zeros((100, 120, 4), dtype=np.uint8)

    result = crop_to_alpha_bbox(image, target_size=(60, 60))

    assert result.strategy == "alpha_bbox_fallback"
    assert result.box == CropBox(left=0, top=0, width=120, height=100)
    assert any("alpha" in warning for warning in result.warnings)
