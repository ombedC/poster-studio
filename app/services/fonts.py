"""本地字体加载。

设计师导出的 SVG 常用 Illustrator 的字体别名，例如::

    font-family="AlibabaPuHuiTi_3_105_Heavy-GBpc-EUC-H, "Alibaba PuHuiTi 3.0""

把字体文件放进 ``static/Fonts`` 后，本模块会读取字体的 family / PostScript 名，
生成 ``@font-face`` 规则注入无头浏览器，让 SVG 用上真实字体而不是系统回退字体。

支持的扩展名：``.ttf`` / ``.otf``（同一字重同时存在时优先 ttf）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from app.config import settings

logger = logging.getLogger(__name__)

_FONT_EXTENSIONS = (".ttf", ".otf")
_ALIAS_RE = re.compile(
    r"(?i)^alibaba[-_ ]?puhuiti[-_ ]?3[-_ ](\d{2,3})[-_ ]([a-z0-9]+)$"
)
_WEIGHT_RE = re.compile(r"[-_ ]3[-_ ](\d{2,3})[-_ ]", re.IGNORECASE)
# 旧版（Alibaba PuHuiTi 1.0/2.0）PostScript 命名：
# AlibabaPuHuiTiB / M / H / R / L / T / EB / BL
_LEGACY_CODES = {
    35: "T",
    45: "L",
    55: "R",
    65: "M",
    75: "S",
    85: "B",
    95: "EB",
    105: "H",
    115: "BL",
}
# 统一注册到同一 family，仅用 font-weight 区分字重
_CSS_WEIGHTS = {
    35: 200,
    45: 300,
    55: 400,
    65: 500,
    75: 600,
    85: 700,
    95: 850,
    105: 800,
    115: 900,
}


@dataclass
class FontFace:
    path: Path
    weight: int = 400
    style: str = "normal"
    families: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    postscript_name: str = ""


def _read_names(path: Path) -> dict[int, str]:
    try:
        from fontTools.ttLib import TTFont
    except Exception:  # pragma: no cover - 没有 fontTools 时用文件名兜底
        return {}
    names: dict[int, str] = {}
    try:
        font = TTFont(str(path), lazy=True)
        for record in font["name"].names:
            if record.nameID not in {1, 2, 4, 6, 16, 17}:
                continue
            try:
                value = record.toUnicode().strip()
            except Exception:
                continue
            # 优先 Windows 英文/中文记录，其他平台只做兜底
            if value and record.nameID not in names:
                names[record.nameID] = value
            elif value and record.platformID == 3 and record.langID in (0x409, 0x804):
                names[record.nameID] = value
        weight = int(font["OS/2"].usWeightClass)
        names[-1] = str(weight)
        subfamily = names.get(17) or names.get(2) or ""
        if "italic" in subfamily.lower() or "oblique" in subfamily.lower():
            names[-2] = "italic"
        font.close()
    except Exception as exc:
        logger.warning("读取字体信息失败：%s（%s）", path.name, exc)
    return names


def _font_face_from_path(path: Path) -> FontFace:
    names = _read_names(path)
    stem = path.stem
    match = _ALIAS_RE.match(stem)
    alias_number = int(match.group(1)) if match else None
    style_token = match.group(2) if match else "Regular"
    weight = alias_number if alias_number else 400
    try:
        os_weight = int(names.get(-1, weight))
    except (TypeError, ValueError):
        os_weight = weight
    weight = _CSS_WEIGHTS.get(alias_number, os_weight)
    style = names.get(-2, "normal")
    postscript = names.get(6, "")
    families: list[str] = []
    for key in (16, 1):
        value = names.get(key)
        if value and value not in families:
            families.append(value)
    for extra in ("Alibaba PuHuiTi 3.0", "阿里巴巴普惠体 3.0"):
        if extra not in families:
            families.append(extra)

    aliases: list[str] = []
    if postscript:
        aliases.append(postscript)
        # nameID 6 可能带 _L3 后缀，SVG 里没有
        trimmed = re.sub(r"_L\d+$", "", postscript, flags=re.IGNORECASE)
        if trimmed != postscript:
            aliases.append(trimmed)
    if match:
        alias_style = re.sub(r"L\d+$", "", style_token, flags=re.IGNORECASE)
        aliases.append(f"AlibabaPuHuiTi_3_{alias_number}_{alias_style}-GBpc-EUC-H")
    legacy = _LEGACY_CODES.get(alias_number)
    if legacy:
        # Illustrator 老模板里常见的 1.0/2.0 字体名，映射到同字重的 3.0 文件
        aliases.extend(
            [
                f"AlibabaPuHuiTi{legacy}",
                f"AlibabaPuHuiTi{legacy}-GBpc-EUC-H",
                f"AlibabaPuHuiTi{legacy} GBpc-EUC-H",
            ]
        )
    deduped: list[str] = []
    for alias in aliases:
        if alias and alias not in deduped:
            deduped.append(alias)

    return FontFace(
        path=path.resolve(),
        weight=weight,
        style=style,
        families=families,
        aliases=deduped,
        postscript_name=postscript,
    )


def _font_directory() -> Path | None:
    directory = Path(settings.font_dir)
    if directory.is_dir():
        return directory
    static_dir = Path(settings.static_dir)
    if static_dir.is_dir():
        for child in static_dir.iterdir():
            if child.is_dir() and child.name.lower() == "fonts":
                return child
    return None


@lru_cache(maxsize=1)
def discover_fonts() -> tuple[FontFace, ...]:
    """扫描字体目录，按字体文件名去重（同名字重优先 ttf）。"""
    directory = _font_directory()
    if not settings.fonts_enabled or directory is None:
        return ()

    chosen: dict[str, Path] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in _FONT_EXTENSIONS or not path.is_file():
            continue
        key = path.stem.lower()
        if key not in chosen or path.suffix.lower() == ".ttf":
            chosen[key] = path

    faces = tuple(_font_face_from_path(path) for path in chosen.values())
    logger.info("已加载 %d 个本地字体（%s）", len(faces), directory)
    return faces


def _css(url_prefix: str | None) -> str:
    rules: list[str] = []
    # For shared family names, CSS tries the later face first. Use L3 as
    # supplemental coverage behind the standard Regular face.
    for face in sorted(discover_fonts(), key=lambda face: not face.path.stem.lower().endswith("regularl3")):
        if url_prefix:
            url = f"{url_prefix.rstrip('/')}/{quote(face.path.name)}"
        else:
            url = face.path.as_uri()
        for alias in face.aliases:
            rules.append(
                f"@font-face{{font-family:{json.dumps(alias, ensure_ascii=False)};"
                f"src:url({json.dumps(url)});font-weight:{face.weight};"
                f"font-style:{face.style};font-display:block;}}"
            )
        for family in face.families:
            rules.append(
                f"@font-face{{font-family:{json.dumps(family, ensure_ascii=False)};"
                f"src:url({json.dumps(url)});font-weight:{face.weight};"
                f"font-style:{face.style};font-display:block;}}"
            )
    return "\n".join(rules)


@lru_cache(maxsize=1)
def font_css() -> str:
    """生成注入页面的 @font-face CSS（file:// 地址，供导出/测量使用）。"""
    return _css(None)


@lru_cache(maxsize=2)
def web_font_css(prefix: str = "/static/Fonts") -> str:
    """生成前端页面用的 CSS（HTTP 地址）。"""
    return _css(prefix)


def font_summary() -> dict[str, object]:
    faces = discover_fonts()
    return {
        "enabled": settings.fonts_enabled,
        "count": len(faces),
        "families": sorted({family for face in faces for family in face.families})[:8],
        "dir": str(_font_directory() or settings.font_dir),
        "revision": font_revision(),
    }


@lru_cache(maxsize=1)
def font_revision() -> str:
    """字体文件 + 别名集合的指纹，用于让缩略图/预览缓存自动失效。"""
    digest = hashlib.sha1()
    for face in discover_fonts():
        digest.update(face.path.name.encode("utf-8"))
        digest.update(str(face.weight).encode("ascii"))
        try:
            digest.update(str(face.path.stat().st_mtime_ns).encode("ascii"))
        except OSError:
            pass
        for alias in face.aliases:
            digest.update(alias.encode("utf-8"))
    return digest.hexdigest()[:10]
