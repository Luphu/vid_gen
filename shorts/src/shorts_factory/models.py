"""Typed, validated data models shared by all stages.

Model outputs are parsed into these classes; anything that fails validation is rejected,
never "repaired" silently.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobState(StrEnum):
    DISCOVERED = "discovered"
    ANALYZED = "analyzed"
    SCRIPTED = "scripted"
    GENERATED = "generated"
    RENDERED = "rendered"
    VALIDATED = "validated"
    UPLOADED = "uploaded"
    PUBLISHED = "published"
    FAILED = "failed"
    REVIEW_REQUIRED = "review_required"
    GENERATION_UNKNOWN = "generation_unknown"
    UPLOAD_UNKNOWN = "upload_unknown"
    DONE_DRY_RUN = "done_dry_run"


PIPELINE_ORDER = [
    JobState.DISCOVERED, JobState.ANALYZED, JobState.SCRIPTED, JobState.GENERATED,
    JobState.RENDERED, JobState.VALIDATED, JobState.UPLOADED, JobState.PUBLISHED,
]
TERMINAL = {JobState.PUBLISHED, JobState.FAILED, JobState.DONE_DRY_RUN}
BLOCKED = {JobState.REVIEW_REQUIRED, JobState.GENERATION_UNKNOWN, JobState.UPLOAD_UNKNOWN}


class FormatStatus(StrEnum):
    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    UNKNOWN = "unknown"
    NOT_SHORT = "not_short"


class Evidence(Strict):
    field: str
    snippet: str = Field(max_length=300)


class AIClassification(Strict):
    ai_likelihood: float | None = Field(default=None, ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)
    classification_source: Literal[
        "none", "metadata_keywords", "creator_disclosure", "vision_model", "combined"
    ] = "none"
    note: str = "heuristic signal, not proof; absence of a label does not prove absence of AI"


class SearchScope(Strict):
    keywords: list[str]
    channel_allowlist: list[str]
    region: str
    language: str
    published_after: datetime
    order: str = "viewCount"
    max_results: int
    pages_fetched: int = 0
    fetched_at: datetime


class Candidate(Strict):
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{6,20}$")
    title: str
    description: str = ""
    channel_id: str
    channel_title: str = ""
    published_at: datetime
    duration_s: float | None = None
    tags: list[str] = Field(default_factory=list)
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    default_language: str | None = None
    contains_synthetic_media: bool | None = None
    format_status: FormatStatus = FormatStatus.UNKNOWN
    format_reason: str = ""
    ai: AIClassification = Field(default_factory=AIClassification)
    fetched_at: datetime
    search_rank: int = 0


class MediaManifest(Strict):
    """Rights metadata for reference media; files without it are not used."""

    video_id: str
    license: str = Field(min_length=3)
    provided_by: str = Field(min_length=2)
    files: list[str] = Field(min_length=1)
    is_short: bool | None = None
    notes: str = ""


class SceneObservation(Strict):
    t_start: float | None = None
    description: str
    composition: str | None = None
    on_screen_text: str | None = None


class Analysis(Strict):
    mode: Literal["metadata_only", "full_media"]
    video_id: str
    topic: str
    hook: str | None = None
    structure: list[str] = Field(default_factory=list)
    pacing: str | None = None
    scene_changes: int | None = None
    scenes: list[SceneObservation] = Field(default_factory=list)
    on_screen_text: list[str] = Field(default_factory=list)
    audio_role: str | None = None
    audience_interest_hypothesis: str | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    sources: list[str] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)
    ai: AIClassification | None = None

    @model_validator(mode="after")
    def _metadata_only_has_no_media_claims(self) -> Analysis:
        if self.mode == "metadata_only":
            invented = [
                n for n in ("pacing", "scene_changes", "audio_role")
                if getattr(self, n) is not None
            ]
            if invented or self.scenes or self.on_screen_text or self.structure:
                raise ValueError(f"metadata_only analysis must not describe media: {invented or 'scenes'}")
        return self


# Text-to-video models generate every scene independently and cannot read text reliably:
# references to other scenes, multi-panel layouts and any lettering produce garbage.
_FORBIDDEN_IN_SCENE = re.compile(
    r"(?i)\b(split[- ]screen|side[- ]by[- ]side|collage|before[- ]and[- ]after|diptych|"
    r"the same|same (?:object|stone|item|subject)|previous scene|earlier scene|"
    r"text|texts|caption|captions|subtitle|subtitles|letters?|words?|logo|logos|watermark|sign|signs|"
    r"label|labels|title|typography)\b")


_CYRILLIC = re.compile(r"[\u0400-\u04FF]")


def _english_only(v: str | None, field: str) -> str | None:
    if v and _CYRILLIC.search(v):
        raise ValueError(f"{field} must be written in English (video models follow English prompts best)")
    return v


class Scene(Strict):
    index: int = Field(ge=0)
    duration_s: float = Field(gt=0.9, le=20)
    # Scene-specific action, setting and camera only; subject and style are added by code.
    generation_prompt: str = Field(min_length=20, max_length=700)
    # How the recurring subject looks in THIS scene if it changed (English), e.g. "now melted into an ingot".
    subject_state: str | None = Field(default=None, max_length=200)
    negative_prompt: str | None = Field(default=None, max_length=1000)
    narration: str = Field(default="", max_length=400)
    on_screen_text: str = Field(default="", max_length=80)
    important_region: Literal["center", "top", "bottom"] = "center"

    @field_validator("generation_prompt")
    @classmethod
    def _video_safe(cls, v: str) -> str:
        _english_only(v, "generation_prompt")
        m = _FORBIDDEN_IN_SCENE.search(v)
        if m:
            raise ValueError(f"generation_prompt must not contain {m.group(0)!r}: each scene is generated "
                             "independently, as one continuous shot, without any lettering")
        return v

    @field_validator("subject_state")
    @classmethod
    def _state_en(cls, v: str | None) -> str | None:
        return _english_only(v, "subject_state")


class Disclosure(Strict):
    contains_synthetic_media: Literal[True] = True
    made_for_kids: bool = False
    disclosure_text: str = Field(min_length=5, max_length=200)


class Script(Strict):
    concept: str = Field(min_length=10, max_length=500)
    target_audience: str = Field(min_length=3, max_length=200)
    language: str = Field(min_length=2, max_length=10)
    hook: str = Field(min_length=3, max_length=200)
    # Canonical English description of the recurring main subject, repeated verbatim in every scene prompt.
    subject: str = Field(min_length=15, max_length=400)
    # One shared English visual style line (lighting, palette, lens, realism) for all scenes.
    visual_style: str = Field(min_length=10, max_length=250)
    scenes: list[Scene] = Field(min_length=1, max_length=10)
    narration: str = Field(default="", max_length=2000)
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=10, max_length=4500)
    tags: list[str] = Field(default_factory=list, max_length=20)
    disclosure: Disclosure

    @field_validator("subject", "visual_style")
    @classmethod
    def _visual_fields_en(cls, v: str, info) -> str:
        return _english_only(v, info.field_name)  # type: ignore[return-value]

    @field_validator("title")
    @classmethod
    def _no_angle_brackets(cls, v: str) -> str:
        if "<" in v or ">" in v:  # YouTube rejects < > in title
            raise ValueError("title must not contain angle brackets")
        return v

    @field_validator("tags")
    @classmethod
    def _tags_len(cls, v: list[str]) -> list[str]:
        if sum(len(t) for t in v) > 450:
            raise ValueError("tags too long")
        return v

    @property
    def total_duration_s(self) -> float:
        return sum(s.duration_s for s in self.scenes)

    def check_limits(self, min_s: float, max_s: float, max_scene_s: float) -> list[str]:
        errors = []
        if not (min_s - 0.01 <= self.total_duration_s <= max_s + 0.01):
            errors.append(f"total duration {self.total_duration_s:.1f}s outside [{min_s}, {max_s}]")
        for s in self.scenes:
            if s.duration_s > max_scene_s:
                errors.append(f"scene {s.index} {s.duration_s}s exceeds generator limit {max_scene_s:.1f}s")
        if [s.index for s in self.scenes] != list(range(len(self.scenes))):
            errors.append("scene indexes must be 0..n-1 in order")
        return errors


class QCCheck(Strict):
    name: str
    ok: bool
    detail: str = ""


class QCReport(Strict):
    ok: bool
    checks: list[QCCheck]
    file_key: str | None = None
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool | None = None

    @property
    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.ok]


class UploadRequest(Strict):
    title: str
    description: str
    tags: list[str]
    category_id: str
    privacy_status: Literal["private", "unlisted", "public"]
    publish_at: datetime | None = None
    self_declared_made_for_kids: bool
    contains_synthetic_media: bool
    default_language: str

    @model_validator(mode="after")
    def _publish_at_private(self) -> UploadRequest:
        if self.publish_at is not None and self.privacy_status != "private":
            raise ValueError("publishAt requires privacyStatus=private (YouTube schedules from private)")
        return self

    def to_resource(self) -> dict:
        status: dict = {
            "privacyStatus": self.privacy_status,
            "selfDeclaredMadeForKids": self.self_declared_made_for_kids,
            "containsSyntheticMedia": self.contains_synthetic_media,
        }
        if self.publish_at:
            status["publishAt"] = self.publish_at.isoformat().replace("+00:00", "Z")
        return {
            "snippet": {
                "title": self.title,
                "description": self.description,
                "tags": self.tags,
                "categoryId": self.category_id,
                "defaultLanguage": self.default_language,
                "defaultAudioLanguage": self.default_language,
            },
            "status": status,
        }
