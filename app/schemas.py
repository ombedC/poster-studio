"""接口出参模型（Pydantic v2）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SizeOut(BaseModel):
    width: int
    height: int


class FaceBoxOut(BaseModel):
    x: int
    y: int
    w: int
    h: int
    confidence: float | None = None
    backend: str = "unknown"


class CropBoxOut(BaseModel):
    left: int
    top: int
    width: int
    height: int


class ArtifactOut(BaseModel):
    """落盘的 PNG 产物。"""

    url: str = Field(description="可直接给 <img> 用的相对地址")
    filename: str
    width: int
    height: int
    bytes: int


class AnalyzeResponse(BaseModel):
    request_id: str
    source: SizeOut
    face: FaceBoxOut | None = None
    face_detected: bool
    backend: str
    target: SizeOut
    suggested_crop: CropBoxOut
    warnings: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)


class CutoutResponse(BaseModel):
    request_id: str
    source: SizeOut
    cutout: ArtifactOut
    timings: dict[str, float] = Field(default_factory=dict)


class ProcessResponse(BaseModel):
    request_id: str
    source: SizeOut
    target: SizeOut
    output: ArtifactOut
    cutout: ArtifactOut | None = None
    face: FaceBoxOut | None = None
    crop: CropBoxOut
    strategy: str
    mode: str = "head_shoulder"
    warnings: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    version: str
    rembg: dict
    data_dir: str
    fonts: dict = Field(default_factory=dict)
    llm: dict = Field(default_factory=dict)
    features: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 海报模板 / 合成
# ---------------------------------------------------------------------------
class PosterParseRequest(BaseModel):
    template_id: str
    text: str = ""
    use_llm: bool = True


class PosterParseResponse(BaseModel):
    template_id: str
    engine: str
    values: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class PosterRenderResponse(BaseModel):
    request_id: str
    template_id: str
    svg_url: str
    png_url: str | None = None
    width: int
    height: int
    preview_only: bool = False
    engine: str = "heuristic"
    values: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    font_adjustments: list[dict] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)


class TemplateSummary(BaseModel):
    template_id: str
    name: str
    canvas: dict
    created_at: str | None = None
    source: str | None = None
    has_preview: bool = False
    text_count: int = 0
    portrait_count: int = 0


class TemplateAnalyzeResponse(BaseModel):
    template_id: str
    name: str
    source: str
    canvas: dict
    text_count: int
    image_count: int
    portraits: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
