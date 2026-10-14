"""Pipeline orchestration: one video at a time, resumable stage by stage.

discovered → analyzed → scripted → generated → rendered → validated → uploaded → published
side states: review_required, generation_unknown, upload_unknown, failed, done_dry_run.

* A global DB lease ("pipeline") guarantees a single active runner.
* Each stage result is written in the same transaction as the state transition.
* Billable generation calls are recorded in external_ops *before* they are sent; a crash
  or ambiguous timeout leaves 'pending'/'unknown', which blocks automatic resend.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import prompts
from .agents import TeamRejected, write_with_team
from .analysis import analyze_full_media, analyze_metadata_only, analyze_public_signals
from .budget import BudgetGuard
from .config import ConfigError, Settings
from .ff import FF, FFmpegError
from .media import ReferenceMediaSource
from .models import BLOCKED, TERMINAL, Analysis, Candidate, FormatStatus, JobState, QCReport, Script, UploadRequest
from .net import AuthError, BudgetExceeded, OutcomeUnknown, ProviderError
from .providers.nim_chat import StructuredOutputError
from .providers.nim_video import VideoRequest
from .providers.tts import TTS
from .qc import check_final, check_scene
from .qc_vision import frame_at, has_lettering, missing_props
from .render import RenderInputs, expected_audio, render
from .scripting import (
    ScriptRejected,
    compose_keyframe_prompt,
    compose_thumbnail_prompt,
    compose_video_prompt,
    frames_for,
    generate_script,
    pick_format,
)
from .storage import ArtifactStore
from .store import Job, Store
from .trends import TrendSource

log = logging.getLogger(__name__)
LOCK = "pipeline"
MAX_ATTEMPTS_PER_SCENE = 2

# Stage name -> state the job must be in to (re)run that stage.
STAGE_ENTRY = {
    "analyze": JobState.DISCOVERED, "script": JobState.ANALYZED, "generate": JobState.SCRIPTED,
    "render": JobState.GENERATED, "validate": JobState.RENDERED, "upload": JobState.VALIDATED,
    "publish": JobState.UPLOADED,
}


class _NeedsReview(RuntimeError):
    """Raised inside a stage helper; the stage converts it into review_required."""


class Deferred(RuntimeError):
    """Stage cannot proceed now (budget, rate limit, processing); state unchanged, retry next run."""


@dataclass
class Deps:
    s: Settings
    store: Store
    budget: BudgetGuard
    artifacts: ArtifactStore
    media: ReferenceMediaSource
    ff: FF
    text: Any = None
    vision: Any = None
    analyst: Any = None  # NVIDIA text model: plot beats from public signals, fact-check of scripts
    agent: Any = None    # SCRIPT_ENGINE=team: writer/director/editor model (NVIDIA)
    critic: Any = None   # SCRIPT_ENGINE=team: critics (a different NVIDIA model)
    image: Any = None    # text-to-image for the i2v_chain keyframe
    thumbnailer: Any = None  # text-to-image for the video's cover (NVIDIA flux.1-dev)
    video: Any = None
    tts: TTS | None = None
    trends: TrendSource | None = None
    youtube: Any = None
    owner: str = field(default_factory=lambda: f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}")
    options: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------
def discover(d: Deps) -> dict:
    if d.trends is None:
        raise ConfigError("no trend source configured")
    scope, cands = d.trends.discover()
    expires = datetime.now(UTC) + timedelta(days=d.s.youtube_data_retention_days)
    for c in cands:
        d.store.upsert_candidate(c.video_id, c.model_dump(mode="json"), c.format_status.value,
                                 c.ai.ai_likelihood, c.fetched_at.timestamp(), expires.timestamp())
    d.store.record_discovery(scope.model_dump(mode="json"), [c.video_id for c in cands], expires.timestamp())
    return {"scope": scope.model_dump(mode="json"), "count": len(cands),
            "candidates": [_cand_view(c) for c in cands]}


def _cand_view(c: Candidate) -> dict:
    return {"video_id": c.video_id, "title": c.title[:80], "views": c.view_count, "duration_s": c.duration_s,
            "format": c.format_status.value, "ai_likelihood": c.ai.ai_likelihood,
            "ai_source": c.ai.classification_source, "rank": c.search_rank}


_FMT_RANK = {FormatStatus.CONFIRMED: 0, FormatStatus.PROBABLE: 1, FormatStatus.UNKNOWN: 2}


def is_song_or_lesson(c: Candidate, words: list[str]) -> bool:
    """Songs and lessons have no plot to retell (seen 2026-10-12: «ABC Song», «Color Song»)."""
    if any(t.rstrip("/").rsplit("/", 1)[-1].lower() == "music" for t in c.topic_categories):
        return True
    text = " ".join([c.title, *c.tags]).lower()
    return any(re.search(rf"(?<!\w){re.escape(w.lower())}(?!\w)", text) for w in words)


def select_candidate(s: Settings, cands: list[Candidate], avoid_channels: set[str] | None = None) -> Candidate | None:
    from .trends.youtube import language_ok  # stored candidates may predate a TREND_LANGUAGES change

    pool = [c for c in cands if c.format_status != FormatStatus.NOT_SHORT
            and c.channel_id not in (avoid_channels or set()) and language_ok(c, s.trend_languages)]
    if s.trend_mode == "kids":
        skip = s.edu_skip_words if s.content_mode == "edu" else s.trend_skip_words
        pool = [c for c in pool if not is_song_or_lesson(c, skip)]
    if s.select_require_ai:
        pool = [c for c in pool if c.ai.ai_likelihood is not None and c.ai.ai_likelihood >= s.ai_review_low]
        pool.sort(key=lambda c: (c.ai.ai_likelihood < s.ai_min_likelihood,  # confident AI first
                                 _FMT_RANK[c.format_status], c.search_rank))
    else:
        if s.trend_require_made_for_kids:
            pool = [c for c in pool if c.made_for_kids is True]
        pool.sort(key=lambda c: (-(c.view_count or 0), c.search_rank))  # most viewed of the top N
    return pool[0] if pool else None


# ---------------------------------------------------------------------------------------------
def run_once(d: Deps) -> dict:
    """Entry point of the CronJob: resume the active job or start one, then advance it."""
    if not d.store.acquire_lock(LOCK, d.owner, d.s.job_lease_seconds):
        return {"status": "locked", "detail": "another runner holds the pipeline lease"}
    try:
        for j in d.store.list_jobs(limit=50, states=[JobState.GENERATION_UNKNOWN]):
            reconcile_generation(d, j)
        active = [j for j in d.store.list_jobs(limit=50) if j.state not in TERMINAL and j.state not in BLOCKED]
        if active:
            job = active[-1]  # oldest unfinished first
        else:
            start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC).timestamp()
            n_today = d.store.count_jobs_since(start)
            if n_today >= d.s.daily_video_limit:
                return {"status": "daily_limit", "detail": f"{n_today} job(s) today, limit {d.s.daily_video_limit}"}
            now = datetime.now(UTC).timestamp()
            pool = [Candidate.model_validate(x) for x in d.store.unused_candidates(now)]
            fresh = [c for c in pool if c.fetched_at.timestamp() > now - d.s.trend_refresh_hours * 3600]
            disc = None
            avoid = d.store.used_channels()
            if select_candidate(d.s, fresh, avoid) is None:  # search only when no fresh usable candidate is stored
                disc = discover(d)
                pool = [Candidate.model_validate(x) for x in d.store.unused_candidates(now)]
            cand = select_candidate(d.s, pool, avoid) or select_candidate(d.s, pool)
            if cand is None:
                return {"status": "no_candidate", "discovery": disc}
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            seq = d.store.count_jobs_since(start, exclude=()) + 1  # failed jobs keep their keys
            job, created = d.store.create_job(f"{day}:{seq}", cand.video_id, d.s.dry_run)
            if created:
                d.store.mark_candidate_used(cand.video_id, job.id)
        job = advance(d, job)
        return {"status": "ok", "job_id": job.id, "state": job.state.value, "reason": job.reason}
    finally:
        d.store.release_lock(LOCK, d.owner)


def advance(d: Deps, job: Job) -> Job:
    handlers = {
        JobState.DISCOVERED: stage_analyze, JobState.ANALYZED: stage_script, JobState.SCRIPTED: stage_generate,
        JobState.GENERATED: stage_render, JobState.RENDERED: stage_validate, JobState.VALIDATED: stage_upload,
        JobState.UPLOADED: stage_publish,
    }
    while job.state in handlers:
        # Keep the lease alive between long stages; stop if another runner took it over.
        if not d.store.acquire_lock(LOCK, d.owner, d.s.job_lease_seconds):
            raise RuntimeError("pipeline lease lost to another runner; stopping without changes")
        before = job.state
        try:
            job = handlers[job.state](d, job)
        except Deferred as e:
            log.info("job %s deferred at %s: %s", job.id, job.state, e)
            job = _note(d, job, f"deferred: {e}")
            break
        except ConfigError as e:
            log.error("job %s blocked at %s by configuration: %s", job.id, job.state, e)
            job = _note(d, job, f"configuration: {e}")
            break
        if job.state == before:
            break
    if job.state in TERMINAL:
        shutil.rmtree(_work(d, job), ignore_errors=True)
    return job


def _note(d: Deps, job: Job, reason: str) -> Job:
    with d.store.tx() as c:
        c.execute("UPDATE jobs SET reason = ? WHERE id = ?", (reason[:1000], job.id))
    return d.store.get_job(job.id) or job


def _work(d: Deps, job: Job) -> Path:
    p = Path(d.s.work_dir) / job.id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _check_disk(d: Deps) -> None:
    root = Path(d.s.work_dir)
    used = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) if root.exists() else 0
    if used > d.s.max_work_dir_bytes:
        raise Deferred(f"work dir uses {used} bytes > MAX_WORK_DIR_BYTES {d.s.max_work_dir_bytes}; run purge")


def _review(d: Deps, job: Job, reason: str, resume: JobState | None, stage: str | None = None,
            payload: dict | None = None) -> Job:
    log.warning("job %s -> review_required: %s", job.id, reason)
    return d.store.transition(job, JobState.REVIEW_REQUIRED, reason=reason[:1000], resume_state=resume,
                              stage=stage, payload=payload)


# ---- stages ---------------------------------------------------------------------------------
def stage_analyze(d: Deps, job: Job) -> Job:
    raw = d.store.get_candidate(job.video_id or "")
    if not raw:
        return d.store.transition(job, JobState.FAILED, reason="candidate data missing or purged")
    cand = Candidate.model_validate(raw)
    if d.text is None:
        raise ConfigError("text provider not configured (REPLICATE_API_TOKEN or TEXT_PROVIDER=nvidia)")
    media = d.media.fetch(cand.video_id, _work(d, job) / "reference")
    try:
        if media and media.video and d.vision is not None:
            if media.manifest.is_short:
                cand.format_status, cand.format_reason = FormatStatus.CONFIRMED, "rights-holder manifest"
            analysis, pref = analyze_full_media(cand, media, d.vision, d.ff, _work(d, job), d.s.content_language,
                                                d.s.analysis_max_frames, d.s.analysis_frame_size)
        elif d.vision is not None and d.analyst is not None and cand.thumbnail_url:
            titles: list[str] = []
            if hasattr(d.trends, "channel_recent_titles"):
                try:
                    titles = d.trends.channel_recent_titles(cand.channel_id)
                except (ProviderError, BudgetExceeded) as e:
                    log.warning("channel pattern unavailable: %s", e)
            analysis, pref = analyze_public_signals(cand, d.vision, d.analyst, titles)
        else:
            analysis, pref = analyze_metadata_only(cand, d.text, d.s.content_language)
    except StructuredOutputError as e:
        return _review(d, job, f"analysis output invalid: {e}", None)
    except BudgetExceeded as e:
        raise Deferred(str(e)) from e
    payload = {"analysis": analysis.model_dump(mode="json"), "prompt": pref,
               "candidate_format": cand.format_status.value, "format_reason": cand.format_reason}
    ai = analysis.ai or cand.ai
    if d.s.select_require_ai and (ai.ai_likelihood is None or ai.ai_likelihood < d.s.ai_min_likelihood):
        return _review(d, job, f"ambiguous AI classification (ai_likelihood={ai.ai_likelihood}, "
                               f"source={ai.classification_source}); approve to continue",
                       JobState.ANALYZED, "analysis", payload)
    return d.store.transition(job, JobState.ANALYZED, stage="analysis", payload=payload)


def stage_script(d: Deps, job: Job) -> Job:
    a = d.store.get_stage(job.id, "analysis")
    cand = Candidate.model_validate(d.store.get_candidate(job.video_id or "") or {})
    analysis = Analysis.model_validate(a["analysis"])  # type: ignore[index]
    team = d.s.script_engine == "team"
    if team and (d.agent is None or d.critic is None):
        raise ConfigError("SCRIPT_ENGINE=team needs NVIDIA_API_KEY (agents and critics run on NVIDIA NIM)")
    if not team and d.text is None:
        raise ConfigError("text provider not configured (REPLICATE_API_TOKEN or TEXT_PROVIDER=nvidia)")
    fmt = pick_format(d.s, job.id)
    report = None
    try:
        if team:
            script, pref, report = write_with_team(d.s, d.agent, d.critic, cand, analysis, fmt, checker=d.analyst,
                                                   viewer=d.analyst if d.s.viewer_test else None)
        else:
            script, pref = generate_script(d.s, d.text, cand, analysis, fmt, checker=d.analyst)
    except TeamRejected as e:  # no video is generated for a script the critics did not approve
        return _review(d, job, f"script rejected: {e}; retry with `shorts retry {job.id} --stage script`", None,
                       "script_review", {"team": e.report})
    except (ScriptRejected, StructuredOutputError) as e:
        return _review(d, job, f"script rejected: {e}; retry with `shorts retry {job.id} --stage script`", None)
    except BudgetExceeded as e:
        raise Deferred(str(e)) from e
    except ProviderError as e:  # overloaded/rate-limited text models: try again on the next run
        if e.retryable:
            raise Deferred(f"text provider unavailable: {e}") from e
        raise
    return d.store.transition(job, JobState.SCRIPTED, stage="script",
                              payload={"script": script.model_dump(mode="json"), "prompt": pref, "format": fmt,
                                       "model": getattr(d.agent if team else d.text, "model", "?"),
                                       "team": report})


def _scene_ops(d: Deps, job: Job, idx: int) -> list[dict]:
    return [o for o in d.store.ops_for_job(job.id) if o["op_key"].startswith(f"{job.id}:gen:{idx}:")]


def _stored_script(d: Deps, job: Job) -> Script:
    """Scripts already accepted are read without generation-time rules added later (old jobs must resume)."""
    return Script.model_validate(d.store.get_stage(job.id, "script")["script"],  # type: ignore[index]
                                 context={"stored": True})


def _keyframe(d: Deps, job: Job, script: Script, work: Path) -> Path:
    """Opening image for i2v_chain (cheap): regenerated with a new seed if lettering is detected."""
    key, local = f"jobs/{job.id}/keyframe.jpg", work / "keyframe.jpg"
    if d.artifacts.exists(key):
        return _fetch(d, key, local)
    ops = [o for o in d.store.ops_for_job(job.id) if o["op_key"].startswith(f"{job.id}:img:")]
    prompt = compose_keyframe_prompt(script)
    for attempt in range(len(ops), d.s.max_keyframe_attempts):
        op_key = f"{job.id}:img:0:{attempt}"
        d.store.begin_op(op_key, job.id, "image_gen")
        try:
            rid = d.image.generate(prompt, local, seed=int(hashlib.sha256(op_key.encode()).hexdigest()[:8], 16))
        except BudgetExceeded as e:
            d.store.update_op(op_key, "failed", detail=f"not sent: {e}"[:500])
            raise Deferred(str(e)) from e
        except ProviderError as e:
            d.store.update_op(op_key, "failed", detail=str(e)[:500])
            if e.retryable:
                raise Deferred(str(e)) from e
            continue
        if d.s.check_lettering and d.vision is not None:
            text, where = has_lettering(d.vision, local)
            if text:
                d.store.update_op(op_key, "failed", request_id=rid, detail=f"lettering in keyframe: {where}")
                log.warning("keyframe attempt %d has lettering (%s); retrying", attempt, where)
                continue
        if d.s.check_props and d.vision is not None and script.key_props:
            gone = missing_props(d.vision, [local], script.key_props)
            if gone:
                d.store.update_op(op_key, "failed", request_id=rid, detail=f"keyframe misses key props: {gone}"[:500])
                log.warning("keyframe attempt %d misses key props %s; retrying", attempt, gone)
                continue
        d.artifacts.put_file(key, local, "image/jpeg")
        d.store.update_op(op_key, "succeeded", request_id=rid, artifact_key=key, detail=prompt[:500])
        return local
    raise _NeedsReview(f"no usable keyframe after {d.s.max_keyframe_attempts} attempts")


def stage_generate(d: Deps, job: Job) -> Job:
    try:
        return _stage_generate(d, job)
    except _NeedsReview as e:
        return _review(d, job, str(e), None)


def _stage_generate(d: Deps, job: Job) -> Job:
    script = _stored_script(d, job)
    _check_disk(d)
    work = _work(d, job) / "scenes"
    work.mkdir(parents=True, exist_ok=True)
    chain = d.s.video_mode == "i2v_chain" and d.image is not None
    warnings: list[str] = []
    start: Path | None = _keyframe(d, job, script, work) if chain else None
    results = []
    for sc in script.scenes:
        key = f"jobs/{job.id}/scenes/{sc.index:02d}.mp4"
        local = work / f"{sc.index:02d}.mp4"
        ops = _scene_ops(d, job, sc.index)
        last = ops[-1] if ops else None
        if last and last["status"] == "succeeded" and d.artifacts.exists(key):
            results.append({"index": sc.index, "key": key, "request_id": last["request_id"]})
            if chain:  # next scene continues from this clip's last frame
                start = frame_at(d.ff, _fetch(d, key, local), work / f"start_{sc.index + 1:02d}.jpg", last=True,
                                 width=None)
            continue
        if last and last["status"] in ("pending", "unknown", "submitted"):
            recovered = False
            if last["status"] == "submitted" and last["request_id"]:
                try:
                    recovered = d.video.poll(last["request_id"], local)
                except ProviderError as e:
                    log.warning("poll failed for %s: %s", last["op_key"], e)
            if recovered and check_scene(d.s, d.ff, local, sc.duration_s).ok:
                d.artifacts.put_file(key, local, "video/mp4")
                d.store.update_op(last["op_key"], "succeeded", artifact_key=key, detail="recovered by polling")
                results.append({"index": sc.index, "key": key, "request_id": last["request_id"]})
                if chain:
                    start = frame_at(d.ff, local, work / f"start_{sc.index + 1:02d}.jpg", last=True, width=None)
                continue
            if last["status"] == "pending":
                d.store.update_op(last["op_key"], "unknown", detail="process stopped after op was recorded")
            return d.store.transition(
                job, JobState.GENERATION_UNKNOWN,
                reason=f"scene {sc.index}: outcome of {last['op_key']} unknown (request_id={last['request_id']}); "
                       f"check provider, then `shorts resolve-op {last['op_key']} --status failed|succeeded`")
        if last and last["status"] == "succeeded":  # succeeded but artifact lost
            return _review(d, job, f"scene {sc.index}: op succeeded but artifact {key} is missing", None)

        superseded = sum(1 for o in ops if (o["detail"] or "").startswith("superseded"))
        if len(ops) - superseded >= MAX_ATTEMPTS_PER_SCENE:
            return _review(d, job, f"scene {sc.index}: {len(ops)} failed attempts: {last['detail'] if last else ''}",
                           None)
        spent = sum(1 for o in d.store.ops_for_job(job.id)
                    if o["kind"] == "video_gen" and not (o["detail"] or "").startswith("not sent"))
        if spent >= d.s.budget.video_requests_per_job:
            return _review(d, job, f"per-job generation budget exhausted ({spent})", None)

        op_key = f"{job.id}:gen:{sc.index}:{len(ops)}"
        d.store.begin_op(op_key, job.id, "video_gen")
        req = VideoRequest(prompt=compose_video_prompt(script, sc), negative_prompt=sc.negative_prompt,
                           num_frames=frames_for(sc.duration_s, d.s.gen_fps, d.s.gen_min_frames,
                                                 d.s.gen_max_frames),
                           fps=d.s.gen_fps, resolution=d.s.gen_resolution, image=start,
                           seed=int(hashlib.sha256(op_key.encode()).hexdigest()[:8], 16) % 2**31)
        try:
            if d.video is None:
                raise ConfigError("video generator not configured")
            rid = d.video.generate(req, local, on_submitted=lambda r, k=op_key: d.store.update_op(
                k, "submitted", request_id=r, detail="async request accepted"))
        except OutcomeUnknown as e:
            d.store.update_op(op_key, "unknown", request_id=e.request_id, detail=str(e)[:500])
            return d.store.transition(job, JobState.GENERATION_UNKNOWN,
                                      reason=f"scene {sc.index}: {e}; not resent automatically")
        except (BudgetExceeded, ConfigError) as e:
            d.store.update_op(op_key, "failed", detail=f"not sent: {e}"[:500])
            if isinstance(e, BudgetExceeded):
                raise Deferred(str(e)) from e
            raise
        except AuthError as e:
            d.store.update_op(op_key, "failed", detail=f"auth: {e}"[:500])
            raise
        except ProviderError as e:
            d.store.update_op(op_key, "failed", detail=str(e)[:500])
            if e.retryable:
                raise Deferred(str(e)) from e
            return _review(d, job, f"scene {sc.index}: generation rejected: {e}", None)
        qc = check_scene(d.s, d.ff, local, sc.duration_s)
        if not qc.ok:
            d.store.update_op(op_key, "failed", request_id=rid, detail="bad result: " + "; ".join(qc.failures))
            log.warning("scene %d result failed QC: %s", sc.index, qc.failures)
            return _stage_generate(d, d.store.get_job(job.id) or job)  # next attempt (bounded above)
        if d.s.check_lettering and d.vision is not None:
            mid = frame_at(d.ff, local, work / f"check_{sc.index:02d}.jpg", at_s=(qc.duration_s or sc.duration_s) / 2)
            text, where = has_lettering(d.vision, mid)
            attempts_left = MAX_ATTEMPTS_PER_SCENE - (len(ops) + 1 - superseded)
            if text and attempts_left > 0:
                d.store.update_op(op_key, "failed", request_id=rid, detail=f"bad result: lettering: {where}"[:500])
                log.warning("scene %d has generated lettering (%s); next attempt", sc.index, where)
                return _stage_generate(d, d.store.get_job(job.id) or job)
            if text and d.s.lettering_last_attempt == "review":  # may be a brand: a human decides
                rkey = f"jobs/{job.id}/review/scene_{sc.index:02d}.mp4"
                d.artifacts.put_file(rkey, local, "video/mp4")
                d.store.update_op(op_key, "failed", request_id=rid, artifact_key=rkey,
                                  detail=f"bad result: lettering on last attempt: {where}"[:500])
                return _review(d, job, f"scene {sc.index}: lettering on the last attempt ({where}); check {rkey}. "
                                       f"Accept: `shorts resolve-op {op_key} --note ok --artifact <file>` then "
                                       f"`shorts retry {job.id} --stage generate`; or regenerate (billable): "
                                       f"`shorts retry {job.id} --stage generate --regenerate-scenes {sc.index}`",
                               None)
            if text:  # last attempt: keep it, flag it for the human reviewer instead of paying again
                warnings.append(f"scene {sc.index}: possible lettering {where}")
                log.warning("scene %d: lettering suspected on last attempt; accepted with warning", sc.index)
        if d.s.check_props and d.vision is not None and sc.must_show:
            dur = qc.duration_s or sc.duration_s
            frames = [frame_at(d.ff, local, work / f"props_{sc.index:02d}_a.jpg", at_s=0.3),
                      frame_at(d.ff, local, work / f"props_{sc.index:02d}_m.jpg", at_s=dur / 2)]
            gone = missing_props(d.vision, frames, sc.must_show)
            if gone:
                attempts_left = MAX_ATTEMPTS_PER_SCENE - (len(ops) + 1 - superseded)
                if attempts_left > 0:
                    d.store.update_op(op_key, "failed", request_id=rid, detail=f"bad result: missing {gone}"[:500])
                    log.warning("scene %d misses key props %s; next attempt", sc.index, gone)
                    return _stage_generate(d, d.store.get_job(job.id) or job)
                rkey = f"jobs/{job.id}/review/scene_{sc.index:02d}.mp4"
                d.artifacts.put_file(rkey, local, "video/mp4")
                d.store.update_op(op_key, "failed", request_id=rid, artifact_key=rkey,
                                  detail=f"bad result: missing on last attempt {gone}"[:500])
                return _review(d, job, f"scene {sc.index}: key props {gone} not visible on the last attempt; check "
                                       f"{rkey}. Accept: `shorts resolve-op {op_key} --note ok --artifact <file>` "
                                       f"then `shorts retry {job.id} --stage generate`; or regenerate (billable): "
                                       f"`shorts retry {job.id} --stage generate --regenerate-scenes {sc.index}`",
                               None)
        d.artifacts.put_file(key, local, "video/mp4")
        d.store.update_op(op_key, "succeeded", request_id=rid, artifact_key=key)
        results.append({"index": sc.index, "key": key, "request_id": rid, "prompt": req.prompt,
                        "start": "keyframe" if chain and sc.index == 0 else ("previous_clip" if chain else None)})
        if chain:
            start = frame_at(d.ff, local, work / f"start_{sc.index + 1:02d}.jpg", last=True, width=None)

    narration = []
    for sc in script.scenes:
        wav = d.tts.synthesize(sc.narration, script.language, work / f"tts_{sc.index:02d}.wav", sc.duration_s,
                               voice=script.voice) \
            if d.tts else None
        nkey = None
        if wav:
            nkey = f"jobs/{job.id}/audio/narration_{sc.index:02d}.wav"
            d.artifacts.put_file(nkey, wav, "audio/wav")
        narration.append(nkey)
    return d.store.transition(job, JobState.GENERATED, stage="generate", payload={
        "scenes": results, "narration": narration, "tts": d.tts.name if d.tts else "none",
        "generator": getattr(d.video, "name", "?"), "warnings": warnings})


def _fetch(d: Deps, key: str, dest: Path) -> Path:
    return dest if dest.is_file() else d.artifacts.get_file(key, dest)


def _media_ok(d: Deps, p: Path) -> bool:
    if not p.is_file() or p.stat().st_size < 1024:
        return False
    try:
        d.ff.probe(p)
        return True
    except FFmpegError:
        return False


def _music(d: Deps, work: Path) -> Path | None:
    if not d.s.music_file_key:
        return None
    lic = d.s.music_file_key + ".license.json"
    if not d.artifacts.exists(lic):
        raise ConfigError(f"music {d.s.music_file_key} has no license manifest {lic}; refusing to use it")
    meta = json.loads(_fetch(d, lic, work / "music.license.json").read_text())
    if not meta.get("license") or not meta.get("source"):
        raise ConfigError("music license manifest must contain 'license' and 'source'")
    return _fetch(d, d.s.music_file_key, work / ("music" + Path(d.s.music_file_key).suffix))


def stage_render(d: Deps, job: Job) -> Job:
    _check_disk(d)
    script = _stored_script(d, job)
    gen = d.store.get_stage(job.id, "generate") or {}
    work = _work(d, job)
    scenes = [(sc, _fetch(d, r["key"], work / "scenes" / f"{sc.index:02d}.mp4"))
              for sc, r in zip(script.scenes, gen["scenes"], strict=True)]
    broken = [sc.index for sc, p in scenes if not _media_ok(d, p)]
    if broken:  # paid clips: never regenerate silently
        return _review(d, job, f"generated clips unreadable after storage failure: scenes {broken}; "
                               f"`shorts retry {job.id} --stage generate --regenerate-scenes "
                               f"{','.join(map(str, broken))}` (billable)", None)
    narr = []
    for sc, k in zip(script.scenes, gen["narration"], strict=True):
        p = _fetch(d, k, work / "audio" / Path(k).name) if k else None
        if p is not None and not _media_ok(d, p):  # cheap: re-synthesize instead of failing
            log.warning("narration %s unreadable (size %d); re-synthesizing", k, p.stat().st_size)
            p.unlink(missing_ok=True)
            p = d.tts.synthesize(sc.narration, script.language, p, sc.duration_s, voice=script.voice) \
                if d.tts else None
            if p is not None:
                d.artifacts.put_file(k, p, "audio/wav")
        narr.append(p)
    music = _music(d, work)
    out, shots, pause = render(d.s, d.ff, script, RenderInputs(scenes, narr, music), work / "render")
    thumb = _thumbnail(d, job, script, work)
    key = f"jobs/{job.id}/final.mp4"
    d.artifacts.put_file(key, out, "video/mp4")
    sha = hashlib.sha256()
    with out.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            sha.update(b)
    return d.store.transition(job, JobState.RENDERED, stage="render", payload={
        "final_key": key, "size": out.stat().st_size, "sha256": sha.hexdigest(),
        "expect_audio": expected_audio(narr, music), "narration": any(narr), "music": d.s.music_file_key or None,
        "edit": {"shots": len(shots), "avg_shot_s": round(sum(shots) / max(1, len(shots)), 2),
                 "first_shot_s": shots[0] if shots else None,
                 "narration_max_pause_s": round(pause, 2) if pause is not None else None},
        "thumbnail": thumb})


class _Rephrased(BaseModel):
    prompt: str = Field(min_length=20, max_length=2000)


def _rephrase_for_filter(d: Deps, prompt: str) -> str | None:
    p = prompts.load("thumb_rephrase_v1")
    try:
        out, _ = d.analyst.structured([{"role": "user", "content": p.render(prompt_json=prompts.untrusted(prompt))}],
                                      _Rephrased, max_tokens=800)
        return out.prompt
    except (StructuredOutputError, ProviderError, BudgetExceeded) as e:
        log.warning("thumbnail prompt rephrase failed: %s", e)
        return None


def _thumbnail(d: Deps, job: Job, script: Script, work: Path) -> dict:
    """Cover image for the video; never blocks the job (a video without a custom thumbnail is still fine)."""
    key = f"jobs/{job.id}/thumbnail.jpg"
    if not d.s.thumbnails or d.thumbnailer is None:
        return {"status": "off"}
    if d.artifacts.exists(key):
        return {"status": "ok", "key": key}
    prompt = compose_thumbnail_prompt(script)
    local, problems = work / "thumbnail.jpg", []
    for attempt in range(d.s.thumbnail_attempts):
        try:
            d.thumbnailer.generate(prompt, local, seed=int(hashlib.sha256(f"{job.id}:thumb:{attempt}".encode())
                                                          .hexdigest()[:8], 16))
        except (ProviderError, BudgetExceeded) as e:
            problems.append(f"attempt {attempt}: {e}"[:200])
            if "CONTENT_FILTERED" in str(e) and d.analyst is not None:
                prompt = _rephrase_for_filter(d, prompt) or prompt  # the filter reacts to words like "T-rex"
            continue
        if d.vision is not None and d.s.check_lettering:
            text, where = has_lettering(d.vision, local)
            if text:
                problems.append(f"attempt {attempt}: lettering {where}"[:200])
                continue
        if d.vision is not None and d.s.check_props and script.key_props:
            gone = missing_props(d.vision, [local], script.key_props)
            if gone:
                problems.append(f"attempt {attempt}: missing {gone}"[:200])
                continue
        d.artifacts.put_file(key, local, "image/jpeg")
        return {"status": "ok", "key": key, "attempts": attempt + 1, "prompt": prompt[:500], "problems": problems}
    log.warning("job %s: no usable thumbnail (%s)", job.id, problems)
    return {"status": "failed", "problems": problems}


def stage_validate(d: Deps, job: Job) -> Job:
    r = d.store.get_stage(job.id, "render") or {}
    final = _fetch(d, r["final_key"], _work(d, job) / "render" / "final.mp4")
    edit = r.get("edit") or {}
    qc: QCReport = check_final(d.s, d.ff, final, r["expect_audio"], edit.get("avg_shot_s"),
                               edit.get("narration_max_pause_s"))
    qc.file_key = r["final_key"]
    payload = {"qc": qc.model_dump(mode="json")}
    if not qc.ok:
        return _review(d, job, "quality check failed: " + "; ".join(qc.failures)
                       + f". Fix and `shorts retry {job.id} --stage render|generate`", None, "validate", payload)
    return d.store.transition(job, JobState.VALIDATED, stage="validate", payload=payload)


_AI_NOTE = re.compile(
    r"(?i)(?:^|(?<=[.!?\n]))\s*[^.!?\n]*(?:\b(?:AI|A\.I\.|artificial intelligence)\b[^.!?\n]*"
    r"\b(?:generat\w*|made|created|render\w*)\b|\b(?:generat\w*|made|created)\b[^.!?\n]*"
    r"\b(?:AI|A\.I\.|artificial intelligence)\b|сгенерирован\w*[^.!?\n]*|нейросет\w*[^.!?\n]*)[^.!?\n]*[.!?]?")


def strip_ai_note(text: str) -> str:
    """Remove sentences that say the video was made with AI (the API flag carries the label)."""
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", _AI_NOTE.sub("", text))).strip()


def build_upload_request(s: Settings, store: Store, script: Script, now: datetime | None = None) -> tuple[
        UploadRequest, list[str]]:
    notes: list[str] = []
    if s.publish_made_for_kids is None:
        raise ConfigError("set PUBLISH_MADE_FOR_KIDS=true|false explicitly before any real upload")
    privacy, publish_at = "private", None
    if s.auto_publish:
        if not s.publish_consent_channel_id or s.publish_consent_channel_id != s.youtube_channel_id:
            raise ConfigError("AUTO_PUBLISH requires PUBLISH_CONSENT_CHANNEL_ID == YOUTUBE_CHANNEL_ID")
        if not store.any_completed_upload():
            notes.append("first real upload is forced to private")
        else:
            privacy = s.publish_privacy
            if s.publish_time_utc and privacy == "public":
                hh, mm = (int(x) for x in s.publish_time_utc.split(":"))
                now = now or datetime.now(UTC)
                slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if slot <= now + timedelta(minutes=15):
                    slot += timedelta(days=1)
                privacy, publish_at = "private", slot
                notes.append(f"scheduled public at {slot.isoformat()}")
            elif s.publish_time_utc:
                raise ConfigError("PUBLISH_TIME_UTC is only supported with PUBLISH_PRIVACY=public")
    else:
        notes.append("AUTO_PUBLISH=false: uploaded as private")
    # The synthetic-media label is the API flag below (Studio "altered or synthetic content"); the owner does not
    # want an AI sentence in the description text (2026-10-13), so none is added and a model-written one is removed
    desc = strip_ai_note(script.description) if not s.description_ai_note else script.description
    if s.description_ai_note and script.disclosure.disclosure_text not in desc:
        desc = f"{desc}\n\n{script.disclosure.disclosure_text}"
    req = UploadRequest(title=script.title, description=desc[:4900], tags=script.tags,
                        category_id=s.youtube_category_id, privacy_status=privacy, publish_at=publish_at,
                        self_declared_made_for_kids=s.publish_made_for_kids, contains_synthetic_media=True,
                        default_language=script.language)
    return req, notes


def stage_upload(d: Deps, job: Job) -> Job:
    from .youtube.upload import UploadUnknown

    script = _stored_script(d, job)
    r = d.store.get_stage(job.id, "render") or {}
    if job.dry_run:
        try:
            req, notes = build_upload_request(d.s, d.store, script)
            planned = req.model_dump(mode="json")
        except ConfigError as e:
            planned, notes = None, [f"real upload would be blocked: {e}"]
        return d.store.transition(job, JobState.DONE_DRY_RUN, reason="DRY_RUN=true: nothing uploaded",
                                  stage="upload", payload={"dry_run": True, "would_upload": planned,
                                                           "notes": notes, "file_key": r["final_key"]})
    req, notes = build_upload_request(d.s, d.store, script)
    start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC).timestamp()
    if d.store.uploads_since(start) >= d.s.publish_daily_limit and not d.store.get_upload(job.id):
        raise Deferred(f"PUBLISH_DAILY_LIMIT={d.s.publish_daily_limit} reached")
    if d.youtube is None:
        raise ConfigError("YouTube client not configured")
    d.youtube.verify_channel()
    final = _fetch(d, r["final_key"], _work(d, job) / "render" / "final.mp4")
    if final.stat().st_size != r["size"]:
        return _review(d, job, "local final file differs from validated render", None)
    try:
        vid = d.youtube.upload(job.id, final, req, r["final_key"],
                               allow_new_session_after_unknown=d.options.get("new_upload_session", False))
    except UploadUnknown as e:
        return d.store.transition(job, JobState.UPLOAD_UNKNOWN, reason=str(e)[:1000])
    except BudgetExceeded as e:
        raise Deferred(str(e)) from e
    thumb = (r.get("thumbnail") or {})
    if thumb.get("status") == "ok":  # non-fatal: Shorts thumbnails need a channel with custom-thumbnail access
        try:
            d.youtube.set_thumbnail(vid, _fetch(d, thumb["key"], _work(d, job) / "thumbnail.jpg"))
            notes.append("custom thumbnail set")
        except (ProviderError, BudgetExceeded) as e:
            notes.append(f"custom thumbnail not set ({str(e)[:200]}); the image is {thumb['key']} — set it in "
                         "YouTube Studio if the channel has custom thumbnails")
    return d.store.transition(job, JobState.UPLOADED, stage="upload",
                              payload={"video_id": vid, "request": req.model_dump(mode="json"), "notes": notes})


def stage_publish(d: Deps, job: Job) -> Job:
    up = d.store.get_stage(job.id, "upload") or {}
    st = d.youtube.processing_status(up["video_id"])
    if not st.get("found"):
        raise Deferred("uploaded video not visible via API yet")
    if st.get("uploadStatus") in ("failed", "rejected") or st.get("processingStatus") == "failed":
        return _review(d, job, f"YouTube processing failed: {st}", None)
    if st.get("processingStatus") != "succeeded":
        raise Deferred(f"processing: {st.get('processingStatus')}")
    return d.store.transition(job, JobState.PUBLISHED, stage="publish", payload=st)


def reconcile_generation(d: Deps, job: Job) -> Job:
    """For generation_unknown: recover results by request id (polling) instead of resending.

    Ops without a request id or without a pollable endpoint stay unknown for the operator.
    """
    if job.state != JobState.GENERATION_UNKNOWN:
        return job
    script = _stored_script(d, job)
    durations = {sc.index: sc.duration_s for sc in script.scenes}
    work = _work(d, job) / "scenes"
    work.mkdir(parents=True, exist_ok=True)
    unresolved = 0
    for op in d.store.ops_for_job(job.id):
        if op["kind"] != "video_gen" or op["status"] not in ("unknown", "pending", "submitted"):
            continue
        idx = int(op["op_key"].split(":")[2])
        local, key = work / f"{idx:02d}.mp4", f"jobs/{job.id}/scenes/{idx:02d}.mp4"
        ok = False
        if op["request_id"] and d.video is not None:
            try:
                ok = d.video.poll(op["request_id"], local)
            except ProviderError as e:
                log.warning("reconcile poll %s failed: %s", op["op_key"], e)
        if ok:
            qc = check_scene(d.s, d.ff, local, durations[idx])
            if qc.ok:
                d.artifacts.put_file(key, local, "video/mp4")
                d.store.update_op(op["op_key"], "succeeded", artifact_key=key, detail="recovered by reconcile")
            else:
                d.store.update_op(op["op_key"], "failed", detail="recovered result failed QC: "
                                  + "; ".join(qc.failures))
        else:
            unresolved += 1
    if unresolved:
        return job
    return d.store.transition(job, JobState.SCRIPTED, reason="generation reconciled")


# ---- operator actions -----------------------------------------------------------------------
def approve(store: Store, job_id: str) -> Job:
    job = store.get_job(job_id)
    if job is None or job.state != JobState.REVIEW_REQUIRED:
        raise ValueError("job is not in review_required")
    if job.resume_state is None:
        raise ValueError("this review item cannot be approved (failed check/rejected output); use retry or reject")
    return store.transition(job, job.resume_state, reason="approved by operator")


def reject(store: Store, job_id: str, reason: str) -> Job:
    job = store.get_job(job_id)
    if job is None or job.state in TERMINAL:
        raise ValueError("job not found or already terminal")
    return store.transition(job, JobState.FAILED, reason=f"rejected: {reason}")


def retry(store: Store, job_id: str, stage: str, regenerate_scenes: list[int] | None = None) -> Job:
    """Move a job back to the entry state of ``stage``.

    ``regenerate_scenes`` (only with stage=generate) supersedes those scenes' successful results, so
    they are generated — and paid for — again. Other scenes are reused.
    """
    job = store.get_job(job_id)
    if job is None:
        raise ValueError("job not found")
    if regenerate_scenes and stage != "generate":
        raise ValueError("--regenerate-scenes requires --stage generate")
    target = STAGE_ENTRY[stage]
    if job.state == JobState.GENERATION_UNKNOWN and stage in ("generate",):
        unresolved = [o["op_key"] for o in store.ops_for_job(job_id) if o["status"] in ("unknown", "pending")]
        if unresolved:
            raise ValueError(f"resolve unknown operations first: {unresolved}")
    if job.state == JobState.UPLOAD_UNKNOWN and stage != "upload":
        raise ValueError("upload outcome unknown: only --stage upload is allowed")
    if job.state in (JobState.UPLOADED, JobState.PUBLISHED) and stage != "publish":
        raise ValueError("video already uploaded; re-running earlier stages would create a duplicate")
    needs = {"script": "analysis", "generate": "script", "render": "generate", "validate": "render",
             "upload": "validate", "publish": "upload"}.get(stage)
    if needs and store.get_stage(job_id, needs) is None:
        raise ValueError(f"stage '{stage}' needs the result of the previous stage ({needs}); retry earlier")
    if stage == "upload" and not (store.get_stage(job_id, "validate") or {}).get("qc", {}).get("ok"):
        raise ValueError("validation did not pass; retry render/validate first")
    job = store.transition(job, target, reason=f"operator retry from {stage}")
    for idx in regenerate_scenes or []:
        # the operator pays for fresh attempts: a kept clip and clips rejected by quality checks (lettering,
        # missing props, QC) no longer count against MAX_ATTEMPTS_PER_SCENE
        for o in store.ops_for_job(job_id):
            if not o["op_key"].startswith(f"{job_id}:gen:{idx}:") or (o["detail"] or "").startswith("superseded"):
                continue
            rejected = o["status"] == "failed" and (o["detail"] or "").startswith("bad result")
            if o["status"] == "succeeded" or rejected:
                store.update_op(o["op_key"], "failed",
                                detail=f"superseded by operator (regenerate): {o['detail'] or ''}"[:500])
    return job


def resolve_op(store: Store, artifacts: ArtifactStore, op_key: str, note: str,
               artifact: Path | None = None) -> None:
    """Close an unknown generation op after the operator checked the provider.

    Without ``artifact``: mark failed (the next run may generate again — possible second charge
    is the operator's decision). With ``artifact``: store the recovered file and mark succeeded.
    """
    op = store.get_op(op_key)
    lettering_reject = (op is not None and op["status"] == "failed"  # or a clip missing a key prop
                        and (op["detail"] or "").startswith(("bad result: lettering", "bad result: missing")))
    if op is None or (op["status"] not in ("unknown", "pending", "submitted") and not lettering_reject):
        raise ValueError("operation not found or not unresolved")
    if lettering_reject and artifact is None:
        raise ValueError("a clip rejected for lettering can only be accepted with --artifact (the reviewed file)")
    if artifact is None:
        store.update_op(op_key, "failed", detail=f"resolved by operator (regenerate allowed): {note}")
        return
    job_id, _, idx, _ = op_key.split(":")
    key = f"jobs/{job_id}/scenes/{int(idx):02d}.mp4"
    artifacts.put_file(key, artifact, "video/mp4")
    store.update_op(op_key, "succeeded", artifact_key=key, detail=f"resolved by operator with file: {note}")
