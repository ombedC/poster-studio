"""下载可选的人脸检测模型（不需要 mediapipe 也能检测）。

* YuNet：约 230 KB，精度好、速度快，OpenCV >= 4.5.3 即可用。
* Haar 级联：OpenCV 4.x 自带；OpenCV 5 起官方包不再附带，这里补一份。

用法::

    python -m scripts.fetch_models
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

MEDIAPIPE_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)
YU_NET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
HAAR_BASE = "https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/"
HAAR_FILES = ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")


def download(url: str, target: Path) -> None:
    if target.exists() and target.stat().st_size > 0:
        print(f"已存在，跳过：{target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 {url}")
    with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    print(f"  -> {target}（{target.stat().st_size / 1024:.0f} KB）")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 YuNet / Haar 人脸检测模型")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = parser.parse_args()

    if args.force:
        Path(settings.yunet_model).unlink(missing_ok=True)
        Path(settings.mediapipe_model).unlink(missing_ok=True)
        for name in HAAR_FILES:
            (Path(settings.haar_cascade_dir) / name).unlink(missing_ok=True)

    download(MEDIAPIPE_URL, Path(settings.mediapipe_model))
    download(YU_NET_URL, Path(settings.yunet_model))
    for name in HAAR_FILES:
        download(HAAR_BASE + name, Path(settings.haar_cascade_dir) / name)

    print("\n完成。检测优先级：mediapipe → YuNet → Haar。")
    print("只装 mediapipe 时也可以不带 YuNet / Haar。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
