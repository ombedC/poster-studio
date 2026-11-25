"""Semantic roles and deterministic cleanup shared by poster analysis and filling."""

from __future__ import annotations

import re
import difflib
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.services import text_incremental

_CN = r"[\u3400-\u9fff]"
_WEEKDAYS = "一二三四五六日"
_CHINESE_ZONE = timezone(timedelta(hours=8))
_RELATIVE = re.compile(r"(今天|今日|明天|明日|后天|后日)|(?:(本|这|下)\s*)?(?:周|星期|礼拜)\s*([一二三四五六日天])")
_DATE = re.compile(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_CLOCK = re.compile(r"(?<!\d)(凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|早|晚)?\s*(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*(?:[:：]\s*(\d{2})(?!\d)|[点時时]\s*(半|[一三]刻|\d{1,2}\s*分?)?)")
_TOPIC = re.compile(r"[丨|｜]")
_QR = re.compile(r"扫码|二维码|朋友圈|好友|领取|回放")
_NUMBER = re.compile(r"^\s*(?:[（(][0-9０-９一二三四五六七八九十]+[）)]\s*|[0-9０-９一二三四五六七八九十]+[.．、)）](?!\d)\s*)")
_BULLET = re.compile(r"^\s*([·•●▪■□◆◇﹒・‧]\s*)")
_SCHOOL = r"(?:附属(?:中学|小学|学校)|初级中学|高级中学|中学|小学|学校|学院|大学|教育集团|教育机构|研究院|研究所|教研室|培训中心|教育中心|[一二三四五六七八九十百0-9]+[中小])"
_SCHOOL_PREFIX = re.compile(rf"^({_CN}[\u3400-\u9fff\d·（）()]{{0,29}}?{_SCHOOL})")
_SCHOOL_END = re.compile(rf"{_SCHOOL}$")
_TEACHER_WORD = re.compile(r"老师|讲师|教师|主讲|姓名")
_NOT_NAME = re.compile(r"老师|讲师|教师|学校|中学|小学|名师|高级|骨干|优秀|课程|公益|复习|介绍|讲座|专题|标题|教学|指导|学科|博士|硕士|校长|教授|教育|委员|时间|校区|周[一二三四五六日天]|报名|[一二三四五六七八九十][中小]")
_SCHOOL_ROLE_SUFFIX = re.compile(r"^(?:副?校长|正?高级|特级|教师|主任|教授|硕士|博士|骨干|名师|教研|班主任|校区)")
_LABELS = {
    "main_title": r"海报主标题|海报标题|主标题|标题",
    "sub_title": r"海报副标题|副标题",
    "slogan": r"宣传语|活动宣传语",
    "activity_tag": r"活动标签|顶部标签",
    "topic_title": r"课程主题|课程标题|课程名称|主题|课程",
    "topic_sub": r"课程副标题|主题副标题",
    "lesson_topic": r"课题|课程课题|本课课题|授课课题",
    "lesson_topic_sub": r"课题副标题|课题副题",
    "teacher_name": r"教师姓名|老师姓名|讲师姓名|授课教师|授课老师|主讲老师|主讲教师|主讲人|老师|教师|讲师|主讲|姓名|师资",
    "teacher_org": r"所属学校|所在学校|所属机构|工作单位|学校|机构|单位|院校",
    "bio": r"主讲介绍|主讲简介|讲师介绍|讲师简介|老师简介|教师简介|老师介绍|教师介绍|个人简介|简介|介绍|履历",
    "schedule": r"直播日期|直播时间|上课时间|开课时间|授课时间|课程时间|上课日期|开课日期|课程日期|安排|时间|日期",
    "org": r"主办方|承办方|主办单位|承办单位|主办|承办",
}
_LABEL = re.compile(r"^(?:【(?P<bracket>" + "|".join(_LABELS.values()) + r")】\s*[：:]?\s*|(?P<bare>" + "|".join(_LABELS.values()) + r")\s*[：:]\s*)")
_LABEL_ROLES = {label: role for role, labels in _LABELS.items() for label in labels.split("|")}
_LEGACY = {
    "main_title": "main_title", "subtitle": "sub_title", "course_title": "topic_title",
    "activity_tag": "activity_tag", "slogan": "slogan",
    "course_subtitle": "topic_sub", "teacher_name": "teacher_name", "teacher_school": "teacher_org",
    "intro": "teacher_bio", "teacher_intro": "teacher_bio", "schedule": "schedule",
    "org": "org", "qr_tip": "qr_tip", "footer": "footer", "other": "other",
}
_ROLES = set(_LEGACY.values()) | {"course_date", "course_time", "teacher_bio_1", "teacher_bio_2", "teacher_bio_3", "lesson_topic", "lesson_topic_sub"}
SEMANTIC_ROLES = tuple(sorted(_ROLES))


def today_in_china() -> date:
    return datetime.now(_CHINESE_ZONE).date()


def _strip_prefix(value: str) -> str:
    value = value.strip()
    while value:
        cleaned = _BULLET.sub("", _NUMBER.sub("", value)).strip()
        if cleaned == value:
            break
        value = cleaned
    return value


def _labeled(value: str) -> tuple[str, str]:
    value = _strip_prefix(value)
    match = _LABEL.match(value)
    if match:
        return _LABEL_ROLES[match["bracket"] or match["bare"]], value[match.end():].strip()
    return "", value


def _school(value: str) -> str:
    match = _SCHOOL_PREFIX.match(_strip_prefix(value))
    return match[1] if match else ""


def extract_teacher_name(value: str) -> str:
    """Recognize a bare Chinese name, or its position after a school/space separator."""
    value = _labeled(value or "")[1]
    value = re.sub(r"\.(?:png|jpe?g|webp|bmp|gif|avif|heic)$", "", value, flags=re.I)
    value = re.sub(r"(?:[ _-]*(?:教师头像|老师头像|头像|照片))+$", "", value)
    value = re.sub(r"(?:老师|讲师|教师|主讲)$", "", value).strip()
    value = re.sub(r"^(?:老师|讲师|教师|主讲)\s*", "", value).strip()
    institution = _school(value)
    if institution and not _SCHOOL_ROLE_SUFFIX.match(value[len(institution):]):
        value = value[len(institution):].strip()
    else:
        value = re.split(r"[\s·丨|｜/]+", value)[-1]
    return value if re.fullmatch(rf"{_CN}{{2,4}}", value) and not _NOT_NAME.search(value) else ""


def _hour(raw: str) -> int:
    if raw.isdecimal():
        return int(raw)
    digits = dict(zip("零一二三四五六七八九", range(10))) | {"〇": 0, "两": 2}
    left, sep, right = raw.partition("十")
    if sep:
        return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return digits.get(raw, -1)


def _clock(value: str) -> tuple[int, int] | None:
    match = _CLOCK.search(value)
    if not match:
        return None
    period, raw_hour, numeric_min, spoken_min = match.groups()
    hour = _hour(raw_hour)
    if period in {"下午", "傍晚", "晚上", "晚", "中午"} and 1 <= hour < 12:
        hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0
    minute_text = (spoken_min or "").rstrip("分 ")
    minute = int(numeric_min) if numeric_min else {"半": 30, "一刻": 15, "三刻": 45}.get(minute_text, int(minute_text) if minute_text.isdecimal() else 0)
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _date(value: str, today: date) -> date | None:
    explicit = _DATE.search(value) or _ISO_DATE.search(value)
    if explicit:
        year, month, day = explicit.groups()
        try:
            return date(int(year or today.year), int(month), int(day))
        except ValueError:
            return None
    relative = _RELATIVE.search(value)
    if not relative:
        return None
    dayword, week, weekday = relative.groups()
    if dayword:
        return today + timedelta(days={"明天": 1, "明日": 1, "后天": 2, "后日": 2}.get(dayword, 0))
    day_index = _WEEKDAYS.index("日" if weekday == "天" else weekday)
    return today + timedelta(days=day_index - today.weekday() + (7 if week == "下" else 0))


def is_schedule(value: str) -> bool:
    return bool(_DATE.search(value) or _ISO_DATE.search(value) or _RELATIVE.search(value) or _CLOCK.search(value))


def schedule_role(value: str) -> str:
    if (_DATE.search(value) or _ISO_DATE.search(value)) and not _CLOCK.search(value):
        return "course_date"
    if _CLOCK.search(value) and not (_DATE.search(value) or _ISO_DATE.search(value)):
        relative = _RELATIVE.search(value)
        if not relative or not (relative[1] or relative[2]):
            return "course_time"
    return "schedule"


def template_role(value: str) -> str | None:
    label, body = _labeled(value)
    if label == "schedule" or (is_schedule(body) and not _QR.search(body)):
        return schedule_role(body)
    if label:
        return "teacher_bio" if label == "bio" else label
    if _QR.search(body):
        return "qr_tip"
    if _TOPIC.search(body):
        return "topic_title"
    if body.startswith("—"):
        return "topic_sub"
    if _BULLET.match(value):
        return "teacher_bio"
    if _SCHOOL_END.search(body):
        return "teacher_org"
    if _school(body) and extract_teacher_name(body):
        return "teacher_name"
    return None


def _slot(entry: dict[str, Any], key: str = "slot") -> int:
    try:
        return max(0, int(entry.get(key, 0)))
    except (TypeError, ValueError):
        return 0


def _ordered(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda item: (float(item.get("y") or 0), float(item.get("x") or 0)))


def _pair_lesson_topics(entries: list[dict[str, Any]]) -> None:
    """A title line directly above a "——…" line is a lesson-topic pair.

    Course posters put the lesson topic as a big line with a dashed sub-line
    beneath it. Bigger poster headings are left alone, so the pair is only
    claimed when the line above is still unclassified.
    """
    lines = [entry for entry in entries if (entry.get("text") or "").strip()]
    for entry in lines:
        text = entry["text"].strip()
        if not text.startswith(("——", "—", "–")):
            continue
        if entry["role"] not in {"topic_sub", "other", "lesson_topic_sub"}:
            continue
        y = float(entry.get("y") or 0)
        font = float(entry.get("font_size") or 16)
        cx = float(entry.get("x") or 0) + float(entry.get("width") or 0) / 2
        above = []
        for other in lines:
            if other is entry or other["role"] in {
                "main_title", "sub_title", "teacher_name", "teacher_org", "org",
                "schedule", "course_date", "course_time", "qr_tip",
            } or other["role"].startswith("teacher_bio"):
                continue
            gap = y - float(other.get("y") or 0)
            if not 0 < gap <= max(font * 2.2, font + 18):
                continue
            ocx = float(other.get("x") or 0) + float(other.get("width") or 0) / 2
            if abs(ocx - cx) > max(font * 2.5, 40):
                continue
            above.append(other)
        if not above:
            continue
        title = max(above, key=lambda item: float(item.get("font_size") or 0))
        if float(title.get("font_size") or 0) <= font * 1.05:
            continue
        title["role"] = title["field"] = "lesson_topic"
        entry["role"] = entry["field"] = "lesson_topic_sub"


def repair_activity_role(entry: dict[str, Any]) -> None:
    text, role = (entry.get('text') or '').strip(), entry.get('role')
    if re.fullmatch(r'.{0,16}名优骨干教师点拨.{2,30}', text) and not _BULLET.match(text):
        entry['role'] = entry['field'] = 'slogan'
        entry['label'] = '活动宣传语'
        for key in ('teacher_slot', 'bio_index', 'font_fit_group'):
            entry.pop(key, None)
    elif role in {'main_title', 'sub_title', 'topic_title', 'other'}:
        dims = text_incremental.input_dimensions(text)
        if set(dims) == {'year', 'region', 'grade'}:
            entry['role'] = entry['field'] = 'main_title'
        elif re.fullmatch(rf'(?:{text_incremental.SUBJECT}).{{1,14}}(?:课|课程)', text):
            entry['role'], entry['field'] = 'topic_title', 'course_title'
        elif re.fullmatch(r'.{0,12}(?:中考|高考|小升初)?公益课', text):
            entry['role'] = entry['field'] = 'activity_tag'
            entry['label'] = '活动标签'


def annotate_roles(entries: list[dict[str, Any]], overrides: dict[str, dict[str, Any]] | None = None, *, repair_activity: bool = False) -> None:
    """Add role, teacher_slot, course_slot and bio_index without touching legacy keys."""
    overrides = overrides or {}
    ordered = _ordered(entries)
    for entry in ordered:
        text = entry.get("text", "")
        field = entry.get("field", "other")
        role = entry.get("role") or (overrides.get(entry["id"]) or {}).get("role")
        if role not in _ROLES:
            hinted = template_role(text)
            legacy = _LEGACY.get(field, field if field in _ROLES else "other")
            role = (hinted or legacy) if legacy == "other" or (legacy == "schedule" and hinted in {"schedule", "course_date", "course_time"}) else legacy
        entry["role"] = role
        if repair_activity:
            repair_activity_role(entry)

    _pair_lesson_topics(ordered)

    teachers = [entry for entry in ordered if entry["role"] == "teacher_name"]
    topics = [entry for entry in ordered if entry["role"] == "topic_title"]
    for index, entry in enumerate(teachers):
        hint = overrides.get(entry["id"], {})
        if repair_activity:
            entry["teacher_slot"] = _slot(hint, "teacher_slot") if "teacher_slot" in hint else index
        else:
            entry.setdefault("teacher_slot", _slot(hint, "teacher_slot") if "teacher_slot" in hint else index)
    for index, entry in enumerate(topics):
        hint = overrides.get(entry["id"], {})
        entry.setdefault("course_slot", _slot(hint, "course_slot") if "course_slot" in hint else index)

    def nearby(entry, anchors, key, preceding=False):
        override = overrides.get(entry["id"]) or {}
        if key in override:
            return _slot(override, key)
        if not anchors:
            return 0 if key == "teacher_slot" and entry["role"].startswith("teacher_bio") else _slot(entry)
        y = float(entry.get("y") or 0)
        x = float(entry.get("x") or 0)
        if not y and not x and all(not item.get("x") and not item.get("y") for item in anchors):
            return min(_slot(entry), len(anchors) - 1)
        font = float(entry.get("font_size") or 16)
        before = [item for item in anchors if float(item.get("y") or 0) <= y + font] if preceding else []
        match = min(before or anchors, key=lambda item: (
            abs(float(item.get("y") or 0) - y) + 0.15 * abs(float(item.get("x") or 0) - x)
        ))
        return _slot(match, key)

    def course_anchors(entry):
        if not topics:
            return teachers, "teacher_slot"
        return topics, "course_slot"

    intro_index: dict[int, int] = {}
    schedule_counts: dict[str, int] = {}
    for entry in ordered:
        role = entry["role"]
        if role == "teacher_org" or role.startswith("teacher_bio"):
            if repair_activity and role.startswith("teacher_bio") and (entry.get('field') == 'slogan' or entry.get('role') == 'slogan'):
                continue
            if repair_activity and role.startswith("teacher_bio"):
                for key in ("teacher_slot", "bio_index", "font_fit_group"):
                    entry.pop(key, None)
                if not re.match(r"^(?:高级|特级|正高级|骨干|优秀|学科带头人|教师|教授|博士|硕士)", (entry.get("text") or "").lstrip("·▪• ")) and re.search(r"名优骨干教师点拨|点拨学科重难点", entry.get("text") or ""):
                    entry["field"] = "slogan"
                    entry["role"] = "slogan"
                    entry["label"] = "活动宣传语"
                    continue
            entry.setdefault("teacher_slot", nearby(entry, teachers, "teacher_slot", preceding=role.startswith("teacher_bio")))
        if role.startswith("teacher_bio"): 
            slot = _slot(entry, "teacher_slot")
            index = entry.get("bio_index", intro_index.get(slot, 0))
            entry["bio_index"] = index
            entry["role"] = f"teacher_bio_{index + 1}" if index < 3 else "teacher_bio"
            intro_index[slot] = max(int(index) + 1, intro_index.get(slot, 0))
        if role in {"topic_sub", "schedule", "course_date", "course_time"} and "course_slot" not in entry:
            anchors, key = course_anchors(entry)
            if role == "topic_sub" or topics or key == "teacher_slot" and len(teachers) > 1:
                # Model course slots are independent of the deprecated generic slot.
                override = overrides.get(entry["id"]) or {}
                entry["course_slot"] = _slot(override, "course_slot") if "course_slot" in override else nearby(entry, anchors, key, preceding=bool(topics))
            else:
                entry["course_slot"] = schedule_counts.get(role, 0)
            schedule_counts[role] = schedule_counts.get(role, 0) + 1


def _schedule_value(
    role: str,
    day: date | None,
    clock: tuple[int, int] | None,
    explicit_date: bool = True,
) -> str:
    """拼装上课时间文案。

    ``explicit_date`` 为 False 表示原文只写了星期（如“周日晚19:00”）而没有具体月日，
    此时只保留星期与时刻，绝不把“周日”换算成某个具体日期覆盖画布上已有的日期。
    """
    day_text = f"{day.month}月{day.day}日" if day and explicit_date else ""
    weekday = f"周{_WEEKDAYS[day.weekday()]}" if day else ""
    time = f"{clock[0]:02d}:{clock[1]:02d}" if clock else ""
    if role == "course_date":
        return day_text
    hour = clock[0] if clock else 0
    period = "凌晨" if hour < 6 else "早" if hour < 12 else "中午" if hour < 14 else "下午" if hour < 18 else "晚"
    if role == "course_time":
        if not time or not weekday:
            return time
        return f"{weekday}{period} {time}"
    if not explicit_date:
        if weekday and time:
            return f"{weekday}{period} {time}"
        return time
    return "".join((day_text, f"（{weekday}）" if weekday else "", time))


def _teacher() -> dict[str, Any]:
    return {"name": "", "org": "", "bio": []}


def _read_notice(text: str, today: date, *, teacher_count: int, separate_lesson: bool = False) -> tuple[dict, list[dict], list[dict], list[tuple], list[str]]:
    header: dict[str, str] = {}
    teachers: list[dict] = []
    topics: list[dict] = []
    schedules: list[tuple] = []
    qr: list[str] = []
    teacher: dict | None = None
    bio_mode = False
    last_day = None
    last_day_explicit = True
    last_schedule_index = None

    def new_teacher() -> dict:
        item = _teacher()
        teachers.append(item)
        return item

    for raw in text.splitlines():
        label, value = _labeled(raw)
        if not value and label != "bio":
            continue
        if label in {"main_title", "sub_title", "org", "activity_tag", "slogan"} or (separate_lesson and label in {"lesson_topic", "lesson_topic_sub"}):
            header[label] = value
            continue
        if label in {"topic_title", "topic_sub", "lesson_topic", "lesson_topic_sub"} or not label and (_TOPIC.search(value) or value.startswith("—")):
            if label in {"topic_sub", "lesson_topic_sub"} or value.startswith("—"):
                if topics:
                    topics[-1]["topic_sub"] = value
                continue
            title, sep, sub = value.partition("——")
            topics.append({"topic_title": title.strip(), "topic_sub": (sep + sub.strip()) if sep else ""})
            if teacher and (teacher["name"] or teacher["bio"]):
                teacher = None
            bio_mode = False
            continue
        if not label and _QR.search(value):
            qr.append(value)
            continue
        day = _date(value, today)
        clock = _clock(value)
        if label == "schedule" or (not label and (day or clock)):
            relative = _RELATIVE.search(value)
            # 明确写了“本周/下周/明天”等日期的才允许换算成具体月日；
            # 只写“周日”时不能凭空编出某个日期覆盖画布。
            explicit = bool(_DATE.search(value) or _ISO_DATE.search(value)
                            or (relative and (relative.group(1) or relative.group(2))))
            if day:
                last_day = day
                last_day_explicit = explicit
            if not (day or clock):
                continue
            if clock and not day and last_schedule_index is not None and schedules[last_schedule_index][1] is None:
                previous = schedules[last_schedule_index]
                schedules[last_schedule_index] = (previous[0], clock, previous[2] if len(previous) > 2 else True)
            else:
                schedules.append((day or last_day, clock, explicit or (not day and last_day_explicit)))
                last_schedule_index = len(schedules) - 1
            bio_mode = False
            continue
        if label == "teacher_org":
            if teacher is None:
                teacher = new_teacher()
            teacher["org"] = value
            continue
        if label == "bio":
            if teacher is None:
                teacher = new_teacher()
            if value:
                teacher["bio"].append(value)
            bio_mode = True
            continue
        name = extract_teacher_name(value)
        numbered = bool(_NUMBER.match(raw.strip()))
        compound = bool(_school(value) and name and value != name)
        likely_name = name and (
            label == "teacher_name" or _TEACHER_WORD.search(value) or compound
            or teacher_count and (numbered or (topics and teacher is None))
        )
        if likely_name:
            if teacher is None or teacher["name"]:
                if teacher and teacher["name"] == name:
                    continue
                teacher = new_teacher()
            teacher["name"] = name
            if compound and not teacher["org"]:
                teacher["org"] = _school(value)
            bio_mode = True
            continue
        if label == "teacher_name":
            continue
        if teacher and (bio_mode or _BULLET.match(raw) or _school(value)):
            teacher["bio"].append(value)
        elif not topics and not teachers and "main_title" not in header:
            header["main_title"] = value
        elif not topics and not teachers and "sub_title" not in header:
            header["sub_title"] = value
    return header, teachers, topics, schedules, qr


def _without_prefix(value: str, role: str) -> str:
    label, cleaned = _labeled(value)
    # Also accept a bare role label followed by whitespace, not a word inside prose.
    if not label:
        key = "bio" if role.startswith("teacher_bio") else role
        labels = _LABELS.get(key)
        if labels:
            cleaned = re.sub(rf"^(?:{labels})(?:\s+[：:]?\s*|[：:]\s*)", "", cleaned)
    return cleaned


TITLE_ROLES = {
    "main_title", "sub_title", "topic_title", "topic_sub",
    "lesson_topic", "lesson_topic_sub", "course_title",
}


def _normalize_text(value: str) -> str:
    return re.sub(r"[\s\u3000·•—\-_,，。.:：!！?？'\"“”‘’()（）\[\]【】]", "", str(value or ""))


def _similarity(left: str, right: str) -> float:
    a, b = _normalize_text(left), _normalize_text(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def align_title_values(
    entries: list[dict[str, Any]],
    values: dict[str, str],
    current_values: dict[str, str] | None = None,
) -> dict[str, str]:
    """按“与坑位当前文案的相似度”纠正标题类字段的落点。

    局部改标题时，模型（或规则）常按字段名把内容写进“主标题”，而真正要改的
    可能是“副标题”。这里以各坑位当前文案为锚点做一次对齐：候选文案与哪个坑位
    的现有文字最像，就写回哪个坑位；目标坑位已有候选时保持原样，避免互相覆盖。
    """
    current_values = current_values or {}
    slots = [entry for entry in entries if entry.get("role") in TITLE_ROLES]
    if len(slots) < 2:  # noqa: PLR2004 - 只有一个标题坑位时无需对齐
        return values
    anchors = {
        entry["id"]: current_values.get(entry["id"]) or entry.get("text", "")
        for entry in slots
    }

    for entry in list(slots):
        node_id = entry["id"]
        candidate = values.get(node_id)
        if not candidate:
            continue
        scores = {slot["id"]: _similarity(candidate, anchors[slot["id"]]) for slot in slots}
        best = max(scores, key=scores.get)
        best_score = scores[best]
        own_score = scores.get(node_id, 0.0)
        if best == node_id or best_score < 0.5 or best_score - own_score < 0.15:
            continue
        if values.get(best):
            continue
        values[best] = candidate
        del values[node_id]
    return values


def build_values(schema: dict[str, Any], text: str, today: date, candidates: dict[str, str] | None = None,
                 current_values: dict[str, str] | None = None, *, align_titles: bool = True) -> dict[str, str]:
    entries = [dict(entry) for entry in schema.get("texts", [])]
    annotate_roles(entries)
    separate_lesson = any(e["role"] == "topic_title" for e in entries)
    teachers_in_schema = [entry for entry in entries if entry["role"] == "teacher_name"]
    header, teachers, topics, schedules, qr = _read_notice(
        text, today, teacher_count=len(teachers_in_schema), separate_lesson=separate_lesson and not align_titles
    )
    candidates = candidates or {}
    result: dict[str, str] = {}
    from_notice = re.sub(r"\s+", "", text)
    qr_index = 0

    # Teacher records are assembled once; an explicit miss must not inherit specimen text.
    resolved_teachers: dict[int, dict] = {}
    for entry in _ordered(entries):
        role = entry["role"]
        if role == 'slogan':
            continue
        if role not in {"teacher_name", "teacher_org"} and not role.startswith("teacher_bio"):
            continue
        slot = _slot(entry, "teacher_slot")
        parsed = teachers[slot] if slot < len(teachers) else _teacher()
        teacher = resolved_teachers.setdefault(slot, {"name": parsed["name"], "org": parsed["org"], "bio": dict(enumerate(parsed["bio"]))})
        inferred = _without_prefix(candidates.get(entry["id"], ""), role)
        if not inferred or re.sub(r"\s+", "", inferred) not in from_notice:
            continue
        if role == "teacher_name":
            teacher["name"] = teacher["name"] or extract_teacher_name(inferred)
            teacher["org"] = teacher["org"] or _school(inferred)
        elif role == "teacher_org":
            teacher["org"] = teacher["org"] or inferred
        elif not teacher["bio"].get(entry["bio_index"]):
            teacher["bio"][entry["bio_index"]] = inferred

    has_org_slots = {_slot(entry, "teacher_slot") for entry in entries if entry["role"] == "teacher_org"}
    for entry in _ordered(entries):
        node_id, role = entry["id"], entry["role"]
        course_slot = _slot(entry, "course_slot")
        teacher_slot = _slot(entry, "teacher_slot")
        candidate = _without_prefix(candidates.get(node_id, ""), role)
        if candidate and role in TITLE_ROLES and _normalize_text(candidate) not in from_notice:
            candidate = ""  # 标题类只接受原文中出现过的文字，防止模型编造
        value = candidate
        original = entry.get("text", "")
        if role == "slogan":
            value = candidate if candidate and candidate in text else ""
        elif role in {"teacher_name", "teacher_org"} or role.startswith("teacher_bio"):
            teacher = resolved_teachers[teacher_slot]
            school = teacher["org"] or _school(teacher["bio"].get(0, ""))
            if role == "teacher_name":
                name = teacher["name"]
                use_compound = teacher_slot not in has_org_slots and bool(_school(original) or re.search(r"\S\s+\S", original))
                value = f"{school} {name}" if school and name and use_compound else name
            elif role == "teacher_org":
                value = school
            else:
                value = teacher["bio"].get(entry["bio_index"], "")
                prefix = _BULLET.match(original)
                if prefix and value:
                    value = prefix[1] + value
            if value:
                result[node_id] = value
            continue
        if role in {"schedule", "course_date", "course_time"}:
            slot = schedules[course_slot] if course_slot < len(schedules) else (None, None, True)
            day, clock = slot[0], slot[1]
            explicit = slot[2] if len(slot) > 2 else True
            # Prefer original date/time tokens; the model supplies values only when rules miss them.
            trusted_candidate = re.sub(r"\s+", "", candidate) in from_notice
            if not day and trusted_candidate:
                day = _date(candidate, today)
                explicit = bool(day)
            if not clock and trusted_candidate:
                clock = _clock(candidate)
            value = _schedule_value(role, day, clock, explicit)
        elif role in header:
            value = candidate or header[role]
        elif role == 'slogan' and candidate and candidate in text:
            value = candidate
        elif role in {"topic_title", "topic_sub", "lesson_topic", "lesson_topic_sub"}:
            if separate_lesson and not align_titles and role not in {"topic_title", "topic_sub"}:
                continue
            if course_slot < len(topics):
                key = "topic_sub" if role in {"topic_sub", "lesson_topic_sub"} else "topic_title"
                value = candidate or topics[course_slot].get(key, "")
        elif role in {"activity_tag", "slogan"}:
            value = candidate if candidate and candidate in text else text_incremental.dimension_patches(
                [entry], {node_id: original}, text_incremental.input_dimensions(text)
            ).get(node_id, "")
        elif role == "qr_tip":
            value = candidate or (qr[qr_index] if qr_index < len(qr) else "")
            qr_index += 1
        if value:
            result[node_id] = value
    return align_title_values(entries, result, current_values) if align_titles else result
