"""Seedream 4 插槽提示词/Skill 目录：扫描、热加载与拼装。

设计原则：程序只认“插槽”，不认识具体风格。
所有渠道、意图与审美 Skill 都以文件形式放在 ``prompts/seedream/``，
按文件 ``(mtime, size)`` 指纹做轻量缓存，改文件后无需重启即可生效。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

CORE_INTENTS = ("direct", "editable_layers", "clean_bg")
SLOT_SYSTEM = "system_assembler.md"
SLOT_CHANNELS = "channels.json"
SLOT_COPY = "copywriting.md"

# channels.json 完全不可用时的兜底，保证生图链路不阻塞
BUILTIN_CHANNELS: dict[str, dict[str, str]] = {
    "wechat_banner": {
        "name": "公众号头图 · 横幅", "ratio": "2.35:1", "size": "1880x800",
        "safe_area": "所有文字与核心主体锁在正中 1:1 安全区内，长标题拆两行，左右仅作背景延展。",
        "composition": "主标题居中偏上，副标题紧随其下。",
    },
    "wechat_square": {
        "name": "公众号头图 · 方图", "ratio": "1:1", "size": "1440x1440",
        "safe_area": "四周留出 8% 呼吸边，文字不贴边。",
        "composition": "主视觉居中，标题两行以内。",
    },
    "caoliao_banner": {
        "name": "草料头图 · 横幅", "ratio": "16:9", "size": "1600x900",
        "safe_area": "主副标题合计不超过两行，避开左右 6% 边缘。",
        "composition": "极简落地页顶栏，低信息密度。",
    },
    "caoliao_square": {
        "name": "草料头图 · 方图", "ratio": "1:1", "size": "1440x1440",
        "safe_area": "卡片式构图，四周留 6% 边距，底部 15% 不排小字。",
        "composition": "大标题 + 一个利益点标签。",
    },
    "mobile_poster": {
        "name": "手机竖版海报", "ratio": "9:16", "size": "1242x2208",
        "safe_area": "上中下三层清晰，底部 18% 预留扫码与落款。",
        "composition": "顶部主题，中部标题与主视觉，底部关键信息。",
    },
    "xhs_cover": {
        "name": "小红书封面", "ratio": "3:4", "size": "1242x1656",
        "safe_area": "超大标题占上半部 55%，四周留白 7%，底部 12% 避开交互遮挡。",
        "composition": "高对比底 + 大标题 + 胶囊标签 + 单一主体。",
    },
    "channels_vertical": {
        "name": "视频号封面 · 竖版", "ratio": "6:7", "size": "1200x1400",
        "safe_area": "严格 6:7，核心文字避开底部 15% 字幕区与四周 6% 边缘。",
        "composition": "主体居中偏上，标题两行内。",
    },
    "channels_horizontal": {
        "name": "视频号封面 · 横版", "ratio": "16:9", "size": "1600x900",
        "safe_area": "核心文字居中偏上，右下 20%×18% 为时长角标遮挡区。",
        "composition": "标题横向居中，右下留空。",
    },
}

_cache: dict[str, tuple[tuple[int, int], Any]] = {}
_lock = threading.Lock()
_FENCE = re.compile(r"^\s*---\s*$")


# ---------------------------------------------------------------------------
# 文件读取与解析
# ---------------------------------------------------------------------------
def prompts_dir() -> Path:
    return Path(settings.seedream_prompts_dir)


def _fingerprint(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return int(stat.st_mtime_ns), int(stat.st_size)


def read_text(path: Path) -> str:
    """UTF-8（含 BOM）+ CRLF 归一，第三方 Skill 直接拖进来也能读。"""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _cached(path: Path, loader) -> Any:
    try:
        stamp = _fingerprint(path)
    except OSError:
        return None
    key = str(path)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        value = loader(path)
    except Exception as exc:  # noqa: BLE001 - 单个文件坏了不影响整体
        logger.warning("Seedream 提示词文件解析失败：%s（%s）", path, exc)
        return None
    with _lock:
        _cache[key] = (stamp, value)
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析简易 YAML frontmatter；没有 ``---`` 时返回空元信息与全文。

    支持 ``key: value``、引号包裹、``[a, b]`` 列表与整数；不依赖 PyYAML。
    """
    lines = text.split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or not _FENCE.match(lines[index]):
        return {}, text.strip()

    meta: dict[str, Any] = {}
    cursor = index + 1
    closed = False
    while cursor < len(lines):
        line = lines[cursor]
        if _FENCE.match(line):
            closed = True
            cursor += 1
            break
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and ":" in stripped:
            key, value = stripped.split(":", 1)
            meta[key.strip().lower()] = _parse_scalar(value.strip())
        cursor += 1
    if not closed:  # Frontmatter 没闭合：整篇当正文，避免吞内容
        return {}, text.strip()
    body = "\n".join(lines[cursor:]).strip()
    return meta, body


