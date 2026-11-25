"""海报模板解析 / 合成测试（不启动浏览器、不调用真实大模型）。"""

from __future__ import annotations

import json

import pytest
from lxml import etree

from app.services import template_analyzer, template_renderer
from app.services.llm_client import llm_configured

TINY_SVG = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 400 300" width="400" height="300">
  <defs><clipPath id="c1"><circle cx="100" cy="100" r="60"/></clipPath></defs>
  <text transform="translate(20 40)" font-size="30" fill="#fff">主标题 测试</text>
  <text transform="translate(20 80)" font-size="18" fill="#fff">1月24日 19:30</text>
  <g clip-path="url(#c1)">
    <image width="120" height="120" xlink:href="data:image/png;base64,iVBORw0KGgo="/>
  </g>
  <image x="300" y="220" width="50" height="50" xlink:href="data:image/png;base64,iVBORw0KGgo="/>
</svg>""".encode("utf-8")


DUP_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" width="400" height="300">
  <text transform="translate(20 40)" font-size="30" fill="#882222">重复标题 测试</text>
  <text transform="translate(20 40)" font-size="30" fill="#ffffff">重复标题 测试</text>
  <text transform="translate(20 90)" font-size="18" fill="#ffffff">1月24日 19:30</text>
</svg>""".encode("utf-8")


@pytest.fixture()
def schema(monkeypatch):
    monkeypatch.setattr(template_analyzer.llm_client, "llm_configured", lambda: False)
    return template_analyzer.analyze_template(
        TINY_SVG, name="测试模板", use_llm=False, use_browser=False, force=True
    )


@pytest.fixture(autouse=True)
def _disable_real_ocr_for_tests(monkeypatch):
    """单元测试不加载真实 OCR 模型；复活测试自行 monkeypatch 为可用。"""
    from app.services import ocr

    monkeypatch.setattr(ocr, "ocr_available", lambda: False)


def test_analyzer_extracts_text_and_portrait(schema):
    fields = {entry["field"] for entry in schema["texts"]}
    assert "main_title" in fields
    assert "schedule" in fields
    assert schema["canvas"]["width"] == 400

    assert len(schema["portraits"]) == 1
    portrait = schema["portraits"][0]
    assert portrait["mode"] == "framed_circle"
    assert portrait["slot"] == 0

    qr = [item for item in schema["images"] if item["field"] == "qr_frame"]
    assert qr and qr[0]["width"] == 50


