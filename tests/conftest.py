"""pytest 全局夹具。

关键点：在导入 app 之前把 ``DATA_DIR`` 指到临时目录，测试产物不会写进 data/。
"""

from __future__ import annotations

import os
import shutil
import sys
from uuid import uuid4
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在导入 app 之前设置 DATA_DIR。注意某些加固机器上 %TEMP% 不可写，
# 在项目内创建独立目录。显式 mkdir 可直接报告权限错误。
_TEST_DATA_DIR = ROOT / "data" / "tests" / f"run-{uuid4().hex}"
_TEST_DATA_DIR.mkdir(parents=True, exist_ok=False)
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["REMBG_REQUIRED"] = "false"
os.environ["ENABLE_CORS"] = "false"
# 测试不串真网：.env 里的 key 会被 _load_env_file 的 setdefault 跳过
for _key in ("LLM_API_KEY", "OPENAI_API_KEY", "ARK_API_KEY"):
    os.environ[_key] = ""


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


from app.services.image_processor import FaceBox, encode_png  # noqa: E402


def _make_portrait(width: int = 1200, height: int = 1600) -> np.ndarray:
    """合成一张"人像"：灰色背景 + 肤色脑袋 + 深色肩膀。"""
    image = np.zeros((height, width, 4), dtype=np.uint8)
    image[...] = (226, 232, 240, 255)
    cv2.ellipse(image, (width // 2, 380), (120, 150), 0, 0, 360, (244, 208, 180, 255), -1)
    cv2.rectangle(image, (width // 2 - 180, 520), (width // 2 + 180, height - 1), (60, 80, 120, 255), -1)
    return image


@pytest.fixture()
def portrait() -> np.ndarray:
    return _make_portrait()


@pytest.fixture()
def png_bytes(portrait: np.ndarray) -> bytes:
    return encode_png(portrait)


@pytest.fixture()
def stub_face() -> FaceBox:
    return FaceBox(x=480, y=260, w=240, h=260, confidence=0.98, backend="stub")


class StubDetector:
    """测试替身：返回固定人脸框（或 None），不依赖 mediapipe。"""

    name = "stub"

    def __init__(self, face: FaceBox | None) -> None:
        self.face = face

    def detect(self, image: np.ndarray) -> FaceBox | None:
        return self.face


def _override_detector(face: FaceBox | None):
    from app.api.images import get_detector
    from app.main import app

    detector = StubDetector(face)
    app.dependency_overrides[get_detector] = lambda: detector
    return detector


@pytest.fixture()
def stub_detector(stub_face: FaceBox):
    from app.api.images import get_detector
    from app.main import app

    _override_detector(stub_face)
    yield stub_face
    app.dependency_overrides.pop(get_detector, None)


@pytest.fixture()
def null_detector():
    from app.api.images import get_detector
    from app.main import app

    _override_detector(None)
    yield None
    app.dependency_overrides.pop(get_detector, None)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
