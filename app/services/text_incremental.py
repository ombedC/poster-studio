"""Deterministic, same-dimension edits against the live canvas snapshot."""
from __future__ import annotations

import re
from typing import Any

PROVINCES = set((
    "北京 天津 上海 重庆 河北 山西 辽宁 吉林 黑龙江 江苏 浙江 安徽 福建 江西 山东 河南 "
    "湖北 湖南 广东 海南 四川 贵州 云南 陕西 甘肃 青海 台湾 内蒙古 广西 西藏 宁夏 新疆 香港 澳门"
).split())
# Common bare city names; any other city can be expressed as XXX市 or inferred from a structured heading.
CITIES = set((
    "厦门 漳州 福州 泉州 莆田 三明 南平 龙岩 宁德 晋江 石狮 龙海 长乐 福清 武夷山 福安 福鼎 "
    "广州 深圳 珠海 汕头 佛山 惠州 东莞 中山 江门 湛江 肇庆 潮州 揭阳 汕尾 河源 清远 韶关 茂名 梅州 阳江 云浮 "
    "杭州 宁波 温州 嘉兴 绍兴 金华 台州 湖州 舟山 衢州 丽水 南京 苏州 无锡 常州 扬州 镇江 南通 徐州 盐城 泰州 淮安 宿迁 连云港 "
    "成都 绵阳 德阳 济南 青岛 烟台 潍坊 哈尔滨 长春 沈阳 大连 南昌 景德镇 长沙 株洲 湘潭 岳阳 衡阳 "
    "武汉 宜昌 襄阳 西安 咸阳 郑州 洛阳 开封 合肥 芜湖 南宁 桂林 柳州 海口 三亚 贵阳 遵义 昆明 大理 "
    "兰州 银川 西宁 拉萨 呼和浩特 包头 乌鲁木齐 石家庄 唐山 秦皇岛 太原 大同 台北 高雄 厦门市 漳州市 福州市 泉州市 深圳市 广州市 杭州市 南京市"
).split())
GRADE = r"[一二三四五六七八九1-9]年级|初[一二三]|高[一二三]|小升初|小学|初中|高中|中考|高考"
SUBJECT = r"道德与法治|信息技术|语文|数学|英语|物理|化学|生物|政治|历史|地理|科学|美术|音乐|体育"
YEAR = r"(?<!\d)(?:19|20)\d{2}(?:年|届)?(?!\d)"
STRUCTURED = re.compile(rf"(?P<year>{YEAR})\s*(?P<region>[\u3400-\u9fff]{{2,9}}?)\s*(?P<grade>{GRADE})")
DIRECTIVE = re.compile(r"(?:把|将)?\s*(?P<target>[^，。；,;\n]{0,24}?)\s*(?:改成|改为|换成|替换成|替换为|更新为|变成|调整成)\s*(?P<value>[^\n。；;]{1,60})")
SUFFIX = r"(特别行政区|自治区|自治州|地区|省|市)$"
PLACE = r"[\u3400-\u9fff]{2,8}(?:自治州|地区|市)"
ACTIVITY = {"main_title", "sub_title", "topic_title", "topic_sub", "lesson_topic", "lesson_topic_sub", "activity_tag", "slogan", "subtitle", "course_title"}
TEACHER = re.compile(r"teacher|\bbio|honor|school|profile|^intro$|老师|荣誉|履历|职称|学校|师资|姓名", re.I)


def directive(text: str) -> tuple[str, str]:
    match = DIRECTIVE.fullmatch(text)
    return (match['value'].strip(), match['target'].strip()) if match else (text, '')


def region_base(word: str) -> str:
    return re.sub(SUFFIX, '', word)


