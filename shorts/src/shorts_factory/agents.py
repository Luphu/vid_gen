"""Script by a team of agents on NVIDIA NIM: screenwriter, director and editor, each paired with a critic.

Every round: the roles that have notes revise (roles downstream adapt to their changes), code assembles the
Script (same validators as always), runs the cheap checks (limits, voice-over density, originality) and every
critic scores the FINAL package — the script plus the exact image/video prompts that would be sent to the
generators. A critic's score counts only without open "must_fix" items. The script is accepted when, in one
round, all critics score >= CRITIC_MIN_SCORE, the code checks pass and the fact-check passes; otherwise
``TeamRejected`` (the job goes to review and no video is generated).

Critics run on a different model than the agents, so no model grades its own work.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import prompts
from .config import Settings
from .models import (
    Analysis,
    Candidate,
    Disclosure,
    Scene,
    Script,
    _english_only,
    check_generation_prompt,
    check_visual_field,
)
from .providers.nim_chat import StructuredOutputError
from .scripting import (
    ScriptRejected,
    compose_keyframe_prompt,
    compose_video_prompt,
    fact_check,
    narration_issues,
    narration_range,
    originality_issues,
    wps_values,
)

log = logging.getLogger(__name__)

ROLES = ("writer", "director", "editor")
# prompt versions per role (prompts/agent_<role>_v<N>.md, prompts/critic_<role>_v<N>.md)
AGENT_VERSIONS = {"writer": 3, "director": 4, "editor": 4}
CRITIC_VERSIONS = {"writer": 4, "director": 6, "editor": 5}
MIN_SCENE_S = 2.0  # shorter than one cut of the dynamic edit


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class StoryScene(_Loose):
    index: int = Field(ge=0)
    duration_s: float = Field(gt=0.9, le=20)
    action: str = Field(min_length=5, max_length=1000)
    narration: str = Field(default="", max_length=400)
    on_screen_text: str = Field(default="", max_length=80)


class Story(_Loose):
    concept: str = Field(min_length=10, max_length=500)
    hook: str = Field(min_length=3, max_length=200)
    trend_link: str = Field(min_length=10, max_length=400)
    hero: str = Field(min_length=3, max_length=500)
    location: str = Field(min_length=3, max_length=500)
    scenes: list[StoryScene] = Field(min_length=1, max_length=10)
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=10, max_length=4500)
    tags: list[str] = Field(default_factory=list, max_length=20)


class Shot(_Loose):
    index: int = Field(ge=0)
    generation_prompt: str = Field(min_length=20, max_length=700)
    subject_state: str | None = Field(default=None, max_length=200)
    important_region: Literal["center", "top", "bottom"] = "center"
    must_show: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("generation_prompt")
    @classmethod
    def _video_safe(cls, v: str) -> str:
        return check_generation_prompt(v)

    @field_validator("subject_state")
    @classmethod
    def _state_en(cls, v: str | None) -> str | None:
        return _english_only(v, "subject_state")


class ShotPlan(_Loose):
    subject: str = Field(min_length=15, max_length=400)
    setting: str = Field(min_length=10, max_length=500)
    visual_style: str = Field(min_length=10, max_length=250)
    key_props: list[str] = Field(default_factory=list, max_length=4)
    scenes: list[Shot] = Field(min_length=1, max_length=10)

    @field_validator("subject", "setting", "visual_style")
    @classmethod
    def _visual(cls, v: str, info) -> str:
        return check_visual_field(info.field_name, v)


class EditScene(_Loose):
    index: int = Field(ge=0)
    duration_s: float = Field(gt=0.9, le=20)
    narration: str = Field(default="", max_length=400)
    on_screen_text: str = Field(default="", max_length=80)


class Edit(_Loose):
    hook: str = Field(min_length=3, max_length=200)
    scenes: list[EditScene] = Field(min_length=1, max_length=10)
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=10, max_length=4500)
    tags: list[str] = Field(default_factory=list, max_length=20)
    disclosure_text: str = ""  # editor v4 no longer writes it; the script keeps an internal record
    voice: str = ""


class Criterion(_Loose):
    name: str = ""
    score: int = Field(ge=1, le=10)
    note: str = ""


class Review(_Loose):
    score: int = Field(ge=1, le=10)
    criteria: list[Criterion] = Field(default_factory=list)
    must_fix: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class Lines(_Loose):
    variants: list[str] = Field(min_length=1, max_length=8)


class ViewerReport(_Loose):
    summary: str = ""
    payoff: str = ""
    confusing: list[str] = Field(default_factory=list)
    understood: bool = False


class ViewerMatch(_Loose):
    match: bool = False
    gap: str = ""


def viewer_test(viewer, script: Script, runs: int = 2) -> tuple[bool, dict]:
    """Blind viewer: sees only what the video shows and says, retells it; a second call compares the retelling
    with the writer's intent. Catches stories the critics approve but nobody can follow. The verdict is noisy on
    borderline stories, so ``runs`` independent viewers must all get it (calibrated 2026-10-13)."""
    results = [_one_viewer(viewer, script) for _ in range(max(1, runs))]
    lost = [r for r in results if not r["ok"]]
    out = lost[0] if lost else results[0]
    return not lost, out | {"ok": not lost, "runs": len(results), "passed": len(results) - len(lost)}


def _one_viewer(viewer, script: Script) -> dict:
    seen = {"hero": script.subject, "location": script.setting,
            "scenes": [{"picture": sc.generation_prompt + (f" ({sc.subject_state})" if sc.subject_state else ""),
                        "voice_over": sc.narration, "text_on_screen": sc.on_screen_text} for sc in script.scenes]}
    p = prompts.load("viewer_v1")
    rep, _ = viewer.structured([{"role": "user", "content": p.render(video_json=prompts.untrusted(seen, 8000))}],
                               ViewerReport, max_tokens=800)
    intent = {"concept": script.concept, "hook": script.hook, "title": script.title,
              "ending_voice_over": script.scenes[-1].narration}
    m = prompts.load("viewer_match_v1")
    res, _ = viewer.structured([{"role": "user", "content": m.render(compare_json=prompts.untrusted(
        {"writer_intent": intent, "viewer_retelling": rep.model_dump()}, 6000))}], ViewerMatch, max_tokens=400)
    return {"ok": rep.understood and res.match, "summary": rep.summary, "payoff": rep.payoff,
            "confusing": rep.confusing, "match": res.match, "gap": res.gap}


OUTPUT = {"writer": Story, "director": ShotPlan, "editor": Edit}


class TeamError(ValueError):
    """The package cannot be assembled because of one role's output."""

    def __init__(self, role: str, msg: str):
        super().__init__(msg)
        self.role = role


