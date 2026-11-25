"""Seedream 生图 / 圈画局改 / 拆层组装的单元测试（全部离线，Mock 方舟）。"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from lxml import etree
from PIL import Image, ImageDraw

from app.config import settings
from app.services import seedream, svg_compose, svg_inline

SVG_NS = "http://www.w3.org/2000/svg"


def png(width=120, height=90, box=None, color=(255, 0, 0, 255), background=(255, 255, 255, 255)) -> bytes:
    image = Image.new("RGBA", (width, height), background)
    if box:
        ImageDraw.Draw(image).rectangle(box, fill=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


@pytest.fixture
def ark(monkeypatch):
    """启用 ARK 配置并返回捕获器。"""
    monkeypatch.setattr(settings, "ark_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "seedream_enabled", True, raising=False)
    calls: list[dict] = []

    async def fake_post(payload):
        calls.append(payload)
        return {"data": [{"url": "https://ark.example/out.png"}], "usage": {}}

    monkeypatch.setattr(seedream, "_post", fake_post)
    return calls


def test_generate_payload_enforces_ark_contract(ark, monkeypatch):
    import asyncio

    async def fake_download(url):
        return png()

    monkeypatch.setattr(seedream, "_download", fake_download)
    result = asyncio.run(
        seedream.generate(prompt="一张海报", size="1242x2208", background="opaque")
    )
    payload = ark[0]
    assert payload["model"] == settings.seedream_model == "doubao-seedream-5-5-261028"
    assert payload["watermark"] is False
    assert payload["output_format"] == "png"
    assert payload["response_format"] == "url"
    assert payload["background"] == "opaque"
    assert payload["size"] == "1242x2208"
    assert "image" not in payload
    version = result["version"]
    assert version["index"] == 1 and version["url"].endswith("/v1.png")
    stored = settings.seedream_project_dir / result["project"]["id"] / "v1.png"
    assert stored.is_file()


def test_generate_rejects_bad_size_and_too_many_references(ark):
    import asyncio

    with pytest.raises(Exception):
        asyncio.run(seedream.generate(prompt="x", size="9K"))
    refs = [data_url(png()) for _ in range(settings.seedream_max_references + 1)]
    with pytest.raises(Exception):
        asyncio.run(seedream.generate(prompt="x", images=refs))


def test_edit_sends_two_reference_images_without_mask_key(ark, monkeypatch):
    import asyncio

    base = png(200, 160, (10, 10, 60, 60))
    mask = Image.new("RGBA", (200, 160), (0, 0, 0, 0))
    ImageDraw.Draw(mask).rectangle((20, 20, 90, 80), fill=(255, 0, 255, 200))
    buffer = io.BytesIO()
    mask.save(buffer, format="PNG")

    async def fake_download(url):
        return png(200, 160)

    monkeypatch.setattr(seedream, "_download", fake_download)
    result = asyncio.run(
        seedream.edit(
            base_image=data_url(base),
            mask_image=data_url(buffer.getvalue()),
            instruction="把标题改成研学营",
        )
    )
    payload = ark[0]
    assert "mask" not in payload
    assert payload["background"] == "opaque"
    assert len(payload["image"]) == 2
    assert payload["image"][0].startswith("data:image/png;base64,")
    assert payload["image"][1].startswith("data:image/png;base64,")
    assert "图1为干净原图" in payload["prompt"] and "图2" in payload["prompt"]
    assert "把标题改成研学营" in payload["prompt"]
    assert "严禁保留任何红色标注线框" in payload["prompt"]
    assert result["version"]["kind"] == "region"

    # 第二张参考图必须是"原图上叠加红色标注"，而不是原图本身
    annotated = base64.b64decode(payload["image"][1].split(",", 1)[1])
    tinted = Image.open(io.BytesIO(annotated)).convert("RGBA")
    assert tinted.getpixel((55, 50))[0] > 200  # 标注内部偏红
    assert tinted.getpixel((150, 20))[3] == 255  # 标注外保持原图


def test_decompose_never_sends_background_and_builds_editable_svg(ark, monkeypatch):
    import asyncio

    base_png = png(300, 200, (0, 0, 300, 200), (250, 250, 250, 255))
    layer_png = png(300, 200, (40, 60, 100, 110), background=(0, 0, 0, 0))  # 紧身裁剪后 61x51
    recognition = {
        "lines": [
            {
                "text": "秋季研学营",
                "bounding_box": {"absolute": [20, 20, 260, 60]},
                "runs": [
                    {
                        "text": "秋季",
                        "bounding_box": {"absolute": [20, 20, 120, 60]},
                        "style": {"foreground_color": "#ff0000", "bold": True},
                    },
                    {
                        "text": "研学营",
                        "bounding_box": {"absolute": [120, 20, 260, 60]},
                        "style": {"foreground_color": "#0033ff", "similar_font_family": "Alibaba PuHuiTi"},
                    },
                ],
            }
        ]
    }

    async def fake_post(payload):
        ark.append(payload)
        return {
            "data": [
                {"url": "https://ark.example/base.png"},
                {"url": "https://ark.example/l1.png"},
                {"url": "https://ark.example/l2.png", "text_recognition": recognition},
            ]
        }

    monkeypatch.setattr(seedream, "_post", fake_post)

    async def fake_download(url):
        return base_png if url.endswith("base.png") else layer_png

    monkeypatch.setattr(seedream, "_download", fake_download)

    captured = {}

    def fake_import(raw, filename, **kwargs):
        captured["svg"] = raw
        captured["filename"] = filename
        captured["kwargs"] = kwargs
        return {"name": "农业-拆层", "analyzed": True, "canvas": {"width": 300, "height": 200}}

    monkeypatch.setattr(seedream, "import_template", fake_import)

    result = asyncio.run(seedream.decompose(image=data_url(base_png), max_images=24))
    payload = ark[0]
    assert "background" not in payload
    assert payload["layer_decomposition"] is True
    assert payload["layer_decomposition_options"] == {"max_images": 24, "text_recognition": True}
    assert payload["size"] == "auto"
    assert payload["watermark"] is False
    assert payload["image"].startswith("data:image/png;base64,")

    root = etree.fromstring(captured["svg"])
    images = root.findall(f"{{{SVG_NS}}}image")
    assert images[0].get("data-layout-role") == "bg"
    layer = images[1]
    assert (layer.get("x"), layer.get("y"), layer.get("width"), layer.get("height")) == (
        "40.00",
        "60.00",
        "61.00",
        "51.00",
    )
    assert layer.get("href").startswith("/api/seedream/files/")
    texts = root.findall(f"{{{SVG_NS}}}text")
    assert len(texts) == 1
    text = texts[0]
    assert text.get("x") == "20.00"
    assert 25 <= float(text.get("font-size")) <= 35
    assert 20 <= float(text.get("y")) <= 60
    runs = text.findall(f"{{{SVG_NS}}}tspan")
    assert [run.text for run in runs] == ["秋季", "研学营"]
    assert runs[0].get("fill") == "#ff0000" and runs[0].get("font-weight") == "700"
    assert runs[1].get("fill") == "#0033ff"
    assert "Alibaba" in runs[1].get("font-family")
    # 文字层不再输出光栅图层，避免重影
    assert len(images) == 2
    assert result["decomposition"]["template"]["name"] == "农业-拆层"
    assert result["decomposition"]["layers"][0]["text_recognition"] is None
    assert result["decomposition"]["layers"][1]["text_recognition"]["lines"][0]["text"] == "秋季研学营"


def test_tight_crop_returns_exact_box(tmp_path):
    path = tmp_path / "layer.png"
    path.write_bytes(png(200, 150, (30, 40, 90, 100), background=(0, 0, 0, 0)))
    png_bytes, x, y, width, height = svg_compose.tight_crop(path)
    assert (x, y, width, height) == (30, 40, 61, 61)
    assert Image.open(io.BytesIO(png_bytes)).size == (61, 61)


def test_inline_local_images_embeds_seedream_assets(tmp_path, monkeypatch):
    project = settings.seedream_project_dir / "abc12345"
    (project / "layers").mkdir(parents=True, exist_ok=True)
    (project / "layers" / "crop-01.png").write_bytes(png(10, 10, (0, 0, 10, 10), background=(0, 0, 0, 0)))
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<image href="/api/seedream/files/abc12345/layers/crop-01.png" width="10" height="10"/>'
        b'</svg>'
    )
    inlined = svg_inline.inline_local_images(svg)
    root = etree.fromstring(inlined)
    href = root.find(f"{{{SVG_NS}}}image").get("href")
    assert href.startswith("data:image/png;base64,")
    assert svg_inline.resolve_local("/api/seedream/files/abc12345/../../secret.png") is None


def test_seedream_routes_validate_and_delegate(client, monkeypatch):
    async def fake_generate(**kwargs):
        return {"project": {"id": "p1", "versions": []}, "version": {"id": "v1"}}

    monkeypatch.setattr(seedream, "generate", fake_generate)
    response = client.post("/api/seedream/generate", json={"prompt": "海报", "size": "2K"})
    assert response.status_code == 200
    assert response.json()["version"]["id"] == "v1"

    bad = client.post("/api/seedream/generate", json={"prompt": "海报", "size": "9K", "background": "x"})
    assert bad.status_code == 422

    config = client.get("/api/seedream/config").json()
    assert config["model"] == "doubao-seedream-5-5-261028"
    assert config["enable_ai_svg"] is False

    health = client.get("/api/v1/health").json()
    assert health["features"]["ai_svg"] is False
    assert "seedream" in health["features"]


def test_screenshot_inlines_local_layers_but_keeps_svg_small(tmp_path):
    """导出渲染时内联本地图层；磁盘上的编辑态 SVG 仍只引用本地 URL。"""
    from app.errors import BrowserUnavailable
    from app.services.svg_browser import screenshot_svg

    project_id = "shot1234"
    project = settings.seedream_project_dir / project_id
    (project / "layers").mkdir(parents=True, exist_ok=True)
    (project / "layers" / "base.png").write_bytes(png(120, 90, (0, 0, 120, 90), (255, 255, 255, 255)))
    layer_file = project / "layers" / "layer-01.png"
    layer_file.write_bytes(png(120, 90, (20, 10, 60, 40), background=(0, 0, 0, 0)))

    svg = svg_compose.compose(
        base_href=f"/api/seedream/files/{project_id}/layers/base.png",
        layers=[{"url": f"/api/seedream/files/{project_id}/layers/layer-01.png", "file": layer_file}],
        canvas=(120, 90),
        job_dir=project,
        asset_prefix=f"/api/seedream/files/{project_id}",
    )
    svg_path = tmp_path / "poster.svg"
    svg_path.write_bytes(svg)
    assert b"/api/seedream/files/" in svg  # 编辑态保持小体积引用

    out = tmp_path / "poster.png"
    try:
        size = screenshot_svg(svg_path, (120, 90), out, scale=2)
    except BrowserUnavailable as exc:  # pragma: no cover - 无浏览器环境
        pytest.skip(str(exc))
    assert size == (240, 180)
    with Image.open(out) as image:
        rgb = image.convert("RGB")
        assert rgb.getpixel((80, 50)) == (255, 0, 0) or rgb.getpixel((80, 50))[0] > 200
        assert rgb.getpixel((10, 170)) == (255, 255, 255)


def test_background_template_wraps_single_locked_layer(monkeypatch):
    """无字底图直接封装为单背景层，不走拆图层接口。"""
    captured = {}

    def fake_import(raw, filename, **kwargs):
        captured["svg"] = raw
        captured["name"] = kwargs.get("name", "")
        return {"name": "底图模板", "analyzed": True, "canvas": {"width": 300, "height": 200}}

    monkeypatch.setattr(seedream, "import_template", fake_import)
    result = seedream.background_template(image=data_url(png(300, 200)), name="底图")

    root = etree.fromstring(captured["svg"])
    images = root.findall(f"{{{SVG_NS}}}image")
    assert len(images) == 1
    assert images[0].get("data-layout-role") == "bg"
    assert images[0].get("href", "").startswith("/api/seedream/files/")
    assert root.findall(f"{{{SVG_NS}}}text") == []
    assert captured["name"] == "底图"
    assert result["canvas"] == {"width": 300, "height": 200}


def test_background_template_route_delegates(client, monkeypatch):
    monkeypatch.setattr(
        seedream,
        "background_template",
        lambda **kwargs: {"template": {"name": "t"}, "canvas": {"width": 1, "height": 1}},
    )
    response = client.post(
        "/api/seedream/background-template",
        json={"image": data_url(png(20, 20)), "name": "底图"},
    )
    assert response.status_code == 200
    assert response.json()["template"]["name"] == "t"


def test_ark_key_can_be_saved_and_cleared_from_ui(client, monkeypatch):
    monkeypatch.setattr(settings, "ark_api_key", "", raising=False)
    assert client.get("/api/seedream/config").json()["has_api_key"] is False

    saved = client.put("/api/seedream/config", json={"api_key": "ark-test-key-123456"})
    assert saved.status_code == 200
    body = saved.json()
    assert body["has_api_key"] is True and body["enabled"] is True
    # 密钥绝不回显
    assert "ark-test-key-123456" not in saved.text
    # 保存即生效，不需要重启
    assert seedream.configured() is True
    assert (settings.data_dir / "seedream.json").is_file()
    assert client.get("/api/seedream/config").json()["has_api_key"] is True

    cleared = client.put("/api/seedream/config", json={"clear_api_key": True}).json()
    assert cleared["has_api_key"] is False
    assert seedream.configured() is False


def test_ark_key_rejects_invalid_values(client):
    assert client.put("/api/seedream/config", json={"api_key": "short"}).status_code == 422
    assert client.put("/api/seedream/config", json={"api_key": "密钥" * 4}).status_code == 422


def test_unauthorized_key_gives_actionable_message(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "ark_api_key", "ark-probe-key-0123456789abcdef", raising=False)

    class FakeResponse:
        status_code = 401

        @staticmethod
        def json():
            return {"error": {"code": "AuthenticationError", "message": "bad key"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(seedream.net, "async_client", lambda **kwargs: FakeClient())
    with pytest.raises(seedream.SeedreamError) as exc:
        asyncio.run(seedream._post({"model": "x"}))
    assert "ARK_API_KEY" in str(exc.value) and "401" in str(exc.value)


def test_verify_key_distinguishes_auth_from_model_errors(monkeypatch):
    import asyncio

    class FakeResponse:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return self.response

    bad = FakeClient(FakeResponse(401, {"error": {"message": "key invalid"}}))
    monkeypatch.setattr(seedream.net, "async_client", lambda **kwargs: bad)
    result = asyncio.run(seedream.verify_key("ark-bad"))
    assert result["ok"] is False and result["status"] == 401
    assert "key invalid" in result["message"]

    # 模型不存在（400/404）说明鉴权已通过，密钥本身可用
    good = FakeClient(FakeResponse(400, {"error": {"message": "model not found"}}))
    monkeypatch.setattr(seedream.net, "async_client", lambda **kwargs: good)
    result = asyncio.run(seedream.verify_key("ark-good"))
    assert result["ok"] is True and result["status"] == 400


def test_verify_without_key_reports_not_configured(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "ark_api_key", "", raising=False)
    result = asyncio.run(seedream.verify_key(None))
    assert result["ok"] is False and "ARK_API_KEY" in result["message"]


def test_posts_fall_back_to_plan_endpoint_on_401(monkeypatch, tmp_path):
    """套餐 key 只在 /api/plan/v3 可用：401 后自动换端点并记住。"""
    import asyncio

    monkeypatch.setattr(settings, "ark_api_key", "ark-plan-key", raising=False)
    monkeypatch.setattr(settings, "ark_base_url", "https://ark.example.com/api/v3", raising=False)
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    seen = []

    class FakeResponse:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            seen.append(url)
            if len(seen) == 1:
                return FakeResponse(401, {"error": {"message": "missing or invalid"}})
            return FakeResponse(200, {"data": [{"url": "https://x/img.png"}]})

    monkeypatch.setattr(seedream.net, "async_client", lambda **kwargs: FakeClient())
    body = asyncio.run(seedream._post({"model": "m"}))
    assert body["data"][0]["url"] == "https://x/img.png"
    assert seen[0].startswith("https://ark.example.com/api/v3")
    assert seen[1] == "https://ark.example.com/api/plan/v3/images/generations"
    # 成功后记住新端点，后续不再多打一次
    assert settings.ark_base_url == "https://ark.example.com/api/plan/v3"


def test_unsupported_model_message_is_actionable(monkeypatch):
    import asyncio

    class FakeResponse:
        status_code = 404

        def json(self):
            return {"error": {"code": "UnsupportedModel",
                              "message": "The requested model does not support the agent plan feature."}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(settings, "ark_api_key", "ark-plan-key", raising=False)
    monkeypatch.setattr(seedream.net, "async_client", lambda **kwargs: FakeClient())
    with pytest.raises(seedream.SeedreamError) as exc:
        asyncio.run(seedream._post({"model": "doubao-seedream-5-0"}))
    assert "5-5-261028" in str(exc.value)


def test_edit_never_sends_auto_size(ark, monkeypatch):
    """auto 只允许用于 layer_decomposition，局改必须换成具体像素。"""
    import asyncio

    base = png(1880, 800)

    async def fake_download(url):
        return base

    monkeypatch.setattr(seedream, "_download", fake_download)
    asyncio.run(seedream.edit(base_image=data_url(base), mask_image=None,
                              instruction="改标题", size="auto"))
    assert ark[-1]["size"] == "1880x800"

    tiny = png(200, 160)

    async def fake_download_tiny(url):
        return tiny

    monkeypatch.setattr(seedream, "_download", fake_download_tiny)
    asyncio.run(seedream.edit(base_image=data_url(tiny), mask_image=None,
                              instruction="改标题", size="auto"))
    assert ark[-1]["size"] == "2K"


def test_generate_never_sends_auto_size(ark, monkeypatch):
    import asyncio

    async def fake_download(url):
        return png()

    monkeypatch.setattr(seedream, "_download", fake_download)
    asyncio.run(seedream.generate(prompt="海报", size="auto"))
    assert ark[-1]["size"] == "2K"


def test_decompose_keeps_auto_size(ark, monkeypatch):
    import asyncio

    async def fake_post(payload):
        ark.append(payload)
        return {"data": [{"url": "https://ark.example/base.png"}]}

    async def fake_download(url):
        return png(300, 200)

    monkeypatch.setattr(seedream, "_post", fake_post)
    monkeypatch.setattr(seedream, "_download", fake_download)
    monkeypatch.setattr(seedream, "import_template",
                        lambda *a, **k: {"name": "t", "analyzed": True})
    asyncio.run(seedream.decompose(image=data_url(png(300, 200)), max_images=20))
    assert ark[-1]["size"] == "auto"
    assert "background" not in ark[-1]


def test_edit_prompt_keeps_element_type(ark, monkeypatch):
    """圈选文字改成“篮球”时，指令必须要求仍以文字呈现，不能画成篮球图标。"""
    import asyncio

    base = png(1880, 800)

    async def fake_download(url):
        return base

    monkeypatch.setattr(seedream, "_download", fake_download)
    asyncio.run(seedream.edit(base_image=data_url(base), mask_image=None,
                              instruction="改成篮球", size="auto"))
    prompt = ark[-1]["prompt"]
    assert "改成篮球" in prompt
    assert "默认保持原标注区域的元素形态不变" in prompt
    assert "仍以文字形式呈现" in prompt
    assert "仅替换对应文案内容" in prompt
    assert "字体、字号、颜色及前后装饰符号/标点" in prompt
    assert "若原区域为图形或图标，则按同画风替换图形" in prompt
