"""矢量模板导入：把 Illustrator .ai / PostScript .eps 转成 SVG。"""
from __future__ import annotations

import base64
import io
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

try:
    import pymupdf
except ImportError:  # SVG passthrough remains available without the optional PDF runtime.
    pymupdf = None
from lxml import etree
from PIL import Image

from app.config import BASE_DIR, settings
from app.errors import TemplateInvalid, VectorArtboardChoice

VECTOR_SUFFIXES = {".svg", ".ai", ".eps"}
_WINDOWS_NO_WINDOW = 0x08000000
_MAX_PAGES = 24
_THUMB_WIDTH = 220
_RENDER_DPI = 16
_SVG_NS = "http://www.w3.org/2000/svg"
_BAD_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffd\ue000-\uf8ff]")
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


def _candidate_paths(name: str) -> list[Path]:
    candidates = [Path(os.environ[env]) for env in ("GHOSTSCRIPT_BIN", "GS_BIN") if os.environ.get(env)]
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    candidates.extend((Path(r"D:\PosterTools\gs\bin\gswin64c.exe"),
                       Path(r"C:\Program Files\gs\gs10.08.0\bin\gswin64c.exe"),
                       BASE_DIR / "data" / "installers" / "gs-portable" / "bin" / "gswin64c.exe"))
    return candidates


def _find_tool(name: str, *, required: bool = True) -> Path | None:
    tool = next((path for path in _candidate_paths(name) if path.is_file()), None)
    if tool is None and required:
        raise TemplateInvalid("缺少 Ghostscript 转换工具，无法读取 EPS。请安装 Ghostscript，或在 .env 配置 GHOSTSCRIPT_BIN。")
    return tool


def _require(name: str) -> Path:
    tool = _find_tool(name)
    if tool is None:
        raise TemplateInvalid(f"缺少 {name} 转换工具，无法读取 AI/EPS")
    return tool


def _run(command: list[str], *, cwd: Path, timeout: int = 180) -> None:
    try:
        result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                creationflags=_WINDOWS_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TemplateInvalid(f"矢量转换工具执行失败：{exc}") from exc
    if result.returncode:
        detail = re.sub(r"\s+", " ", (result.stderr or result.stdout or "").strip())[-500:]
        raise TemplateInvalid(f"矢量转换失败：{detail or '未知错误'}")


def _looks_like_svg(data: bytes) -> bool:
    head = data.lstrip()[:2048].lower()
    return b"<svg" in head or head.startswith(b"<?xml")


def _looks_like_pdf(data: bytes) -> bool:
    return b"%PDF" in data[:2048]


def _looks_like_postscript(data: bytes) -> bool:
    return b"%!PS" in data[:2048]


def _source_kind(data: bytes | Path) -> str:
    if isinstance(data, Path):
        with data.open("rb") as source:
            head = source.read(2048)
            if b"\xc5\xd0\xd3\xc6" == head[:4]:
                return "eps"
        if _looks_like_svg(head):
            return "svg"
        if _looks_like_pdf(head):
            return "pdf"
        if _looks_like_postscript(head):
            return "eps"
        raise TemplateInvalid("无法识别文件格式：既不是 SVG，也不是 PDF/EPS")
    if _looks_like_svg(data):
        return "svg"
    if _looks_like_pdf(data):
        return "pdf"
    if _looks_like_postscript(data):
        return "eps"
    raise TemplateInvalid("无法识别文件格式：既不是 SVG，也不是 PDF/EPS")


