"""External raster assets for lightweight named SVG templates."""
from __future__ import annotations

import base64
import io
import re
import uuid
from binascii import Error as Base64Error
from pathlib import Path
from urllib.parse import quote

from lxml import etree
from PIL import Image

from app.config import settings

SVG_NS = "http://www.w3.org/2000/svg"
XLINK = "{http://www.w3.org/1999/xlink}href"
DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|jpg|webp);base64,\s*", re.I)
MAX_IMAGE_PIXELS = 120_000_000


def resource_directory(template_name: str) -> Path:
    return settings.template_dir / f"{template_name}.resources"


def externalize_svg_images(svg: bytes, template_name: str, canvas: tuple[float, float], *, resource_dir: Path | None = None) -> tuple[bytes, list[Path]]:
    """Externalize embedded raster image resources and downsample oversized placements."""
    try:
        root = etree.fromstring(svg, etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True))
    except etree.XMLSyntaxError:
        return svg, []
    resource_dir = resource_dir or resource_directory(template_name)
    created: list[Path] = []
    for node in root.iter(f"{{{SVG_NS}}}image"):
        href_attr = "href" if node.get("href") is not None else XLINK
        match = DATA_IMAGE.match(node.get(href_attr, ""))
        if not match:
            continue
        try:
            encoded = re.sub(r"\s+", "", node.get(href_attr)[match.end():])
            payload = base64.b64decode(encoded, validate=True)
            try:
                decoded = Image.open(io.BytesIO(payload))
            except (OSError, ValueError):
                node.attrib.pop(href_attr, None)
                continue
            with decoded as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError(f"图片像素过大：{source.width}×{source.height}")
                source.load()
                image = source.copy()
                try:
                    w = abs(float(node.get("width") or image.width))
                    h = abs(float(node.get("height") or image.height))
                except ValueError:
                    w, h = float(image.width), float(image.height)
                # SVG user units are CSS pixels here; retain at most 2× display resolution.
                scale = min(1.0, max(1.0, w) * 2 / image.width, max(1.0, h) * 2 / image.height)
                if scale < 1:
                    image.thumbnail((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
                alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
                ext = ".png" if alpha else ".jpg"
                resource_dir.mkdir(parents=True, exist_ok=True)
                target = resource_dir / f"image-{uuid.uuid4().hex}{ext}"
                if alpha:
                    image.save(target, "PNG", optimize=True)
                else:
                    image.convert("RGB").save(target, "JPEG", quality=92, optimize=True, progressive=True)
        except (OSError, ValueError, Base64Error, Image.DecompressionBombError) as exc:
            if isinstance(exc, OSError) and any(token in str(exc).lower() for token in ("no space", "disk full", "not enough space")):
                raise
            raise ValueError(f"无法处理嵌入位图：{exc}") from exc
        created.append(target)
        url = f"/api/v1/posters/templates/{quote(template_name, safe='')}/resources/{target.name}"
        node.set(href_attr, url)
        if href_attr == "href" and node.get(XLINK) is not None:
            node.set(XLINK, url)
    return etree.tostring(root, encoding="utf-8", xml_declaration=True), created


def cleanup_resources(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
    for directory in {path.parent for path in paths}:
        try:
            directory.rmdir()
        except OSError:
            pass


def replace_resource_set(resource_dir: Path, keep: list[Path]) -> None:
    keep_set = {path.resolve() for path in keep}
    if not resource_dir.is_dir():
        return
    for old in resource_dir.iterdir():
        if old.is_file() and old.resolve() not in keep_set:
            old.unlink(missing_ok=True)
    try:
        resource_dir.rmdir()
    except OSError:
        pass
