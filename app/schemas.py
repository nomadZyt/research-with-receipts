"""Contracts for plans, screenshot receipts, and research claims.

A claim can be returned only when it names what was claimed, the source
URL, a quote, and a screenshot receipt with timestamps. ``POST /api/research``
builds claims through these models. ``POST /api/claims/validate`` checks the
same shape without running the sandbox.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_URLS = 5
MAX_QUESTION_LEN = 2000


class PlannerKind(str, Enum):
    stub = "stub"
    vultr_inference = "vultr_inference"


class Verdict(str, Enum):
    pending_vision_check = "pending_vision_check"
    supported = "supported"
    unsupported = "unsupported"


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def require_http_url(value: str) -> str:
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("url must not embed credentials")
    return text


def require_screenshot_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise ValueError("screenshot path must be relative and stay under screenshots/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if ".." in parts:
        raise ValueError("screenshot path must not contain '..'")
    normalized = "/".join(parts)
    if not normalized.startswith("screenshots/"):
        raise ValueError("screenshot path must start with screenshots/")
    if not normalized.endswith(".png"):
        raise ValueError("screenshot path must end with .png")
    return normalized


class PlannedVisit(BaseModel):
    """One URL the planner asks the sandbox to open."""

    model_config = ConfigDict(extra="forbid")

    url: str
    purpose: str = Field(..., min_length=1, max_length=500)
    expected_claim: str = Field(..., min_length=1, max_length=1000)

    @field_validator("url")
    @classmethod
    def url_is_http(cls, value: str) -> str:
        return require_http_url(value)


class ResearchPlan(BaseModel):
    """Planning shape: a question becomes one or more URLs to visit."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_LEN)
    planner: PlannerKind
    model: Optional[str] = None
    planned_at: datetime
    visits: list[PlannedVisit] = Field(..., min_length=1, max_length=MAX_URLS)
    note: Optional[str] = None

    @field_validator("planned_at")
    @classmethod
    def planned_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ScreenshotReceipt(BaseModel):
    """One Playwright capture published under screenshots/."""

    model_config = ConfigDict(extra="forbid")

    screenshot_id: str = Field(..., min_length=1, max_length=80)
    sandbox_id: str = Field(..., min_length=1, max_length=40)
    index: int = Field(..., ge=0, lt=MAX_URLS)
    url: str
    relative_path: str
    title: Optional[str] = None
    quote: Optional[str] = None
    captured_at: datetime
    byte_size: int = Field(..., ge=0)
    ok: bool
    error: Optional[str] = None

    @field_validator("url")
    @classmethod
    def url_is_http(cls, value: str) -> str:
        return require_http_url(value)

    @field_validator("relative_path")
    @classmethod
    def path_under_screenshots(cls, value: str) -> str:
        return require_screenshot_path(value)

    @field_validator("captured_at")
    @classmethod
    def captured_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class Claim(BaseModel):
    """Research claim bound to a source URL and a screenshot receipt.

    ``claim`` is the statement. ``quote`` is text taken from the captured
    page. ``screenshot_path`` points at the PNG receipt. ``captured_at`` is
    when the sandbox took the screenshot; ``created_at`` is when the
    orchestrator assembled the claim.
    """

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(..., min_length=1, max_length=80)
    claim: str = Field(..., min_length=1, max_length=1000)
    url: str
    quote: str = Field(..., min_length=1, max_length=2000)
    screenshot_id: str = Field(..., min_length=1, max_length=80)
    screenshot_path: str
    captured_at: datetime
    created_at: datetime
    source_title: Optional[str] = None
    verdict: Verdict = Verdict.pending_vision_check

    @field_validator("url")
    @classmethod
    def url_is_http(cls, value: str) -> str:
        return require_http_url(value)

    @field_validator("screenshot_path")
    @classmethod
    def path_under_screenshots(cls, value: str) -> str:
        return require_screenshot_path(value)

    @field_validator("captured_at", "created_at")
    @classmethod
    def timestamps_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_LEN)


class SandboxRunRequest(BaseModel):
    """Direct URL → sandbox contract. Does not call Inference."""

    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(..., min_length=1, max_length=MAX_URLS)

    @field_validator("urls")
    @classmethod
    def urls_are_http(cls, values: list[str]) -> list[str]:
        return [require_http_url(value) for value in values]


class ResearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., pattern="^(ok|refused)$")
    question: str
    planner: PlannerKind
    planned_at: datetime
    visits: list[PlannedVisit]
    claims: list[Claim] = Field(default_factory=list)
    receipts: list[ScreenshotReceipt] = Field(default_factory=list)
    refusal: Optional[str] = None
    note: Optional[str] = None
    sandbox_id: Optional[str] = None
    destroyed: bool = False
    screenshots_dir: str = "screenshots"

    @field_validator("planned_at")
    @classmethod
    def planned_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class SandboxRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandbox_id: str
    destroyed: bool
    skipped: bool = False
    receipts: list[ScreenshotReceipt] = Field(default_factory=list)
    refusal: Optional[str] = None
    screenshots_dir: str = "screenshots"
