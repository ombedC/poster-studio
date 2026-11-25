from datetime import date

import pytest

from app.services import poster_semantics, template_analyzer, template_renderer


TODAY = date(2026, 11, 18)


@pytest.mark.parametrize(
    ('notice', 'expected'),
    [
        ('本周六晚8点', '11月21日（周六）20:00'),
        ('明天晚上8点半', '11月19日（周四）20:30'),
        ('下周日晚八点半', '11月29日（周日）20:30'),
        ('2027年1月1日下午3点15分', '1月1日（周五）15:15'),
        ('11月31日晚上25点', None),
    ],
)
def test_schedule_combines_relative_date_and_spoken_clock(notice, expected):
    schema = {'texts': [{'id': 'time', 'field': 'schedule', 'text': '1月24日（周六）19:30'}]}
    values, engine, _ = template_renderer.parse_text_values(schema, notice, today=TODAY, use_llm=False)
    assert engine == 'heuristic'
    assert values.get('time') == expected


def test_split_schedule_rolls_over_year():
    schema = {'texts': [
        {'id': 'day', 'field': 'schedule', 'text': '1月24日', 'slot': 0},
        {'id': 'time', 'field': 'schedule', 'text': '周六晚 19:30', 'slot': 1},
    ]}
    values = poster_semantics.build_values(schema, '日期：明天\n时间：晚上8点半', date(2026, 12, 31))
    assert values == {'day': '1月1日', 'time': '周五晚 20:30'}


def test_cleaner_keeps_bio_teacher_association_and_removes_old_school():
    schema = {'texts': [
        {'id': 'topic1', 'field': 'course_title', 'y': 10},
        {'id': 'name1', 'field': 'teacher_name', 'text': '晋江市南湾中学 林文权', 'y': 20},
        {'id': 'first1', 'field': 'intro', 'text': '· 旧介绍一', 'y': 45},
        {'id': 'second1', 'field': 'intro', 'text': '▪ 旧介绍二', 'y': 65},
        {'id': 'third1', 'field': 'intro', 'text': '· 旧介绍三', 'y': 85},
        {'id': 'topic2', 'field': 'course_title', 'y': 150},
        {'id': 'name2', 'field': 'teacher_name', 'text': '泉州某中学 许老师', 'y': 165},
        {'id': 'org2', 'field': 'teacher_school', 'text': '泉州某中学', 'y': 167},
        {'id': 'first2', 'field': 'intro', 'text': '· 另一位旧介绍', 'y': 200},
    ]}
    notice = '课题：数学丨寻根\n1.曾芳芳\n介绍：台商一中高级教师\n· 数学教研组长\n课题：英语丨阅读\n2.陈小明\n介绍：教研组特聘讲师'
    values = poster_semantics.build_values(schema, notice, TODAY)
    # 增量补丁：没有内容的字段直接省略，不再用空串覆盖画布
    assert values == {
        'topic1': '数学丨寻根', 'name1': '台商一中 曾芳芳', 'first1': '· 台商一中高级教师',
        'second1': '▪ 数学教研组长', 'topic2': '英语丨阅读', 'name2': '陈小明',
        'first2': '· 教研组特聘讲师',
    }
    assert 'third1' not in values and 'org2' not in values
    assert poster_semantics.extract_teacher_name('台商一中 曾芳芳.png') == '曾芳芳'


def test_model_does_not_reintroduce_old_values_and_gets_date(monkeypatch):
    schema = {'texts': [
        {'id': 'name', 'field': 'teacher_name', 'text': '晋江市南湾中学 林文权'},
        {'id': 'bio', 'field': 'intro', 'text': '旧教师简介'},
        {'id': 'time', 'field': 'schedule', 'text': '1月24日（周六）19:30'},
    ]}

    def fake_model(_system, prompt, **_kwargs):
        assert '2026-11-18' in prompt
        return {'intent': 'full_notice', 'patches': [
            {'slot_key': 'time', 'old_text': '1月24日（周六）19:30', 'new_text': '假的时间20:59', 'reason': '错误日期'},
            {'slot_key': 'name', 'old_text': '晋江市南湾中学 林文权', 'new_text': '晋江市南湾中学 曾芳芳', 'reason': '老师明确更换'},
            {'slot_key': 'bio', 'old_text': '旧教师简介', 'new_text': '旧教师简介', 'reason': '无变化'},
        ]}

    monkeypatch.setattr(template_renderer.llm_client, 'llm_configured', lambda: True)
    monkeypatch.setattr(template_renderer.llm_client, 'chat_json', fake_model)
    values, engine, _ = template_renderer.parse_text_values(schema, '老师：曾芳芳\n明天晚上8点半', today=TODAY)
    assert engine == 'llm'
    # 模型回吐的旧简介不会被写回，也不产生空串补丁
    assert values == {'name': '曾芳芳', 'time': '11月19日（周四）20:30'}
    assert 'bio' not in values