class TeamRejected(ScriptRejected):
    def __init__(self, msg: str, report: dict):
        super().__init__(msg)
        self.report = report


def assemble(s: Settings, story: Story, plan: ShotPlan, edit: Edit) -> Script:
    idx = [sc.index for sc in story.scenes]
    if [sh.index for sh in plan.scenes] != idx:
        raise TeamError("director", f"shot indexes {[sh.index for sh in plan.scenes]} must equal story scenes {idx}")
    if [e.index for e in edit.scenes] != idx:
        raise TeamError("editor", f"scene indexes {[e.index for e in edit.scenes]} must equal story scenes {idx}")
    shots = {sh.index: sh for sh in plan.scenes}
    try:
        scenes = [Scene(index=e.index, duration_s=e.duration_s, generation_prompt=shots[e.index].generation_prompt,
                        subject_state=shots[e.index].subject_state, narration=e.narration,
                        on_screen_text=e.on_screen_text, important_region=shots[e.index].important_region,
                        must_show=shots[e.index].must_show)
                  for e in edit.scenes]
        return Script(concept=story.concept, target_audience=s.target_audience[:200], language=s.content_language,
                      hook=edit.hook, subject=plan.subject, visual_style=plan.visual_style, setting=plan.setting,
                      trend_link=story.trend_link, scenes=scenes, key_props=plan.key_props,
                      voice=edit.voice or None,
                      narration=" ".join(e.narration for e in edit.scenes),
                      title=edit.title, description=edit.description, tags=edit.tags,
                      disclosure=Disclosure(made_for_kids=False, disclosure_text=edit.disclosure_text
                                            or "AI-generated visuals; labelled via the YouTube synthetic-media flag"))
    except ValidationError as e:
        visual = any(str(p) in ("subject", "setting", "visual_style", "generation_prompt", "subject_state")
                     for err in e.errors() for p in err["loc"])
        raise TeamError("director" if visual else "editor", str(e)[:800]) from e