def _parse_scalar(value: str) -> Any:
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [
            item.strip().strip("\"'").strip()
            for item in inner.split(",")
            if item.strip()
        ]
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _load_markdown(path: Path) -> dict[str, Any]:
    meta, body = parse_frontmatter(read_text(path))
    return {"meta": meta, "body": body, "path": path, "id": path.stem}


def _item_from_file(path: Path, *, core_ids: tuple[str, ...] = ()) -> dict[str, Any] | None:
    data = _cached(path, _load_markdown)
    if not data or not data["body"]:
        return None
    meta = data["meta"]
    item_id = str(meta.get("id") or path.stem).strip()
    if not item_id:
        return None
    order = meta.get("order")
    if not isinstance(order, int):
        order = (list(core_ids).index(item_id) + 1) if item_id in core_ids else 100
    recommended = meta.get("recommended_channels") or []
    if isinstance(recommended, str):
        recommended = [part.strip() for part in recommended.split(",") if part.strip()]
    return {
        "id": item_id,
        "name": str(meta.get("name") or item_id),
        "summary": str(meta.get("summary") or ""),
        "order": order,
        "core": item_id in core_ids,
        "recommended_channels": list(recommended),
        "body": data["body"],
        "file": path.name,
        "path": str(path),
    }


# ---------------------------------------------------------------------------
# 目录扫描
# ---------------------------------------------------------------------------
def _markdown_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def default_channels() -> list[dict[str, str]]:
    return [
        {"id": key, **value} for key, value in BUILTIN_CHANNELS.items()
    ]