def _to_pdf(data: bytes | Path, kind: str, work: Path) -> Path:
    if pymupdf is None:
        raise TemplateInvalid("缺少 PyMuPDF，无法导入 AI/EPS；请安装 requirements.txt 中的依赖")
    path = work / "source.pdf"
    if kind == "pdf":
        if isinstance(data, Path):
            return data
        if len(data) < 20 or b"%%EOF" not in data[-1024:]:
            raise TemplateInvalid("AI/EPS PDF 文件不完整，无法转换")
        path.write_bytes(data)
        return path
    if isinstance(data, Path):
        with data.open("rb") as source:
            header = source.read(30)
        if header[:4] == b"\xc5\xd0\xd3\xc6":
            if len(header) < 30:
                raise TemplateInvalid("EPS 文件头不完整")
            offset, length = struct.unpack_from("<II", header, 4)
            if offset < 30 or length < 4:
                raise TemplateInvalid("EPS 内嵌 PostScript 无效")
            eps_path = work / "source.eps"
            with data.open("rb") as source, eps_path.open("wb") as target:
                source.seek(offset)
                remaining = length
                while remaining:
                    chunk = source.read(min(2 * 1024 * 1024, remaining))
                    if not chunk:
                        raise TemplateInvalid("EPS 内嵌 PostScript 被截断")
                    if remaining == length and not chunk.startswith(b"%!PS"):
                        raise TemplateInvalid("EPS 内嵌 PostScript 无效")
                    target.write(chunk)
                    remaining -= len(chunk)
            eps = eps_path
        else:
            eps = data
    else:
        if data.startswith(b"\xc5\xd0\xd3\xc6"):
            if len(data) < 30:
                raise TemplateInvalid("EPS 文件头不完整")
            offset, length = struct.unpack_from("<II", data, 4)
            if offset < 30 or offset + length > len(data) or data[offset:offset + 4] != b"%!PS":
                raise TemplateInvalid("EPS 内嵌 PostScript 无效")
            data = data[offset:offset + length]
        eps = work / "source.eps"
        eps.write_bytes(data)
    gs = _require("gs")
    _run([str(gs), "-dBATCH", "-dNOPAUSE", "-dSAFER", "-dEPSCrop", "-sDEVICE=pdfwrite",
          f"-sOutputFile={path.name}", eps.name], cwd=work)
    return path


def _open_pdf(pdf_path: Path):
    try:
        with pdf_path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            if size < 20:
                raise TemplateInvalid("矢量转换失败：PDF 输出过小")
            source.seek(max(0, size - 1024))
            if b"%%EOF" not in source.read():
                raise TemplateInvalid("矢量转换失败：PDF 输出不完整")
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise TemplateInvalid("矢量转换失败：无法读取 PDF 画板") from exc
    if not doc.page_count or doc.needs_pass:
        doc.close()
        raise TemplateInvalid("矢量转换失败：PDF 无画板或受密码保护")
    return doc


def _render_page(page) -> Image.Image:
    scale = min(_RENDER_DPI / 72, 1024 / max(page.rect.width, page.rect.height))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, annots=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _is_blank(image: Image.Image) -> bool:
    return all(high - low <= 6 for low, high in image.convert("RGB").getextrema())


def _thumbnail(image: Image.Image) -> str:
    width = _THUMB_WIDTH
    height = max(1, round(image.height * width / max(1, image.width)))
    buffer = io.BytesIO()
    image.resize((width, height), Image.LANCZOS).save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _valid_pdf_text(text: str) -> bool:
    return bool(text.strip()) and not _BAD_TEXT.search(text)


def _inspect(doc, *, kind: str, max_pages: int) -> dict:
    pages = []
    total = doc.page_count
    for index in range(min(total, max(1, max_pages))):
        page = doc[index]
        image = _render_page(page)
        text = page.get_text(flags=pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE)
        pages.append({"index": index + 1, "width": round(page.rect.width, 2),
                      "height": round(page.rect.height, 2), "blank": _is_blank(image),
                      "text_status": "editable" if _valid_pdf_text(text) else "outlined" if text.strip() else "no_text",
                      "thumbnail": _thumbnail(image)})
    non_blank = [item["index"] for item in pages if not item["blank"]]
    return {"kind": kind, "total": total, "pages": pages, "non_blank": non_blank,
            "auto_page": non_blank[0] if len(non_blank) == 1 else 0,
            "needs_choice": len(non_blank) > 1, "truncated": total > max(1, max_pages)}


