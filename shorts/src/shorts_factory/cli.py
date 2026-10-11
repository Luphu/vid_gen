"""Command line: doctor, smoke-test, discover, run, status, review, approve/reject, retry, resolve-op,
auth init, purge. Exit codes: 0 ok, 1 error, 2 blocked/needs attention."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from . import pipeline
from .config import ConfigError, Settings, load_settings
from .log import setup_logging
from .models import BLOCKED, JobState

log = logging.getLogger("shorts")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _deps(s: Settings):
    from .factory import make_deps

    return make_deps(s)


# ---- doctor ---------------------------------------------------------------------------------
def cmd_doctor(s: Settings, args) -> int:
    from .factory import register_secrets
    from .ff import FF

    register_secrets(s)
    report: dict = {"mode": {"APP_ENV": s.app_env, "DRY_RUN": s.dry_run, "AUTO_PUBLISH": s.auto_publish,
                             "PROVIDER_MODE": s.provider_mode, "TREND_SOURCE": s.trend_source,
                             "REFERENCE_SOURCE": s.reference_source, "STORAGE_BACKEND": s.storage_backend,
                             "TTS_PROVIDER": s.tts_provider}}
    checks: list[dict] = []
    blockers: list[str] = []

    def chk(name: str, ok: bool, detail: str = "", blocking: str | None = None) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok and blocking:
            blockers.append(blocking)

    creds = {k: bool(getattr(s, k)) for k in ("nvidia_api_key", "youtube_api_key", "youtube_client_id",
                                              "youtube_client_secret", "youtube_refresh_token",
                                              "youtube_channel_id", "upload_uri_key", "s3_access_key_id")}
    report["credentials_present"] = creds  # booleans only, never values

    ff = FF(threads=s.ffmpeg_threads)
    if ff.available():
        enc = ff.query(["-encoders"])
        flt = ff.query(["-filters"])
        chk("ffmpeg", True, shutil.which("ffmpeg") or "")
        chk("ffmpeg libx264", " libx264 " in enc, blocking="ffmpeg without libx264")
        chk("ffmpeg aac", " aac " in enc, blocking="ffmpeg without aac")
        chk("ffmpeg drawtext", " drawtext " in flt, blocking=None if not s.burn_subtitles else "no drawtext")
        chk("font file", Path(s.font_file).is_file(), s.font_file,
            blocking="FONT_FILE missing" if s.burn_subtitles else None)
    else:
        chk("ffmpeg", False, "ffmpeg/ffprobe not on PATH", blocking="install ffmpeg")

    try:
        from .store import Store

        st = Store(s.database_url.get_secret_value())
        st.migrate()
        chk("database", True, "postgresql" if st.pg else "sqlite (dev only)")
    except Exception as e:  # noqa: BLE001
        chk("database", False, type(e).__name__ + ": " + str(e)[:200], blocking="database unreachable")
        st = None

    try:
        from .storage import make_store

        art = make_store(s)
        with tempfile.NamedTemporaryFile(suffix=".txt") as tf:
            tf.write(b"doctor")
            tf.flush()
            art.put_file("doctor/probe.txt", Path(tf.name), "text/plain")
        chk("artifact storage", art.exists("doctor/probe.txt"), s.storage_backend)
    except Exception as e:  # noqa: BLE001
        chk("artifact storage", False, type(e).__name__ + ": " + str(e)[:200], blocking="storage unavailable")

    if s.provider_mode == "nvidia":
        need_replicate = "replicate" in (s.text_provider, s.video_provider, s.tts_provider)
        if need_replicate:
            chk("REPLICATE_API_TOKEN set", bool(s.replicate_api_token), blocking="REPLICATE_API_TOKEN missing")
        if s.text_provider == "nvidia" or s.video_provider == "nvidia":
            chk("NVIDIA_API_KEY set", creds["nvidia_api_key"], blocking="NVIDIA_API_KEY missing")
        if s.video_provider == "nvidia":
            chk("NIM_VIDEO_URL set", bool(s.nim_video_url), s.nim_video_url or "not configured",
                blocking="video generator endpoint not configured (docs/capabilities.md §1.3)")
        chk("video budget > 0", s.budget.video_requests_per_day > 0,
            f"BUDGET_VIDEO_REQUESTS_PER_DAY={s.budget.video_requests_per_day}",
            blocking="generation disabled until BUDGET_VIDEO_REQUESTS_PER_DAY > 0")
    if s.trend_source == "youtube":
        chk("YOUTUBE_API_KEY set", creds["youtube_api_key"], blocking="YOUTUBE_API_KEY missing")
    if not s.dry_run:
        for k in ("youtube_client_id", "youtube_client_secret", "youtube_refresh_token", "youtube_channel_id",
                  "upload_uri_key"):
            chk(f"{k.upper()} set", creds[k], blocking=f"{k.upper()} missing for real upload")
        chk("PUBLISH_MADE_FOR_KIDS chosen", s.publish_made_for_kids is not None,
            blocking="PUBLISH_MADE_FOR_KIDS must be set explicitly")
    if s.reference_source == "none":
        checks.append({"check": "reference media", "ok": True,
                       "detail": "none: analysis runs in metadata_only mode"})
    if s.tts_provider == "none":
        checks.append({"check": "tts", "ok": True, "detail": "none: videos have no narration (declared)"})

    if args.live:
        _doctor_live(s, chk, st)

    report["checks"], report["blockers"] = checks, blockers
    if st is not None:
        from .budget import BudgetGuard

        report["budget_usage"] = BudgetGuard(st, s.budget).usage()
    _print(report)
    return 0 if not blockers else 2


def _doctor_live(s: Settings, chk, st) -> None:
    """Real requests that cost nothing or ~nothing. No video generation here."""
    import httpx

    from .budget import BudgetGuard
    from .net import make_client

    http = make_client(10, 30)
    try:
        r = http.get(s.nim_text_url.rsplit("/chat/completions", 1)[0] + "/models")
        ids = {m["id"] for m in r.json().get("data", [])} if r.status_code == 200 else set()
        chk("nim /v1/models", r.status_code == 200, f"HTTP {r.status_code}, {len(ids)} models")
        chk(f"text model listed: {s.nim_text_model}", s.nim_text_model in ids)
        chk(f"vision model listed: {s.nim_vision_model}", s.nim_vision_model in ids,
            "not listed; vision calls may 404" if s.nim_vision_model not in ids else "")
    except httpx.HTTPError as e:
        chk("nim /v1/models", False, type(e).__name__)
    if s.replicate_api_token:
        hdr = {"Authorization": f"Bearer {s.replicate_api_token.get_secret_value()}"}
        try:
            r = http.get(f"{s.replicate_api_base}/account", headers=hdr)
            chk("replicate token", r.status_code == 200,
                f"HTTP {r.status_code}" + (f", account {r.json().get('username')}" if r.status_code == 200 else ""))
            for m in {s.replicate_text_model, s.replicate_video_model, s.replicate_tts_model}:
                r = http.get(f"{s.replicate_api_base}/models/{m}", headers=hdr)
                chk(f"replicate model {m}", r.status_code == 200, f"HTTP {r.status_code}")
        except httpx.HTTPError as e:
            chk("replicate token", False, type(e).__name__)
    if s.nvidia_api_key and st is not None:
        from .providers.nim_chat import NimTextClient

        try:
            txt, _ = NimTextClient(s, BudgetGuard(st, s.budget)).chat(
                [{"role": "user", "content": "Reply with the single word OK."}], max_tokens=64)
            chk("nim text completion", bool(txt), "1 short request (counted in nim_text budget)")
        except Exception as e:  # noqa: BLE001
            chk("nim text completion", False, f"{type(e).__name__}: {str(e)[:200]}")
    if s.nim_video_url:
        base = s.nim_video_url.rsplit("/v1/", 1)[0]
        try:
            r = http.get(base + "/v1/health/ready",
                         headers={"Authorization": f"Bearer {s.nvidia_api_key.get_secret_value()}"}
                         if s.nvidia_api_key else {})
            chk("video endpoint /v1/health/ready", r.status_code == 200, f"HTTP {r.status_code}")
        except httpx.HTTPError as e:
            chk("video endpoint /v1/health/ready", False, type(e).__name__)
    if s.youtube_api_key and st is not None:
        try:
            from .trends.youtube import YouTubeSearchSource

            data = YouTubeSearchSource(s, BudgetGuard(st, s.budget), http)._get(
                "videos", {"part": "id", "id": "jNQXAC9IVRw"}, "yt_units")
            chk("youtube data api key", bool(data.get("items")), "videos.list (1 unit)")
        except Exception as e:  # noqa: BLE001
            chk("youtube data api key", False, f"{type(e).__name__}: {str(e)[:200]}")
    if s.youtube_refresh_token and st is not None:
        try:
            from .factory import make_youtube

            ch = make_youtube(s, st, BudgetGuard(st, s.budget)).verify_channel()
            chk("youtube oauth + channel", True, f"channel {ch['id']} ({ch['snippet']['title']})")
        except Exception as e:  # noqa: BLE001
            chk("youtube oauth + channel", False, f"{type(e).__name__}: {str(e)[:200]}")


# ---- smoke test (billable) -----------------------------------------------------------------
def cmd_smoke(s: Settings, args) -> int:
    if not args.i_accept_cost:
        print("Refusing: smoke-test --generate sends ONE real billable generation request. "
              "Re-run with --i-accept-cost after setting BUDGET_VIDEO_REQUESTS_PER_DAY.", file=sys.stderr)
        return 2
    if s.provider_mode != "nvidia":
        print("smoke-test requires real providers (PROVIDER_MODE=nvidia)", file=sys.stderr)
        return 2
    from .providers.nim_video import VideoRequest
    from .qc import check_scene

    d = _deps(s)
    if d.video is None:
        print("video provider not configured (REPLICATE_API_TOKEN / NIM_VIDEO_URL)", file=sys.stderr)
        return 2
    out = Path(s.work_dir) / "smoke" / f"smoke_{int(time.time())}.mp4"
    frames = s.gen_min_frames
    req = VideoRequest(prompt="A paper boat floating on a calm pond at sunrise, gentle ripples, vertical framing, "
                       "photorealistic, slow camera push-in", num_frames=frames, fps=s.gen_fps,
                       resolution=args.resolution or s.gen_resolution)
    t0 = time.time()
    rid = d.video.generate(req, out)
    qc = check_scene(s, d.ff, out, frames / s.gen_fps)
    _print({"request_id": rid, "seconds": round(time.time() - t0, 1), "file": str(out),
            "qc": qc.model_dump(), "probe": d.ff.probe(out).get("streams", [])[:1]})
    return 0 if qc.ok else 1


# ---- pipeline commands ---------------------------------------------------------------------
def cmd_discover(s: Settings, args) -> int:
    d = _deps(s)
    _print(pipeline.discover(d))
    return 0


def cmd_run(s: Settings, args) -> int:
    if args.dry_run:
        s = s.model_copy(update={"dry_run": True})
    d = _deps(s)
    d.options["new_upload_session"] = False
    if args.job:
        job = d.store.get_job(args.job)
        if job is None:
            print("job not found", file=sys.stderr)
            return 1
        if not d.store.acquire_lock(pipeline.LOCK, d.owner, s.job_lease_seconds):
            print("pipeline is locked by another runner", file=sys.stderr)
            return 2
        try:
            d.options["new_upload_session"] = args.new_upload_session
            job = pipeline.advance(d, job)
        finally:
            d.store.release_lock(pipeline.LOCK, d.owner)
        res = {"status": "ok", "job_id": job.id, "state": job.state.value, "reason": job.reason}
    else:
        res = pipeline.run_once(d)
    _print(res)
    state = res.get("state")
    blocked = state in {b.value for b in BLOCKED} or (res.get("reason") or "").startswith("configuration:")
    return 2 if blocked else 0


def cmd_status(s: Settings, args) -> int:
    from .budget import BudgetGuard
    from .store import Store

    st = Store(s.database_url.get_secret_value())
    st.migrate()
    if args.job:
        job = st.get_job(args.job)
        if not job:
            print("job not found", file=sys.stderr)
            return 1
        stages = {k: st.get_stage(job.id, k) for k in ("analysis", "script", "generate", "render", "validate",
                                                       "upload", "publish")}
        _print({"job": job.__dict__, "events": st.events(job.id), "ops": st.ops_for_job(job.id),
                "upload": _upload_view(st.get_upload(job.id)), "stages": stages})
    else:
        _print({"jobs": [{"id": j.id, "state": j.state.value, "video_id": j.video_id, "dry_run": j.dry_run,
                          "reason": j.reason, "updated": datetime.fromtimestamp(j.updated_at, UTC).isoformat()}
                         for j in st.list_jobs(limit=args.limit)],
                "budget": BudgetGuard(st, s.budget).usage()})
    return 0


def _upload_view(u: dict | None) -> dict | None:
    if not u:
        return None
    u = dict(u)
    u["session_uri_enc"] = "<encrypted>" if u.get("session_uri_enc") else None
    return u


def cmd_review(s: Settings, args) -> int:
    from .store import Store

    st = Store(s.database_url.get_secret_value())
    st.migrate()
    items = st.list_jobs(limit=100, states=[JobState.REVIEW_REQUIRED, JobState.GENERATION_UNKNOWN,
                                            JobState.UPLOAD_UNKNOWN])
    _print([{"id": j.id, "state": j.state.value, "reason": j.reason,
             "approvable": j.state == JobState.REVIEW_REQUIRED and j.resume_state is not None,
             "on_approve": j.resume_state.value if j.resume_state else None} for j in items])
    return 0


def _store(s: Settings):
    from .store import Store

    st = Store(s.database_url.get_secret_value())
    st.migrate()
    return st


def cmd_approve(s: Settings, args) -> int:
    j = pipeline.approve(_store(s), args.job)
    _print({"job_id": j.id, "state": j.state.value})
    return 0


def cmd_reject(s: Settings, args) -> int:
    j = pipeline.reject(_store(s), args.job, args.reason)
    _print({"job_id": j.id, "state": j.state.value})
    return 0


def cmd_retry(s: Settings, args) -> int:
    scenes = [int(x) for x in args.regenerate_scenes.split(",")] if args.regenerate_scenes else None
    j = pipeline.retry(_store(s), args.job, args.stage, scenes)
    _print({"job_id": j.id, "state": j.state.value,
            "next": f"shorts run --job {j.id}" + (" --new-upload-session" if args.stage == "upload" else "")})
    return 0


def cmd_reconcile(s: Settings, args) -> int:
    d = _deps(s)
    job = d.store.get_job(args.job)
    if job is None:
        print("job not found", file=sys.stderr)
        return 1
    job = pipeline.reconcile_generation(d, job)
    _print({"job_id": job.id, "state": job.state.value, "ops": d.store.ops_for_job(job.id)})
    return 0 if job.state != JobState.GENERATION_UNKNOWN else 2


def cmd_resolve_op(s: Settings, args) -> int:
    from .storage import make_store

    pipeline.resolve_op(_store(s), make_store(s), args.op_key, args.note,
                        Path(args.artifact) if args.artifact else None)
    print("resolved")
    return 0


def cmd_auth(s: Settings, args) -> int:
    from .youtube.oauth import run_local_auth

    s.require("youtube_client_id", "youtube_client_secret")
    run_local_auth(s.youtube_client_id, s.youtube_client_secret.get_secret_value(),  # type: ignore[union-attr]
                   Path(args.out), s.youtube_token_url, open_browser=not args.no_browser)
    return 0


def cmd_purge(s: Settings, args) -> int:
    from .storage import make_store

    st = _store(s)
    res = st.purge_expired(time.time())
    res["artifacts_deleted"] = make_store(s).delete_older_than("jobs/", s.artifact_retention_days * 86400)
    work = Path(s.work_dir)
    removed = 0
    if work.exists():
        active = {j.id for j in st.list_jobs(limit=200) if j.state not in pipeline.TERMINAL}
        for p in work.iterdir():
            if p.is_dir() and p.name not in active:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
    res["work_dirs_removed"] = removed
    _print(res)
    return 0


def cmd_backup_upload(s: Settings, args) -> int:
    from .storage import make_store

    src = Path(args.file)
    if not src.is_file() or src.stat().st_size == 0:
        print(f"backup file missing or empty: {src}", file=sys.stderr)
        return 1
    key = f"backups/{datetime.now(UTC).strftime('%Y-%m-%d')}/shorts.dump"
    art = make_store(s)
    art.put_file(key, src, "application/octet-stream")
    removed = art.delete_older_than("backups/", s.backup_retention_days * 86400)
    _print({"uploaded": key, "bytes": src.stat().st_size, "old_backups_deleted": removed})
    return 0


def cmd_migrate(s: Settings, args) -> int:
    _store(s)
    print("migrated")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shorts")
    p.add_argument("--json-logs", action="store_true")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check configuration; --live makes free/cheap real requests")
    d.add_argument("--live", action="store_true")
    sm = sub.add_parser("smoke-test", help="ONE real billable video generation (25 frames)")
    sm.add_argument("--i-accept-cost", action="store_true")
    sm.add_argument("--resolution", default=None, help="default: provider resolution (e.g. 480p for cheapest)")
    sub.add_parser("discover")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="force DRY_RUN for this run")
    r.add_argument("--job")
    r.add_argument("--new-upload-session", action="store_true",
                   help="after verifying in Studio that no video exists, allow a new upload session")
    st = sub.add_parser("status")
    st.add_argument("job", nargs="?")
    st.add_argument("--limit", type=int, default=20)
    sub.add_parser("review")
    a = sub.add_parser("approve")
    a.add_argument("job")
    rj = sub.add_parser("reject")
    rj.add_argument("job")
    rj.add_argument("--reason", required=True)
    rt = sub.add_parser("retry")
    rt.add_argument("job")
    rt.add_argument("--stage", required=True, choices=list(pipeline.STAGE_ENTRY))
    rt.add_argument("--regenerate-scenes", help="comma-separated scene indexes to generate again (billable)")
    rc = sub.add_parser("reconcile", help="recover generation_unknown results by request id (no resend)")
    rc.add_argument("job")
    ro = sub.add_parser("resolve-op")
    ro.add_argument("op_key")
    ro.add_argument("--note", required=True)
    ro.add_argument("--artifact", help="recovered MP4 for this scene; without it the op is marked failed")
    au = sub.add_parser("auth")
    au_sub = au.add_subparsers(dest="auth_cmd", required=True)
    ai = au_sub.add_parser("init")
    ai.add_argument("--out", required=True, help="file for the refresh token (mode 0600)")
    ai.add_argument("--no-browser", action="store_true")
    sub.add_parser("purge")
    bu = sub.add_parser("backup-upload", help="copy a pg_dump file to artifact storage (backups/)")
    bu.add_argument("file")
    sub.add_parser("migrate")

    args = p.parse_args(argv)
    setup_logging(args.log_level, args.json_logs)
    try:
        s = load_settings()
        handlers = {"doctor": cmd_doctor, "smoke-test": cmd_smoke, "discover": cmd_discover, "run": cmd_run,
                    "status": cmd_status, "review": cmd_review, "approve": cmd_approve, "reject": cmd_reject,
                    "retry": cmd_retry, "reconcile": cmd_reconcile, "resolve-op": cmd_resolve_op,
                    "auth": cmd_auth, "purge": cmd_purge, "migrate": cmd_migrate,
                    "backup-upload": cmd_backup_upload}
        return handlers[args.cmd](s, args)
    except (ConfigError, ValueError) as e:
        log.error("%s", e)
        return 1
    except Exception as e:  # noqa: BLE001
        log.exception("command failed: %s", type(e).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
