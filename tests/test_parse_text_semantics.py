"""AI 智能填表的增量语义：未提及字段不得被空值覆盖，也不得凭空编造日期。"""
from __future__ import annotations

from datetime import date

from app.services import poster_semantics as ps
from app.services import template_renderer as renderer

TODAY = date(2026, 11, 22)  # 周日
SCHEMA = {
    "texts": [
        {"id": "title", "field": "main_title", "role": "main_title", "text": "2026期末复习指导课"},
        {"id": "teacher", "field": "teacher_name", "role": "teacher_name", "text": "林晓娟", "teacher_slot": 0},
        {"id": "school", "field": "teacher_org", "role": "teacher_org", "text": "福建师范大学", "teacher_slot": 0},
        {"id": "bio", "field": "teacher_bio_1", "role": "teacher_bio_1", "text": "省级骨干教师", "teacher_slot": 0, "bio_index": 0},
        {"id": "schedule", "field": "schedule", "role": "schedule", "text": "11月20日（周四）19:00", "course_slot": 0},
    ]
}


def test_partial_text_only_returns_mentioned_fields():
    values = ps.build_values(SCHEMA, "周日晚19:00", TODAY)
    assert set(values) == {"schedule"}
    assert values["schedule"] == "周日晚 19:00"
    # 绝不能返回空字符串去覆盖画布上已有的姓名 / 学校 / 简介
    assert all(value.strip() for value in values.values())


def test_weekday_only_never_invents_calendar_date():
    values = ps.build_values(SCHEMA, "周日晚上7点", TODAY)
    assert "11月" not in values["schedule"] and "月" not in values["schedule"]
    assert values["schedule"].startswith("周日")


def test_explicit_date_still_normalises_and_keeps_weekday():
    values = ps.build_values(SCHEMA, "11月25日 19:00 开课", TODAY)
    assert values["schedule"].startswith("11月25日")
    assert "周三" in values["schedule"]
    assert values["schedule"].endswith("19:00")


def test_legacy_llm_mapping_is_rejected_for_partial_edits(monkeypatch):
    monkeypatch.setattr(renderer.llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(
        renderer.llm_client,
        "chat_json",
        lambda system, user, **kwargs: {
            "values": {
                "schedule": "周日晚 19:00",
                "teacher": "",
                "school": None,
                "bio": "   ",
            }
        },
    )
    values, engine, warnings = renderer.parse_text_values(SCHEMA, "周日晚19:00", use_llm=True, today=TODAY)
    # Known schedule syntax still uses its deterministic parser, independent of legacy model output.
    assert engine == "heuristic"
    assert values == {"schedule": "周日晚 19:00"}
    assert warnings == ["大模型解析失败，已改用安全规则解析"]


TITLE_SCHEMA = {
    "texts": [
        {"id": "sub", "field": "sub_title", "role": "sub_title", "label": "副标题",
         "text": "2026漳州九年级"},
        {"id": "main", "field": "main_title", "role": "main_title", "label": "主标题",
         "text": "语文期末冲刺课"},
    ]
}


def test_title_patch_aligns_to_slot_matching_current_text():
    """“2026厦门九年级”与副标题同构，不能因为字段名带“主标题”就覆盖课程名。"""
    values = ps.build_values(TITLE_SCHEMA, "2026厦门九年级", TODAY)
    assert values == {"sub": "2026厦门九年级"}


def test_llm_prompt_carries_current_and_default_text_and_reroutes(monkeypatch):
    captured = {}

    def fake_model(system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        # 模拟模型提交有效的旧文案对比 Patch，但落错槽位。
        return {"intent": "partial_edit", "patches": [{
            "slot_key": "main", "old_text": "语文期末冲刺课",
            "new_text": "2026厦门九年级", "reason": "模型盲目按字段名写入",
        }]}

    monkeypatch.setattr(renderer.llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(renderer.llm_client, "chat_json", fake_model)

    values, engine, _ = renderer.parse_text_values(
        TITLE_SCHEMA,
        "2026厦门九年级",
        use_llm=True,
        today=TODAY,
        current_values={"sub": "2026漳州九年级", "main": "语文期末冲刺课"},
    )
    assert engine == "heuristic"
    # 结构化地点/年份/年级修改以确定性维度匹配为准，短 Patch 不能覆盖课程标题。
    assert values == {"sub": "2026厦门九年级"}

    assert "2026漳州九年级" in captured["user"]
    assert "语文期末冲刺课" in captured["user"]
    assert "current_text" in captured["user"] and "default_text" in captured["user"]
    assert "副标题" in captured["system"]
    assert "current_text" in captured["system"]
    assert "严禁只凭字段名" in captured["system"] or "不能只凭字段名" in captured["system"]


def test_fabricated_title_candidate_is_dropped():
    """模型编造、原文里没有的标题一律丢弃，回落到规则结果。"""
    values = ps.build_values(
        TITLE_SCHEMA, "2026厦门九年级", TODAY, candidates={"main": "985名校冲刺计划"}
    )
    assert values == {"sub": "2026厦门九年级"}
