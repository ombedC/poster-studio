"""人脸检测服务。

策略：优先 MediaPipe（精度高、对姿态友好），当前 wheel 不再提供
``mediapipe.solutions`` 时自动降级到 OpenCV Haar 级联。两套实现都只用本地模型
文件，不需要联网，适合"照片不出机器"的本地工作台。

对外只暴露 :class:`FaceDetector` 协议、:func:`get_detector` 单例工厂，
FastAPI 通过 ``Depends(get_detector)`` 注入，测试里可以直接覆盖。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Protocol, Sequence

import cv2
import numpy as np

from app.config import settings
from app.errors import FaceDetectionUnavailable
from app.services.image_processor import FaceBox, ensure_rgba

logger = logging.getLogger(__name__)


def _ascii_safe_copy(path: Path) -> Path:
    """OpenCV / MediaPipe 的 C++ 加载器不支持非 ASCII 路径（中文目录必崩）。

    这里把模型复制到 :attr:`settings.cache_dir` 下的 ASCII 文件名，再交给底层库。
    就本项目放在 ``D:\\...\\中文目录`` 的情况而言，这一步是必需的。
    """
    try:
        str(path).encode("ascii")
        return path
    except UnicodeEncodeError:
        pass

    stat = path.stat()
    digest = hashlib.sha1(
        f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:12]
    cache_root = Path(settings.cache_dir)
    try:
        str(cache_root).encode("ascii")
    except UnicodeEncodeError as exc:
        raise FaceDetectionUnavailable(
            "找不到可写的纯英文缓存目录，模型无法交给 OpenCV/MediaPipe；"
            "请设置 POSTER_CACHE_DIR 指向英文路径（例如 D:\\poster-cache）"
        ) from exc

    target = cache_root / f"poster-model-{digest}{path.suffix}"
    if not target.exists() or target.stat().st_size != stat.st_size:
        target.write_bytes(path.read_bytes())
        logger.info("已把模型复制到 ASCII 缓存路径：%s", target)
    return target


class FaceDetector(Protocol):
    """检测器协议：返回图中最大的人脸，没有则返回 None。"""

    name: str

    def detect(self, image: np.ndarray) -> FaceBox | None:  # pragma: no cover - 协议定义
        ...


def _downscale(image: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    """检测前把长边缩到 max_side 以内，返回 (图像, 缩放系数)。"""
    rgb = ensure_rgba(image)[..., :3]
    height, width = rgb.shape[:2]
    longest = max(height, width)
    if max_side <= 0 or longest <= max_side:
        return rgb, 1.0
    scale = max_side / float(longest)
    resized = cv2.resize(
        rgb,
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def _largest(boxes: Sequence[FaceBox]) -> FaceBox | None:
    return max(boxes, key=lambda box: box.area, default=None)


class MediaPipeFaceDetector:
    """MediaPipe 人脸检测器。

    新版 mediapipe（0.10.30+）移除了 ``mp.solutions``，所以优先用官方 Tasks API +
    ``blaze_face_short_range.tflite``；旧版 wheel 则回退到 ``mp.solutions``。
    """

    name = "mediapipe"

    def __init__(self, min_confidence: float = 0.5, model_selection: int = 1) -> None:
        self._mode = ""
        self._min_confidence = float(min_confidence)
        self._model_selection = model_selection
        self._tasks = None
        self._solutions_detector = None

        try:
            self._init_tasks()
            self._mode = "tasks"
            logger.info("MediaPipe 使用 Tasks API")
            return
        except Exception as exc:
            logger.warning("MediaPipe Tasks 不可用（%s），尝试旧版 solutions", exc)

        self._init_solutions()
        self._mode = "solutions"
        logger.info("MediaPipe 使用 legacy solutions")

    def _init_tasks(self) -> None:
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        model_path = Path(settings.mediapipe_model)
        if not model_path.exists():
            raise FaceDetectionUnavailable(f"缺少 MediaPipe 人脸模型：{model_path}")
        options = vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(_ascii_safe_copy(model_path))),
            running_mode=vision.RunningMode.IMAGE,
            min_detection_confidence=self._min_confidence,
        )
        self._tasks = vision.FaceDetector.create_from_options(options)
        self._mp = mp

    def _init_solutions(self) -> None:
        try:
            import mediapipe as mp
        except Exception as exc:  # pragma: no cover - 依赖缺失时走降级
            raise FaceDetectionUnavailable(f"mediapipe 未安装或导入失败：{exc}") from exc

        solutions = getattr(mp, "solutions", None)
        face_detection = getattr(solutions, "face_detection", None) if solutions else None
        if face_detection is None:
            raise FaceDetectionUnavailable(
                "当前 mediapipe 既没有 Tasks 模型文件，也没有 solutions.face_detection"
            )
        self._solutions_detector = face_detection.FaceDetection(
            model_selection=self._model_selection,
            min_detection_confidence=self._min_confidence,
        )

    def detect(self, image: np.ndarray) -> FaceBox | None:
        if self._mode == "tasks":
            return self._detect_tasks(image)
        return self._detect_solutions(image)

    def _detect_tasks(self, image: np.ndarray) -> FaceBox | None:
        rgb, scale = _downscale(image, settings.face_max_side)
        mp_image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB,
            data=np.ascontiguousarray(rgb),
        )
        result = self._tasks.detect(mp_image)
        height, width = rgb.shape[:2]

        boxes: list[FaceBox] = []
        for detection in result.detections:
            bbox = detection.bounding_box
            x1 = max(0, int(bbox.origin_x))
            y1 = max(0, int(bbox.origin_y))
            x2 = min(width, x1 + int(bbox.width))
            y2 = min(height, y1 + int(bbox.height))
            if x2 <= x1 or y2 <= y1:
                continue
            confidence = None
            if detection.categories:
                confidence = float(detection.categories[0].score)
            boxes.append(
                FaceBox(
                    x=int(round(x1 / scale)),
                    y=int(round(y1 / scale)),
                    w=max(1, int(round((x2 - x1) / scale))),
                    h=max(1, int(round((y2 - y1) / scale))),
                    confidence=confidence,
                    backend=self.name,
                )
            )
        return _largest(boxes)

    def _detect_solutions(self, image: np.ndarray) -> FaceBox | None:
        rgb, scale = _downscale(image, settings.face_max_side)
        results = self._solutions_detector.process(np.ascontiguousarray(rgb))
        detections = getattr(results, "detections", None)
        if not detections:
            return None

        height, width = rgb.shape[:2]
        boxes: list[FaceBox] = []
        for detection in detections:
            bbox = detection.location_data.relative_bounding_box
            x1 = max(0.0, float(bbox.xmin))
            y1 = max(0.0, float(bbox.ymin))
            x2 = min(1.0, float(bbox.xmin) + float(bbox.width))
            y2 = min(1.0, float(bbox.ymin) + float(bbox.height))
            if x2 <= x1 or y2 <= y1:
                continue
            confidence = None
            try:
                confidence = float(detection.score[0])
            except (IndexError, TypeError, AttributeError):
                confidence = None
            boxes.append(
                FaceBox(
                    x=int(round(x1 * width / scale)),
                    y=int(round(y1 * height / scale)),
                    w=max(1, int(round((x2 - x1) * width / scale))),
                    h=max(1, int(round((y2 - y1) * height / scale))),
                    confidence=confidence,
                    backend=self.name,
                )
            )
        return _largest(boxes)

    def close(self) -> None:
        if self._tasks is not None:
            self._tasks.close()
        if self._solutions_detector is not None:
            self._solutions_detector.close()


class YuNetFaceDetector:
    """OpenCV DNN 的 YuNet 轻量人脸模型。

    模型只有 ~230 KB，精度明显好于 Haar，且不依赖 mediapipe；
    OpenCV 5 移除 ``CascadeClassifier`` 后这条路径依然可用。
    """

    name = "opencv-yunet"

    def __init__(self, model_path: str | Path | None = None) -> None:
        if not hasattr(cv2, "FaceDetectorYN_create"):
            raise FaceDetectionUnavailable("当前 OpenCV 版本没有 FaceDetectorYN（需 >= 4.5.3）")
        self._model_path = Path(model_path or settings.yunet_model)
        if not self._model_path.exists():
            raise FaceDetectionUnavailable(f"缺少 YuNet 模型文件：{self._model_path}")
        self._runtime_path = _ascii_safe_copy(self._model_path)
        self._detector = None
        self._input_size: tuple[int, int] | None = None

    def _ensure(self, width: int, height: int):
        if self._detector is None or self._input_size != (width, height):
            self._detector = cv2.FaceDetectorYN_create(
                str(self._runtime_path), "", (width, height), 0.6, 0.3, 5000
            )
            self._input_size = (width, height)
        return self._detector

    def detect(self, image: np.ndarray) -> FaceBox | None:
        rgb, scale = _downscale(image, settings.face_max_side)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)  # YuNet 用 BGR
        height, width = bgr.shape[:2]
        _, faces = self._ensure(width, height).detect(np.ascontiguousarray(bgr))
        if faces is None:
            return None

        boxes: list[FaceBox] = []
        for row in faces:
            x, y, w, h = (float(value) for value in row[:4])
            confidence = float(row[-1])
            x1 = max(0.0, x)
            y1 = max(0.0, y)
            x2 = min(float(width), x + w)
            y2 = min(float(height), y + h)
            if x2 <= x1 or y2 <= y1:
                continue
            boxes.append(
                FaceBox(
                    x=int(round(x1 / scale)),
                    y=int(round(y1 / scale)),
                    w=max(1, int(round((x2 - x1) / scale))),
                    h=max(1, int(round((y2 - y1) / scale))),
                    confidence=confidence,
                    backend=self.name,
                )
            )
        return _largest(boxes)


class HaarFaceDetector:
    """OpenCV Haar 级联兜底：正脸 + 侧脸（含镜像），只依赖本地 xml。"""

    name = "opencv-haar"

    def __init__(self) -> None:
        if not hasattr(cv2, "CascadeClassifier"):
            raise FaceDetectionUnavailable("当前 OpenCV 版本已移除 CascadeClassifier（5.x）")
        frontal_path = self._find("haarcascade_frontalface_default.xml")
        profile_path = self._find("haarcascade_profileface.xml")
        self._frontal = self._load(frontal_path) if frontal_path else None
        self._profile = self._load(profile_path) if profile_path else None
        if self._frontal is None and self._profile is None:
            raise FaceDetectionUnavailable(
                "找不到 Haar 级联模型（OpenCV 5 已不再随包附带）；"
                "请安装 mediapipe，或把 xml 放到 HAAR_CASCADE_DIR 指定的目录"
            )

    def _find(self, filename: str) -> Path | None:
        """依次在自定义目录、cv2 自带目录里找级联文件。"""
        candidates = [Path(settings.haar_cascade_dir), Path(cv2.data.haarcascades)]
        for directory in candidates:
            candidate = directory / filename
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _load(path: Path) -> cv2.CascadeClassifier | None:
        if not path.exists():
            return None
        cascade = cv2.CascadeClassifier(str(_ascii_safe_copy(path)))
        return None if cascade.empty() else cascade

    def detect(self, image: np.ndarray) -> FaceBox | None:
        rgb, scale = _downscale(image, settings.face_max_side)
        gray = cv2.equalizeHist(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
        height, width = gray.shape[:2]
        min_side = max(24, int(round(min(height, width) * 0.06)))

        rectangles: list[tuple[int, int, int, int]] = []
        if self._frontal is not None:
            rectangles.extend(
                self._frontal.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side)
                )
            )
        if self._profile is not None and not rectangles:
            found = self._profile.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side)
            )
            rectangles.extend(found)
            mirrored = cv2.flip(gray, 1)
            for x, y, w, h in self._profile.detectMultiScale(
                mirrored, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side)
            ):
                rectangles.append((width - x - w, y, w, h))

        boxes = [
            FaceBox(
                x=int(round(x / scale)),
                y=int(round(y / scale)),
                w=max(1, int(round(w / scale))),
                h=max(1, int(round(h / scale))),
                confidence=None,
                backend=self.name,
            )
            for x, y, w, h in rectangles
        ]
        return _largest(boxes)


class UnavailableDetector:
    """检测后端全都不可用时的占位实现。

    不在依赖注入阶段抛 503，否则非法文件也会得到"服务不可用"而不是格式错误；
    真正调用 :meth:`detect` 时才抛，让调用方决定是报错还是退化为顶部居中。
    """

    name = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def detect(self, image: np.ndarray) -> FaceBox | None:
        raise FaceDetectionUnavailable(self.reason)


_detector: FaceDetector | None = None

# 默认顺序：YuNet 的人脸框更接近"头部轮廓"（包含额头与部分头发），
# 按 0.45h 留白时不会切到头发；MediaPipe BlazeFace 的框偏紧，作为备选。
DEFAULT_BACKEND_ORDER = ("yunet", "mediapipe", "haar")
_BACKENDS = {
    "mediapipe": lambda: MediaPipeFaceDetector(settings.face_min_confidence),
    "yunet": YuNetFaceDetector,
    "haar": HaarFaceDetector,
}


def build_detector(order: Sequence[str] | None = None) -> FaceDetector:
    """按给定顺序尝试构建检测器，返回第一个可用的后端。"""
    if order:
        names = [name.strip().lower() for name in order]
    elif settings.face_backend and settings.face_backend != "auto":
        names = [settings.face_backend]
    else:
        names = list(DEFAULT_BACKEND_ORDER)

    problems: list[str] = []
    for name in names:
        factory = _BACKENDS.get(name)
        if factory is None:
            problems.append(f"{name}: 未知后端")
            continue
        try:
            detector = factory()
            logger.info("人脸检测后端：%s", name)
            return detector
        except Exception as exc:  # noqa: BLE001 - 逐个尝试，失败就换下一个
            logger.warning("%s 不可用（%s）", name, exc)
            problems.append(f"{name}: {exc}")

    raise FaceDetectionUnavailable(
        "没有可用的人脸检测后端（" + "；".join(problems) + "）。"
        "可运行 `python -m scripts.fetch_models` 下载 YuNet 模型。"
    )


def get_detector() -> FaceDetector:
    """进程内单例，避免重复加载模型。FastAPI 依赖注入入口。

    后端不可用时返回 :class:`UnavailableDetector` 而不是在这里抛 503。
    """
    global _detector
    if _detector is None:
        try:
            _detector = build_detector()
        except FaceDetectionUnavailable as exc:
            logger.error("人脸检测后端不可用：%s", exc)
            _detector = UnavailableDetector(str(exc))
    return _detector


def reset_detector() -> None:
    """测试或热更新时清空单例。"""
    global _detector
    _detector = None
