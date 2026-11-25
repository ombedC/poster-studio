"""集中配置：全部可通过环境变量覆盖，默认值保证在本机离线可用。"""

from __future__ import annotations

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """极简 .env 读取（不覆盖已有环境变量），避免为 MVP 引入额外依赖。"""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file(BASE_DIR / ".env")

_BRACKETED_HOST = re.compile(r"^\[([0-9A-Fa-f:.]+)\](?::\d+)?$")


def _sanitize_no_proxy(value: str) -> str:
    """``[::1]`` 这类写法会让 httpx 直接抛 InvalidURL，统一改成裸 IPv6。"""
    tokens: list[str] = []
    for raw in re.split(r"[,\s]+", (value or "").strip()):
        if not raw:
            continue
        match = _BRACKETED_HOST.fullmatch(raw)
        tokens.append(match.group(1) if match else raw)
    return ",".join(tokens)


for _proxy_key in ("NO_PROXY", "no_proxy"):
    _proxy_value = os.environ.get(_proxy_key)
    if _proxy_value and "[" in _proxy_value:
        os.environ[_proxy_key] = _sanitize_no_proxy(_proxy_value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _is_ascii(path: Path) -> bool:
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _pick_cache_dir() -> Path:
    """挑一个可写、且路径为纯 ASCII 的缓存目录。

    OpenCV / MediaPipe 的 C++ 加载器在 Windows 上不认中文路径，而项目本身可能
    就放在中文目录里，所以模型要先复制到纯英文路径。系统 %TEMP% 在部分加固过的
    机器上不可写，因此按顺序实测多个候选目录。
    """
    candidates: list[Path] = []
    env_dir = os.getenv("POSTER_CACHE_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    if _is_ascii(BASE_DIR.parent):
        candidates.append(BASE_DIR.parent / ".poster-cache")
    for name in ("TEMP", "TMP"):
        value = os.getenv(name)
        if value and _is_ascii(Path(value)):
            candidates.append(Path(value) / "poster-workbench")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-probe"
            probe.write_bytes(b"ok")
            probe.unlink()
            return candidate
        except Exception:  # noqa: BLE001 - 逐个尝试，失败就换下一个
            continue
    return BASE_DIR / "data" / ".cache"


class Settings:
    """运行时配置。

    测试时在导入 app 之前设置 ``DATA_DIR`` 指向临时目录，即可避免污染 data/。
    """

    def __init__(self) -> None:
        self.app_name = "教培海报自动化工作台"
        self.version = "0.4.0"
        self.api_prefix = "/api/v1"

        self.base_dir = BASE_DIR
        self.static_dir = BASE_DIR / "static"
        self.data_dir = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
        self.output_dir = self.data_dir / "outputs"
        self.model_dir = self.data_dir / "models"

        # OpenCV / MediaPipe 只能从纯 ASCII 路径加载模型，这里统一放一份缓存副本
        self.cache_dir = _pick_cache_dir()
        # 本机 %TEMP% 可能不可写（Playwright / pooch 会直接崩），统一改到缓存目录
        playwright_tmp = self.cache_dir / "tmp"
        playwright_tmp.mkdir(parents=True, exist_ok=True)
        for key in ("TMPDIR", "TEMP", "TMP"):
            os.environ[key] = str(playwright_tmp)
        # mediapipe 会顺手 import matplotlib；把缓存指向可写目录，
        # 避免在项目根目录生成 matplotlib-xxxx 临时文件夹。
        os.environ.setdefault("MPLCONFIGDIR", str(self.cache_dir / "matplotlib"))
        # rembg 默认存到 ~/.u2net；加固过的机器用户目录可能不可写，改到缓存目录
        os.environ.setdefault("U2NET_HOME", str(self.cache_dir / "rembg"))

        # 模板源文件默认不限制；MAX_TEMPLATE_UPLOAD_MB=0 means unlimited.
        self.max_template_upload_mb = max(0, _env_int("MAX_TEMPLATE_UPLOAD_MB", 0))
        self.max_template_upload_bytes = self.max_template_upload_mb * 1024 * 1024

        # 上传与输出限制
        self.max_upload_mb = _env_int("MAX_UPLOAD_MB", 20)
        self.max_upload_bytes = self.max_upload_mb * 1024 * 1024
        self.min_output_side = 64
        self.max_output_side = _env_int("MAX_OUTPUT_SIDE", 4096)
        self.allowed_mimes = frozenset(
            {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp"}
        )

        # rembg
        self.rembg_model = os.getenv("REMBG_MODEL", "u2net")
        self.rembg_required = _env_bool("REMBG_REQUIRED", False)
        self.rembg_alpha_matting = _env_bool("REMBG_ALPHA_MATTING", False)

        # 人脸检测
        self.face_min_confidence = float(os.getenv("FACE_MIN_CONFIDENCE", "0.5"))
        self.face_max_side = _env_int("FACE_MAX_SIDE", 1600)
        # auto = yunet → mediapipe → haar；也可强制指定 mediapipe / yunet / haar
        self.face_backend = os.getenv("FACE_BACKEND", "auto").strip().lower()
        # OpenCV 5 起不再随包附带 Haar 级联，允许把 xml 放到自定义目录
        self.haar_cascade_dir = os.getenv(
            "HAAR_CASCADE_DIR", str(self.model_dir / "haarcascades")
        )
        # YuNet 轻量人脸模型（约 230 KB），比 Haar 稳、比 MediaPipe 更易分发
        self.yunet_model = os.getenv(
            "YUNET_MODEL", str(self.model_dir / "face_detection_yunet_2023mar.onnx")
        )
        # MediaPipe Tasks API 的人脸模型（约 230 KB）
        self.mediapipe_model = os.getenv(
            "MEDIAPIPE_MODEL", str(self.model_dir / "blaze_face_short_range.tflite")
        )

        # ------------------------------------------------------------------
        # 海报模板 / 大模型 / 无头浏览器
        # ------------------------------------------------------------------
        # 兼容 OpenAI 格式的服务；优先 LLM_*，其次 OPENAI_*
        self.llm_api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        self.llm_base_url = (
            os.getenv("LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        self.llm_model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        self.llm_timeout = _env_int("LLM_TIMEOUT", 120)
        self.llm_max_tokens = _env_int("LLM_MAX_TOKENS", 20000)
        self.llm_vision = _env_bool("LLM_VISION", True)
        self.llm_json_mode = _env_bool("LLM_JSON_MODE", True)

        self.template_dir = self.data_dir / "templates"
        self.poster_cache_dir = self.data_dir / "cache" / "photos"
        self.font_dir = Path(
            os.getenv("FONT_DIR", str(BASE_DIR / "static" / "Fonts"))
        ).resolve()
        self.fonts_enabled = _env_bool("FONTS_ENABLED", True)
        self.playwright_channel = os.getenv("PLAYWRIGHT_CHANNEL", "msedge")
        self.playwright_headless = _env_bool("PLAYWRIGHT_HEADLESS", True)
        self.poster_render_scale = _env_int("POSTER_RENDER_SCALE", 2)
        self.poster_max_scale = 3

        self.enable_cors = _env_bool("ENABLE_CORS", True)

        # 转曲文字复活（上传 / 重新识别时自动执行）
        self.revive_outlined_text = _env_bool("REVIVE_OUTLINED_TEXT", True)
        self.ocr_enabled = _env_bool("OCR_ENABLED", True)
        self.ocr_min_confidence = float(os.getenv("OCR_MIN_CONFIDENCE", "0.62"))
        self.revival_min_line_height = _env_int("REVIVAL_MIN_LINE_HEIGHT", 8)
        self.revival_min_iou = float(os.getenv("REVIVAL_MIN_IOU", "0.45"))

        # ------------------------------------------------------------------
        # 火山方舟 Seedream 生图 / 局改 / 拆图层
        # ------------------------------------------------------------------
        # 旧版「AI 生成 SVG」工作台仅由该开关控制界面显隐，代码与接口始终保留。
        self.enable_ai_svg = _env_bool("ENABLE_AI_SVG", False)
        self.ark_api_key = os.getenv("ARK_API_KEY", "").strip()
        self.ark_base_url = (
            os.getenv("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"
        ).rstrip("/")
        self.seedream_model = os.getenv("SEEDREAM_MODEL", "doubao-seedream-5-5-261028")
        self.seedream_enabled = _env_bool("SEEDREAM_ENABLED", True)
        self.seedream_timeout = _env_int("SEEDREAM_TIMEOUT", 300)
        self.seedream_max_references = min(10, max(1, _env_int("SEEDREAM_MAX_REFERENCES", 10)))
        self.seedream_project_dir = self.output_dir / "seedream"
        # 4 插槽提示词 / Skill 目录：改文件即生效，可用环境变量指向自定义目录
        self.seedream_prompts_dir = Path(
            os.getenv("SEEDREAM_PROMPTS_DIR", str(BASE_DIR / "prompts" / "seedream"))
        ).resolve()


settings = Settings()
