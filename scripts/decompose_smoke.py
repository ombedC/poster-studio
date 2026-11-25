#!/usr/bin/env python
"""单图 CPU 拆层冒烟脚本（方案 B · S2）

用一张真实海报跑通：LayerD 逐层剥离 → 紧致裁边 → PaddleOCR 整图识别
→ 按 OCR 行把文字从图层里擦掉并转成 <text> → 组装 base64 内嵌的标准 SVG，
输出每阶段耗时、内存峰值、图层清单与可直接用浏览器打开的 out.svg。

必须在独立环境 ``.venv-decompose`` 里运行：

    .\\.venv-decompose\\Scripts\\python.exe scripts\\decompose_smoke.py --image .\\老师照片\\海报.png

常用参数：
    --download-models   首次预下载 BiRefNet / LaMa / PaddleOCR 模型
    --self-check        只做依赖与缓存自检，不加载大模型
    --render            额外用本机 Edge 截图（拆层环境装 playwright 才生效）
    --ocr none          只测拆层，不做文字识别
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG_NS = "http://www.w3.org/2000/svg"
MODEL_REPO = "cyberagent/layerd-birefnet"
LAMA_URL = (
    "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/"
    "v0.1.0/big-lama.pt"
)
DEFAULT_HF_ENDPOINT = "https://huggingface.co"
MIN_LAYER_PX = 8
MIN_OPAQUE_PX = 64
MAX_LAYERS = 40
OCR_MIN_CONFIDENCE = 0.80
TEXT_MIN_OPACITY = 0.35
TEXT_MIN_HEIGHT = 10
# 颜色方差超过该值说明带描边/阴影/渐变，字号超过画布高度比例说明是装饰大标题，
# 两者都保留 PNG，避免把效果字改成普通字体。
TEXT_MAX_COLOR_STD = 35.0
# 行高超过画布高度 4% 视为装饰性大标题（主标题约 7%），保留 PNG；
# 正文/副标题（约 2%~3%）转成可编辑 <text>。
TEXT_MAX_HEIGHT_RATIO = 0.04
_LAST_OCR_DEBUG: list[dict] = []


# ---------------------------------------------------------------------------
# 控制台与通用工具
# ---------------------------------------------------------------------------
def setup_console() -> None:
    """避免 Windows 控制台把中文输出成乱码。"""
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def say(message: str = "") -> None:
    print(message, flush=True)


def step(message: str) -> None:
    print(f"\n=== {message} ===", flush=True)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def human_bytes(size: float | int | None) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _import(module: str, hint: str):
    try:
        from importlib import import_module

        return import_module(module)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"缺少依赖 {module}（{exc}）。{hint}") from exc


class ProcessMemory:
    """Windows 下读取本进程当前/峰值 RSS；其它平台退化为 None。"""

    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    def __init__(self) -> None:
        self.enabled = os.name == "nt"
        self.peak_mb: float | None = None
        self.current_mb: float | None = None
        self._handle = None
        self._query = None
        if not self.enabled:
            return
        try:
            kernel32 = ctypes.windll.kernel32
            pid = kernel32.GetCurrentProcessId()
            self._handle = kernel32.OpenProcess(0x0400 | 0x1000, False, pid)
            query = getattr(ctypes.windll.psapi, "GetProcessMemoryInfo", None)
            if query is None:
                query = getattr(kernel32, "K32GetProcessMemoryInfo", None)
            if not self._handle or query is None:
                self.enabled = False
                return
            query.argtypes = [ctypes.c_void_p, ctypes.POINTER(self._Counters), ctypes.c_ulong]
            query.restype = ctypes.c_int
            self._query = query
        except Exception:  # noqa: BLE001
            self.enabled = False

    def sample(self) -> None:
        if not self.enabled or self._query is None:
            return
        try:
            counters = self._Counters()
            counters.cb = ctypes.sizeof(self._Counters)
            if not self._query(self._handle, ctypes.byref(counters), counters.cb):
                self.enabled = False
                return
            self.current_mb = counters.WorkingSetSize / (1024 * 1024)
            peak = counters.PeakWorkingSetSize / (1024 * 1024)
            self.peak_mb = peak if self.peak_mb is None else max(self.peak_mb, peak)
        except Exception:  # noqa: BLE001
            self.enabled = False

    def close(self) -> None:
        if self._handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = None


class Timings:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def run(self, name: str):
        return _Stage(self, name)

    def add(self, name: str, ms: float) -> None:
        self.items.append({"stage": name, "ms": round(ms, 1)})


class _Stage:
    def __init__(self, timings: Timings, name: str) -> None:
        self.timings = timings
        self.name = name
        self.started = 0.0

    def __enter__(self):
        say(f"[..] {self.name}")
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = (time.perf_counter() - self.started) * 1000
        if exc_type is None:
            self.timings.add(self.name, elapsed)
            say(f"[ok] {self.name}: {elapsed / 1000:.1f}s")
        else:
            say(f"[!!] {self.name}: 失败 {exc}")
        return False


# ---------------------------------------------------------------------------
# 模型缓存路径（Paddle 必须 ASCII，HF 可以中文）
# ---------------------------------------------------------------------------
def _is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _ascii_cache_root(cache_root: Path) -> Path:
    if _is_ascii_path(cache_root):
        return cache_root
    override = os.environ.get("DECOMPOSE_ASCII_CACHE")
    if override:
        return Path(override)
    # 往上找一个纯 ASCII 且可写的祖先目录（项目父目录通常可写；盘符根目录常常不行）
    parent = cache_root.parent
    while parent != parent.parent and not _is_ascii_path(parent):
        parent = parent.parent
    if _is_ascii_path(parent):
        return parent / "poster-decompose-cache"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
    if base and _is_ascii_path(Path(base)):
        return Path(base) / "poster-decompose"
    return Path("C:/poster-decompose")


@dataclass
class EnvPaths:
    cache_root: Path
    ascii_root: Path
    hf_home: Path
    lama_path: Path
    paddle_home: Path
    hf_endpoint: str

    def apply(self) -> None:
        self.hf_home.mkdir(parents=True, exist_ok=True)
        self.lama_path.parent.mkdir(parents=True, exist_ok=True)
        self.paddle_home.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(self.hf_home)
        if self.hf_endpoint:
            os.environ["HF_ENDPOINT"] = self.hf_endpoint
        else:
            os.environ.pop("HF_ENDPOINT", None)
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        if self.lama_path.is_file():
            os.environ["LAMA_MODEL"] = str(self.lama_path)
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(self.paddle_home)
        os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "huggingface")
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        os.environ.setdefault("MPLCONFIGDIR", str(self.ascii_root / ".matplotlib"))
        os.environ.setdefault("TORCH_HOME", str(self.cache_root / "torch"))
        # paddle.dataset 会在 import paddle 时创建 ~/.cache/paddle/dataset
        try:
            (Path(os.path.expanduser("~")) / ".cache" / "paddle" / "dataset").mkdir(
                parents=True, exist_ok=True
            )
        except OSError:
            home = self.ascii_root / "home"
            os.environ["USERPROFILE"] = str(home)
            os.environ["HOME"] = str(home)
            (home / ".cache" / "paddle" / "dataset").mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, str]:
        return {
            "cache_root": str(self.cache_root),
            "ascii_cache_root": str(self.ascii_root),
            "HF_HOME": os.environ.get("HF_HOME", ""),
            "HF_ENDPOINT": os.environ.get("HF_ENDPOINT", ""),
            "LAMA_MODEL": os.environ.get("LAMA_MODEL", ""),
            "PADDLE_PDX_CACHE_HOME": os.environ.get("PADDLE_PDX_CACHE_HOME", ""),
            "PADDLE_PDX_MODEL_SOURCE": os.environ.get("PADDLE_PDX_MODEL_SOURCE", ""),
        }


def build_env(args) -> EnvPaths:
    cache_root = Path(args.cache_root).expanduser().resolve()
    ascii_root = Path(args.ascii_cache).expanduser().resolve() if args.ascii_cache else _ascii_cache_root(cache_root)
    env = EnvPaths(
        cache_root=cache_root,
        ascii_root=ascii_root,
        hf_home=cache_root / "huggingface",
        lama_path=ascii_root / "decompose" / "big-lama.pt",
        paddle_home=ascii_root / "paddle",
        hf_endpoint="" if args.no_hf_mirror else (args.hf_endpoint or DEFAULT_HF_ENDPOINT),
    )
    env.apply()
    return env


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


# ---------------------------------------------------------------------------
# 自检 / 模型预下载
# ---------------------------------------------------------------------------
def module_version(name: str) -> str:
    try:
        from importlib import metadata

        return metadata.version(name)
    except Exception:  # noqa: BLE001
        return ""


def _free_gb(path: Path) -> float:
    try:
        import shutil

        return shutil.disk_usage(path).free / (1024 ** 3)
    except Exception:  # noqa: BLE001
        return 0.0


def self_check(args, env: EnvPaths) -> int:
    step("依赖自检")
    mem = ProcessMemory()
    mem.sample()
    report: dict[str, object] = {
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
        "env": env.describe(),
        "checks": {},
        "ok": True,
    }
    checks: dict[str, object] = {}

    try:
        import torch

        cuda = torch.version.cuda
        checks["torch"] = {
            "ok": cuda is None,
            "version": torch.__version__,
            "cuda": cuda,
            "threads": torch.get_num_threads(),
        }
        if cuda is not None:
            checks["torch"]["hint"] = "检测到 CUDA 版 torch，必须重装纯 CPU 版"
            report["ok"] = False
    except Exception as exc:  # noqa: BLE001
        checks["torch"] = {"ok": False, "error": str(exc)}
        report["ok"] = False

    try:
        import layerd  # noqa: F401

        checks["layerd"] = {
            "ok": hasattr(layerd, "LayerDPipeline"),
            "version": module_version("layerd"),
        }
        if not checks["layerd"]["ok"]:
            report["ok"] = False
    except Exception as exc:  # noqa: BLE001
        checks["layerd"] = {"ok": False, "error": str(exc)}
        report["ok"] = False

    try:
        import paddleocr  # noqa: F401

        checks["paddleocr"] = {"ok": True, "version": module_version("paddleocr")}
    except Exception as exc:  # noqa: BLE001
        checks["paddleocr"] = {
            "ok": False,
            "error": str(exc),
            "hint": "可先用 --ocr rapidocr 兜底；或按安装脚本重装 paddlepaddle/paddleocr",
        }

    hf_model = env.hf_home / "hub" / "models--cyberagent--layerd-birefnet"
    checks["models"] = {
        "birefnet": {"path": str(hf_model), "present": hf_model.is_dir(), "bytes": dir_size(hf_model)},
        "lama": {
            "path": str(env.lama_path),
            "present": env.lama_path.is_file(),
            "bytes": env.lama_path.stat().st_size if env.lama_path.is_file() else 0,
        },
        "paddle": {"path": str(env.paddle_home), "present": dir_size(env.paddle_home) > 0,
                   "bytes": dir_size(env.paddle_home)},
    }
    report["checks"] = checks
    report["peak_rss_mb"] = round(mem.peak_mb, 1) if mem.peak_mb else None
    report["free_disk_gb"] = round(_free_gb(ROOT), 1)

    say("")
    for name, info in checks.items():
        if name == "models":
            for key, value in info.items():
                mark = "OK " if value.get("present") else "-- "
                say(f"  [{mark}] {key:10s} {human_bytes(value['bytes']):>10s}  {value['path']}")
            continue
        mark = "OK " if info.get("ok") else "FAIL"
        detail = info.get("version") or info.get("cuda") or info.get("error") or ""
        say(f"  [{mark}] {name:10s} {detail}")
    say(f"\n  峰值内存：{report['peak_rss_mb']} MB；磁盘剩余：{report['free_disk_gb']} GB")
    if args.json:
        say(json.dumps(report, ensure_ascii=False, indent=2))
    mem.close()
    return 0 if report["ok"] else 1


def download_models(args, env: EnvPaths) -> int:
    step("预下载模型（首次约 1.2~1.5 GB）")
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"缺少 huggingface_hub（{exc}），请先运行安装脚本") from exc

    target = env.hf_home / "hub"
    say(f"[..] BiRefNet ← {MODEL_REPO}（HF_ENDPOINT={os.environ.get('HF_ENDPOINT') or '默认'}）")
    path = snapshot_download(
        repo_id=MODEL_REPO,
        cache_dir=str(target),
        allow_patterns=["*.json", "*.py", "*.safetensors", "*.txt", "*.model"],
        max_workers=4,
    )
    size = dir_size(env.hf_home / "hub" / "models--cyberagent--layerd-birefnet")
    say(f"[ok] BiRefNet: {human_bytes(size)} @ {path}")

    if env.lama_path.is_file() and env.lama_path.stat().st_size > 150_000_000:
        say(f"[ok] LaMa 已存在：{human_bytes(env.lama_path.stat().st_size)} @ {env.lama_path}")
    else:
        say(f"[..] LaMa ← {LAMA_URL}")
        _download_with_progress(LAMA_URL, env.lama_path)
        say(f"[ok] LaMa: {human_bytes(env.lama_path.stat().st_size)} @ {env.lama_path}")

    if args.skip_ocr:
        say("[--] 跳过 PaddleOCR 预热")
        return 0
    say("[..] PaddleOCR 预热（首次会自动下载检测/识别模型）")
    try:
        engine = build_ocr(args)
        if engine is not None:
            lines = read_lines(engine, _synthetic_text_image())
            say(f"[ok] PaddleOCR 可用（合成图识别 {len(lines)} 行）")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        say(f"[!!] PaddleOCR 预热失败：{exc}")
        say("     可用 --ocr rapidocr 或 --skip-ocr，稍后按需排查")
        return 1
    return 0


def _download_with_progress(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    existing = part.stat().st_size if part.is_file() else 0
    headers = {"User-Agent": "poster-decompose-smoke/1.0"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(url, headers=headers)
    mode = "ab" if existing else "wb"
    last = 0.0
    with urllib.request.urlopen(request, timeout=60) as response, open(part, mode) as handle:
        total = int(response.headers.get("Content-Length") or 0) + existing
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            handle.write(chunk)
            existing += len(chunk)
            now = time.time()
            if now - last > 2:
                last = now
                ratio = f"{existing * 100 / total:.1f}%" if total else "?"
                say(f"     {human_bytes(existing)} / {human_bytes(total)} ({ratio})")
    part.replace(target)


def _synthetic_text_image():
    np = _import("numpy", "请先运行安装脚本")
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 96), "white")
    ImageDraw.Draw(image).text((12, 24), "ABC 2026", fill="black")
    return np.asarray(image)


# ---------------------------------------------------------------------------
# OCR：PaddleOCR 3.x / 2.x / RapidOCR 兼容
# ---------------------------------------------------------------------------
@dataclass
class OcrLine:
    text: str
    confidence: float
    points: list[tuple[float, float]]


def build_ocr(args):
    if args.ocr == "none":
        return None
    if args.ocr == "rapidocr":
        module = _import("rapidocr_onnxruntime", "拆层环境未安装 rapidocr-onnxruntime")
        return ("rapidocr", module.RapidOCR())
    module = _import("paddleocr", "请先运行安装脚本安装 paddleocr（CPU 版）")
    PaddleOCR = module.PaddleOCR
    attempts = (
        {"lang": "ch", "use_doc_orientation_classify": False,
         "use_doc_unwarping": False, "use_textline_orientation": False},
        {"lang": "ch", "use_angle_cls": False},
        {"lang": "ch"},
        {},
    )
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            return ("paddle", PaddleOCR(**kwargs))
        except TypeError as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"PaddleOCR 初始化失败：{exc}") from exc
    raise SystemExit(f"PaddleOCR 参数不兼容：{last_error}")


def read_lines(engine, image_rgb) -> list[OcrLine]:
    if engine is None:
        return []
    kind, impl = engine
    np = _import("numpy", "请先运行安装脚本")
    array = np.asarray(image_rgb)
    if kind == "rapidocr":
        result, _ = impl(array)
        lines = []
        for item in result or []:
            try:
                points = [(float(p[0]), float(p[1])) for p in item[0]]
                text = str(item[1]).strip()
                score = float(item[2])
            except (TypeError, ValueError, IndexError):
                continue
            if text:
                lines.append(OcrLine(text, score, points))
        return lines

    try:
        pages = impl.predict(array)
    except AttributeError:
        pages = impl.ocr(array, cls=False)
    lines: list[OcrLine] = []
    for page in pages or []:
        payload = _ocr_payload(page)
        texts = payload.get("rec_texts") or payload.get("rec_text") or []
        scores = payload.get("rec_scores") or payload.get("rec_score") or []
        polys = payload.get("rec_polys") or payload.get("dt_polys") or payload.get("rec_boxes") or []
        try:
            texts, scores, polys = list(texts), list(scores), list(polys)
        except TypeError:
            texts, scores, polys = [], [], []
        if not texts:
            lines.extend(_legacy_paddle_lines(page))
            continue
        for index, text in enumerate(texts):
            text = str(text).strip()
            if not text:
                continue
            try:
                score = float(scores[index])
            except (IndexError, TypeError, ValueError):
                score = 0.0
            points = _clean_points(polys[index] if index < len(polys) else None)
            if points:
                lines.append(OcrLine(text, score, points))
    return lines


def _ocr_payload(page):
    if page is None:
        return {}
    if isinstance(page, dict):
        inner = page.get("res")
        return inner if isinstance(inner, dict) else page
    payload = getattr(page, "json", None)
    if callable(payload):
        try:
            payload = payload()
        except Exception:  # noqa: BLE001
            payload = None
    if isinstance(payload, dict):
        inner = payload.get("res")
        return inner if isinstance(inner, dict) else payload
    try:
        return dict(page)
    except Exception:  # noqa: BLE001
        return {}


def _legacy_paddle_lines(page) -> list[OcrLine]:
    lines: list[OcrLine] = []
    if not isinstance(page, list):
        return lines
    for item in page:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        payload = item[1]
        if not isinstance(payload, (list, tuple)) or len(payload) < 2:
            continue
        try:
            text = str(payload[0]).strip()
            score = float(payload[1])
        except (TypeError, ValueError):
            continue
        points = _clean_points(item[0])
        if text and points:
            lines.append(OcrLine(text, score, points))
    return lines


def _clean_points(raw) -> list[tuple[float, float]]:
    if raw is None:
        return []
    try:
        values = list(raw)
    except TypeError:
        return []
    points: list[tuple[float, float]] = []
    for item in values:
        try:
            points.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return points if len(points) >= 3 else []


# ---------------------------------------------------------------------------
# 图层与文字
# ---------------------------------------------------------------------------
@dataclass
class Layer:
    id: str
    z: int
    box: tuple[float, float, float, float]
    local_box: tuple[int, int, int, int]
    png: bytes = b""
    opaque: int = 0
    text: str = ""
    line_nodes: list[dict[str, object]] = field(default_factory=list)
    confidence: float = 0.0
    coverage: float = 0.0
    fill: str = "#000000"
    keep_image: bool = True
    action: str = "png"
    reason: str = ""


def _mask_bbox(mask) -> tuple[int, int, int, int] | None:
    import numpy as np

    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not rows.any() or not cols.any():
        return None
    y0, y1 = np.where(rows)[0][[0, -1]]
    x0, x1 = np.where(cols)[0][[0, -1]]
    return int(x0), int(y0), int(x1) + 1, int(y1) + 1


def _polygon_mask(points, size):
    import numpy as np
    from PIL import Image, ImageDraw

    mask = Image.new("L", size, 0)
    if len(points) >= 3:
        ImageDraw.Draw(mask).polygon([(float(x), float(y)) for x, y in points], fill=255)
    return np.asarray(mask) > 0


def _hex_color(pixels) -> str:
    import numpy as np

    if not len(pixels):
        return "#000000"
    median = np.median(pixels, axis=0).astype(int)
    return "#%02x%02x%02x" % tuple(int(value) for value in median[:3])


def _line_fill(pixels, background) -> str:
    """取文字颜色：传入的是“字形内部”像素（已腐蚀掉边缘），直接取中位数即可。"""
    return _hex_color(pixels)


def _png_bytes(image) -> bytes:
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def build_layers(result, scale: float, args, resized_rgb, engine):
    """把 LayerD 原始图层 + 整图 OCR 结果组装成可编辑单元。

    LayerD 的“一层”常是整块未遮挡内容（标题 + 照片 + 装饰），
    organizer 的连通域又会把文字切成单个字，所以在整图上跑一次 OCR，
    再把每个 OCR 行按重叠率归属到某一层、局部擦除并转成 <text>。
    """
    np = _import("numpy", "请先运行安装脚本")
    Image = _import("PIL.Image", "请先运行安装脚本")
    from PIL import ImageFilter

    global _LAST_OCR_DEBUG
    _LAST_OCR_DEBUG = []

    warnings: list[str] = []
    raw_layers = list(getattr(result, "layers", None) or [])
    background = raw_layers[0].convert("RGBA") if raw_layers else None
    if background is None:
        return [], ["LayerD 未返回任何图层"], None
    size = background.size

    entries: list[dict] = []
    for z, layer_img in enumerate(raw_layers[1:], start=1):
        rgba = np.asarray(layer_img.convert("RGBA")).copy()
        alpha = rgba[..., 3] > 16
        bbox = _mask_bbox(alpha)
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        if x1 - x0 < MIN_LAYER_PX or y1 - y0 < MIN_LAYER_PX:
            continue
        if int(np.count_nonzero(alpha)) < MIN_OPAQUE_PX:
            continue
        entries.append({"z": z, "rgba": rgba, "alpha": alpha, "bbox": bbox})

    for entry in entries:
        alpha_values = entry["rgba"][..., 3]
        solid = entry["rgba"][..., :3][alpha_values >= 200]
        if not len(solid):
            solid = entry["rgba"][..., :3][alpha_values >= 16]
        entry["bg_rgb"] = (
            np.median(solid.astype(np.float32), axis=0) if len(solid) else np.zeros(3, dtype=np.float32)
        )

    line_infos: list[dict] = []
    if engine is not None and args.convert_text:
        try:
            lines = read_lines(engine, resized_rgb)
        except Exception as exc:  # noqa: BLE001
            lines = []
            warnings.append(f"整图 OCR 失败：{exc}")
        for line in lines:
            if line.confidence < OCR_MIN_CONFIDENCE:
                continue
            mask = _polygon_mask(line.points, size)
            bbox = _mask_bbox(mask)
            if bbox is None:
                continue
            if bbox[3] - bbox[1] < TEXT_MIN_HEIGHT:
                continue
            line_infos.append({"line": line, "mask": mask, "bbox": bbox})

    # 每个 OCR 行归属到重叠最多的图层，并判断是否适合转文字。
    # 颜色方差大（描边、阴影、渐变）或字号过大的装饰标题一律保留 PNG，
    # 否则强行换字体会把效果图改成“普通字体”，比不转更难看。
    for info in line_infos:
        best = None
        best_overlap = 0.0
        for entry in entries:
            area = int(np.count_nonzero(info["mask"]))
            if not area:
                continue
            overlap = int(np.count_nonzero(info["mask"] & entry["alpha"])) / area
            if overlap > best_overlap:
                best, best_overlap = entry, overlap
        info["entry"] = best
        info["opacity"] = best_overlap
        info["convert"] = False
        info["fill"] = "#000000"
        info["spread"] = 0.0
        if best is None or not args.convert_text or args.keep_png_text:
            continue
        if best_overlap < TEXT_MIN_OPACITY:
            continue
        # 取“字形内部”像素：先腐蚀掉 1px 边缘，避免抗锯齿像素把颜色带偏
        alpha_values = best["rgba"][..., 3]
        core = info["mask"] & (alpha_values >= 200)
        core_img = Image.fromarray((core * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(3))
        core_mask = np.asarray(core_img) > 0
        pixels = best["rgba"][..., :3][core_mask]
        if len(pixels) < 16:
            pixels = best["rgba"][..., :3][info["mask"] & (alpha_values >= 16)]
        if not len(pixels):
            continue
        spread = float(np.std(pixels.astype(np.float32), axis=0).max())
        line_h = info["bbox"][3] - info["bbox"][1]
        info["spread"] = round(spread, 1)
        # 亮底装饰条（黄色课程条、白色徽章）上的字通常是美术字，强转会明显走样
        ring_img = Image.fromarray((info["mask"] * 255).astype(np.uint8))
        ring = np.asarray(ring_img.filter(ImageFilter.MaxFilter(15))) > 0
        ring &= ~(np.asarray(ring_img.filter(ImageFilter.MaxFilter(7))) > 0)
        bg_lum = 0.0
        if ring.any():
            bg_pixels = np.asarray(background.convert("RGB"))[ring]
            if len(bg_pixels):
                bg_lum = float(np.median(bg_pixels.mean(axis=1)))
        info["bg_lum"] = round(bg_lum, 1)
        converted = line_h <= size[1] * TEXT_MAX_HEIGHT_RATIO and bg_lum <= 170
        info["convert"] = converted
        info["fill"] = _line_fill(pixels, best.get("bg_rgb")) if converted else "#000000"
        _LAST_OCR_DEBUG.append(
            {
                "text": info["line"].text,
                "confidence": round(float(info["line"].confidence), 4),
                "bbox": [int(value) for value in info["bbox"]],
                "opacity": round(float(best_overlap), 3),
                "spread": round(spread, 1),
                "height": int(line_h),
                "bg_lum": round(bg_lum, 1),
                "converted": bool(converted),
            }
        )
        continue

    # 统一在所有图层上擦除已转文字的区域：LayerD 的同一段文字可能残留
    # 在别的图层里，只擦“归属层”会在画布上出现重影。
    erase_union = np.zeros((size[1], size[0]), dtype=bool)
    for info in line_infos:
        if info.get("convert"):
            erase_union |= info["mask"]
    for entry in entries:
        entry["opaque_before"] = int(np.count_nonzero(entry["alpha"]))
        entry["orig_alpha"] = entry["alpha"].copy()
    if erase_union.any():
        erase_img = Image.fromarray((erase_union * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(7))
        erase_alpha = np.asarray(erase_img) > 0
        for entry in entries:
            entry["rgba"][..., 3][erase_alpha & entry["alpha"]] = 0
            entry["alpha"] = entry["rgba"][..., 3] > 16
            entry["bbox"] = _mask_bbox(entry["alpha"])
        # 背景层也可能被 LaMa“补”出文字残影；用文字四周的底色把该区域抹平，
        # 否则擦掉前景字后会在底图上留下旧的文字（画布上表现为重影）。
        bg_np = np.asarray(background.convert("RGBA")).copy()
        for info in line_infos:
            if not info.get("convert"):
                continue
            mask_img = Image.fromarray((info["mask"] * 255).astype(np.uint8))
            fill_mask = np.asarray(mask_img.filter(ImageFilter.MaxFilter(7))) > 0
            ring = np.asarray(mask_img.filter(ImageFilter.MaxFilter(15))) > 0
            ring &= ~fill_mask
            ring_pixels = bg_np[ring][:, :3]
            if len(ring_pixels) < 16:
                continue
            color = np.median(ring_pixels, axis=0).astype(np.uint8)
            bg_np[fill_mask] = [int(color[0]), int(color[1]), int(color[2]), 255]
        background = Image.fromarray(bg_np, mode="RGBA")

    layers: list[Layer] = []
    for entry in entries:
        rgba = entry["rgba"]
        alpha = entry["alpha"]
        bbox = entry["bbox"]
        mine = [info for info in line_infos if info.get("entry") is entry]
        convertible = [info for info in mine if info.get("convert")]
        nodes: list[dict[str, object]] = []
        for info in convertible:
            line = info["line"]
            lx0, ly0, lx1, ly1 = info["bbox"]
            font_size = max(8.0, round((ly1 - ly0) * scale * 0.86, 1))
            nodes.append(
                {
                    "text": line.text,
                    "x": round(lx0 * scale, 1),
                    "y": round(ly0 * scale + font_size * 0.88, 1),
                    "w": round((lx1 - lx0) * scale, 1),
                    "h": round((ly1 - ly0) * scale, 1),
                    "font_size": font_size,
                    "confidence": round(float(line.confidence), 4),
                    "fill": str(info.get("fill") or "#000000"),
                }
            )
        nodes.sort(key=lambda item: float(item["y"]))

        keep_image = bbox is not None and int(np.count_nonzero(alpha)) >= MIN_OPAQUE_PX
        if bbox is None:
            bbox = (0, 0, 0, 0)
        converted = bool(nodes)
        coverage = 0.0
        if converted:
            union = np.zeros_like(alpha)
            for info in convertible:
                union |= info["mask"]
            base = max(1, int(entry.get("opaque_before") or 1))
            coverage = round(float(np.count_nonzero(union & entry["orig_alpha"])) / base, 3)

        x0, y0, x1, y1 = bbox
        if not keep_image and not nodes:
            continue

        crop = Image.fromarray(rgba[y0:y1, x0:x1], mode="RGBA") if keep_image else None
        if converted:
            reason = f"转为 {len(nodes)} 行可编辑 text" + ("，残余图形保留 PNG" if keep_image else "")
        elif mine:
            reason = "文字带描边/阴影/渐变或字号过大，保留 PNG"
        else:
            reason = "未识别到文字"

        layers.append(
            Layer(
                id=f"L{len(layers) + 1:02d}",
                z=len(layers),
                box=(round(x0 * scale, 1), round(y0 * scale, 1),
                     round((x1 - x0) * scale, 1), round((y1 - y0) * scale, 1)),
                local_box=(x0, y0, x1 - x0, y1 - y0),
                png=_png_bytes(crop) if crop is not None else b"",
                opaque=int(np.count_nonzero(alpha)),
                text=" ".join(str(node["text"]) for node in nodes),
                line_nodes=nodes,
                confidence=round(min((float(info["line"].confidence) for info in convertible), default=0.0), 4),
                coverage=coverage,
                fill=nodes[0]["fill"] if nodes else "#000000",
                keep_image=keep_image,
                action="text" if converted else "png",
                reason=reason,
            )
        )

    if len(layers) > MAX_LAYERS:
        warnings.append(f"图层数 {len(layers)} 超过上限 {MAX_LAYERS}，按面积保留最大的部分")
        keep = sorted(layers, key=lambda item: item.box[2] * item.box[3], reverse=True)[:MAX_LAYERS]
        layers = sorted(keep, key=lambda item: item.z)
        for index, layer in enumerate(layers):
            layer.z = index
            layer.id = f"L{index + 1:02d}"
    return layers, warnings, background


# ---------------------------------------------------------------------------
# SVG 组装
# ---------------------------------------------------------------------------
def _num(value: float) -> str:
    return str(round(float(value), 1)).rstrip("0").rstrip(".")


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _as_png(value) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    import io

    buffer = io.BytesIO()
    value.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def build_svg(background, layers: list[Layer], canvas: tuple[int, int], args) -> bytes:
    ET.register_namespace("", SVG_NS)
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": str(canvas[0]),
            "height": str(canvas[1]),
            "viewBox": f"0 0 {canvas[0]} {canvas[1]}",
        },
    )
    if background is not None:
        ET.SubElement(
            root,
            f"{{{SVG_NS}}}image",
            {
                "id": "bg",
                "data-decompose-layer": "bg",
                "href": _data_uri(_as_png(background)),
                "x": "0",
                "y": "0",
                "width": str(canvas[0]),
                "height": str(canvas[1]),
            },
        )

    text_index = 0
    for layer in layers:
        x, y, width, height = layer.box
        if layer.keep_image and layer.png:
            ET.SubElement(
                root,
                f"{{{SVG_NS}}}image",
                {
                    "id": f"layer-{layer.id}",
                    "data-decompose-layer": str(layer.z + 1),
                    "href": _data_uri(layer.png),
                    "x": str(x),
                    "y": str(y),
                    "width": str(width),
                    "height": str(height),
                },
            )
        if layer.action == "text":
            for node in layer.line_nodes:
                text_index += 1
                font_size = float(node["font_size"])
                attributes = {
                    "id": f"text-{text_index:02d}",
                    "data-decompose-layer": "text",
                    "data-box-w": str(node.get("w", "")),
                    "x": _num(node["x"]),
                    "y": _num(node["y"]),
                    "font-family": f"'{args.font_family}'" if "'" not in args.font_family else args.font_family,
                    "font-size": _num(font_size),
                    "font-weight": "700" if font_size >= 64 else "400",
                    "fill": str(node.get("fill") or layer.fill),
                    "textLength": _num(node.get("w") or 0),
                    "lengthAdjust": "spacingAndGlyphs",
                }
                element = ET.SubElement(root, f"{{{SVG_NS}}}text", attributes)
                element.text = str(node["text"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# 合成与渲染
# ---------------------------------------------------------------------------
def composite_png(raw_layers, layers: list[Layer], background, out: Path) -> None:
    Image = _import("PIL.Image", "请先运行安装脚本")
    if raw_layers:
        base = raw_layers[0].convert("RGBA")
        for layer in raw_layers[1:]:
            base = Image.alpha_composite(base, layer.convert("RGBA"))
        base.convert("RGB").save(out, quality=92)
        return
    if background is None:
        return
    base = background.copy().convert("RGBA")
    import io

    for layer in layers:
        if not layer.keep_image or not layer.png:
            continue
        with Image.open(io.BytesIO(layer.png)) as image:
            base.alpha_composite(image.convert("RGBA"), dest=(layer.local_box[0], layer.local_box[1]))
    base.convert("RGB").save(out, quality=92)


def render_svg(svg_path: Path, png_path: Path, canvas: tuple[int, int]) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return "未安装 playwright，跳过渲染（可直接用浏览器打开 out.svg）"
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="msedge", headless=True)
            except Exception:  # noqa: BLE001
                browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                viewport={"width": canvas[0], "height": canvas[1]}, device_scale_factor=1
            )
            page.goto(svg_path.resolve().as_uri(), wait_until="load", timeout=120000)
            page.wait_for_timeout(300)
            page.locator("svg").first.screenshot(path=str(png_path), timeout=120000)
            browser.close()
        return f"已渲染 {png_path.name}"
    except Exception as exc:  # noqa: BLE001
        return f"渲染失败：{exc}"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_source(path: Path, max_side: int):
    Image = _import("PIL.Image", "请先运行安装脚本")
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.load()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"无法读取图片 {path}：{exc}") from exc
    original = image.size
    long_side = max(original)
    scale = 1.0
    if long_side > max_side:
        ratio = max_side / long_side
        resized = image.resize(
            (max(1, round(original[0] * ratio)), max(1, round(original[1] * ratio))),
            Image.LANCZOS,
        )
        scale = original[0] / resized.size[0]
    else:
        resized = image.copy()
    return image, resized, original, scale


def run(args) -> int:
    source = Path(args.image).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"图片不存在：{source}")
    env = build_env(args)
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else (
        ROOT / "data" / "decompose" / f"smoke-{now_stamp()}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "layers").mkdir(exist_ok=True)

    mem = ProcessMemory()
    timings = Timings()
    mem.sample()
    started = time.perf_counter()
    say(f"输入：{source}")
    say(f"输出：{out_dir}")

    original, resized, original_size, scale = load_source(source, args.max_side)
    resized.save(out_dir / "input_resized.png")
    say(f"原图 {original_size[0]}x{original_size[1]} → 推理 "
        f"{resized.size[0]}x{resized.size[1]}（scale={scale:.4f}）")

    with timings.run("load_models（LayerD + LaMa）"):
        layerd = _import("layerd", "请先运行安装脚本安装 LayerD")
        pipeline = layerd.LayerDPipeline(device="cpu")
    mem.sample()

    with timings.run("decompose（逐层剥离与补底）"):
        result = pipeline(resized, max_iterations=args.max_iterations)
    mem.sample()

    engine = None
    if args.ocr != "none":
        with timings.run("load_ocr"):
            engine = build_ocr(args)

    with timings.run("ocr（整图识别与图层判定）"):
        layers, warnings, background = build_layers(result, scale, args, resized, engine)
    mem.sample()
    say(f"前景图层：{len(layers)} 个（文字行已按层归属判定）")

    canvas = (round(original_size[0]), round(original_size[1]))
    with timings.run("compose（组装 SVG）"):
        svg_bytes = build_svg(background, layers, canvas, args)
    (out_dir / "out.svg").write_bytes(svg_bytes)
    composite_png(list(getattr(result, "layers", None) or []), layers, background,
                  out_dir / "composite.png")
    for layer in layers:
        if layer.png:
            (out_dir / "layers" / f"{layer.id}.png").write_bytes(layer.png)

    render_note = ""
    if args.render:
        with timings.run("render（Edge 截图）"):
            render_note = render_svg(out_dir / "out.svg", out_dir / "render.png", canvas)

    elapsed = time.perf_counter() - started
    text_layers = [layer for layer in layers if layer.action == "text"]
    summary = {
        "image": str(source),
        "original_size": original_size,
        "inference_size": resized.size,
        "scale": round(scale, 4),
        "max_side": args.max_side,
        "max_iterations": args.max_iterations,
        "device": "cpu",
        "layers": len(layers),
        "text_layers": len(text_layers),
        "text_nodes": sum(len(layer.line_nodes) for layer in layers),
        "image_layers": len([layer for layer in layers if layer.keep_image]),
        "svg_bytes": len(svg_bytes),
        "elapsed_s": round(elapsed, 1),
        "timings": timings.items,
        "peak_rss_mb": round(mem.peak_mb, 1) if mem.peak_mb else None,
        "ocr": args.ocr,
        "ocr_lines": _LAST_OCR_DEBUG,
        "render": render_note,
        "warnings": warnings,
        "env": env.describe(),
        "versions": {
            "python": sys.version.split()[0],
            "torch": module_version("torch"),
            "layerd": module_version("layerd"),
            "paddleocr": module_version("paddleocr"),
            "opencv": module_version("opencv-contrib-python"),
        },
    }
    detail = _layer_details(layers)
    (out_dir / "report.json").write_text(
        json.dumps({**summary, "layer_details": detail}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(_markdown(summary, detail), encoding="utf-8")

    step("冒烟结果")
    say(f"  图层：{summary['layers']}（文字层 {summary['text_layers']} / 文字节点 "
        f"{summary['text_nodes']} / 保留位图 {summary['image_layers']}）")
    for item in timings.items:
        say(f"  - {item['stage']:<26s} {float(item['ms']) / 1000:7.1f}s")
    say(f"  总计：{summary['elapsed_s']}s；SVG {human_bytes(len(svg_bytes))}；"
        f"峰值内存 {summary['peak_rss_mb']} MB")
    if render_note:
        say(f"  {render_note}")
    for warning in warnings:
        say(f"  [warn] {warning}")
    say("")
    say("产物：")
    say(f"  SVG      {out_dir / 'out.svg'}")
    say(f"  合成预览 {out_dir / 'composite.png'}")
    say(f"  逐层 PNG {out_dir / 'layers'}")
    say(f"  报告     {out_dir / 'report.md'}")
    say("")
    say("下一步：用浏览器打开 out.svg 对照原图；确认耗时/效果后即可进入 S3（后端服务）。")
    mem.close()
    return 0


def _layer_details(layers: list[Layer]) -> list[dict]:
    return [
        {
            "id": layer.id,
            "action": layer.action,
            "box": list(layer.box),
            "local_box": list(layer.local_box),
            "opaque_px": layer.opaque,
            "text": layer.text,
            "text_nodes": len(layer.line_nodes),
            "keep_image": layer.keep_image,
            "confidence": layer.confidence,
            "coverage": layer.coverage,
            "fill": layer.fill,
            "reason": layer.reason,
            "png_bytes": len(layer.png),
        }
        for layer in layers
    ]


def _markdown(summary: dict, details: list[dict]) -> str:
    lines = [
        "# 本地拆层冒烟报告",
        "",
        f"- 输入：`{summary['image']}`",
        f"- 原图：{summary['original_size'][0]}×{summary['original_size'][1]}；"
        f"推理：{summary['inference_size'][0]}×{summary['inference_size'][1]}"
        f"（scale={summary['scale']}，最大边 {summary['max_side']}）",
        f"- 设备：CPU；最大迭代：{summary['max_iterations']}",
        f"- 总耗时：**{summary['elapsed_s']} s**；峰值内存：**{summary['peak_rss_mb']} MB**",
        f"- 图层：{summary['layers']}（文字层 {summary['text_layers']} / 文字节点 "
        f"{summary['text_nodes']} / 保留位图 {summary['image_layers']}）；"
        f"SVG {human_bytes(summary['svg_bytes'])}",
        "",
        "## 阶段耗时",
        "",
        "| 阶段 | 秒 |",
        "| --- | ---: |",
    ]
    for item in summary["timings"]:
        lines.append(f"| {item['stage']} | {float(item['ms']) / 1000:.1f} |")
    lines += [
        "",
        "## 图层明细",
        "",
        "| 图层 | 动作 | 原图坐标 (x,y,w,h) | 不透明像素 | OCR 文本 | 置信度 | 覆盖率 | 颜色 | 判定说明 |",
        "| --- | --- | --- | ---: | --- | ---: | ---: | --- | --- |",
    ]
    for item in details:
        box = ", ".join(str(round(value, 1)) for value in item["box"])
        text = (item["text"] or "").replace("|", "\\|")
        lines.append(
            f"| {item['id']} | {item['action']} | {box} | {item['opaque_px']} | "
            f"{text} | {item['confidence']:.2f} | {item['coverage']:.2f} | "
            f"{item['fill']} | {item['reason']} |"
        )
    if summary["warnings"]:
        lines += ["", "## 告警", ""] + [f"- {item}" for item in summary["warnings"]]
    ocr_lines = summary.get("ocr_lines") or []
    if ocr_lines:
        lines += [
            "",
            "## OCR 行判定",
            "",
            "| 文本 | 置信度 | 行高(px) | 颜色方差 | 归属层不透明度 | 是否转文字 |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
        for item in ocr_lines:
            text = str(item.get("text") or "").replace("|", "\\|")
            lines.append(
                f"| {text} | {float(item.get('confidence', 0)):.2f} | {item.get('height')} | "
                f"{item.get('spread')} | {item.get('opacity')} | "
                f"{'是' if item.get('converted') else '否（保留 PNG）'} |"
            )
    lines += ["", "## 环境", "", "```json",
              json.dumps(summary["env"], ensure_ascii=False, indent=2), "```"]
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="单图 CPU 拆层冒烟（LayerD + PaddleOCR）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--image", help="输入海报（png/jpg/webp）")
    parser.add_argument("--out-dir", default="", help="产物目录，默认 data/decompose/smoke-<时间>")
    parser.add_argument("--cache-root", default="", help="模型缓存根目录，默认 <项目>/data/models")
    parser.add_argument("--ascii-cache", default="",
                        help="Paddle/LaMa 的纯 ASCII 缓存目录（默认按需自动选择）")
    parser.add_argument("--max-side", type=int, default=1024, choices=[1024, 1280],
                        help="推理最大边长")
    parser.add_argument("--max-iterations", type=int, default=3, help="LayerD 最大剥离迭代次数")
    parser.add_argument("--device", default="cpu", choices=["cpu"], help="只允许 CPU")
    parser.add_argument("--ocr", default="paddle", choices=["paddle", "rapidocr", "none"],
                        help="OCR 引擎")
    parser.add_argument("--convert-text", dest="convert_text",
                        action=argparse.BooleanOptionalAction, default=True,
                        help="是否把文字行转成可编辑 text（--no-convert-text 关闭）")
    parser.add_argument("--keep-png-text", action="store_true",
                        help="即使识别成功也保留 PNG（只测拆层效果时用）")
    parser.add_argument("--font-family", default="Alibaba PuHuiTi 3.0",
                        help="转出 text 使用的字体族")
    parser.add_argument("--hf-endpoint", default="",
                        help="HF 镜像地址，默认官方 https://huggingface.co（hf-mirror 可能因重定向校验失败）")
    parser.add_argument("--no-hf-mirror", action="store_true", help="兼容旧参数：强制官方源")
    parser.add_argument("--render", action="store_true", help="额外用 Edge 截图")
    parser.add_argument("--download-models", action="store_true", help="只预下载模型后退出")
    parser.add_argument("--self-check", action="store_true", help="只做依赖自检后退出")
    parser.add_argument("--skip-ocr", action="store_true", help="下载模型时跳过 PaddleOCR 预热")
    parser.add_argument("--json", action="store_true", help="自检时输出 JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)
    if not args.cache_root:
        args.cache_root = str(ROOT / "data" / "models")
    env = build_env(args)
    if args.self_check:
        return self_check(args, env)
    if args.download_models:
        return download_models(args, env)
    if not args.image:
        say("缺少 --image。示例：")
        say("  .\\.venv-decompose\\Scripts\\python.exe scripts\\decompose_smoke.py "
            "--image .\\老师照片\\海报.png")
        say("或先执行 --self-check / --download-models")
        return 2
    if args.device != "cpu":
        raise SystemExit("本方案只允许 device=cpu")
    return run(args)


if __name__ == "__main__":
    try:
        exit_code = main()
    except SystemExit as exc:
        exit_code = int(exc.code or 0) if isinstance(exc.code, int) else 1
    # 直接退出，跳过 Paddle/torch 的析构流程（在 Windows 上可能把退出码改成 1）
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(exit_code)

