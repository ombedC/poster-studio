"""多画板 .ai 导入：空白画板自动跳过、多非空画板由前端选择、按画板边界裁切。"""
from __future__ import annotations

import io
import re

import pytest
from PIL import Image, ImageDraw

from app.config import settings
from app.services import template_meta, vector_importer


def board_pdf(pages: list[tuple[int, int, str | None]]) -> bytes:
    """pages: [(width, height, label)]，label 为 None 表示空白画板。"""
    images = []
    for index, (width, height, label) in enumerate(pages):
        image = Image.new("RGB", (width, height), (255, 255, 255))
        if label is not None:
            draw = ImageDraw.Draw(image)
            draw.rectangle((30, 30, width - 30, height - 30), fill=(200, 40, 40))
            draw.text((60, height // 2), f"{label} {index + 1}", fill=(255, 255, 255))
        images.append(image)
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def upload(client, data: bytes, *, page: int | None = None, name: str = "多画板.ai", display: str = "多画板测试"):
    form = {"name": display}
    if page is not None:
        form["page"] = str(page)
    return client.post(
        "/api/v1/posters/templates/upload",
        files={"file": (name, data, "application/pdf")},
        data=form,
    )


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------
def test_inspect_reports_blank_boards(client):
    data = board_pdf([(600, 800, None), (600, 800, "A"), (600, 800, "B")])
    response = client.post(
        "/api/v1/posters/templates/inspect",
        files={"file": ("multi.ai", data, "application/pdf")},
    )
    assert response.status_code == 200, response.text
    info = response.json()
    assert info["total"] == 3
    assert [page["index"] for page in info["pages"]] == [1, 2, 3]
    assert [page["blank"] for page in info["pages"]] == [True, False, False]
    assert info["non_blank"] == [2, 3]
    assert info["needs_choice"] is True
    assert info["auto_page"] == 0
    assert all(page["thumbnail"].startswith("data:image/png;base64,") for page in info["pages"])
    assert info["pages"][1]["width"] == pytest.approx(600, abs=1)
    assert info["pages"][1]["height"] == pytest.approx(800, abs=1)


def test_inspect_marks_single_non_blank_as_auto(client):
    data = board_pdf([(600, 800, None), (600, 800, "ONLY")])
    info = client.post(
        "/api/v1/posters/templates/inspect",
        files={"file": ("one.ai", data, "application/pdf")},
    ).json()
    assert info["non_blank"] == [2]
    assert info["auto_page"] == 2
    assert info["needs_choice"] is False


# ---------------------------------------------------------------------------
# 上传：多画板 → 请求选择；空白页 → 自动跳过
# ---------------------------------------------------------------------------
def test_upload_returns_board_choices_instead_of_white_poster(client):
    data = board_pdf([(600, 800, None), (600, 800, "A"), (600, 800, "B")])
    response = upload(client, data)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "choose_artboard"
    assert body["non_blank"] == [2, 3]
    # 不应创建模板
    assert not (settings.template_dir / "多画板测试.fixed.svg").exists()


def test_upload_auto_skips_blank_first_board(client):
    data = board_pdf([(600, 800, None), (600, 800, None), (600, 800, "ONLY")])
    response = upload(client, data, display="自动跳过测试")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("status") != "choose_artboard"
    assert body["canvas"] == {"width": pytest.approx(600, abs=1), "height": pytest.approx(800, abs=1)}
    assert any("自动跳过空白画板" in note and "第 3 个画板" in note for note in body["warnings"])
    assert template_meta.get("自动跳过测试") is not None


def test_upload_uses_selected_board_and_crops_to_page(client):
    data = board_pdf([(600, 800, None), (600, 800, "A"), (600, 800, "B")])
    response = upload(client, data, page=3, display="指定画板测试")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "指定画板测试"
    svg = (settings.template_dir / f"{body['name']}.fixed.svg").read_text(encoding="utf-8")
    view_box = re.search(r'viewBox="([^"]+)"', svg)
    assert view_box, svg[:300]
    values = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", view_box.group(1))]
    assert values[0] == 0 and values[1] == 0
    assert values[2] == pytest.approx(600, abs=1)
    assert values[3] == pytest.approx(800, abs=1)
    # 溢出内容被画板 clip 包住
    assert "clip-path" in svg


def test_upload_rejects_all_blank_boards(client):
    data = board_pdf([(600, 800, None), (600, 800, None)])
    response = upload(client, data)
    assert response.status_code == 422
    assert "空白" in response.json()["detail"]


def test_upload_rejects_out_of_range_page(client):
    data = board_pdf([(600, 800, None), (600, 800, "A"), (600, 800, "B")])
    response = upload(client, data, page=9)
    assert response.status_code == 422


def test_crop_helper_wraps_overflow():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="600pt" height="800pt" viewBox="0 0 600 800">'
        '<g><rect x="-50" y="-50" width="900" height="1200" fill="red"/></g></svg>'
    ).encode("utf-8")
    cropped = vector_importer._crop_to_page(svg).decode("utf-8")
    assert 'viewBox="0 0 600 800"' in cropped
    assert 'width="600"' in cropped and 'height="800"' in cropped
    assert "clip-path" in cropped and "poster-artboard-clip" in cropped