def _with_suffix(old: str, new: str) -> str:
    ending = old[len(region_base(old)):]
    if not ending:
        return region_base(new)
    supplied = new[len(region_base(new)):]
    if supplied:
        return new
    base = region_base(new)
    if base in {'北京', '天津', '上海', '重庆'}:
        return base + '市'
    if base in {'内蒙古', '广西', '西藏', '宁夏', '新疆'}:
        return base + '自治区'
    if base in {'香港', '澳门'}:
        return base + '特别行政区'
    return base + ('省' if base in PROVINCES else ending)


def _place(text: str) -> bool:
    base = region_base(text)
    return base in PROVINCES | CITIES or any(city.startswith(base) or base.startswith(city) for city in CITIES) or bool(re.fullmatch(PLACE, text))


def input_dimensions(text: str) -> dict[str, str]:
    if match := STRUCTURED.fullmatch(text):
        # A nearby year and grade alone do not prove the intervening text is a place.
        if _place(match['region']):
            return match.groupdict()
    for dim, pattern in [('year', YEAR), ('grade', GRADE), ('subject', SUBJECT)]:
        if re.fullmatch(pattern, text):
            return {dim: text}
    return {'region': text} if _place(text) else {}


def is_literal_patch(old: str, new: str, token: str) -> bool:
    if not old or not token or token == new or old == new:
        return False
    for match in re.finditer(re.escape(token), new):
        start, end = match.span()
        before, after = new[:start], new[end:]
        removed = len(old) - len(before) - len(after)
        if before or after:
            if 0 < removed <= max(6, len(token)) and old.startswith(before) and old.endswith(after):
                return True
    return False


def teacher_field(entry: dict[str, Any]) -> bool:
    role = entry.get('role')
    if role in {'slogan', 'activity_tag'}:
        return False
    return (entry.get('teacher_slot') is not None or entry.get('bio_index') is not None
            or any(TEACHER.search(str(entry.get(key) or '')) for key in ('field', 'role', 'label')))


def _activity(entry: dict[str, Any]) -> bool:
    if entry.get('role') == 'slogan':
        return True
    return not teacher_field(entry) and bool(ACTIVITY.intersection((entry.get('role'), entry.get('field'))))


def dimension_patches(entries: list[dict], baseline: dict[str, str], dimensions: dict[str, str]) -> dict[str, str]:
    eligible = {e['id']: baseline[e['id']] for e in entries if _activity(e)}
    result = dict(eligible)
    for dim, new in dimensions.items():
        if dim == 'region':
            names = PROVINCES | CITIES | {region_base(m['region']) for value in eligible.values()
                                         for m in STRUCTURED.finditer(value)}
            alternatives = '|'.join(sorted(map(re.escape, names), key=lambda name: (-len(name), name)))
            pattern = re.compile(rf'(?P<base>{alternatives})(?:特别行政区|自治区|自治州|地区|省|市)?')
            province = region_base(new) in PROVINCES
            same_level = {m['base'] for value in eligible.values() for m in pattern.finditer(value)
                          if (region_base(m['base']) in PROVINCES) == province
                          or (re.search(r'名优骨干教师点拨|点拨学科重难点', value)
                              and (region_base(new) in PROVINCES or not province))}
            if province and any(old != region_base(new) for old in same_level):
                # Retargeting a poster to a different province also updates its old event city.
                same_level.update(region_base(m['region']) for value in eligible.values() for m in STRUCTURED.finditer(value))
            elif not same_level and province:
                same_level = {region_base(m['region']) for value in eligible.values() for m in STRUCTURED.finditer(value)}
            for key, value in result.items():
                result[key] = pattern.sub(lambda m: _with_suffix(m[0], new) if m['base'] in same_level else m[0], value)
        else:
            pattern = {'year': YEAR, 'grade': GRADE, 'subject': SUBJECT}[dim]
            for key, value in result.items():
                if dim == 'grade' and re.search(r'(?:中考|高考)', value):
                    continue
                result[key] = re.sub(pattern, lambda m: (re.sub(r'[年届]$', '', new) + m[0][4:])
                                     if dim == 'year' else new, value)
    return {key: value for key, value in result.items() if value != baseline[key]}
