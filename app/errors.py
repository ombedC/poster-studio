"""领域异常。

统一由 main.py 的异常处理器转成 ``{"error": code, "message": ..., "detail": ...}``。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "app_error"

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class UnsupportedMediaType(AppError):
    status_code = 415
    code = "unsupported_media_type"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"


class InvalidImage(AppError):
    status_code = 422
    code = "invalid_image"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_error"


class FaceDetectionUnavailable(AppError):
    status_code = 503
    code = "face_detection_unavailable"


class BackgroundRemovalUnavailable(AppError):
    status_code = 503
    code = "background_removal_unavailable"


class LLMUnavailable(AppError):
    status_code = 503
    code = "llm_unavailable"


class BrowserUnavailable(AppError):
    status_code = 503
    code = "browser_unavailable"


class TemplateNotFound(AppError):
    status_code = 404
    code = "template_not_found"


class TemplateInvalid(AppError):
    status_code = 422
    code = "template_invalid"


class VectorArtboardChoice(TemplateInvalid):
    def __init__(self, inspection: dict) -> None:
        super().__init__("请选择要导入的画板", detail=inspection)


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"
