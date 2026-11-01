"""Pydantic schemas shared by the REST layer."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ReviewMode = Literal["off", "risk_only", "manual", "all"]


class ReviewOptions(BaseModel):
    enable_structure: bool = True
    enable_visual: bool = False
    enable_mllm: bool = False
    # None follows current API configuration; bool records a deliberate choice.
    mllm_user_choice: bool | None = None
    review_mode: ReviewMode = "risk_only"
    max_review_pages: int = Field(default=6, ge=0, le=50)
    max_api_calls: int = Field(default=6, ge=0, le=50)
    manual_pages: list[int] = Field(default_factory=list, max_length=50)
    enable_six_detectors: bool = True
    review_strategy: Literal["full_review", "selective_review"] | None = None
    llm_backend: Literal["api", "mock"] = "api"
    purpose: str = Field(default="", max_length=500)
    style: str = Field(default="", max_length=500)
    review_focus: list[str] = Field(default_factory=list, max_length=12)
    original_prompt: str = Field(default="", max_length=12000)

    @field_validator("manual_pages")
    @classmethod
    def _clean_pages(cls, value: list[int]) -> list[int]:
        return sorted({int(v) for v in value if int(v) > 0})

    @model_validator(mode="after")
    def _clip_follows_six_screening(self):
        if self.enable_visual and not self.enable_six_detectors:
            raise ValueError("CLIP是R2六类首筛后的四类辅助复核；开启CLIP时请同时启用六类专用检测。")
        return self


class CreateTaskRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    options: ReviewOptions = Field(default_factory=ReviewOptions)


class StartTaskRequest(BaseModel):
    options: ReviewOptions = Field(default_factory=ReviewOptions)
    name: str | None = Field(default=None, max_length=120)


class RecoveryRequest(BaseModel):
    action: Literal['continue', 'restart']
    llm_backend: Literal['api', 'mock'] | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100,
        pattern=r'^[A-Za-z0-9_-]+$')


class DemoTaskRequest(BaseModel):
    scenario: Literal["full", "risk", "api_failure", "module_missing"] = "full"
    name: str = Field(default="", max_length=120)


class SettingsUpdate(BaseModel):
    api_base_url: str = Field(default="", max_length=400)
    api_model: str = Field(default="", max_length=160)
    api_key: str | None = Field(default=None, max_length=400)
    api_timeout_s: int = Field(default=60, ge=5, le=600)
    api_max_retries: int = Field(default=1, ge=0, le=5)
    api_max_concurrency: int = Field(default=2, ge=1, le=8)
    api_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_calls_per_task: int = Field(default=8, ge=0, le=50)
    allow_private_base_url: bool = False
    allow_insecure_base_url: bool = False
    response_format_json: bool = True


class ConnectionTestRequest(BaseModel):
    mode: Literal["text", "vision"] = "text"
    api_key: str | None = Field(default=None, max_length=400)


class DeleteResponse(BaseModel):
    ok: bool = True
    removed_files: bool = False


class ApiError(BaseModel):
    code: str
    message: str
    detail: Any | None = None


DefectType = Literal["improper_font_sizing", "occluded_content", "cluttered_layout",
                     "content_overflow_cutoff", "excessive_whitespace", "generative_design_anomaly"]
DetectionStatus = Literal["candidate", "no_candidate_in_available_scope", "unknown", "unsupported", "not_checked"]


class DetectorClaim(BaseModel):
    """Original detector observation; a risk score is not a probability."""
    model_config = {"extra": "allow"}
    candidate_id: str
    document_id: str
    slide_id: str
    slide_index: int = Field(ge=1)
    defect_type: DefectType
    subtype: str | None = None
    detector_version: str
    raw_status: DetectionStatus
    score: float | None = Field(default=None, allow_inf_nan=False)
    score_kind: str = "ordinal_not_calibrated_probability"
    object_ids: list[str] = Field(default_factory=list)
    native_bbox: dict[str, float] | None = None
    normalized_bbox: dict[str, float] | None = None
    related_slides: list[int] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    coverage: str = "partial"
    limitations: list[str] = Field(default_factory=list)
    input_version: dict[str, Any] = Field(default_factory=dict)


class InteractiveReviewRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    llm_backend: Literal["api", "mock"] = "mock"


class VerdictRequest(BaseModel):
    """Human confirmation that a finding is a misjudgement (or a restore)."""
    status: Literal["dismissed", "confirmed"] = "dismissed"
    note: str = Field(default="", max_length=300)


class AdviceRequest(BaseModel):
    backend: Literal["api", "rule"] = "api"