def test_analyzer_merges_llm_mapping(monkeypatch):
    monkeypatch.setattr(template_analyzer.llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(
        template_analyzer.llm_client,
        "chat_json",
        lambda *args, **kwargs: {
            "texts": [{"id": "text-001", "field": "subtitle", "slot": 0}],
            "images": [
                {"id": "image-001", "field": "portrait_frame", "slot": 0, "mode": "head_shoulder"}
            ],
            "summary": "大模型摘要",
        },
    )
    result = template_analyzer.analyze_template(
        TINY_SVG, name="测试模板", use_llm=True, use_browser=False, force=True
    )
    assert result["source"] == "llm"
    assert result["summary"] == "大模型摘要"
    fields = {entry["id"]: entry["field"] for entry in result["texts"]}
    assert fields["text-001"] == "subtitle"
    assert result["portraits"][0]["mode"] == "head_shoulder"


def test_parse_text_heuristic(schema):
    values, engine, warnings = template_renderer.parse_text_values(
        schema, "新主标题\n1月25日 20:00", use_llm=False
    )
    assert engine == "heuristic"
    main_id = next(item["id"] for item in schema["texts"] if item["field"] == "main_title")
    assert values[main_id] == "新主标题"


def test_renderer_replaces_text(schema):
    from lxml import etree

    root = etree.fromstring(
        template_analyzer.template_svg_path(schema["template_id"]).read_bytes()
    )
    main = next(item for item in schema["texts"] if item["field"] == "main_title")
    applied, warnings = template_renderer._apply_text_values(root, schema, {main["id"]: "新标题"})
    assert applied and not warnings
    assert "新标题" in "".join(root.itertext())


def test_analyzer_removes_duplicate_text_layers(monkeypatch):
    monkeypatch.setattr(template_analyzer.llm_client, "llm_configured", lambda: False)
    result = template_analyzer.analyze_template(
        DUP_SVG, name="重影模板", use_llm=False, use_browser=False, force=True
    )
    duplicates = [item for item in result["texts"] if "重复标题" in item["text"]]
    assert len(duplicates) == 1, "同坐标同内容的底层文字应被移除"
    assert result["removed_duplicates"], "应记录被移除的重复图层"
    assert any("重复文字图层" in warning for warning in result["warnings"])


def test_renderer_clears_tspan_children_when_replacing_whole_text():
    from lxml import etree

    root = etree.fromstring(
        '<svg xmlns="http://www.w3.org/2000/svg"><text id="t1" font-size="20">旧标题<tspan>旧副标题</tspan></text></svg>'
    )
    schema = {
        "texts": [
            {
                "id": "t1",
                "kind": "text",
                "field": "main_title",
                "targets": [
                    {
                        "id": "t1",
                        "kind": "text",
                        "preserve_children": False,
                        "member_ids": ["t1"],
                    }
                ],
            }
        ]
    }
    applied, warnings = template_renderer._apply_text_values(
        root, schema, {"t1": "全新的完整标题"}
    )
    element = root.find("{http://www.w3.org/2000/svg}text")
    assert applied and not warnings
    assert len(list(element)) == 0, "旧 tspan 必须被清空"
    assert element.text == "全新的完整标题"


def test_renderer_keeps_default_when_value_empty():
    from lxml import etree

    root = etree.fromstring(
        '<svg xmlns="http://www.w3.org/2000/svg"><text id="t1">本周课程回放</text></svg>'
    )
    schema = {
        "texts": [
            {
                "id": "t1",
                "kind": "text",
                "field": "qr_tip",
                "targets": [{"id": "t1", "kind": "text"}],
            }
        ]
    }
    applied, _ = template_renderer._apply_text_values(root, schema, {"t1": ""})
    element = root.find("{http://www.w3.org/2000/svg}text")
    assert applied == []
    assert element.text == "本周课程回放"


def test_renderer_keeps_mapped_tspan_when_replacing_direct_text():
    from lxml import etree

    root = etree.fromstring(
        '<svg xmlns="http://www.w3.org/2000/svg"><text id="t1" font-size="20">标题<tspan id="t2" font-size="12">副标题</tspan></text></svg>'
    )
    schema = {
        "texts": [
            {
                "id": "t1",
                "kind": "text",
                "field": "course_title",
                "targets": [{"id": "t1", "kind": "text"}],
            },
            {
                "id": "t2",
                "kind": "tspan",
                "field": "course_subtitle",
                "targets": [{"id": "t2", "kind": "tspan", "member_ids": ["t2"]}],
            },
        ]
    }
    template_renderer._apply_text_values(root, schema, {"t1": "新标题"})
    element = root.find("{http://www.w3.org/2000/svg}text")
    tspan = element.find("{http://www.w3.org/2000/svg}tspan")
    assert element.text == "新标题"
    assert tspan is not None and tspan.text == "副标题", "独立字段的 tspan 不能被误删"


def test_renderer_consumes_split_leading_char_and_keeps_highlight():
    from lxml import etree

    svg = """<svg xmlns="http://www.w3.org/2000/svg">
      <text id="t1" transform="translate(76.89 1952.81)" fill="#ffffff">扫<tspan id="t2" x="54.74">码加</tspan><tspan id="t3" x="149.08" fill="#fde2a7">【2026漳州中招君】</tspan><tspan id="t4" x="635.51">为好友</tspan></text>
    </svg>"""
    root = etree.fromstring(svg)
    schema = {
        "texts": [
            {
                "id": "t1",
                "kind": "text",
                "text": "扫",
                "x": 76.89,
                "y": 1952.81,
                "width": 55.7,
                "targets": [{"id": "t1", "kind": "text"}],
            },
            {
                "id": "t2",
                "kind": "tspan",
                "text": "码加【2026漳州中招君】为好友",
                "x": 131.63,
                "y": 1952.81,
                "width": 744.0,
                "targets": [
                    {
                        "id": "t2",
                        "kind": "tspan",
                        "collapse": True,
                        "member_ids": ["t2", "t3", "t4"],
                    }
                ],
            },
        ]
    }
    template_renderer._apply_text_values(
        root, schema, {"t1": "扫", "t2": "扫码加【2026漳州中招君】为好友"}
    )

    ns = "{http://www.w3.org/2000/svg}"
    text = root.find(ns + "text")
    assert text.text is None, "行首独立的“扫”应被清空，避免扫扫码"
    tspan = text.find(ns + "tspan")
    assert tspan is not None
    assert tspan.text == "扫码加"
    assert tspan.get("x") == "0.00", "剩余文本应左移回原行首"
    highlight = tspan.find(ns + "tspan")
    assert highlight is not None
    assert highlight.get("fill") == "#fde2a7"
    assert highlight.text == "【2026漳州中招君】"
    assert highlight.tail == "为好友"
    assert text.find(ns + "tspan[@id='t3']") is None


def test_renderer_removes_separate_leading_single_char_text():
    from lxml import etree

    svg = """<svg xmlns="http://www.w3.org/2000/svg">
      <text id="a" x="0" y="100">扫</text>
      <text id="b" x="57" y="100">码加好友</text>
    </svg>"""
    root = etree.fromstring(svg)
    schema = {
        "texts": [
            {
                "id": "a",
                "kind": "text",
                "text": "扫",
                "x": 0,
                "y": 100,
                "width": 55,
                "targets": [{"id": "a", "kind": "text"}],
            },
            {
                "id": "b",
                "kind": "text",
                "text": "码加好友",
                "x": 57,
                "y": 100,
                "width": 200,
                "targets": [{"id": "b", "kind": "text"}],
            },
        ]
    }
    template_renderer._apply_text_values(
        root, schema, {"a": "扫", "b": "扫码加好友"}
    )
    ns = "{http://www.w3.org/2000/svg}"
    ids = [element.get("id") for element in root.findall(ns + "text")]
    assert "a" not in ids, "独立的行首单字节点应被移除"
    b = root.find(ns + "text[@id='b']")
    assert b is not None
    assert b.text == "扫码加好友"


def test_renderer_without_brackets_keeps_plain_text():
    from lxml import etree

    root = etree.fromstring(
        '<svg xmlns="http://www.w3.org/2000/svg"><text id="t1" fill="#fff">旧文字<tspan fill="#fde2a7">高亮</tspan></text></svg>'
    )
    schema = {
        "texts": [
            {
                "id": "t1",
                "kind": "text",
                "text": "旧文字高亮",
                "targets": [{"id": "t1", "kind": "text"}],
            }
        ]
    }
    template_renderer._apply_text_values(root, schema, {"t1": "普通新文字"})
    element = root.find("{http://www.w3.org/2000/svg}text")
    assert element.text == "普通新文字"
    assert element.find("{http://www.w3.org/2000/svg}tspan") is None


def test_local_fonts_expose_illustrator_aliases():
    from app.services import fonts

    faces = fonts.discover_fonts()
    if not faces:
        pytest.skip("static/Fonts 下没有字体文件")
    aliases = {alias for face in faces for alias in face.aliases}
    assert "AlibabaPuHuiTi_3_105_Heavy-GBpc-EUC-H" in aliases
    assert "AlibabaPuHuiTi_3_55_Regular-GBpc-EUC-H" in aliases
    # 老版 Illustrator 字体名（1.0/2.0）也要能映射
    assert "AlibabaPuHuiTiB" in aliases
    assert "AlibabaPuHuiTiM" in aliases
    assert "AlibabaPuHuiTiH" in aliases
    css = fonts.font_css()
    assert "@font-face" in css
    assert "file:///" in css


@pytest.fixture()
def named_template(client, monkeypatch):
    from PIL import Image
    from app.config import settings
    from app.services import llm_client, svg_browser

    name = "demo"
    settings.template_dir.mkdir(parents=True, exist_ok=True)
    (settings.template_dir / f"{name}.fixed.svg").write_bytes(TINY_SVG)
    monkeypatch.setattr(llm_client, "llm_configured", lambda: False)
    monkeypatch.setattr(svg_browser, "measure_svg", lambda *args, **kwargs: {})

    def fake_screenshot(svg_path, canvas, out_path, scale=1):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (12, 12), "white").save(out_path)
        return (12, 12)

    def fake_thumbnail(svg_path, canvas, out_path, width=320):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (width, 320), "white").save(out_path, format="JPEG")
        return (width, 320)

    monkeypatch.setattr(svg_browser, "screenshot_svg", fake_screenshot)
    monkeypatch.setattr(svg_browser, "thumbnail_svg", fake_thumbnail)
    return name