def package(script: Script, analysis_view: dict) -> dict:
    """What the critics judge: the final script and the exact prompts the generators would receive."""
    return {
        "reference_analysis": analysis_view, "concept": script.concept, "trend_link": script.trend_link,
        "hook": script.hook, "title": script.title, "description": script.description, "tags": script.tags,
        "voice": script.voice, "opening_image_prompt": compose_keyframe_prompt(script),
        "scenes": [{"index": sc.index, "duration_s": sc.duration_s, "narration": sc.narration,
                    "on_screen_text": sc.on_screen_text, "video_prompt": compose_video_prompt(script, sc)}
                   for sc in script.scenes],
    }


MODE_RULES = {
    "story": {
        "reference_rules": (
            "Reference: a popular short from the CHILDREN's segment (analysis below). Keep the CORE SITUATION and "
            "EMOTION of\nits plot and retell it with a new hero, new details and a new resolution for a general "
            "audience 13+ (not for\nkids: no nursery rhymes, toddler toys, baby talk, counting/colour lessons). "
            "Story format: $format."),
        "truth_rules": (
            "Truthfulness: it is a STORY. No real-world claims except common knowledge; no numbers, records, "
            "studies.\nThe characters' own impossible events are fine without any \"imagine\" framing; never state "
            "a false\ngeneral fact about the real world."),
        "extra_criteria": "",
    },
    "edu": {
        "reference_rules": (
            "Reference: a popular EDUCATIONAL short for children (analysis below). Take its TOPIC (animals, space, "
            "the body,\nscience, how things work…) and make a video for a general audience 13+ that teaches ONE "
            "surprising, TRUE fact\nfrom that topic, shown through a mini story with your hero: the hero runs into "
            "the fact, the viewer learns it,\nthe ending lands it with a joke or twist. The fact is stated clearly "
            "in plain words in the narration (by the last\nscene at the latest). Stay on the reference's topic "
            "(a balloon experiment -> air, pressure or static\nelectricity, not apples). Not a lesson for toddlers: "
            "no counting, colours or ABC. Format: $format."),
        "truth_rules": (
            "Truthfulness (critical): the taught fact and every real-world statement must be well-established, "
            "found in any\nencyclopedia (e.g. \"an octopus has three hearts\", \"honey never spoils\"). No "
            "precise numbers unless textbook-famous,\nno records, no studies, no health advice, no \"scientists "
            "say\". If unsure, choose a different fact. An independent\nfact-checker rejects false or doubtful "
            "claims. The hero's own story may be fiction."),
        "extra_criteria": (
            "- fact: the video teaches one clear, surprising, well-established fact from the reference's topic; it "
            "is stated\n  plainly and the story makes it memorable (a missing, vague or doubtful fact, or a fact "
            "from another topic\n  than the reference's, is a blocking defect);\n"),
    },
}