def inspect_pages(data: bytes | Path, filename: str, *, max_pages: int = _MAX_PAGES) -> dict:
    if Path(filename).suffix.lower() not in VECTOR_SUFFIXES:
        raise TemplateInvalid("只支持 .svg / .ai / .eps 模板文件")
    kind = _source_kind(data)
    if kind == "svg":
        return {"kind": "svg", "total": 1, "pages": [], "non_blank": [1], "auto_page": 1,
                "needs_choice": False, "truncated": False}
    with tempfile.TemporaryDirectory(prefix="poster-vector-", dir=str(settings.cache_dir)) as tmp:
        path = _to_pdf(data, kind, Path(tmp))
        with _open_pdf(path) as doc:
            return _inspect(doc, kind=kind, max_pages=max_pages)


def _page_box(root: etree._Element) -> tuple[float, float]:
    nums = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", root.get("viewBox") or "")]
    if len(nums) == 4 and nums[2] > 0 and nums[3] > 0:
        return nums[2], nums[3]
    raise TemplateInvalid("转换后的 SVG 缺少画板尺寸")


def _crop_to_page(svg_bytes: bytes) -> bytes:
    root = etree.fromstring(svg_bytes, parser=etree.XMLParser(recover=True, huge_tree=True))
    if root is None or not root.tag.endswith("svg"):
        raise TemplateInvalid("PDF 转 SVG 输出格式异常")
    width, height = _page_box(root)
    root.set("width", f"{width:g}")
    root.set("height", f"{height:g}")
    root.set("viewBox", f"0 0 {width:g} {height:g}")
    defs = next((node for node in root if node.tag.endswith("defs")), None)
    if defs is None:
        defs = etree.Element(f"{{{_SVG_NS}}}defs")
        root.insert(0, defs)
    clip = etree.SubElement(defs, f"{{{_SVG_NS}}}clipPath", id="poster-artboard-clip", clipPathUnits="userSpaceOnUse")
    etree.SubElement(clip, f"{{{_SVG_NS}}}rect", x="0", y="0", width=f"{width:g}", height=f"{height:g}")
    keep = {"defs", "metadata", "title", "desc"}
    content = [node for node in root if node.tag.split("}")[-1] not in keep]
    if content:
        group = etree.Element(f"{{{_SVG_NS}}}g", **{"clip-path": "url(#poster-artboard-clip)"})
        index = list(root).index(content[0])
        for node in content:
            root.remove(node)
            group.append(node)
        root.insert(index, group)
    return etree.tostring(root, encoding="utf-8", xml_declaration=True)


def _independent_images(root: etree._Element) -> None:
    images = {node.get("id"): node for node in root.iter(f"{{{_SVG_NS}}}image") if node.get("id")}
    for use in list(root.iter(f"{{{_SVG_NS}}}use")):
        ref = use.get(_XLINK_HREF, "")
        source = images.get(ref[1:]) if ref.startswith("#") else None
        if source is None:
            continue
        image = etree.Element(f"{{{_SVG_NS}}}image", attrib={key: val for key, val in source.attrib.items() if key != "id"})
        image.attrib.update({key: val for key, val in use.attrib.items() if key != _XLINK_HREF})
        use.getparent().replace(use, image)


def _convert_page(page) -> tuple[bytes, dict]:
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE
    extracted = page.get_text("dict", flags=flags)
    spans = [span for block in extracted["blocks"] if "lines" in block for line in block["lines"] for span in line["spans"] if span["text"].strip()]
    text = "".join(span["text"] for span in spans)
    editable = bool(spans) and _valid_pdf_text(text)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    root = etree.fromstring(page.get_svg_image(text_as_path=not editable).encode(), parser)
    _independent_images(root)
    text_nodes = [node for node in root.iter(f"{{{_SVG_NS}}}text") if "".join(node.itertext()).strip()] if editable else []
    if editable and not text_nodes:
        # An invisible duplicate over the original paths is not editable artwork.
        root = etree.fromstring(page.get_svg_image(text_as_path=True).encode(), parser)
        _independent_images(root)
    for node in text_nodes:
        node.set("data-poster-editable", "true")
    count = sum(1 for _ in root.iter(f"{{{_SVG_NS}}}image"))
    return _crop_to_page(etree.tostring(root)), {"status": "editable" if text_nodes else "outlined" if text.strip() else "no_text",
                                               "editable_count": len(text_nodes), "image_count": count}