def list_channels() -> tuple[list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    path = prompts_dir() / SLOT_CHANNELS
    data = _cached(path, lambda target: json.loads(read_text(target)))
    if not isinstance(data, dict) or not data:
        if path.exists():
            warnings.append(f"{SLOT_CHANNELS} 格式异常，已使用内置渠道兜底")
        else:
            warnings.append(f"缺少 {SLOT_CHANNELS}，已使用内置渠道兜底")
        return default_channels(), warnings

    items: list[dict[str, str]] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            warnings.append(f"渠道 {key} 配置不是对象，已跳过")
            continue
        size = str(value.get("size") or "")
        if not re.fullmatch(r"\d{2,5}x\d{2,5}", size):
            warnings.append(f"渠道 {key} 尺寸 {size!r} 非法，已跳过")
            continue
        items.append({
            "id": str(key),
            "name": str(value.get("name") or key),
            "ratio": str(value.get("ratio") or ""),
            "size": size,
            "safe_area": str(value.get("safe_area") or ""),
            "composition": str(value.get("composition") or ""),
        })
    if not items:
        warnings.append("channels.json 没有可用渠道，已使用内置兜底")
        return default_channels(), warnings
    return items, warnings


def list_intents() -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    items = []
    for path in _markdown_files(prompts_dir() / "intents"):
        item = _item_from_file(path, core_ids=CORE_INTENTS)
        if item is None:
            warnings.append(f"意图 {path.name} 无正文或元信息损坏，已跳过")
            continue
        items.append(item)
    items.sort(key=lambda item: (item["order"], item["id"]))
    if not items:
        warnings.append("intents/ 下没有可用意图文件")
    for core in CORE_INTENTS:
        if not any(item["id"] == core for item in items):
            warnings.append(f"缺少核心意图 {core}.md")
    return items, warnings


def list_styles() -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    items = []
    for path in _markdown_files(prompts_dir() / "styles"):
        item = _item_from_file(path)
        if item is None:
            warnings.append(f"风格 {path.name} 无正文或元信息损坏，已跳过")
            continue
        items.append(item)
    items.sort(key=lambda item: (item["order"], item["id"]))
    return items, warnings


def catalog() -> dict[str, Any]:
    channels, channel_warnings = list_channels()
    intents, intent_warnings = list_intents()
    styles, style_warnings = list_styles()
    return {
        "channels": channels,
        "intents": [
            {"id": item["id"], "name": item["name"], "summary": item["summary"],
             "core": item["core"], "order": item["order"]}
            for item in intents
        ],
        "styles": [
            {"id": item["id"], "name": item["name"], "summary": item["summary"],
             "recommended_channels": item["recommended_channels"]}
            for item in styles
        ],
        "prompts_dir": str(prompts_dir()),
        "warnings": channel_warnings + intent_warnings + style_warnings,
    }


# ---------------------------------------------------------------------------
# 拼装
# ---------------------------------------------------------------------------
def _slot_file(name: str) -> Path:
    return prompts_dir() / name


def _read_slot(name: str, warnings: list[str]) -> str:
    path = _slot_file(name)
    if not path.is_file():
        warnings.append(f"缺少 {name}，该插槽已跳过")
        return ""
    data = _cached(path, lambda target: read_text(target))
    if not data:
        warnings.append(f"{name} 读取失败，该插槽已跳过")
        return ""
    return data.strip()


def _channel_block(channel: dict[str, str]) -> str:
    return "\n".join([
        f"渠道名称：{channel['name']}",
        f"画面比例：{channel['ratio']}（实际输出像素为 {channel['size']}，由接口参数控制，"
        f"提示词中不要复述像素数字）",
        f"安全区硬约束：{channel['safe_area']}",
        f"构图要点：{channel['composition']}",
    ])


def _find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    return next((item for item in items if item["id"] == item_id), None)


def assemble(
    *,
    channel_id: str,
    intent_id: str,
    style_id: str | None = None,
    has_ref_images: bool = False,
) -> dict[str, Any]:
    """按插槽顺序拼装 System Prompt，返回 system / used_files / 元信息 / warnings。"""
    warnings: list[str] = []
    sections: list[str] = []
    used: list[str] = []

    channels, channel_warnings = list_channels()
    intents, intent_warnings = list_intents()
    styles, style_warnings = list_styles()
    warnings.extend(channel_warnings + intent_warnings + style_warnings)

    channel = _find(channels, channel_id)
    if channel is None:
        raise ValueError(f"未知渠道：{channel_id}")

    intent = _find(intents, intent_id) or _find(intents, "direct")
    if intent is None:
        raise ValueError(f"未知产出意图：{intent_id}")

    system_text = _read_slot(SLOT_SYSTEM, warnings)
    if system_text:
        sections.append(f"【插槽 1 · 总控】\n{system_text}")
        used.append(SLOT_SYSTEM)

    sections.append(f"【插槽 2 · 渠道骨架】\n{_channel_block(channel)}")
    used.append(f"{SLOT_CHANNELS}#{channel['id']}")

    if intent["id"] != "clean_bg":
        copy_text = _read_slot(SLOT_COPY, warnings)
        if copy_text:
            sections.append(f"【插槽 3 · 文案提炼与防幻觉】\n{copy_text}")
            used.append(SLOT_COPY)

    sections.append(f"【插槽 4 · 产出意图：{intent['name']}】\n{intent['body']}")
    used.append(f"intents/{intent['file']}")

    style = _find(styles, style_id) if style_id else None
    if style_id and style is None:
        warnings.append(f"风格 {style_id} 不存在，已忽略")
    if style is not None:
        sections.append(f"【插槽 5 · 视觉风格：{style['name']}】\n{style['body']}")
        used.append(f"styles/{style['file']}")

    if has_ref_images:
        sections.append(
            "【参考图】\n用户已提供参考图。参考图只用于取用其配色、材质与构图氛围，"
            "不要照抄参考图里的文字内容，也不要在画面中复述参考图上的文字。"
        )

    sections.append(
        "【输出契约】\n只返回 JSON：{\"prompt\": \"最终中文生图提示词\", "
        "\"notes\": \"可选说明\"}。prompt 必须是一段连续中文，不含 Markdown 与代码块。"
    )

    system = "\n\n".join(section for section in sections if section.strip())
    return {
        "system": system,
        "used_files": used,
        "warnings": warnings,
        "channel": {key: channel[key] for key in ("id", "name", "ratio", "size")},
        "intent": {"id": intent["id"], "name": intent["name"]},
        "style": {"id": style["id"], "name": style["name"]} if style else None,
    }


_TEXT_HINT = re.compile(
    r"(写上|写出|写下|添加|加上|加|绘制|绘制出|排上|排版|标注|放置|加入|贴上|附上)[^，。；\n]{0,10}"
    r"(文字|标题|副标题|文案|数字|英文|字母|logo|LOGO|水印|二维码)"
)


def sanitize_clean_bg(prompt: str) -> tuple[str, list[str]]:
    """无字底图模式：剔除仍要求「画文字」的句子，并追加一条硬禁止。"""
    warnings: list[str] = []
    if not prompt:
        return prompt, warnings
    kept: list[str] = []
    removed = 0
    for sentence in re.split(r"(?<=[。；\n])", prompt):
        if _TEXT_HINT.search(sentence):
            removed += 1
            continue
        kept.append(sentence)
    cleaned = "".join(kept).strip()
    if removed:
        warnings.append(f"已从提示词中移除 {removed} 处要求绘制文字的表述")
    cleaned = (
        f"{cleaned}\n\n【硬性禁止】画面中不得出现任何文字、数字、字母、水印、logo 或伪文字，"
        "只保留背景、主体与留白。"
    )
    return cleaned, warnings
