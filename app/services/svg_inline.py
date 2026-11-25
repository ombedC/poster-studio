"""Inline explicitly opted-in local template and Seedream assets for exports."""
from __future__ import annotations

import base64
import re
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

from app.config import settings

SEEDREAM_PREFIX = "/api/seedream/files/"
_TEMPLATE_PREFIX = "/api/v1/posters/templates/"
_HINT = re.compile(rb"/api/(?:seedream/files|v1/posters/templates)/")
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


def resolve_local(href: str) -> Path | None:
    if not isinstance(href, str):
        return None
    if href.startswith(SEEDREAM_PREFIX):
        rest = unquote(href[len(SEEDREAM_PREFIX):]).split("?", 1)[0].split("#", 1)[0]
        project, _, name = rest.partition("/")
        root = settings.seedream_project_dir.resolve()
        path = (root / project / name).resolve()
        return path if project and name and root in path.parents and path.is_file() else None
    if href.startswith(_TEMPLATE_PREFIX):
        rest = unquote(href[len(_TEMPLATE_PREFIX):]).split("?", 1)[0].split("#", 1)[0]
        name, sep, tail = rest.partition("/resources/")
        resource = Path(tail)
        if not sep or not name or resource.name != tail or resource.suffix.lower() not in {".png", ".jpg"}:
            return None
        root = (settings.template_dir / f"{name}.resources").resolve()
        path = (root / tail).resolve()
        return path if root in path.parents and path.is_file() else None
    return None


def inline_local_images(svg: bytes) -> bytes:
    if not _HINT.search(svg):
        return svg
    parser = etree.XMLParser(recover=True, huge_tree=True)
    try:
        root = etree.fromstring(svg, parser)
    except Exception:
        return svg
    if root is None:
        return svg
    changed = False
    for element in root.iter():
        if not isinstance(element.tag, str) or element.tag.rsplit("}", 1)[-1] != "image":
            continue
        for attr in ("href", XLINK_HREF):
            value = element.get(attr)
            path = resolve_local(value) if value else None
            if path is None:
                continue
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            element.set(attr, f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii"))
            changed = True
    return etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True) if changed else svg