def convert_to_svg(data: bytes | Path, filename: str, *, max_svg_bytes: int = 0, page: int = 0) -> tuple[bytes, list[str]]:
    if Path(filename).suffix.lower() not in VECTOR_SUFFIXES:
        raise TemplateInvalid("只支持 .svg / .ai / .eps 模板文件")
    if isinstance(data, bytes) and _looks_like_svg(data):
        return data, ["SVG 原文件"]
    if isinstance(data, Path) and _source_kind(data) == "svg":
        return data.read_bytes(), ["SVG 原文件"]
    kind = _source_kind(data)
    notes = ["已将 EPS/PostScript 转为 PDF" if kind == "eps" else "已识别 PDF 兼容的 AI 文件"]
    with tempfile.TemporaryDirectory(prefix="poster-vector-", dir=str(settings.cache_dir)) as tmp:
        work = Path(tmp)
        pdf_path = _to_pdf(data, kind, work)
        if pdf_path != data:
            with pdf_path.open("rb") as converted_pdf:
                if not _looks_like_pdf(converted_pdf.read(2048)):
                    raise TemplateInvalid("矢量转换失败：Ghostscript 没有生成有效 PDF")
        with _open_pdf(pdf_path) as doc:
            if page:
                if page < 1 or page > doc.page_count:
                    raise TemplateInvalid(f"画板编号超出范围：文件共 {doc.page_count} 个画板")
                if _is_blank(_render_page(doc[page - 1])):
                    raise TemplateInvalid(f"第 {page} 个画板为空白，无法导入")
                target = page
                notes.append(f"使用第 {target} 个画板（共 {doc.page_count} 个）")
            else:
                info = _inspect(doc, kind=kind, max_pages=_MAX_PAGES)
                if len(info["non_blank"]) > 1 or (info["truncated"] and info["non_blank"]):
                    raise VectorArtboardChoice(info)
                if not info["non_blank"]:
                    raise TemplateInvalid("所检查的画板全是空白，无法导入" if info["truncated"] else "这个文件的画板全是空白，无法导入")
                target = info["non_blank"][0]
                if doc.page_count > 1:
                    notes.append(f"已自动跳过空白画板，使用第 {target} 个画板（共 {doc.page_count} 个）")
            converted, status = _convert_page(doc[target - 1])
    if max_svg_bytes and len(converted) > max_svg_bytes:
        raise TemplateInvalid(f"转换后的 SVG 为 {len(converted) / (1024 * 1024):.1f}MB，超过 {max_svg_bytes / (1024 * 1024):g}MB 限制")
    if not _looks_like_svg(converted):
        raise TemplateInvalid("PDF 转 SVG 输出格式异常")
    notes.append("已按画板边界裁切并转为 SVG，可继续清洗和坑位识别")
    if status["status"] == "no_text":
        notes.append(f"文字状态:no_text;图片:{status['image_count']}；源文件没有可提取文本对象，导入后文字无法编辑。建议导出未转曲版本，或使用 Seedream 拆图层 OCR 还原")
    elif status["status"] == "outlined":
        notes.append(f"文字状态:outlined;图片:{status['image_count']}；检测到文字但字符编码不可靠，保留原轮廓；建议检查字体映射后重导")
    else:
        notes.append(f"文字状态:editable:{status['editable_count']};图片:{status['image_count']}")
    return converted, notes