def test_named_template_list_analyze_and_schema(client, named_template):
    listing = client.get("/api/v1/posters/templates").json()
    assert listing[0]["name"] == named_template
    assert listing[0]["display_name"] == named_template
    assert listing[0]["analyzed"] is False

    response = client.post(f"/api/v1/posters/templates/{named_template}/analyze")
    assert response.status_code == 200, response.text
    assert response.json()["analyzed"] is True

    listing = client.get("/api/v1/posters/templates").json()
    assert listing[0]["analyzed"] is True

    schema = client.get(f"/api/v1/posters/templates/{named_template}/schema")
    assert schema.status_code == 200
    body = schema.json()
    assert body["canvas"]["width"] == 400
    assert body["texts"] and body["images"]


def test_named_template_thumbnail_is_cached(client, named_template):
    first = client.get(f"/api/v1/posters/templates/{named_template}/thumbnail")
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"
    second = client.get(f"/api/v1/posters/templates/{named_template}/thumbnail")
    assert second.status_code == 200
    assert len(second.content) == len(first.content)


def test_workbench_parse_preview_and_export(client, named_template, monkeypatch):
    from app.services import svg_browser

    client.post(f"/api/v1/posters/templates/{named_template}/analyze")
    schema = client.get(f"/api/v1/posters/templates/{named_template}/schema").json()
    main = next(item for item in schema["texts"] if item["field"] == "main_title")

    parsed = client.post(
        "/api/v1/posters/parse-text",
        json={"template_name": named_template, "raw_text": "全新主标题\n1月25日 20:00"},
    )
    assert parsed.status_code == 200
    assert parsed.json()["values"]

    preview = client.post(
        "/api/v1/posters/preview",
        json={"template_name": named_template, "data": {main["id"]: "预览新标题"}},
    )
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"].startswith("image/svg+xml")
    assert "预览新标题" in preview.text

    monkeypatch.setattr(svg_browser, "fit_and_export", lambda *args, **kwargs: {})
    export = client.post(
        "/api/v1/posters/export",
        json={"template_name": named_template, "data": {main["id"]: "导出标题"}},
    )
    assert export.status_code == 200, export.text
    body = export.json()
    assert body["width"] == 800
    assert body["url"].endswith("poster.png")


