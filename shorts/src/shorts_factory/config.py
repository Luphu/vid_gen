"""Configuration from environment variables.

All knobs are env-driven so the same image runs locally, in tests and in k3s.
Secrets are wrapped in SecretStr and never appear in repr/logs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator, model_validator

NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"


class ConfigError(RuntimeError):
    """Configuration is missing or unsafe for the requested operation."""


def _csv(v: str | None) -> list[str]:
    return [p.strip() for p in (v or "").split(",") if p.strip()]


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    if v.lower() in {"1", "true", "yes", "on"}:
        return True
    if v.lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"not a boolean: {v!r}")


def _opt_bool(v: str | None) -> bool | None:
    return None if v in (None, "") else _bool(v, False)


class Budget(BaseModel):
    """Daily/per-job limits. Generation is disabled until a positive budget is set explicitly."""

    text_requests_per_day: int = 40
    vision_requests_per_day: int = 60
    image_requests_per_day: int = 30
    tts_requests_per_day: int = 40
    video_requests_per_day: int = 0
    video_requests_per_job: int = 6
    yt_search_calls_per_day: int = 10
    yt_units_per_day: int = 300
    yt_uploads_per_day: int = 1


class Settings(BaseModel):
    app_env: Literal["dev", "test", "prod"] = "dev"
    database_url: SecretStr = SecretStr("sqlite:///./var/shorts.db")
    work_dir: str = "./var/work"
    max_work_dir_bytes: int = 2 * 1024**3
    artifact_retention_days: int = 14
    backup_retention_days: int = 30
    youtube_data_retention_days: int = 25
    job_lease_seconds: int = 3 * 3600
    daily_video_limit: int = 1

    dry_run: bool = True
    auto_publish: bool = False
    provider_mode: Literal["nvidia", "mock"] = "nvidia"  # legacy switch: mock replaces every provider
    text_provider: Literal["nvidia", "replicate"] = "replicate"
    video_provider: Literal["nvidia", "replicate"] = "replicate"

    # NVIDIA
    nvidia_api_key: SecretStr | None = None
    nim_text_url: str = NVIDIA_CHAT_URL
    nim_text_model: str = "nvidia/nemotron-3-super-120b-a12b"
    nim_text_response_format: Literal["none", "json_object"] = "none"
    nim_text_max_tokens: int = 2048
    nim_text_disable_thinking: bool = True  # chat_template_kwargs.enable_thinking=false (verified on nemotron-3)
    nim_vision_url: str = NVIDIA_CHAT_URL
    # verified 2026-10-12 with this account: llama-3.2-11b-vision answers in ~7 s; cosmos-reason2-8b,
    # gemma-3-12b-it and vila return 404 (not enabled), llama-3.2-90b / gemma-4 / nemotron omni time out
    nim_vision_model: str = "meta/llama-3.2-11b-vision-instruct"
    nim_vision_input: Literal["image_url", "video_frames"] = "image_url"
    nim_video_url: str = ""  # no default on purpose: hosted cosmos3-nano URL is not documented
    nim_video_async_status_url: str = ""  # e.g. https://.../status/{request_id}; empty = no polling
    nim_video_resolution: str = "720_9_16"
    nim_video_fps: int = 24
    nim_video_max_frames: int = 197
    nim_video_timeout_s: float = 1800.0
    nim_video_extra: dict = Field(default_factory=dict)
    nim_connect_timeout_s: float = 10.0
    nim_read_timeout_s: float = 120.0
    nim_max_concurrency: int = 1
    nim_max_retries: int = 4
    nim_max_response_bytes: int = 96 * 1024**2

    # Replicate (https://replicate.com/docs/reference/http)
    replicate_api_token: SecretStr | None = None
    replicate_api_base: str = "https://api.replicate.com/v1"
    replicate_output_host_suffix: str = "replicate.delivery"  # outputs that need the API token
    # Some models (e.g. flux-schnell, seen 2026-10-12) return pre-signed Cloudflare R2 URLs: allowed, but the
    # API token is never sent there.
    replicate_signed_output_suffixes: list[str] = Field(default_factory=lambda: ["r2.cloudflarestorage.com"])
    replicate_text_model: str = "qwen/qwen3-235b-a22b-instruct-2507"
    replicate_video_model: str = "wan-video/wan-2.2-t2v-fast"
    replicate_video_resolution: str = "720p"
    replicate_video_fps: int = 16          # model prices by duration at 16 fps
    replicate_video_min_frames: int = 81   # schema: 81..121
    replicate_video_max_frames: int = 121
    replicate_video_extra: dict = Field(default_factory=dict)
    # i2v_chain: one keyframe (text-to-image) -> scene 0 animated from it, scene k from the last frame of k-1;
    # keeps the same subject in every scene. t2v: every scene generated independently from text.
    video_mode: Literal["t2v", "i2v_chain"] = "i2v_chain"
    replicate_image_model: str = "black-forest-labs/flux-schnell"
    replicate_i2v_model: str = "wan-video/wan-2.2-i2v-fast"
    max_keyframe_attempts: int = 3
    # Quality gates using the NVIDIA models: lettering in generated images/clips, factual claims in the script
    check_lettering: bool = True
    fact_check: bool = True
    replicate_tts_model: str = "minimax/speech-02-turbo"
    replicate_tts_voice: str = "Russian_ReliableMan"
    replicate_poll_interval_s: float = 3.0

    budget: Budget = Field(default_factory=Budget)

    # Trend discovery
    trend_source: Literal["youtube", "fixture"] = "youtube"
    # kids: popular Shorts declared made-for-kids (status.madeForKids), top N by views; ai: AI-content search
    trend_mode: Literal["ai", "kids"] = "ai"
    trend_top_n: int = 30
    trend_require_made_for_kids: bool = False
    select_require_ai: bool = True
    # Keep candidates whose audio language (metadata) or title script matches; empty = no filter.
    # regionCode/relevanceLanguage only bias search ranking, they do not filter.
    trend_languages: list[str] = Field(default_factory=list)
    trend_fixture_file: str = ""
    youtube_api_key: SecretStr | None = None
    trend_keywords: list[str] = Field(default_factory=lambda: ["ai generated", "ai video"])
    trend_channel_allowlist: list[str] = Field(default_factory=list)
    trend_region: str = "US"
    trend_language: str = "en"
    trend_window_hours: int = 72
    trend_max_results: int = 25
    trend_max_pages: int = 1
    ai_keywords: list[str] = Field(
        default_factory=lambda: [
            "ai generated", "ai-generated", "made with ai", "#aiart", "#aivideo", "#ai", "sora", "veo",
            "runway", "kling", "midjourney", "pika", "luma", "stable diffusion", "нейросеть", "сгенерировано",
        ]
    )
    ai_min_likelihood: float = 0.6
    ai_review_low: float = 0.3

    # Reference media
    reference_source: Literal["none", "local", "s3"] = "none"
    reference_local_dir: str = "./var/reference"
    reference_s3_prefix: str = "reference/"
    analysis_max_frames: int = 8
    analysis_frame_size: int = 448
    max_reference_bytes: int = 200 * 1024**2

    # Storage
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_dir: str = "./var/storage"
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = "shorts/"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None

    # Video / render
    target_min_s: float = 15.0
    target_max_s: float = 30.0
    out_width: int = 720
    out_height: int = 1280
    out_fps: int = 30
    ffmpeg_threads: int = 2
    ffmpeg_timeout_s: int = 900
    max_output_bytes: int = 256 * 1024**2
    black_scene_max_ratio: float = 0.9
    burn_subtitles: bool = True
    # Dynamic editing: every generated clip is cut into short shots with alternating punch-ins
    edit_max_shot_s: float = 2.0
    edit_first_shot_s: float = 1.2
    edit_punch_in: float = 1.15
    edit_max_avg_shot_s: float = 2.5
    fit_mode: Literal["pad", "crop"] = "pad"
    font_file: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # Content
    content_language: str = "ru"
    target_audience: str = "широкая аудитория 13+, интересующаяся технологиями и необычными фактами; не детский контент"
    tts_provider: Literal["none", "mock", "replicate"] = "none"
    # Story formats: a trend is a situation + emotion, so the new video retells one (fact lists lost the trend)
    content_formats: list[str] = Field(default_factory=lambda: [
        "неудача, паника и неожиданное решение", "превращение героя или предмета", "мини-история с поворотом в конце",
        "соревнование или спор с неожиданным победителем", "маленький герой против большой проблемы"])
    trend_refresh_hours: int = 12  # reuse unused candidates fetched within this window instead of searching again
    music_file_key: str = ""  # storage key of a licensed track; its manifest must exist next to it

    # YouTube upload
    youtube_client_id: str = ""
    youtube_client_secret: SecretStr | None = None
    youtube_refresh_token: SecretStr | None = None
    youtube_channel_id: str = ""
    youtube_api_base: str = "https://www.googleapis.com/youtube/v3"
    youtube_upload_url: str = "https://www.googleapis.com/upload/youtube/v3/videos"
    youtube_token_url: str = "https://oauth2.googleapis.com/token"  # noqa: S105 (URL, not a secret)
    youtube_category_id: str = "28"  # Science & Technology
    upload_chunk_bytes: int = 8 * 1024 * 1024
    upload_uri_key: SecretStr | None = None

    # Publication consent (used only with AUTO_PUBLISH=true)
    publish_consent_channel_id: str = ""
    publish_privacy: Literal["private", "unlisted", "public"] = "private"
    publish_made_for_kids: bool | None = None
    publish_time_utc: str = ""  # "HH:MM" -> publishAt next slot; empty = immediate with chosen privacy
    publish_daily_limit: int = 1

    @field_validator("upload_chunk_bytes")
    @classmethod
    def _chunk(cls, v: int) -> int:
        if v <= 0 or v % (256 * 1024):
            raise ValueError("UPLOAD_CHUNK_BYTES must be a positive multiple of 256 KiB")
        return v

    @model_validator(mode="after")
    def _prod_guards(self) -> Settings:
        if self.app_env == "prod":
            if self.provider_mode == "mock" or self.tts_provider == "mock":
                raise ValueError("mock providers are forbidden with APP_ENV=prod")
            if self.database_url.get_secret_value().startswith("sqlite"):
                raise ValueError("APP_ENV=prod requires PostgreSQL DATABASE_URL")
        if self.target_min_s > self.target_max_s:
            raise ValueError("TARGET_MIN_S > TARGET_MAX_S")
        return self

    # Generator-neutral video parameters
    @property
    def gen_fps(self) -> int:
        return self.replicate_video_fps if self.video_provider == "replicate" else self.nim_video_fps

    @property
    def gen_min_frames(self) -> int:
        return self.replicate_video_min_frames if self.video_provider == "replicate" else 25

    @property
    def gen_max_frames(self) -> int:
        return self.replicate_video_max_frames if self.video_provider == "replicate" else self.nim_video_max_frames

    @property
    def gen_resolution(self) -> str:
        return self.replicate_video_resolution if self.video_provider == "replicate" else self.nim_video_resolution

    @property
    def max_scene_seconds(self) -> float:
        return self.gen_max_frames / self.gen_fps

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError("missing configuration: " + ", ".join(n.upper() for n in missing))


_ENV_MAP: dict[str, tuple[str, str]] = {}  # field -> (ENV, kind)


def _kind(annotation) -> str:
    """Parsing kind by type structure (not by repr: Literal['unlisted'] must not look like a list)."""
    import types
    import typing

    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return "optbool" if args == [bool] else _kind(args[0])
    if annotation is list or origin is list:
        return "csv"
    if annotation is dict or origin is dict:
        return "json"
    if annotation is bool:
        return "bool"
    return "raw"


def _register() -> None:
    for name, field in Settings.model_fields.items():
        if name != "budget":
            _ENV_MAP[name] = (name.upper(), _kind(field.annotation))


_register()


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    import json

    env = os.environ if env is None else env
    data: dict = {}
    for name, (key, kind) in _ENV_MAP.items():
        if key not in env:
            continue
        raw = env[key]
        if kind == "csv":
            data[name] = _csv(raw)
        elif kind == "json":
            data[name] = json.loads(raw) if raw else {}
        elif kind == "bool":
            data[name] = _bool(raw, Settings.model_fields[name].default)
        elif kind == "optbool":
            data[name] = _opt_bool(raw)
        else:
            if raw == "" and Settings.model_fields[name].default is None:
                continue
            data[name] = raw
    budget = {}
    for bname in Budget.model_fields:
        key = "BUDGET_" + bname.upper()
        if key in env and env[key] != "":
            budget[bname] = int(env[key])
    data["budget"] = Budget(**budget)
    try:
        return Settings(**data)
    except ValidationError as e:
        # Never echo input values: they may contain secrets before SecretStr wrapping.
        msgs = [f"{'.'.join(str(x) for x in err['loc']) or 'settings'}: {err['msg']}" for err in e.errors()]
        raise ConfigError("invalid configuration: " + "; ".join(msgs)) from None
