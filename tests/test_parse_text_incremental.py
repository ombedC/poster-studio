"""End-to-end parsing regressions against the reported poster's saved slot snapshot."""
import json
from datetime import date
from pathlib import Path

import pytest

from app.services import template_renderer as renderer

TODAY = date(2026, 11, 24)
TAG = 'revival-text-002'
TITLE = 'revival-text-012'
COURSE = 'revived-text-001'
SLOGAN = 'revival-text-001'
SCHOOL = 'revival-text-009'
HONOR = 'tspan-001'
CITY_PATCH = {TITLE: '2026厦门九年级', SLOGAN: '厦门市名优骨干教师点拨学科重难点'}
NOTICE = '主标题：2027厦门初二\n课程：数学复习课\n老师：陈小明\n学校：厦门一中\n时间：12月12日19:30'


@pytest.fixture
def snapshot():
    path = Path(__file__).parent / 'fixtures' / 'fujian_poster_slots.json'
    schema = json.loads(path.read_bytes())
    return schema, {slot['id']: slot['text'] for slot in schema['texts']}


@pytest.mark.parametrize(('text', 'expected'), [
    ('厦门', CITY_PATCH),
    ('广东', {TAG: '广东中考公益课', TITLE: '2026广东九年级', SLOGAN: '广东省名优骨干教师点拨学科重难点'}),
    ('2026厦门九年级', CITY_PATCH),
    ('初二', {TITLE: '2026漳州初二'}),
    ('物理', {COURSE: '物理期末冲刺课'}),
    ('2027', {TITLE: '2027漳州九年级'}),
    ('把地区改为厦门', CITY_PATCH),
    (NOTICE, {TITLE: '2027厦门初二', COURSE: '数学复习课', 'revival-text-005': '12月12日',
              'revival-text-006': '周六晚 19:30', 'revival-text-011': '周六晚 19:30', 'revival-text-004': '陈小明', SCHOOL: '厦门一中'}),
])
def test_actual_slot_snapshot_exact_changes(snapshot, text, expected):
    schema, current = snapshot
    result = renderer.parse_text_patch(schema, text, today=TODAY, current_values=current, use_llm=False)
    assert result['intent'] == ('full_notice' if text == NOTICE else 'partial_edit')
    assert result['values'] == expected
    assert {item['slot_key']: item['new_text'] for item in result['patches']} == expected
    assert all(item['old_text'] == current[item['slot_key']] and item['reason'] for item in result['patches'])
    if text != NOTICE:
        assert not set(result['values']) & {SCHOOL, HONOR, 'revival-text-003', 'tspan-002'}


def test_api_live_text_and_malicious_patch(snapshot, monkeypatch):
    schema, current = snapshot
    current[TITLE] = '2027泉州八年级'
    current[SLOGAN] = '泉州市名优骨干教师点拨学科重难点'
    current[COURSE] = ''
    current[SCHOOL] = '  ' + '测试' * 100 + '  '
    def model(system, user, **kwargs):
        slots = {slot['slot_key']: slot for slot in json.loads(next(line for line in user.splitlines() if line.startswith('[{')))}
        assert {k: v['current_text'] for k, v in slots.items()} == current
        assert [slots[key]['role'] for key in (TAG, TITLE, COURSE, SLOGAN, HONOR)] == [
            'activity_tag', 'main_title', 'topic_title', 'slogan', 'teacher_bio_2']
        assert slots[SLOGAN]['label'] == '活动宣传语'
        assert all(fragment in system for fragment in ('广东', '初二', '厦门', 'full_notice', 'old_text', '荣誉'))
        return {'intent': 'full_notice', 'patches': [
            {'slot_key': COURSE, 'old_text': '', 'new_text': '厦门', 'reason': '错填'},
            {'slot_key': SCHOOL, 'old_text': current[SCHOOL], 'new_text': '厦门市某学校', 'reason': '不实'},
            {'slot_key': HONOR, 'old_text': current[HONOR], 'new_text': '· 厦门市学科带头人', 'reason': '不实'},
            {'slot_key': TITLE, 'old_text': current[TITLE], 'new_text': '2027厦门八年级', 'reason': '只改活动城市'},
            {'slot_key': SLOGAN, 'old_text': '旧文本已过期', 'new_text': '厦门市名优骨干教师点拨学科重难点', 'reason': '过期'},
        ]}
    monkeypatch.setattr(renderer.llm_client, 'llm_configured', lambda: True)
    monkeypatch.setattr(renderer.llm_client, 'chat_json', model)
    result = renderer.parse_text_patch(schema, '厦门', today=TODAY, current_values=current)
    assert result['intent'] == 'partial_edit'
    assert result['values'] == {TITLE: '2027厦门八年级', SLOGAN: CITY_PATCH[SLOGAN]}
    assert result['engine'] == 'llm'
    assert any(item['new_text'] == '2027厦门八年级' for item in result['patches'])
    assert any(item['new_text'] == CITY_PATCH[SLOGAN] for item in result['patches'])
    assert all(patch['reason'] != '过期' for patch in result['patches'])


def test_unlisted_short_token_must_not_guess_or_replace_whole_slot(snapshot, monkeypatch):
    schema, current = snapshot
    monkeypatch.setattr(renderer.llm_client, 'llm_configured', lambda: True)
    monkeypatch.setattr(renderer.llm_client, 'chat_json', lambda *a, **kw: {'intent':'full_notice', 'patches':[
        {'slot_key': COURSE, 'old_text': current[COURSE], 'new_text': '未知', 'reason':'错填'}]})
    assert renderer.parse_text_patch(schema, '未知', today=TODAY, current_values=current)['values'] == {}
