"""Original script generation with structural and originality checks."""

from __future__ import annotations

import re

from . import prompts
from .config import Settings
from .models import Analysis, Candidate, Script


class ScriptRejected(RuntimeError):
    """Valid JSON, but violates production limits or originality checks."""


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
                    fmt: str = "короткий ролик") -> tuple[Script, str]:
    if s.publish_made_for_kids is True:
        raise ScriptRejected("made-for-kids content is out of scope for this pipeline")
    p = prompts.load("script_v4")
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
    return script, p.ref


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