def _values(s: Settings, analysis_view: dict, fmt: str) -> dict:
    max_scene = min(s.max_scene_seconds, 20.0)
    min_scenes = max(1, int(-(-s.target_min_s // max_scene)))
    hint = "Видеоряд сгенерирован ИИ." if s.content_language.lower().startswith("ru") else \
        "Visuals are AI-generated."
    rules = {k: v.replace("$format", fmt) for k, v in MODE_RULES[s.content_mode].items()}
    return {**rules, "language": s.content_language, "audience": s.target_audience, "format": fmt,
            "min_s": s.target_min_s, "max_s": s.target_max_s, "min_scenes": min_scenes,
            "max_scenes": min(10, min_scenes + 3), "max_scene_s": round(max_scene, 2), "disclosure_hint": hint,
            "analysis_json": prompts.untrusted(analysis_view), **wps_values(s),
            "voices": "\n".join(f"  - {k}: {v.get('desc', '')}" for k, v in s.tts_voices.items()) or "  - (default)"}


def _word_targets(s: Settings, story) -> dict:
    """Language models cannot count words reliably: the editor gets the exact range for each scene."""
    out = {}
    for sc in story.scenes:
        lo, hi = narration_range(s, sc.duration_s)
        out[str(sc.index)] = f"{lo}-{hi} words for {sc.duration_s} s (or change duration_s and keep words/s)"
    if s.tts_voices:  # faster voices need more words for the same scene
        out["note"] = ("counts are for the default voice; scale by the chosen voice's speech rate: "
                       + ", ".join(f"{k} {v.get('wps')} w/s" for k, v in s.tts_voices.items())
                       + f" (default {s.narration_words_per_s} w/s)")
    return out


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text))


def fit_narration(s: Settings, client, story: Story, edit: Edit, attempts: int = 2) -> list[str]:
    """Bring every scene's narration into its word range (checked by code). Models cannot count words, so they
    write several lengths and the code picks one that fits; returns the scenes still out of range."""
    if s.tts_provider == "none":
        return []
    actions = {sc.index: sc.action for sc in story.scenes}
    left = []
    p = prompts.load("agent_fit_line_v1")
    for sc in edit.scenes:
        lo, hi = narration_range(s, sc.duration_s, edit.voice or None)
        target = (lo + hi) // 2
        for _ in range(attempts):
            n = _words(sc.narration)
            if lo <= n <= hi:
                break
            out, _ = client.structured([{"role": "user", "content": p.render(
                lo=lo, hi=hi, target=target, count=n, language=s.content_language,
                action=actions.get(sc.index, "")[:300], line_json=prompts.untrusted(sc.narration, 1000))}],
                Lines, max_tokens=800)
            options = [v.strip() for v in out.variants if v.strip() and len(v) <= 400]
            if options:
                sc.narration = min(options, key=lambda v: (not lo <= _words(v) <= hi, abs(_words(v) - target)))
        if not lo <= _words(sc.narration) <= hi:
            left.append(f"scene {sc.index}")
    return left


def _author(s: Settings, client, role: str, vals: dict, out: dict, notes: list[str]) -> BaseModel:
    data = {"writer": {},
            "director": {"story_json": prompts.untrusted(out.get("writer").model_dump(), 12000)
                         if "writer" in out else ""},
            "editor": {"team_json": prompts.untrusted(
                {"story": out["writer"].model_dump(), "shots": out["director"].model_dump(),
                 "narration_word_targets": _word_targets(s, out["writer"])}, 16000)
                if "director" in out else ""}}[role]
    p = prompts.load(f"agent_{role}_v{AGENT_VERSIONS[role]}")
    messages = [{"role": "user", "content": p.render(**vals, **data)}]
    prev = out.get(role)
    if prev is not None and role != "writer" and "writer" in out and \
            [sc.index for sc in prev.scenes] != [sc.index for sc in out["writer"].scenes]:  # type: ignore
        prev = None  # the story's scene list changed: a fresh plan, not a revision of the old one
    if prev is not None:
        ask = ("Your critic and the automatic checks require: " + " | ".join(notes)) if notes else \
            "Another team member changed the input above."
        messages += [{"role": "assistant", "content": prev.model_dump_json()},
                     {"role": "user", "content": ask + " Update your work to the current input above, fix every "
                      "point, keep what already works, and return the full corrected JSON object."}]
    try:
        obj, _ = client.structured(messages, OUTPUT[role], max_tokens=s.agent_max_tokens)
    except StructuredOutputError as e:  # one more full attempt that knows the error, then keep the last good one
        log.warning("team: %s output invalid twice (%s); asking once more", role, str(e)[:200])
        retry = messages + [{"role": "user", "content": "Your answer was rejected by the validator: "
                             + str(e)[:1200] + ". Avoid that and return the full JSON object."}]
        try:
            obj, _ = client.structured(retry, OUTPUT[role], max_tokens=s.agent_max_tokens)
        except StructuredOutputError:
            if prev is None:
                raise
            log.warning("team: keeping the previous %s output", role)
            return prev
    return obj


def _critique(s: Settings, client, role: str, pkg: dict, vals: dict, previous: Review | None) -> Review:
    view = dict(pkg)
    if role != "writer":
        view.pop("reference_analysis", None)
    # Showing the critic its own earlier verdict keeps the goalposts from moving between rounds.
    prev = ""
    if previous is not None and previous.must_fix:
        prev = ("This is a revised version. Your previous review (score " + str(previous.score) + ") listed these "
                "blocking defects: " + " | ".join(previous.must_fix) + ". Check whether each one is fixed now; "
                "raise a new blocking defect only if it is real and was not there before.")
    elif previous is not None:
        prev = (f"You approved the previous version with score {previous.score}. Other parts were revised since. "
                "Lower the score only for a real blocking defect in what changed.")
    p = prompts.load(f"critic_{role}_v{CRITIC_VERSIONS[role]}")
    rv, _ = client.structured([{"role": "user", "content": p.render(
        **vals, previous=prev, package_json=prompts.untrusted(view, 16000))}], Review, max_tokens=2000)
    return rv


def _code_checks(s: Settings, script: Script, candidate: Candidate, analysis: Analysis) -> list[str]:
    """Automatic checks of the final text; all of them are the editor's to fix."""
    max_scene = min(s.max_scene_seconds, 20.0)
    found = script.check_limits(s.target_min_s, s.target_max_s, max_scene) + narration_issues(s, script)
    if s.tts_provider != "none" and s.tts_voices and script.voice not in s.tts_voices:
        found.append(f"voice '{script.voice}' is not one of {sorted(s.tts_voices)}")
    found += [f"scene {sc.index} lasts {sc.duration_s}s; every scene needs at least {MIN_SCENE_S}s"
              for sc in script.scenes if sc.duration_s < MIN_SCENE_S]
    for sc in script.scenes:  # on-screen text must add something, not caption the voice-over
        text = set(re.findall(r"\w+", sc.on_screen_text.lower()))
        said = set(re.findall(r"\w+", sc.narration.lower()))
        if len(text) >= 2 and len(text & said) / len(text) >= 0.75:
            found.append(f"scene {sc.index}: on-screen text '{sc.on_screen_text}' repeats the narration; "
                         "show the emotion or what is visible instead")
    return found + ["originality: " + i for i in originality_issues(script, candidate, analysis)]


def director_checks(plan: ShotPlan) -> list[str]:
    """Key props must be drawn into the opening image (setting) and assigned to the shots that show them."""
    found = []
    setting = plan.setting.lower()
    props = {p.lower(): p for p in plan.key_props}
    for p in plan.key_props:
        head = re.findall(r"[a-z]+", p.lower())[-1:] or [""]
        if head[0] not in setting:
            found.append(f"key prop '{p}' is not described in the setting: the opening image is drawn from the "
                         "setting, so a prop missing there never appears")
    shown = set()
    for sh in plan.scenes:
        for m in sh.must_show:
            if m.lower() not in props:
                found.append(f"shot {sh.index}: must_show '{m}' is not one of key_props {list(props.values())}")
            shown.add(m.lower())
    found += [f"key prop '{p}' is not in must_show of any shot" for k, p in props.items() if k not in shown]
    return found


def _provisional_edit(story: Story, hint: str) -> Edit:
    """Before the editor works, the director's shots are judged against the screenwriter's own text."""
    return Edit(hook=story.hook, title=story.title, tags=story.tags, disclosure_text=hint.rstrip("."),
                description=story.description,
                scenes=[EditScene(index=sc.index, duration_s=sc.duration_s, narration=sc.narration,
                                  on_screen_text=sc.on_screen_text) for sc in story.scenes])


def _verdict(s: Settings, rv: Review) -> tuple[int, list[str]]:
    """Approval needs the overall score >= CRITIC_MIN_SCORE, no open blocking defects and every criterion
    >= CRITIC_MIN_CRITERION (a single weak criterion means the work is not ready)."""
    weak = [c for c in rv.criteria if c.score < s.critic_min_criterion]
    score = rv.score
    if rv.must_fix or weak or not rv.criteria:
        score = min(score, s.critic_min_score - 1)
    if score >= s.critic_min_score:
        return score, []
    fixes = list(rv.must_fix) + [f"{c.name} scored {c.score}/10: {c.note}" for c in weak]
    if not rv.criteria:
        fixes.append("the review had no per-criterion scores")
    return score, fixes or [f"critic scored {rv.score}/10; raise the quality"]


def _fingerprint(pkg: dict) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(pkg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class _Team:
    def __init__(self, s: Settings, agent, critic, candidate: Candidate, analysis: Analysis, fmt: str,
                 checker=None):
        self.s, self.agent, self.critic, self.candidate, self.analysis = s, agent, critic, candidate, analysis
        self.checker = checker
        self.analysis_view = analysis.model_dump(include={
            "mode", "topic", "hook", "structure", "pacing", "plot_beats", "characters", "setting",
            "visual_description", "audience_interest_hypothesis", "unknowns", "limitations"})
        self.analysis_view["reference_made_for_kids"] = candidate.made_for_kids
        self.vals = _values(s, self.analysis_view, fmt)
        self.out: dict[str, BaseModel] = {}
        self.previous: dict[str, Review] = {}
        self.report: dict = {"phases": {r: [] for r in ROLES}, "final": [], "escalations": []}
        self.approved_at: dict[str, str] = {}  # role -> fingerprint of the final package its critic approved

    def assemble(self, provisional: bool = False) -> Script:
        edit = self.out.get("editor")
        if provisional or edit is None:
            edit = _provisional_edit(self.out["writer"], self.vals["disclosure_hint"])  # type: ignore[arg-type]
        return assemble(self.s, self.out["writer"], self.out["director"], edit)  # type: ignore[arg-type]

    def view(self, role: str) -> dict:
        if role == "writer":  # the story is judged alone; the full package only at the final gate
            return {"reference_analysis": self.analysis_view, "story": self.out["writer"].model_dump()}
        return package(self.assemble(provisional=role == "director"), self.analysis_view)

    def story_facts(self) -> list[str]:
        """Edu mode: the taught fact is the point of the video, so doubtful claims go back to the writer at once
        (the final-gate check found them only after the director and editor had worked, 2026-10-13)."""
        if self.s.content_mode != "edu" or self.checker is None or not self.s.fact_check:
            return []
        fc = fact_check(self.checker, self.out["writer"], self.s.content_language)
        return [f"fact-check: «{c.text}» is {c.verdict} ({c.reason}); use a well-established fact or tell it as "
                "the hero's story" for c in fc.problems]

    def phase(self, role: str, notes: list[str]) -> bool:
        """Author <-> critic for one role on locked upstream work; True when the critic approves."""
        entries = self.report["phases"][role]
        verdicts = checks_failed = 0  # automatic-check failures do not use up the critic's rounds
        while verdicts < self.s.agent_max_rounds and checks_failed < self.s.agent_max_rounds:
            if role not in self.out or notes:
                self.out[role] = _author(self.s, self.agent, role, self.vals, self.out, notes)
                if role == "editor":
                    fit_narration(self.s, self.agent, self.out["writer"], self.out["editor"])  # type: ignore[arg-type]
            entry: dict = {"round": len(entries) + 1}
            entries.append(entry)
            try:
                view = self.view(role)
                checks = (_code_checks(self.s, self.assemble(), self.candidate, self.analysis) if role == "editor"
                          else director_checks(self.out["director"]) if role == "director"  # type: ignore
                          else self.story_facts() if role == "writer" else [])
            except TeamError as e:  # e.g. director shots do not match the story's scenes
                checks = [str(e)]
            if checks:  # critics judge only work that passes the automatic checks
                checks_failed += 1
                entry["checks"], notes = checks, checks
                log.info("team %s round %d: automatic checks failed: %s", role, entry["round"], checks)
                continue
            verdicts += 1
            rv = _critique(self.s, self.critic, role, view, self.vals, self.previous.get(role))
            self.previous[role] = rv
            entry["score"], notes = _verdict(self.s, rv)
            entry["must_fix"], entry["suggestions"] = notes, rv.suggestions[:5]
            log.info("team %s round %d: score %d, must fix %d", role, entry["round"], entry["score"], len(notes))
            if not notes:
                self.approved_at.pop(role, None)
                if "director" in self.out:  # the writer is approved on the story alone
                    try:
                        self.approved_at[role] = _fingerprint(package(self.assemble(), self.analysis_view))
                    except TeamError:
                        pass
                return True
        return False


def write_with_team(s: Settings, agent, critic, candidate: Candidate, analysis: Analysis, fmt: str,
                    checker=None, viewer=None) -> tuple[Script, str, dict]:
    if s.publish_made_for_kids is True:
        raise ScriptRejected("made-for-kids content is out of scope for this pipeline")
    team = _Team(s, agent, critic, candidate, analysis, fmt, checker)
    ref = "team@v2(" + ",".join([prompts.load(f"agent_{r}_v{AGENT_VERSIONS[r]}").ref for r in ROLES]
                                + [prompts.load(f"critic_{r}_v{CRITIC_VERSIONS[r]}").ref for r in ROLES]) + ")"
    report = team.report | {"agent_model": getattr(agent, "model", "?"), "critic_model": getattr(critic, "model", "?"),
                            "min_score": s.critic_min_score, "min_criterion": s.critic_min_criterion}

    def reject(msg: str):
        return TeamRejected(msg, report)

    # 1-3: each role works on locked upstream work until its critic approves
    escalated: set[str] = set()
    why = {"director": "could not make this story filmable",
           "editor": "could not make the voice-over land (an ending or payoff problem is the story's)"}
    i = 0
    while i < len(ROLES):
        role = ROLES[i]
        if team.phase(role, []):
            i += 1
            continue
        last = team.report["phases"][role][-1]
        if role in why and role not in escalated:
            # the problem may be in the story itself: the screenwriter rewrites it once, later roles start over
            escalated.add(role)
            notes = [f"The {role} {why[role]}; the {role}'s critic says: "
                     + " | ".join(last.get("must_fix") or last.get("checks") or [])]
            team.report["escalations"].append({"from": role, "notes": notes})
            log.info("team: %s phase failed; the screenwriter rewrites the story", role)
            if not team.phase("writer", notes):
                raise reject(f"writer critic did not approve the rewrite: {team.report['phases']['writer'][-1]}")
            for later in ROLES[1:]:
                team.out.pop(later, None)
                team.previous.pop(later, None)
            i = 1
            continue
        raise reject(f"{role} critic did not approve after {s.agent_max_rounds} rounds: {last}")
    # 4: final gate - every critic scores the final package (the exact prompts) in the same round
    for g in range(1, s.agent_final_rounds + 1):
        script = team.assemble()
        entry: dict = {"round": g, "scores": {}, "must_fix": {}}
        team.report["final"].append(entry)
        failing: dict[str, list[str]] = {}
        if viewer is not None:  # a story nobody can follow goes back to the writer before the critics
            ok, entry["viewer"] = viewer_test(viewer, script, s.viewer_runs)
            log.info("team final gate %d: viewer %s: %s", g, "understood" if ok else "lost", entry["viewer"]["summary"])
            if not ok:
                v = entry["viewer"]
                failing["writer"] = ["viewer test: a first-time viewer retold it as «" + v["summary"] + "»"
                                     + (f"; missed: {v['gap']}" if v["gap"] else "")
                                     + (f"; confusing: {'; '.join(v['confusing'][:4])}" if v["confusing"] else "")
                                     + ". Make the story easy to follow from the picture, voice-over and text alone."]
        pkg = package(script, team.analysis_view)
        fp = _fingerprint(pkg)
        for role in ([] if failing else ROLES):
            if team.approved_at.get(role) == fp:  # its critic approved exactly this package: asking again is noise
                entry["scores"][role], entry["must_fix"][role] = team.report["phases"][role][-1]["score"], []
                continue
            rv = _critique(s, critic, role, pkg, team.vals, team.previous.get(role))
            team.previous[role] = rv
            entry["scores"][role], fixes = _verdict(s, rv)
            entry["must_fix"][role] = fixes
            if fixes:
                failing[role] = fixes
        log.info("team final gate %d: scores %s", g, entry["scores"])
        if not failing and checker is not None and s.fact_check:
            fc = fact_check(checker, script)
            if fc.problems:
                failing["writer"] = [f"fact-check: «{c.text}» is {c.verdict} ({c.reason}); tell it as a story "
                                     "or remove it" for c in fc.problems]
                entry["fact_check"] = failing["writer"]
        if not failing:
            return script, ref, report
        if g == s.agent_final_rounds:
            break
        # the earliest failing role revises; every role after it adapts and is re-approved by its critic
        first = next(r for r in ROLES if r in failing)
        for role in ROLES[ROLES.index(first):]:
            notes = failing.get(role) or [f"The {first}'s work changed; adapt yours to it."]
            if role == "editor" and "writer" in failing:
                notes = notes + ["story critic: " + f for f in failing["writer"]]
            if not team.phase(role, notes):
                raise reject(f"{role} critic did not approve the revision: {team.report['phases'][role][-1]}")
    raise reject(f"final gate: critics did not all approve after {s.agent_final_rounds} rounds: "
                 f"{team.report['final'][-1]['scores']}")
