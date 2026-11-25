"""Seedream 4 插槽提示词：渠道合法性、热加载、frontmatter 兼容与拼装注入。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.config import settings
from app.services import llm_client, seedream_prompts as sp

MIN_PIXELS, MAX_PIXELS = 921_600, 4_624_220
EXPECTED_CHANNELS = [
    ("wechat_banner", "1880x800"),
    ("wechat_square", "1440x1440"),
    ("caoliao_banner", "1600x900"),
    ("caoliao_square", "1440x1440"),
    ("mobile_poster", "1242x2208"),
    ("xhs_cover", "1242x1656"),
    ("channels_vertical", "1200x1400"),
    ("channels_horizontal", "1600x900"),
]


def _pixels(size: str) -> int:
    width, height = (int(part) for part in size.split("x"))
    return width * height


def test_all_channel_presets_fit_seedream_pixel_range():
    data = json.loads((sp.prompts_dir() / "channels.json").read_text(encoding="utf-8"))
    assert list(data) == [channel_id for channel_id, _ in EXPECTED_CHANNELS]
    for channel_id, size in EXPECTED_CHANNELS:
        preset = data[channel_id]
        assert preset["size"] == size
        assert MIN_PIXELS <= _pixels(preset["size"]) <= MAX_PIXELS, channel_id
        assert preset["ratio"] and preset["safe_area"] and preset["name"]


def test_catalog_exposes_four_slots():
    catalog = sp.catalog()
    assert [item["id"] for item in catalog["channels"]] == [  # noqa: PLR2004
        channel_id for channel_id, _ in EXPECTED_CHANNELS
    ]
    assert [item["id"] for item in catalog["intents"]][:3] == ["direct", "editable_layers", "clean_bg"]
    assert all(item["core"] for item in catalog["intents"][:3])
    assert [item["id"] for item in catalog["styles"]] == [
        "academic_blue", "editorial_grid", "festive_red_gold", "xhs_bold_card",
    ]
    assert catalog["warnings"] == []


@pytest.fixture
def hot_dir(tmp_path, monkeypatch):
    """把 prompts/seedream 复制到临时目录，模拟用户现场改文件。"""
    target = tmp_path / "seedream"
    shutil.copytree(sp.prompts_dir(), target)
    monkeypatch.setattr(settings, "seedream_prompts_dir", target, raising=False)
    return target


def test_new_style_file_is_picked_up_without_restart(hot_dir):
    assert not any(item["id"] == "custom_test" for item in sp.catalog()["styles"])

    (hot_dir / "styles" / "custom_test.md").write_text(
        "---\nid: custom_test\nname: 自定义测试风\nsummary: 临时风格\n"
        "recommended_channels: [wechat_banner]\n---\n画面使用莫兰迪色与大面积留白。\n",
        encoding="utf-8",
    )
    styles = sp.catalog()["styles"]
    assert any(item["id"] == "custom_test" for item in styles)

    assembled = sp.assemble(channel_id="wechat_banner", intent_id="direct", style_id="custom_test")
    assert "styles/custom_test.md" in assembled["used_files"]
    assert "莫兰迪色" in assembled["system"]


def test_plain_markdown_without_frontmatter_and_crlf_bom(hot_dir):
    raw = "\ufeff这是第三方 Skill 正文，没有 frontmatter。\r\n第二行：保持克制。\r\n"
    (hot_dir / "styles" / "third_party_skill.md").write_text(raw, encoding="utf-8")
    styles = sp.catalog()["styles"]
    item = next(entry for entry in styles if entry["id"] == "third_party_skill")
    assert item["name"] == "third_party_skill"
    assembled = sp.assemble(channel_id="xhs_cover", intent_id="direct", style_id="third_party_skill")
    assert "第三方 Skill 正文" in assembled["system"]
    assert "\r" not in assembled["system"]


def test_intent_order_and_prompt_injection():
    banner = sp.assemble(channel_id="wechat_banner", intent_id="direct")
    assert "正中 1:1 安全区" in banner["system"]
    assert "严禁编造原文没有的日期" in banner["system"]  # 文案插槽参与 direct
    assert "channels.json#wechat_banner" in banner["used_files"]

    layers = sp.assemble(channel_id="xhs_cover", intent_id="editable_layers")
    assert "无衬线黑体" in layers["system"] and "高对比" in layers["system"]

    clean = sp.assemble(channel_id="mobile_poster", intent_id="clean_bg")
    assert "严禁出现任何文字" in clean["system"]
    assert "copywriting.md" not in clean["used_files"]  # 无字模式不注入文案插槽
    assert "严禁编造原文没有的日期" not in clean["system"]


def test_unknown_style_is_ignored_with_warning():
    assembled = sp.assemble(channel_id="wechat_square", intent_id="direct", style_id="not_exists")
    assert assembled["style"] is None
    assert any("not_exists" in warning for warning in assembled["warnings"])


def test_broken_frontmatter_falls_back_to_body(hot_dir):
    (hot_dir / "styles" / "broken.md").write_text(
        "---\nid: broken\nname: 未闭合\n", encoding="utf-8"
    )
    # 未闭合的 frontmatter 不吞掉正文：id 用文件名，正文为全文
    item = sp.parse_frontmatter((hot_dir / "styles" / "broken.md").read_text(encoding="utf-8"))
    assert item[0] == {}
    assert "name: 未闭合" in item[1]


def test_clean_bg_sanitizer_drops_text_requests():
    cleaned, warnings = sp.sanitize_clean_bg("深蓝背景，中部留白。在画面上写上标题文字。加一枚水印。")
    assert "写上标题文字" not in cleaned
    assert "加一枚水印" not in cleaned
    assert "不得出现任何文字" in cleaned
    assert warnings


def test_config_endpoint_merges_catalog_and_llm_flag(client, monkeypatch):
    monkeypatch.setattr(llm_client, "llm_configured", lambda: True)
    body = client.get("/api/seedream/config").json()
    assert len(body["channels"]) == 8  # noqa: PLR2004
    assert [item["id"] for item in body["intents"]][:3] == ["direct", "editable_layers", "clean_bg"]
    assert body["llm_configured"] is True
    assert body["prompts_dir"].endswith("seedream")


def test_optimize_prompt_rejects_bad_channel(client, monkeypatch):
    monkeypatch.setattr(llm_client, "llm_configured", lambda: True)
    response = client.post(
        "/api/seedream/optimize-prompt",
        json={"raw_input": "秋季研学营", "channel_id": "nope", "intent_id": "direct"},
    )
    assert response.status_code == 422


def test_optimize_prompt_assembles_slots_and_calls_llm(client, monkeypatch):
    captured = {}

    def fake_chat(system, user, **kwargs):
        captured["system"] = system
        captured["user"] = user
        captured["temperature"] = kwargs.get("temperature")
        return {"prompt": "一张克制排版的秋季研学营海报，主标题 “秋季研学营”。", "notes": "ok"}

    monkeypatch.setattr(llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(llm_client, "chat_json", fake_chat)

    response = client.post(
        "/api/seedream/optimize-prompt",
        json={
            "raw_input": "秋季研学营 11月25日 19:00",
            "channel_id": "wechat_banner",
            "intent_id": "direct",
            "style_id": "editorial_grid",
            "has_ref_images": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["prompt"].startswith("一张克制排版")
    assert "system_assembler.md" in body["used_files"]
    assert "intents/direct.md" in body["used_files"]
    assert "styles/editorial_grid.md" in body["used_files"]
    assert body["channel"]["size"] == "1880x800"
    assert "正中 1:1 安全区" in captured["system"]
    assert "参考图" in captured["system"]
    assert "秋季研学营 11月25日 19:00" in captured["user"]
    assert captured["temperature"] == pytest.approx(0.7)


def test_optimize_prompt_clean_bg_never_asks_for_text(client, monkeypatch):
    monkeypatch.setattr(llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(
        llm_client,
        "chat_json",
        lambda system, user, **kwargs: {
            "prompt": "深蓝渐变背景，中部留白。在画面中央写上标题文字。"
        },
    )
    body = client.post(
        "/api/seedream/optimize-prompt",
        json={"raw_input": "长按扫码进群", "channel_id": "mobile_poster", "intent_id": "clean_bg"},
    ).json()
    assert "写上标题文字" not in body["prompt"]
    assert "不得出现任何文字" in body["prompt"]
    assert body["warnings"]


def test_optimize_prompt_without_llm_returns_503(client, monkeypatch):
    monkeypatch.setattr(llm_client, "llm_configured", lambda: False)
    response = client.post(
        "/api/seedream/optimize-prompt",
        json={"raw_input": "随便写点", "channel_id": "wechat_banner", "intent_id": "direct"},
    )
    assert response.status_code == 503
