"""Bounded-memory upload staging and configured template size guard."""
from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

from app.config import settings
from app.errors import PayloadTooLarge, TemplateInvalid

CHUNK_BYTES = 2 * 1024 * 1024
STAGED_TTL_SECONDS = 60 * 60


def _staged_root() -> Path:
    root = settings.cache_dir / "template-staging"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cleanup_stale_stages() -> None:
    cutoff = time.time() - STAGED_TTL_SECONDS
    for path in _staged_root().glob("*.upload"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def upload_limit_error(actual_bytes: int, filename: str) -> PayloadTooLarge | None:
    limit = settings.max_template_upload_bytes
    if not limit or actual_bytes <= limit:
        return None
    actual_mb = actual_bytes / (1024 * 1024)
    limit_mb = limit / (1024 * 1024)
    return PayloadTooLarge(f"模板文件「{filename}」实际 {actual_mb:.1f}MB，超过配置上限 {limit_mb:g}MB（MAX_TEMPLATE_UPLOAD_MB）")


def stage_upload(source: BinaryIO, filename: str) -> tuple[Path, int]:
    """Copy an UploadFile stream in bounded chunks to disk; always rewind the source."""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".svg", ".ai", ".eps"}:
        raise TemplateInvalid("只支持上传 .svg / .ai / .eps 模板文件")
    path = None
    size = 0
    try:
        cleanup_stale_stages()
        source.seek(0)
        with tempfile.NamedTemporaryFile(prefix="upload-", suffix=".upload",
                                         dir=_staged_root(), delete=False) as staged:
            path = Path(staged.name)
            while True:
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                limit_error = upload_limit_error(size, filename)
                if limit_error:
                    raise limit_error
                staged.write(chunk)
            staged.flush()
        if size == 0:
            raise TemplateInvalid("模板内容为空")
        return path, size
    except OSError as exc:
        if path:
            path.unlink(missing_ok=True)
        raise TemplateInvalid(f"无法落盘上传的模板「{filename}」：{exc}") from exc
    except Exception:
        if path:
            path.unlink(missing_ok=True)
        raise
    finally:
        source.seek(0)


def stage_token(path: Path) -> str:
    if path.parent.resolve() != _staged_root().resolve() or not path.is_file():
        raise TemplateInvalid("暂存模板不存在")
    return path.name.removesuffix(".upload")


def resolve_stage(token: str) -> Path:
    cleanup_stale_stages()
    if not isinstance(token, str) or not re.fullmatch(r"upload-[A-Za-z0-9_-]+", token):
        raise TemplateInvalid("暂存模板令牌无效或已过期")
    root = _staged_root().resolve()
    path = (root / f"{token}.upload").resolve()
    if root not in path.parents or not path.is_file() or time.time() - path.stat().st_mtime > STAGED_TTL_SECONDS:
        path.unlink(missing_ok=True)
        raise TemplateInvalid("暂存模板已过期，请重新选择文件")
    return path


def remove_staged(path: Path) -> None:
    path.unlink(missing_ok=True)
