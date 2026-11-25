"""接口层集成测试：用 TestClient 打真实路由，检测器用替身，不联网。"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from app.config import settings


def _load_png(client, url: str) -> Image.Image:
    response = client.get(url)
    assert response.status_code == 200
    return Image.open(io.BytesIO(response.content)).convert("RGBA")


def _post(client, png_bytes: bytes, data: dict):
    return client.post(
        "/api/v1/images/process",
        files={"file": ("teacher.png", png_bytes, "image/png")},
        data=data,
    )


def _process(client, png_bytes: bytes, **overrides):
    data = {"target_width": 540, "target_height": 720, "remove_bg": "false"}
    data.update({key: str(value) for key, value in overrides.items()})
    return client.post(
        "/api/v1/images/process",
        files={"file": ("teacher.png", png_bytes, "image/png")},
        data=data,
    )


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "rembg" in body


def test_process_returns_exact_target_png(client, png_bytes, stub_detector):
    response = _process(client, png_bytes)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["face"]["x"] == stub_detector.x
    assert body["strategy"] == "face"
    assert body["target"] == {"width": 540, "height": 720}

    download = client.get(body["output"]["url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "image/png"
    image = Image.open(io.BytesIO(download.content))
    assert image.size == (540, 720)
    assert image.mode == "RGBA"


def test_process_falls_back_when_no_face(client, png_bytes, null_detector):
    response = _process(client, png_bytes)
    assert response.status_code == 200
    body = response.json()
    assert body["face"] is None
    assert body["strategy"] == "fallback_top_center"
    assert any("未检测到人脸" in warning for warning in body["warnings"])
    assert body["crop"]["top"] == 0


def test_process_accepts_manual_face_box(client, png_bytes, null_detector):
    response = _process(client, png_bytes, face_x=480, face_y=260, face_w=240, face_h=260)
    assert response.status_code == 200
    body = response.json()
    assert body["face"]["backend"] == "manual"
    assert body["strategy"] == "face"


def test_partial_manual_face_box_is_rejected(client, png_bytes, null_detector):
    response = _process(client, png_bytes, face_x=480)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_analyze_reports_face_and_suggested_crop(client, png_bytes, stub_detector):
    response = client.post(
        "/api/v1/images/analyze",
        files={"file": ("teacher.png", png_bytes, "image/png")},
        data={"target_width": 1080, "target_height": 1440},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["face_detected"] is True
    assert body["backend"] == "stub"
    assert body["suggested_crop"]["width"] > 0
    assert body["source"] == {"width": 1200, "height": 1600}


def test_rejects_unsupported_content_type(client, png_bytes):
    response = client.post(
        "/api/v1/images/analyze",
        files={"file": ("teacher.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_media_type"


def test_rejects_corrupted_image(client):
    response = client.post(
        "/api/v1/images/analyze",
        files={"file": ("teacher.png", b"not a real png", "image/png")},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_image"


def test_rejects_too_small_target(client, png_bytes, stub_detector):
    response = _process(client, png_bytes, target_width=10)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_rejects_non_numeric_target(client, png_bytes, stub_detector):
    response = _process(client, png_bytes, target_width="abc")
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert "请求参数不合法" in response.json()["message"]


def test_process_survives_missing_rembg(client, png_bytes, stub_detector, monkeypatch):
    from app.errors import BackgroundRemovalUnavailable
    from app.services import image_processor as ip

    def boom(*args, **kwargs):
        raise BackgroundRemovalUnavailable("未安装 rembg")

    monkeypatch.setattr(ip, "remove_background", boom)
    response = _process(client, png_bytes, remove_bg="true")

    assert response.status_code == 200
    body = response.json()
    assert body["cutout"] is None
    assert any("抠图已跳过" in warning for warning in body["warnings"])


def test_process_fails_when_rembg_is_required(client, png_bytes, stub_detector, monkeypatch):
    from app.errors import BackgroundRemovalUnavailable
    from app.services import image_processor as ip

    def boom(*args, **kwargs):
        raise BackgroundRemovalUnavailable("未安装 rembg")

    monkeypatch.setattr(ip, "remove_background", boom)
    monkeypatch.setattr(settings, "rembg_required", True)

    response = _process(client, png_bytes, remove_bg="true")
    assert response.status_code == 503
    assert response.json()["error"] == "background_removal_unavailable"


def test_generated_file_route_blocks_path_traversal(client):
    response = client.get("/api/v1/images/files/..%2Fapp%2Fmain.py")
    assert response.status_code == 404


def test_generated_file_route_returns_404_for_unknown(client):
    response = client.get("/api/v1/images/files/deadbeef_poster.png")
    assert response.status_code == 404


def test_rejects_oversized_upload(client, png_bytes, stub_detector, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 16)
    response = _process(client, png_bytes)
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"


def test_cutout_endpoint_returns_transparent_png(client, png_bytes, monkeypatch):
    from app.services import image_processor as ip

    def fake_remove(image, **kwargs):
        return image  # 已经是 RGBA，直接当作抠图结果

    monkeypatch.setattr(ip, "remove_background", fake_remove)
    response = client.post(
        "/api/v1/images/cutout",
        files={"file": ("teacher.png", png_bytes, "image/png")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cutout"]["width"] == 1200
    assert client.get(body["cutout"]["url"]).status_code == 200


def test_crop_endpoint_skips_cutout_by_default(client, png_bytes, stub_detector):
    response = client.post(
        "/api/v1/images/crop",
        files={"file": ("teacher.png", png_bytes, "image/png")},
        data={"target_width": 540, "target_height": 720},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cutout"] is None
    assert body["strategy"] == "face"


# ---------------------------------------------------------------------------
# mode=framed_circle：正方形 + 圆形蒙版 + 内侧描边
# ---------------------------------------------------------------------------
def test_framed_circle_returns_square_with_transparent_corners(client, png_bytes, stub_detector):
    response = _process(
        client, png_bytes, mode="framed_circle", target_width=1080, target_height=1440
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["mode"] == "framed_circle"
    assert body["strategy"] == "framed_circle"
    # 非正方形请求会按短边输出正方形，target 回传真实尺寸
    assert body["target"] == {"width": 1080, "height": 1080}

    image = _load_png(client, body["output"]["url"])
    assert image.size == (1080, 1080)
    assert image.getpixel((0, 0))[3] == 0
    assert image.getpixel((1079, 1079))[3] == 0
    assert image.getpixel((540, 540))[3] == 255


def test_framed_circle_defaults_to_no_background_removal(client, png_bytes, stub_detector, monkeypatch):
    from app.services import image_processor as ip

    def boom(*args, **kwargs):
        raise AssertionError("framed_circle 未显式传 remove_bg 时不应调用 rembg")

    monkeypatch.setattr(ip, "remove_background", boom)
    response = _post(client, png_bytes, {"mode": "framed_circle"})

    assert response.status_code == 200
    body = response.json()
    assert body["cutout"] is None


def test_framed_circle_draws_border(client, png_bytes, stub_detector):
    response = _process(
        client,
        png_bytes,
        mode="framed_circle",
        border_width=10,
        border_color="#FF00FF",
        target_width=600,
        target_height=600,
    )
    assert response.status_code == 200, response.text

    image = np.array(_load_png(client, response.json()["output"]["url"]))
    magenta = (
        (image[..., 0] > 240)
        & (image[..., 1] < 40)
        & (image[..., 2] > 240)
        & (image[..., 3] > 200)
    )
    assert magenta.sum() > 200
    assert image[0, 0, 3] == 0


def test_rejects_unknown_mode(client, png_bytes, stub_detector):
    response = _process(client, png_bytes, mode="banana")
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_rejects_invalid_border_color(client, png_bytes, stub_detector):
    response = _process(
        client, png_bytes, mode="framed_circle", border_width=8, border_color="white"
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


# ---------------------------------------------------------------------------
# mode=full_body：强制抠图 + alpha 外轮廓裁切
# ---------------------------------------------------------------------------
def test_full_body_crops_alpha_bbox_and_hits_bottom(client, png_bytes, stub_detector, monkeypatch):
    from app.services import image_processor as ip

    def fake_remove(image, **kwargs):
        out = np.zeros_like(image)
        out[400:1500, 500:700] = image[400:1500, 500:700]  # 模拟"头顶到鞋底"的人像
        return out

    monkeypatch.setattr(ip, "remove_background", fake_remove)
    # 显式传 remove_bg=false 也会被 full_body 强制改成 true
    response = _process(
        client, png_bytes, mode="full_body", remove_bg="false", target_width=600, target_height=800
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["mode"] == "full_body"
    assert body["cutout"] is not None
    assert body["strategy"] == "alpha_bbox"
    assert body["target"] == {"width": 600, "height": 800}
    # 脸部/检测不参与 full_body 的构图
    assert body["face"] is None

    image = np.array(_load_png(client, body["output"]["url"]))
    assert image.shape == (800, 600, 4)
    assert image[0, 0, 3] == 0
    assert image[-1, -1, 3] == 0
    rows = np.where(image[..., 3].max(axis=1) > 10)[0]
    assert rows.size > 0
    assert rows.max() >= 760, "人物底部应贴近画布底边"


def test_full_body_requires_rembg(client, png_bytes, stub_detector, monkeypatch):
    from app.errors import BackgroundRemovalUnavailable
    from app.services import image_processor as ip

    def boom(*args, **kwargs):
        raise BackgroundRemovalUnavailable("未安装 rembg")

    monkeypatch.setattr(ip, "remove_background", boom)
    response = _process(client, png_bytes, mode="full_body")
    assert response.status_code == 503
    assert response.json()["error"] == "background_removal_unavailable"


def test_crop_endpoint_supports_mode(client, png_bytes, stub_detector):
    response = client.post(
        "/api/v1/images/crop",
        files={"file": ("teacher.png", png_bytes, "image/png")},
        data={"mode": "framed_circle", "target_width": 540, "target_height": 720},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "framed_circle"
    assert body["target"] == {"width": 540, "height": 540}