def test_assets_upload_list_and_fetch(client):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buffer, format="PNG")
    response = client.post(
        "/api/v1/posters/assets/upload",
        files={"file": ("qr.png", buffer.getvalue(), "image/png")},
        data={"kind": "qrcode"},
    )
    assert response.status_code == 200, response.text
    asset = response.json()["asset"]
    assert asset["kind"] == "qrcode"

    listing = client.get("/api/v1/posters/assets?kind=qrcode").json()
    assert any(item["id"] == asset["id"] for item in listing)
    assert client.get(asset["url"]).status_code == 200
    assert client.get(asset["thumbnail_url"]).status_code == 200


def test_sanitize_svg_repairs_common_issues():
    from lxml import etree
    from app.services.svg_sanitizer import sanitize_svg

    raw = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><text font-family="A, "B C"">x & y</text></svg>'.encode()
    cleaned, notes = sanitize_svg(raw)
    root = etree.fromstring(cleaned)
    assert root.get("viewBox") == "0 0 400.0 300.0"
    assert "Alibaba PuHuiTi 3.0" in root[0].get("font-family")
    assert "修复 font-family 内嵌引号" in notes
    assert "转义孤立 &" in notes


def test_upload_reports_revived_texts(client, monkeypatch):
    from app.services import svg_browser

    monkeypatch.setattr(svg_browser, "thumbnail_svg", lambda *args, **kwargs: (320, 240))
    monkeypatch.setattr(
        template_analyzer,
        "revive_named_template",
        lambda name, use_llm=True: {
            "available": True,
            "revived": [{"text": "语文期末冲刺课"}],
            "skipped": [],
            "warnings": ["已复活 1 行转曲文字"],
            "revived_count": 1,
        },
    )
    response = client.post(
        "/api/v1/posters/templates/upload",
        files={"file": ("revival.fixed.svg", TINY_SVG.decode("utf-8"), "image/svg+xml")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revived_texts"] == 1
    assert body["revival_report"]["revived"][0]["text"] == "语文期末冲刺课"
    assert any("已复活" in item for item in body["warnings"])


def test_delete_template_removes_all_artifacts(client):
    from urllib.parse import quote
    from app.config import settings

    name = "delete-me-template"
    source = settings.template_dir / f"{name}.fixed.svg"
    source.write_bytes(TINY_SVG)
    (settings.template_dir / f"{name}.schema.json").write_text("{}", encoding="utf-8")
    (settings.template_dir / f"{name}.template.svg").write_bytes(TINY_SVG)
    (settings.template_dir / f"{name}.fixed.pre-revival.svg").write_bytes(TINY_SVG)
    thumbs = settings.template_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    (thumbs / f"{name}.jpg").write_bytes(b"jpg")
    (thumbs / f"{name}.rev").write_text("rev", encoding="utf-8")

    response = client.delete(f"/api/v1/posters/templates/{quote(name, safe='')}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    for path in (
        source,
        settings.template_dir / f"{name}.schema.json",
        settings.template_dir / f"{name}.template.svg",
        settings.template_dir / f"{name}.fixed.pre-revival.svg",
        thumbs / f"{name}.jpg",
        thumbs / f"{name}.rev",
    ):
        assert not path.exists(), path
    second = client.delete(f"/api/v1/posters/templates/{quote(name, safe='')}")
    assert second.status_code == 404


def test_template_name_from_vector_extensions():
    from app.services.template_import import _template_name_from

    assert _template_name_from("春季海报.ai") == "春季海报"
    assert _template_name_from("春季海报.EPS") == "春季海报"
    assert _template_name_from("春季海报.fixed.svg") == "春季海报"
    assert _template_name_from("春季海报.svg") == "春季海报"


def test_vector_importer_passthrough_svg():
    from app.services import vector_importer

    raw = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
    converted, notes = vector_importer.convert_to_svg(raw, "test.svg", max_svg_bytes=1024)
    assert converted == raw
    assert notes == ["SVG 原文件"]


def test_vector_importer_preserves_pdf_cjk_digits_and_images():
    from app.services import vector_importer
    from lxml import etree
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=240, height=160)
    font = (template_analyzer.settings.base_dir / "static" / "Fonts" / "AlibabaPuHuiTi-3-65-Medium.ttf").read_bytes()
    page.insert_font(fontname="cn", fontbuffer=font)
    page.insert_text((18, 42), "中考公益课 2026", fontname="cn", fontsize=24)
    png = __import__("io").BytesIO()
    from PIL import Image
    Image.new("RGB", (2, 2), "red").save(png, "PNG")
    page.insert_image(pymupdf.Rect(180, 70, 210, 100), stream=png.getvalue())
    converted, status = vector_importer._convert_page(page)
    root = etree.fromstring(converted)
    text = root.xpath("string(.//svg:text)", namespaces={"svg": "http://www.w3.org/2000/svg"})
    assert "中考公益课" in text and "2026" in text
    assert root.xpath("count(.//svg:image)", namespaces={"svg": "http://www.w3.org/2000/svg"}) == 1
    assert status == {"status": "editable", "editable_count": 1, "image_count": 1}


def test_vector_importer_missing_tool_message(monkeypatch):
    from app.services import vector_importer

    monkeypatch.setattr(vector_importer, "_find_tool", lambda name, required=True: None)
    try:
        vector_importer.convert_to_svg(b"%PDF-1.4\n", "test.ai", max_svg_bytes=1024)
    except Exception as exc:
        assert "PyMuPDF" in str(exc) or "转换" in str(exc)
    else:
        raise AssertionError("expected conversion failure")


def test_upload_svg_template(client, monkeypatch):
    from app.config import settings
    from app.services import svg_browser

    monkeypatch.setattr(svg_browser, "thumbnail_svg", lambda *args, **kwargs: (320, 240))
    payload = TINY_SVG.decode("utf-8")
    first = client.post(
        "/api/v1/posters/templates/upload",
        files={"file": ("新模板.fixed.svg", payload, "image/svg+xml")},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["analyzed"] is True
    assert (settings.template_dir / f"{body['name']}.fixed.svg").is_file()
    stored = template_analyzer.load_named_schema(body["name"])
    assert all(entry.get("role") for entry in stored["texts"])

    listing = client.get("/api/v1/posters/templates").json()
    assert any(item["name"] == body["name"] for item in listing)

    # 同名再传一次会自动加后缀，避免覆盖
    second = client.post(
        "/api/v1/posters/templates/upload",
        files={"file": ("新模板.fixed.svg", payload, "image/svg+xml")},
    )
    assert second.status_code == 200
    assert second.json()["name"] != body["name"]


def test_upload_svg_cleans_broken_markup(client, monkeypatch):
    from lxml import etree
    from app.config import settings
    from app.services import svg_browser

    monkeypatch.setattr(svg_browser, "thumbnail_svg", lambda *args, **kwargs: (320, 240))
    broken = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="500" height="700">'
        '<text font-family="A, "B"">A & B</text></svg>'
    )
    response = client.post(
        "/api/v1/posters/templates/upload",
        files={"file": ("broken.fixed.svg", broken, "image/svg+xml")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["warnings"]
    saved = settings.template_dir / f"{body['name']}.fixed.svg"
    root = etree.fromstring(saved.read_bytes())
    assert root.get("viewBox") == "0 0 500.0 700.0"
    assert "Alibaba PuHuiTi 3.0" in root[0].get("font-family")


def test_sanitizer_maps_unknown_fonts_to_fallback():
    from lxml import etree
    from app.services.svg_sanitizer import sanitize_svg

    raw = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
        '<text font-family="SimSun, serif" font-size="80">大标题</text>'
        '<text font-family="AlibabaPuHuiTiB" font-size="20">正文</text>'
        "</svg>"
    ).encode()
    cleaned, notes = sanitize_svg(raw)
    root = etree.fromstring(cleaned)
    title, body = list(root)
    assert "Alibaba PuHuiTi 3.0" in title.get("font-family")
    assert title.get("font-weight") is None
    assert "Alibaba PuHuiTi 3.0" in body.get("font-family")
    assert body.get("font-weight") == "700"
    assert any("统一" in note for note in notes)


def test_sanitizer_maps_font_weight_from_legacy_names():
    from lxml import etree
    from app.services.svg_sanitizer import sanitize_svg

    raw = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
        '<text font-family="AlibabaPuHuiTiH" font-size="40">H</text>'
        '<text font-family="AlibabaPuHuiTiB" font-size="40">B</text>'
        '<text font-family="AlibabaPuHuiTiM" font-size="40">M</text>'
        '<text font-family="AlibabaPuHuiTiR" font-size="40">R</text>'
        '<text font-family="AlibabaPuHuiTiBL" font-size="40">BL</text>'
        "</svg>"
    ).encode()
    cleaned, _ = sanitize_svg(raw)
    root = etree.fromstring(cleaned)
    weights = [element.get("font-weight") for element in root]
    families = {element.get("font-family") for element in root}
    assert weights == ["800", "700", "500", "400", "900"]
    assert families == {"'Alibaba PuHuiTi 3.0'"}


def test_analyzer_keeps_stroke_from_duplicate_layer():
    from app.services import template_analyzer

    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" width="400" height="300">
      <text transform="translate(20 100)" font-size="40" fill="#ffffff" stroke="#cb0f0f" stroke-width="8">描边标题</text>
      <text transform="translate(20 100)" font-size="40" fill="#ffffff">描边标题</text>
    </svg>""".encode("utf-8")
    schema = template_analyzer.analyze_template(
        svg, name="描边模板", use_llm=False, use_browser=False, force=True
    )
    entry = next(item for item in schema["texts"] if item["text"] == "描边标题")
    assert entry["style"]["stroke_color"] == "#cb0f0f"
    assert entry["style"]["stroke_width"] == 8


def test_renderer_applies_stroke_and_dash_spacing():
    from lxml import etree

    svg = """<svg xmlns="http://www.w3.org/2000/svg">
      <text id="t1" letter-spacing="0.08em" font-size="40" fill="#ffffff">成为读者——用书打开世界</text>
    </svg>"""
    root = etree.fromstring(svg)
    schema = {
        "texts": [
            {
                "id": "t1",
                "kind": "text",
                "text": "成为读者——用书打开世界",
                "style": {"stroke_color": "#cb0f0f", "stroke_width": 8},
                "targets": [{"id": "t1", "kind": "text"}],
            }
        ]
    }
    template_renderer._apply_text_values(
        root, schema, {"t1": "新的读者——用书打开世界"}
    )
    element = root.find("{http://www.w3.org/2000/svg}text")
    assert element.get("stroke") == "#cb0f0f"
    assert "paint-order:stroke fill" in element.get("style")
    assert element.get("letter-spacing") is None
    dash = [
        child
        for child in element.iter("{http://www.w3.org/2000/svg}tspan")
        if (child.text or "") == "——"
    ]
    assert dash
    assert dash[0].get("letter-spacing") is None


def test_analyzer_merges_same_line_fragments_and_renderer_rewrites():
    from lxml import etree
    from app.services import template_analyzer

    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 300" width="500" height="300">
      <text transform="translate(20 100)" font-size="40" fill="#ffffff">4</text>
      <text transform="translate(70 100)" font-size="40" fill="#ffffff">月11日</text>
    </svg>""".encode("utf-8")
    schema = template_analyzer.analyze_template(
        svg, name="同行合并", use_llm=False, use_browser=False, force=True
    )
    entry = next(item for item in schema["texts"] if "4月11日" in item["text"])
    assert entry["merge"] is True
    assert len(entry["targets"]) == 2

    rendered, _ = template_renderer.build_svg(schema, {entry["id"]: "5月12日"})
    assert "5月12日" in rendered
    assert "11日" not in rendered


def test_build_svg_applies_style_overrides():
    from lxml import etree

    root_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" width="400" height="300">
      <text id="t1" transform="translate(20 80)" font-size="40">标题</text>
    </svg>""".encode("utf-8")
    import tempfile
    from pathlib import Path

    schema = {
        "canvas": {"width": 400, "height": 300},
        "texts": [
            {
                "id": "t1",
                "kind": "text",
                "text": "标题",
                "font_size": 40,
                "max_width": 300,
                "targets": [{"id": "t1", "kind": "text"}],
            }
        ],
        "images": [],
        "svg_path": "",
    }
    # 直接给 schema 一个临时 SVG 路径
    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as handle:
        handle.write(root_svg)
        path = Path(handle.name)
    schema["svg_path"] = str(path)
    try:
        rendered, _ = template_renderer.build_svg(
            schema, {"t1": "标题"}, styles={"t1": {"font_scale": 1.2, "dx": 2, "dy": -3}}
        )
        root = etree.fromstring(rendered.encode())
        text = root.find("{http://www.w3.org/2000/svg}text")
        assert float(text.get("font-size")) == 48
        assert "translate(2 -3)" in text.get("transform")
    finally:
        path.unlink(missing_ok=True)


def test_analyzer_merges_dash_with_following_text():
    from app.services import template_analyzer

    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 300" width="800" height="300">
      <text transform="translate(20 100)" font-size="65" fill="#ffffff">——</text>
      <text transform="translate(160 100)" font-size="52" fill="#ffffff">用《昆虫记》打开世界</text>
    </svg>""".encode("utf-8")
    schema = template_analyzer.analyze_template(
        svg, name="破折号合并", use_llm=False, use_browser=False, force=True
    )
    entry = next(item for item in schema["texts"] if "——" in item["text"])
    assert entry["text"].startswith("——用《昆虫记》")
    assert entry["merge"] is True
    assert len(entry["targets"]) == 2


def test_revival_replaces_gradient_outline(tmp_path, monkeypatch):
    from app.services import ocr, text_revival
    from PIL import Image

    svg_path = tmp_path / "revive.fixed.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200" viewBox="0 0 400 200">
        <defs>
          <linearGradient id="grad" gradientUnits="userSpaceOnUse" x1="20" y1="20" x2="380" y2="20">
            <stop offset="0" stop-color="#ff0000"/>
            <stop offset="1" stop-color="#ffff00"/>
          </linearGradient>
        </defs>
        <path id="glyph" d="M20 20 H380 V100 H20 Z" fill="url(#grad)"/>
        <text id="live" x="20" y="150" font-size="20" fill="#ffffff">已有文字</text>
        </svg>""",
        encoding="utf-8",
    )

    def fake_screenshot(svg_path, canvas, out_path, scale=1):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (400, 200), "white").save(out_path)
        return (400, 200)

    def fake_inspect(svg_path, canvas):
        return [
            {
                "id": "glyph",
                "x": 20.0,
                "y": 20.0,
                "w": 360.0,
                "h": 80.0,
                "fill": "url(#grad)",
                "fillOpacity": "1",
                "parentId": "",
                "transformedAncestorId": "",
                "clipped": False,
            }
        ]

    def fake_measure(svg_path, canvas, ids):
        from lxml import etree

        root = etree.fromstring(svg_path.read_bytes())
        by_id = {element.get("id"): element for element in root.iter()}
        out = {}
        for element_id in ids:
            element = by_id.get(element_id)
            if element is None or element.tag.rsplit("}", 1)[-1] != "text":
                continue
            text = "".join(element.itertext())
            font = float(element.get("font-size") or 16)
            x = float(element.get("x") or 0)
            width = len(text) * font * 0.95
            if element.get("text-anchor") == "middle":
                x -= width / 2
            out[element_id] = {
                "x": x,
                "y": float(element.get("y") or 0) - font * 0.8,
                "w": width,
                "h": font * 1.1,
                "font_size": font,
                "anchor": element.get("text-anchor") or "start",
            }
        return out

    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(
        ocr,
        "detect_text_lines",
        lambda image_png, canvas: [
            {"text": "语文期末冲刺课", "bbox": (20.0, 20.0, 360.0, 80.0), "confidence": 0.98}
        ],
    )
    monkeypatch.setattr(text_revival.svg_browser, "screenshot_svg", fake_screenshot)
    monkeypatch.setattr(text_revival.svg_browser, "inspect_paths", fake_inspect)
    monkeypatch.setattr(text_revival.svg_browser, "measure_svg", fake_measure)

    from contextlib import contextmanager

    @contextmanager
    def fake_ink_probe(path, canvas):
        root = text_revival._parse_svg(path.read_bytes())

        def probe(node_id, sizes=None):
            node = next(el for el in root.iter() if el.get("id") == node_id)
            sizes = sizes or [float(node.get("font-size"))]
            x, y = float(node.get("x")), float(node.get("y"))
            return [{"size": size, "box": [x - 2.25 * size if node.get("text-anchor") == "middle" else x,
                                           y - size, 4.5 * size, size], "scale": [1, 1]} for size in sizes]

        yield probe

    monkeypatch.setattr(text_revival.svg_browser, "text_ink_probe", fake_ink_probe)
    report = text_revival.revive_outlined_text(svg_path, use_llm=False)
    assert report["revived_count"] == 1, report
    assert report["revived"][0]["text"] == "语文期末冲刺课"
    root = etree.fromstring(svg_path.read_bytes())
    assert root.find(".//{http://www.w3.org/2000/svg}path[@id='glyph']") is None
    revived = root.find(".//{http://www.w3.org/2000/svg}text[@data-revived='1']")
    assert revived is not None
    assert revived.text == "语文期末冲刺课"
    assert revived.get("font-weight") == "800"
    assert revived.get("fill", "").startswith("url(#revived-grad-")
    cloned = root.find(".//{http://www.w3.org/2000/svg}linearGradient[@gradientUnits='objectBoundingBox']")
    assert cloned is not None
    assert len(cloned.findall("{http://www.w3.org/2000/svg}stop")) == 2


def test_revival_without_ocr_keeps_file_untouched(tmp_path, monkeypatch):
    from app.services import ocr, text_revival

    svg_path = tmp_path / "untouched.fixed.svg"
    original = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><path d="M0 0h10v10H0z"/></svg>'
    svg_path.write_bytes(original)
    monkeypatch.setattr(ocr, "ocr_available", lambda: False)
    report = text_revival.revive_outlined_text(svg_path, use_llm=False)
    assert report["revived_count"] == 0
    assert svg_path.read_bytes() == original


def test_analyzer_splits_nested_tspan_prefix_and_styled_child():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="600" height="200" viewBox="0 0 600 200">
      <text id="text-001" x="20" y="80" fill="#ffffff">
        <tspan id="outer" font-size="40">数学 | <tspan id="inner" fill="#ffdd00">寻“根”之旅</tspan></tspan>
        <tspan id="small" font-size="20">——一元二次方程专题复习</tspan>
      </text>
    </svg>""".encode("utf-8")
    schema = template_analyzer.analyze_template(
        svg, name="嵌套多字号", use_llm=False, use_browser=False, force=True
    )
    texts = {entry["text"]: entry for entry in schema["texts"]}
    assert "数学 |" in texts
    assert "寻“根”之旅" in texts
    assert texts["数学 |"]["kind"] == "tspan"
    assert texts["寻“根”之旅"]["kind"] == "tspan"
    assert texts["数学 |"]["font_size"] == 40
    assert texts["寻“根”之旅"]["font_size"] == 40
    # 颜色不同的同字号 tspan 不能合并
    assert texts["数学 |"]["targets"][0]["id"] != texts["寻“根”之旅"]["targets"][0]["id"]


def test_analyzer_splits_same_size_tspans_with_different_fill():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120" viewBox="0 0 400 120">
      <text id="text-001" x="20" y="60" font-size="30">
        <tspan id="red" fill="#ff0000">红色</tspan>
        <tspan id="blue" fill="#0000ff">蓝色</tspan>
      </text>
    </svg>""".encode("utf-8")
    schema = template_analyzer.analyze_template(
        svg, name="同字号多色", use_llm=False, use_browser=False, force=True
    )
    assert {entry["id"] for entry in schema["texts"]} >= {"red", "blue"}
    assert all(entry["kind"] == "tspan" for entry in schema["texts"])


def test_renderer_updates_tspan_without_deleting_sibling_tspans():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="600" height="200" viewBox="0 0 600 200">
      <text id="text-001" x="20" y="80" fill="#fff">
        <tspan id="big" font-size="40">数学 | 寻“根”之旅</tspan>
        <tspan id="small" font-size="20">——一元二次方程专题复习</tspan>
      </text>
    </svg>""".encode("utf-8")
    schema = template_analyzer.analyze_template(
        svg, name="多字号同行", use_llm=False, use_browser=False, force=True
    )
    entries = {entry["id"]: entry for entry in schema["texts"]}
    assert set(entries) >= {"big", "small"}
    root = template_analyzer._parse_svg(svg)
    template_renderer._apply_text_values(
        root,
        schema,
        {"big": "数学 | 新根之旅", "small": "——新专题复习"},
    )
    text = root.find(".//{http://www.w3.org/2000/svg}text")
    tspans = text.findall("{http://www.w3.org/2000/svg}tspan")
    assert len(tspans) == 2
    assert tspans[0].get("id") == "big"
    assert "".join(tspans[0].itertext()) == "数学 | 新根之旅"
    assert tspans[1].get("id") == "small"
    assert "".join(tspans[1].itertext()) == "——新专题复习"
    assert tspans[0].get("font-size") == "40"
    assert tspans[1].get("font-size") == "20"


def test_embed_image_removes_old_transform_and_aligns_to_clip_frame():
    from lxml import etree

    root = etree.fromstring(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
        b'<g transform="translate(100 50) scale(2)">'
        b'<g clip-path="url(#clip)">'
        b'<image id="img" x="3" y="4" width="300" height="250" transform="scale(3)"/>'
        b"</g></g>"
        b'<defs><clipPath id="clip"><circle cx="50" cy="50" r="40"/></clipPath></defs>'
        b"</svg>"
    )
    entry = {
        "id": "img",
        "frame": {"x": 100.0, "y": 50.0, "width": 80.0, "height": 80.0},
    }
    template_renderer._embed_image(root, entry, b"png-bytes", reposition=True)
    image = root.find(".//{http://www.w3.org/2000/svg}image")
    assert image.get("transform") is None
    assert image.get("x") == "0.00"
    assert image.get("y") == "0.00"
    assert image.get("width") == "40.00"
    assert image.get("height") == "40.00"
    assert image.get("preserveAspectRatio") == "xMidYMid slice"


def test_export_svg_endpoint_preserves_namespace(client, monkeypatch):
    from PIL import Image
    from app.services import svg_browser

    def fake_screenshot(svg_path, canvas, out_path, scale=1):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (100, 100), "white").save(out_path)
        return (100, 100)

    monkeypatch.setattr(svg_browser, "screenshot_svg", fake_screenshot)
    response = client.post(
        "/api/v1/posters/export-svg",
        json={"svg": '<svg width="100" height="100"><text fill="#fff">Hi</text></svg>'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    svg = client.get(body["svg_url"])
    assert svg.status_code == 200
    assert b'xmlns="http://www.w3.org/2000/svg"' in svg.content
    assert client.get(body["url"]).status_code == 200


def test_fonts_endpoint_lists_unified_family(client):
    response = client.get("/api/v1/posters/fonts")
    assert response.status_code == 200
    family = response.json()["families"][0]
    assert family["family"] == "Alibaba PuHuiTi 3.0"
    assert 800 in family["weights"]


def test_unknown_template_returns_404(client):
    response = client.get("/api/v1/posters/templates/not-exist/schema")
    assert response.status_code == 404
    assert response.json()["error"] == "template_not_found"
