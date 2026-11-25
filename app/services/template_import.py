"""Shared SVG, Illustrator and EPS template import."""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import quote

from app.config import settings
from app.errors import InvalidImage, PayloadTooLarge, TemplateInvalid, UnsupportedMediaType
from app.services import svg_browser, svg_sanitizer, template_analyzer as analyzer, template_meta, template_resources, vector_importer

MAX_SVG_BYTES = 0


def _template_name_from(filename: str) -> str:
    base = Path(filename or "").name
    lowered = base.lower()
    if lowered.endswith(".fixed.svg"):
        base = base[: -len(".fixed.svg")]
    elif lowered.endswith((".svg", ".ai", ".eps")):
        base = base[: -len(Path(base).suffix)]
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(" .")
    return (base[:80] or "未命名模板")


def _cleanup_template_artifacts(name: str) -> None:
    resources = template_resources.resource_directory(name)
    if resources.is_dir():
        shutil.rmtree(resources, ignore_errors=True)
    for path in (
        analyzer.named_schema_path(name), analyzer.named_svg_path(name),
        analyzer.revival_backup_path(name), settings.template_dir / "thumbs" / f"{name}.jpg",
    ):
        if path.is_file():
            path.unlink()


def import_template(
    raw: bytes | Path,
    filename: str,
    *,
    name: str = "",
    overwrite: bool = False,
    analyze: bool = True,
    owner_id: str = "",
    team_id: str = "",
    scope: str = "public",
    page: int = 0,
) -> dict:
    suffix = Path(filename).suffix.lower()
    if suffix not in vector_importer.VECTOR_SUFFIXES:
        raise UnsupportedMediaType("只支持上传 .svg / .ai / .eps 模板文件")
    size = raw.stat().st_size if isinstance(raw, Path) else len(raw)
    if not size:
        raise InvalidImage("模板内容为空")

    vector_bytes, conversion_notes = vector_importer.convert_to_svg(raw, filename, max_svg_bytes=MAX_SVG_BYTES, page=page)
    cleaned, sanitize_notes = svg_sanitizer.sanitize_svg(vector_bytes)
    notes = list(conversion_notes) + list(sanitize_notes)
    text_status = next((
        {"status": match.group(1), "editable_count": int(match.group(2) or 0), "image_count": int(match.group(3))}
        for note in conversion_notes
        if (match := re.search(r"文字状态:(no_text|outlined|editable)(?::(\d+))?;图片:(\d+)", note))
    ), None)
    base = _template_name_from(name or filename)
    template_name = base if overwrite else base
    index = 2
    while not overwrite and analyzer.source_svg_path(template_name).exists():
        template_name = f"{base}-{index}"
        index += 1

    settings.template_dir.mkdir(parents=True, exist_ok=True)
    target = analyzer.source_svg_path(template_name)
    resource_dir = template_resources.resource_directory(template_name)
    staging_resources = settings.template_dir / f".{template_name}.resources-{uuid.uuid4().hex}"
    staged_assets: list[Path] = []
    try:
        canvas = analyzer.svg_canvas_bytes(cleaned)
        cleaned, staged_assets = template_resources.externalize_svg_images(cleaned, template_name, canvas, resource_dir=staging_resources)
        # Keep the old resource set available until its replacement directory is fully prepared.
        if staging_resources.exists():
            old_resources = settings.template_dir / f".{template_name}.resources-old-{uuid.uuid4().hex}"
            if resource_dir.exists():
                resource_dir.replace(old_resources)
            try:
                staging_resources.replace(resource_dir)
            except OSError:
                if old_resources.exists() and not resource_dir.exists():
                    old_resources.replace(resource_dir)
                raise
            if old_resources.exists():
                shutil.rmtree(old_resources, ignore_errors=True)
        elif resource_dir.exists():
            shutil.rmtree(resource_dir)
    except OSError as exc:
        template_resources.cleanup_resources(staged_assets)
        if staging_resources.exists():
            shutil.rmtree(staging_resources, ignore_errors=True)
        raise PayloadTooLarge(f"模板「{filename}」实际 {size / (1024 * 1024):.1f}MB；位图资源写盘失败，可能磁盘空间不足：{exc}") from exc
    except Exception as exc:
        template_resources.cleanup_resources(staged_assets)
        if staging_resources.exists():
            shutil.rmtree(staging_resources, ignore_errors=True)
        if isinstance(exc, OSError):
            raise PayloadTooLarge(f"模板「{filename}」实际 {size / (1024 * 1024):.1f}MB；位图资源处理时写盘失败，可能磁盘空间不足：{exc}") from exc
        if isinstance(exc, TemplateInvalid):
            raise
        raise TemplateInvalid(f"SVG/位图资源处理失败：{exc}") from exc

    staging = target.with_name(f".{uuid.uuid4().hex}.upload")
    try:
        staging.write_bytes(cleaned)
        staging.replace(target)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        shutil.rmtree(resource_dir, ignore_errors=True)
        raise PayloadTooLarge(f"模板「{filename}」实际 {size / (1024 * 1024):.1f}MB；保存转换结果失败，可能磁盘空间不足：{exc}") from exc
    if overwrite:
        for path in (analyzer.named_schema_path(template_name), analyzer.named_svg_path(template_name),
                     analyzer.revival_backup_path(template_name), settings.template_dir / "thumbs" / f"{template_name}.jpg"):
            path.unlink(missing_ok=True)

    warnings = list(notes)
    if staged_assets:
        warnings.append(f"已将 {len(staged_assets)} 张嵌入位图外置并按显示尺寸优化")
    revival = {"available": False, "revived": [], "skipped": [], "warnings": [], "revived_count": 0}
    try:
        revival = analyzer.revive_named_template(template_name, use_llm=True)
        warnings.extend(revival.get("warnings") or [])
    except Exception as exc:
        warnings.append(f"转曲文字复活失败，已保留原路径：{exc}")
    thumb = settings.template_dir / "thumbs" / f"{template_name}.jpg"
    try:
        svg_browser.thumbnail_svg(target, canvas, thumb, width=320)
    except Exception as exc:
        warnings.append(f"缩略图生成失败，已跳过：{exc}")
    analyzed = analyzer.is_named_template_analyzed(template_name)
    if analyze:
        try:
            analyzer.analyze_named_template(template_name, force=True, use_llm=True)
            analyzed = True
        except Exception as exc:
            warnings.append(f"自动解析坑位失败：{exc}")
    template_meta.ensure(template_name, owner_id=owner_id, team_id=team_id, scope=scope)
    version = int(thumb.stat().st_mtime) if thumb.is_file() else int(target.stat().st_mtime)
    return {
        "name": template_name,
        "display_name": analyzer.display_name_of(template_name),
        "analyzed": analyzed,
        "thumbnail_url": f"/api/v1/posters/templates/{quote(template_name, safe='')}/thumbnail?v={version}",
        "canvas": {"width": canvas[0], "height": canvas[1]},
        "warnings": warnings,
        "text_status": text_status,
        "revived_texts": revival.get("revived_count", 0),
        "revival_report": revival,
    }
