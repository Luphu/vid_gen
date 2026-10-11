"""Reference analysis: metadata_only or full_media.

The model writes only descriptive fields. Sources, limitations and measured values are set
by code, so the model cannot claim to have seen or heard what it was not given.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import prompts
from .ff import FF, duration_of, stream
from .media import ReferenceMedia
from .models import AIClassification, Analysis, Candidate, Evidence, SceneObservation
from .trends.classify import merge_ai

log = logging.getLogger(__name__)


class MetaOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    topic: str = Field(min_length=3, max_length=300)
    hook: str | None = Field(default=None, max_length=300)
    audience_interest_hypothesis: str | None = Field(default=None, max_length=600)
    confidence: Literal["low", "medium"] = "low"


class MediaOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    topic: str = Field(min_length=3, max_length=300)
    hook: str | None = Field(default=None, max_length=300)
    structure: list[str] = Field(default_factory=list, max_length=12)
    pacing: str | None = Field(default=None, max_length=300)
    scenes: list[SceneObservation] = Field(default_factory=list, max_length=20)
    on_screen_text: list[str] = Field(default_factory=list, max_length=20)
    audience_interest_hypothesis: str | None = Field(default=None, max_length=600)
    visual_ai_cues: list[str] = Field(default_factory=list, max_length=10)
    visual_ai_likelihood: float | None = Field(default=None, ge=0, le=1)
    confidence: Literal["low", "medium", "high"] = "low"


def _metadata(c: Candidate) -> dict:
    return {"title": c.title, "description": c.description[:2500], "tags": c.tags[:30],
            "channel": c.channel_title, "published_at": c.published_at, "duration_s": c.duration_s,
            "view_count": c.view_count, "language": c.default_language}


def analyze_metadata_only(c: Candidate, text_client, language: str) -> tuple[Analysis, str]:
    p = prompts.load("analysis_metadata_v1")
    msg = p.render(language=language, metadata_json=prompts.untrusted(_metadata(c)))
    out, _rid = text_client.structured([{"role": "user", "content": msg}], MetaOut, max_tokens=800)
    a = Analysis(
        mode="metadata_only", video_id=c.video_id, topic=out.topic, hook=out.hook,
        audience_interest_hypothesis=out.audience_interest_hypothesis, confidence=out.confidence,
        sources=[f"youtube_data_api:videos.list fetched_at={c.fetched_at.isoformat()}"],
        limitations=[
            "no media access: frames, editing, pacing, on-screen text, speech and music were not analysed",
            "hook inferred from title/description only",
            "audience-interest reason is a hypothesis of the text model, not a measurement",
        ],
        ai=c.ai,
    )
    return a, p.ref


def analyze_full_media(c: Candidate, media: ReferenceMedia, vision_client, ff: FF, work: Path,
                       language: str, max_frames: int, frame_size: int) -> tuple[Analysis, str]:
    assert media.video is not None
    info = ff.probe(media.video)
    dur = duration_of(info)
    cuts = ff.scene_changes(media.video)
    frames = ff.extract_frames(media.video, work / "frames", max_frames, frame_size, dur)
    if not frames:
        raise RuntimeError("no frames extracted from reference media")
    step = dur / len(frames)
    times = [round(i * step, 1) for i in range(len(frames))]
    audio = stream(info, "audio")
    limitations = ["audio content (speech, music) not analysed by a model; only presence/level measured",
                   f"vision model saw {len(frames)} sampled frames, not the full video"]
    if audio:
        vol = ff.mean_volume_db(media.video)
        audio_role = f"audio track present ({audio.get('codec_name')}), mean volume {vol} dB; content not analysed"
    else:
        audio_role = "no audio track in reference file"
    subs = media.subtitles.read_text(errors="replace")[:3000] if media.subtitles else None
    if subs is None:
        limitations.append("no subtitles provided")

    p = prompts.load("analysis_media_v1")
    meta = _metadata(c) | ({"subtitles": subs} if subs else {})
    msg = p.render(language=language, duration_s=round(dur, 1), scene_changes=len(cuts),
                   frame_times=times, metadata_json=prompts.untrusted(meta))
    content = [{"type": "text", "text": msg}, *vision_client.frames_content(frames)]
    out, _rid = vision_client.structured([{"role": "user", "content": content}], MediaOut, max_tokens=1800)

    vis_ai = None
    if out.visual_ai_likelihood is not None:
        vis_ai = AIClassification(
            ai_likelihood=out.visual_ai_likelihood,
            evidence=[Evidence(field="vision_model", snippet=x[:300]) for x in out.visual_ai_cues] or
                     [Evidence(field="vision_model", snippet="model opinion without listed cues")],
            classification_source="vision_model",
        )
    a = Analysis(
        mode="full_media", video_id=c.video_id, topic=out.topic, hook=out.hook, structure=out.structure,
        pacing=out.pacing, scene_changes=len(cuts), scenes=out.scenes, on_screen_text=out.on_screen_text,
        audio_role=audio_role, audience_interest_hypothesis=out.audience_interest_hypothesis,
        confidence=out.confidence,
        sources=[f"youtube_data_api:videos.list fetched_at={c.fetched_at.isoformat()}",
                 f"reference_media:{media.source} license={media.manifest.license!r}",
                 "ffprobe/ffmpeg measurements"],
        limitations=limitations, ai=merge_ai(c.ai, vis_ai),
    )
    return a, p.ref