def test_title_above_dashed_line_becomes_lesson_topic_pair():
    entries = [
        {'id': 'topic', 'field': 'course_title', 'text': '语文期末冲刺课', 'x': 74.5, 'y': 298.4, 'width': 900.0, 'font_size': 158.0},
        {'id': 'lesson', 'field': 'other', 'text': '成为真正的阅读者', 'x': 327.6, 'y': 1586.2, 'width': 593.3, 'font_size': 67.2},
        {'id': 'lesson_sub', 'field': 'other', 'text': '——用《昆虫记》打开一扇通向世界的窗', 'x': 99.7, 'y': 1678.2, 'width': 1104.4, 'font_size': 52.0},
    ]
    poster_semantics.annotate_roles(entries)
    by_id = {entry['id']: entry for entry in entries}
    assert (by_id['lesson']['field'], by_id['lesson']['role']) == ('lesson_topic', 'lesson_topic')
    assert (by_id['lesson_sub']['field'], by_id['lesson_sub']['role']) == ('lesson_topic_sub', 'lesson_topic_sub')
    # The poster's course heading is not claimed by the pair.
    assert by_id['topic']['field'] == 'course_title'


def test_lesson_topic_label_and_same_line_dash_stay_separate_fields():
    assert template_analyzer.FIELD_LABELS['lesson_topic'] == '课题'
    assert template_analyzer.FIELD_LABELS['lesson_topic_sub'] == '课题副标题'
    entries = [
        {'id': 'title', 'field': 'course_title', 'text': '数学丨寻“根”之旅', 'x': 180.9, 'y': 727.96, 'width': 471.5, 'font_size': 53.0},
        {'id': 'sub', 'field': 'course_subtitle', 'text': '——一元二次方程专题复习', 'x': 652.5, 'y': 727.96, 'width': 412.1, 'font_size': 35.0},
    ]
    poster_semantics.annotate_roles(entries)
    # Same row: the dashed run is the course subtitle, not a second lesson topic.
    assert [entry['field'] for entry in entries] == ['course_title', 'course_subtitle']


def test_analyzer_emits_split_roles_with_legacy_schedule_keys():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">' + (
        '<text id="title" x="20" y="40" font-size="35">\u6d77\u62a5\u4e3b\u9898</text>'
        '<text id="day" x="20" y="120" font-size="20">1\u670824\u65e5</text>'
        '<text id="time" x="200" y="120" font-size="20">\u5468\u516d\u665a19:30</text></svg>'
    ).encode()
    root = template_analyzer._parse_svg(svg)
    canvas = template_analyzer._canvas_size(root)
    leaves = template_analyzer._collect_text_leaves(root)
    template_analyzer._apply_measurements(leaves, [], {}, canvas)
    text_map, image_map = template_analyzer._heuristic_mapping(leaves, [], canvas)
    schema = template_analyzer._build_schema(
        template_id='000000000000', name='test', canvas=canvas, leaves=leaves, images=[],
        text_map=text_map, image_map=image_map, source='heuristic', summary='', warnings=[], has_preview=False,
    )
    assert schema['version'] == template_analyzer.SCHEMA_VERSION
    entries = {entry['id']: entry for entry in schema['texts']}
    assert entries['title']['base_font_size'] == 35
    assert entries['day']['base_font_size'] == 20
    assert [(entries[key]['field'], entries[key]['role'], entries[key]['key']) for key in ('day', 'time')] == [
        ('schedule', 'course_date', 'schedule_1'), ('schedule', 'course_time', 'schedule_2'),
    ]


def test_roles_are_additive_and_ordered_by_teacher():
    entries = [
        {'id': 'name1', 'field': 'teacher_name', 'y': 10},
        {'id': 'bio1', 'field': 'intro', 'key': 'intro_1', 'y': 30},
        {'id': 'name2', 'field': 'teacher_name', 'y': 200},
        {'id': 'bio2', 'field': 'intro', 'key': 'intro_2', 'y': 220},
    ]
    poster_semantics.annotate_roles(entries)
    assert (entries[1]['field'], entries[1]['key'], entries[1]['role'], entries[1]['teacher_slot']) == ('intro', 'intro_1', 'teacher_bio_1', 0)
    assert (entries[3]['field'], entries[3]['key'], entries[3]['role'], entries[3]['teacher_slot']) == ('intro', 'intro_2', 'teacher_bio_1', 1)
