"""已处理素材库：老师头像 / 二维码。

每次上传并处理完的图片都会留一份在 ``data/assets``，下次可直接点选复用。
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from app.config import settings

_LOCK = threading.Lock()
_KINDS = {"teacher": "teachers", "qrcode": "qrcodes"}


def _root() -> Path:
    return settings.data_dir / "assets"


def _kind_dir(kind: str) -> Path:
    folder = _KINDS.get(kind)
    if folder is None:
        raise ValueError(f"未知素材类型：{kind}")
    path = _root() / folder
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index_path() -> Path:
    path = _root() / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_index() -> list[dict[str, Any]]:
    path = _index_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_index(items: list[dict[str, Any]]) -> None:
    path = _index_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _thumbnail(png: bytes, path: Path, max_side: int = 240) -> None:
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    image.thumbnail((max_side, max_side), Image.LANCZOS)
    canvas = Image.new("RGB", image.size, (255, 255, 255))
    canvas.paste(image, mask=image.split()[-1])
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, format="JPEG", quality=86)


def add_asset(
    kind: str,
    png: bytes,
    *,
    mode: str = "",
    label: str = "",
    width: int = 0,
    height: int = 0,
) -> dict[str, Any]:
    directory = _kind_dir(kind)
    digest = hashlib.sha1()
    digest.update(kind.encode("ascii"))
    digest.update(mode.encode("utf-8"))
    digest.update(png)
    asset_id = digest.hexdigest()[:16]

    with _LOCK:
        items = _read_index()
        for item in items:
            if item.get("id") == asset_id:
                return item

        path = directory / f"{asset_id}.png"
        if not path.is_file():
            path.write_bytes(png)
        thumb = _root() / "thumbnails" / f"{asset_id}.jpg"
        if not thumb.is_file():
            _thumbnail(png, thumb)

        item = {
            "id": asset_id,
            "kind": kind,
            "mode": mode,
            "label": label,
            "width": width,
            "height": height,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        items.insert(0, item)
        _write_index(items[:500])
        return item


def list_assets(kind: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
    items = _read_index()
    if kind:
        items = [item for item in items if item.get("kind") == kind]
    return items[: max(1, min(limit, 200))]


def get_asset(asset_id: str) -> dict[str, Any] | None:
    for item in _read_index():
        if item.get("id") == asset_id:
            return item
    return None


def asset_path(asset_id: str, *, thumbnail: bool = False) -> Path | None:
    item = get_asset(asset_id)
    if item is None:
        return None
    if thumbnail:
        path = _root() / "thumbnails" / f"{asset_id}.jpg"
    else:
        folder = _KINDS.get(item.get("kind", "teacher"))
        if folder is None:
            return None
        path = _root() / folder / f"{asset_id}.png"
    return path if path.is_file() else None
