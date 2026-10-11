"""Original script generation with structural and originality checks."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import prompts
from .config import Settings
from .models import Analysis, Candidate, Script


class ScriptRejected(RuntimeError):
    """Valid JSON, but violates production limits or originality checks."""


class Claim(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(max_length=500)
    verdict: Literal["true", "false", "uncertain", "story"]
    reason: str = Field(default="", max_length=500)


class FactCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    claims: list[Claim] = Field(default_factory=list, max_length=40)

    @property
    def problems(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict in ("false", "uncertain")]


def fact_check(checker, script: Script) -> FactCheck:
    """Independent model (NVIDIA analyst) checks every spoken/shown statement."""
    p = prompts.load("fact_check_v1")
    view = {"title": script.title, "hook": script.hook, "description": script.description,
            "scenes": [{"narration": s.narration, "on_screen_text": s.on_screen_text} for s in script.scenes]}
    out, _ = checker.structured([{"role": "user", "content": p.render(
        language=script.language, script_json=prompts.untrusted(view, 6000))}], FactCheck, max_tokens=1500)
    return out


def _shingles(text: str, n: int = 4) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def overlap_ratio(ours: str, theirs: str, n: int = 4) -> float:
    a, b = _shingles(ours, n), _shingles(theirs, n)
    return len(a & b) / len(a) if a else 0.0


def originality_issues(script: Script, ref: Candidate, analysis: Analysis, max_overlap: float = 0.15) -> list[str]:
    ours = " ".join([script.title, script.hook, script.narration, script.description,
                     *(s.narration + " " + s.on_screen_text for s in script.scenes)])
    theirs = " ".join([ref.title, ref.description, *analysis.on_screen_text])
    issues = []
    r = overlap_ratio(ours, theirs)
    if r > max_overlap:
        issues.append(f"text overlap with reference {r:.0%} > {max_overlap:.0%}")
    if overlap_ratio(script.title, ref.title, 3) > 0.5 or script.title.strip().lower() == ref.title.strip().lower():
        issues.append("title too close to reference title")
    return issues


def pick_format(s: Settings, key: str) -> str:
    """Deterministic per job, rotates across jobs to avoid one repeated template."""
    import hashlib

    formats = s.content_formats or ["короткий ролик"]
    return formats[int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(formats)]


def generate_script(s: Settings, text_client, candidate: Candidate, analysis: Analysis,
                    fmt: str = "короткий ролик", checker=None) -> tuple[Script, str]:
    if s.publish_made_for_kids is True:
        raise ScriptRejected("made-for-kids content is out of scope for this pipeline")
    p = prompts.load("script_v5")
    max_scene = min(s.max_scene_seconds, 20.0)
    min_scenes = max(1, int(-(-s.target_min_s // max_scene)))  # ceil
    analysis_view = analysis.model_dump(include={
        "mode", "topic", "hook", "structure", "pacing", "plot_beats", "characters", "setting",
        "visual_description", "audience_interest_hypothesis", "unknowns", "limitations"})
    analysis_view["reference_made_for_kids"] = candidate.made_for_kids
    msg = p.render(language=s.content_language, audience=s.target_audience, min_s=s.target_min_s,
                   max_s=s.target_max_s, min_scenes=min_scenes, max_scenes=min(10, min_scenes + 3),
                   max_scene_s=round(max_scene, 2), region="center", made_for_kids="false", format=fmt,
                   analysis_json=prompts.untrusted(analysis_view))
    messages = [{"role": "user", "content": msg}]
    script, _ = text_client.structured(messages, Script, max_tokens=s.nim_text_max_tokens)
    errors = script.check_limits(s.target_min_s, s.target_max_s, max_scene)
    if errors:
        retry = messages + [{"role": "user", "content": "Previous answer violated limits: " + "; ".join(errors)
                             + ". Return a corrected full JSON object."}]
        script, _ = text_client.structured(retry, Script, max_tokens=s.nim_text_max_tokens)
        errors = script.check_limits(s.target_min_s, s.target_max_s, max_scene)
        if errors:
            raise ScriptRejected("; ".join(errors))
    if script.language.lower()[:2] != s.content_language.lower()[:2]:
        raise ScriptRejected(f"script language {script.language} != {s.content_language}")
    issues = originality_issues(script, candidate, analysis)
    if issues:
        raise ScriptRejected("originality: " + "; ".join(issues))
    if checker is not None and s.fact_check:
        fc = fact_check(checker, script)
        if fc.problems:
            listed = "; ".join(f"«{c.text}» — {c.verdict}: {c.reason}" for c in fc.problems)
            retry = messages + [{"role": "assistant", "content": script.model_dump_json()},
                                {"role": "user", "content": "A fact-checker rejected these statements: " + listed
                                 + ". Rewrite them so that nothing false or doubtful is stated as real-world fact"
                                 " (tell it as a story instead). Return the full corrected JSON object."}]
            script, _ = text_client.structured(retry, Script, max_tokens=s.nim_text_max_tokens)
            errors = script.check_limits(s.target_min_s, s.target_max_s, max_scene)
            if errors:
                raise ScriptRejected("; ".join(errors))
            fc = fact_check(checker, script)
            if fc.problems:
                raise ScriptRejected("fact-check: " + "; ".join(f"«{c.text}» ({c.verdict})" for c in fc.problems))
    return script, p.ref


def compose_keyframe_prompt(script: Script) -> str:
    """Opening image for i2v_chain: the subject in the story's single setting."""
    return (f"{script.subject.strip().rstrip('.')}, {script.setting.strip().rstrip('.')}. "
            f"{script.visual_style.strip().rstrip('.')}. Vertical 9:16 composition, the subject large and centered, "
            "plain clothing without patches, badges or prints, no screens or displays, "
            "clean image with no lettering, no signs, no captions, no logos and no watermark.")


def compose_video_prompt(script: Script, scene) -> str:
    """Deterministic final prompt: identical subject/style text in every scene keeps them consistent.

    Wan 2.2 on Replicate has no negative_prompt, so the "no lettering" constraint is stated positively."""
    region = {"center": "centered", "top": "in the upper part of the frame",
              "bottom": "in the lower part of the frame"}[scene.important_region]
    parts = [scene.generation_prompt.strip().rstrip("."),
             f"Main subject: {script.subject.strip().rstrip('.')}"
             + (f", {scene.subject_state.strip().rstrip('.')}" if scene.subject_state else ""),
             f"Style: {script.visual_style.strip().rstrip('.')}",
             f"Single continuous shot, vertical 9:16 composition, main subject {region}",
             "Clean image with no lettering, no captions, no logos and no watermark"]
    return ". ".join(parts) + "."


def frames_for(duration_s: float, fps: int, min_frames: int, max_frames: int) -> int:
    """Generator frame count for a scene within the model's documented range.

    A clip longer than the scene (min_frames) is trimmed in render."""
    return max(min_frames, min(max_frames, int(round(duration_s * fps)) + 1))
