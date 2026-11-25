"""图像处理基础能力测试：编解码、预乘缩放、rembg 适配。"""

from __future__ import annotations

import sys
import types

import cv2
import numpy as np
import pytest

from app.errors import BackgroundRemovalUnavailable, InvalidImage
from app.services import image_processor as ip


def test_decode_and_encode_roundtrip(portrait: np.ndarray):
    decoded = ip.decode_image(ip.encode_png(portrait))
    assert decoded.shape == portrait.shape
    assert np.array_equal(decoded, portrait)


def test_decode_rejects_garbage():
    with pytest.raises(InvalidImage):
        ip.decode_image(b"definitely not an image")


def test_decode_rejects_empty_payload():
    with pytest.raises(InvalidImage):
        ip.decode_image(b"")


def test_ensure_rgba_adds_opaque_alpha():
    rgba = ip.ensure_rgba(np.zeros((10, 10, 3), dtype=np.uint8))
    assert rgba.shape == (10, 10, 4)
    assert (rgba[..., 3] == 255).all()


def test_ensure_rgba_rejects_unknown_shape():
    with pytest.raises(ValueError):
        ip.ensure_rgba(np.zeros((10, 10, 2), dtype=np.uint8))


def test_resize_keeps_colour_where_opaque():
    """预乘 alpha 重采样：透明区域不能把不透明像素"染黑"。"""
    image = np.zeros((100, 100, 4), dtype=np.uint8)
    image[..., 0] = 255
    image[:, :50, 3] = 0
    image[:, 50:, 3] = 255

    resized = ip.resize_rgba(image, (50, 50), interpolation=cv2.INTER_AREA)

    opaque = resized[..., 3] > 10
    assert opaque.any()
    assert resized[..., 0][opaque].min() > 200
    assert resized[..., 1][opaque].max() < 20


def test_fit_aspect_pads_with_transparent_pixels():
    image = np.full((100, 50, 4), 200, dtype=np.uint8)

    fitted, pad = ip.fit_aspect(image, 1.0)

    assert fitted.shape == (100, 100, 4)
    assert pad == (25, 0, 25, 0)
    assert fitted[0, 0, 3] == 0
    assert fitted[0, 50, 3] == 200


def test_fit_aspect_is_noop_when_ratio_matches():
    image = np.full((100, 100, 4), 7, dtype=np.uint8)
    fitted, pad = ip.fit_aspect(image, 1.0)
    assert pad == (0, 0, 0, 0)
    assert np.array_equal(fitted, image)


def test_remove_background_reports_missing_rembg(monkeypatch):
    monkeypatch.setitem(sys.modules, "rembg", None)
    with pytest.raises(BackgroundRemovalUnavailable):
        ip.remove_background(np.zeros((10, 10, 4), dtype=np.uint8))


def test_remove_background_returns_rgba_from_rembg(monkeypatch):
    fake = types.ModuleType("rembg")

    def fake_remove(data, session=None, alpha_matting=False, post_process_mask=True):
        assert data.ndim == 3 and data.shape[2] == 3, "rembg 必须收到 RGB"
        result = np.zeros((*data.shape[:2], 4), dtype=np.uint8)
        result[..., :3] = data
        result[..., 3] = 255
        result[:3, :, 3] = 0
        return result

    fake.remove = fake_remove
    fake.new_session = lambda model: object()
    monkeypatch.setitem(sys.modules, "rembg", fake)
    ip._REMBG_SESSIONS.clear()

    image = np.full((20, 20, 4), 180, dtype=np.uint8)
    result = ip.remove_background(image, model="u2net")

    assert result.shape == (20, 20, 4)
    assert result[0, 0, 3] == 0
    assert result[10, 10, 3] == 255
    ip._REMBG_SESSIONS.clear()
