"""End-to-end pipeline with mock providers, synthetic media and a fake YouTube.

No network, no spend, nothing published. Requires ffmpeg (runs in the Docker test image and CI).
"""

from __future__ import annotations

import base64
import json
import os

import httpx
import pytest

from shorts_factory import pipeline
from shorts_factory.config import load_settings
from shorts_factory.factory import make_deps
from shorts_factory.ff import FF
from shorts_factory.models import JobState
from shorts_factory.providers.mock import MockVideo
from shorts_factory.qc import check_final, check_scene
from shorts_factory.youtube.oauth import TokenProvider
from shorts_factory.youtube.upload import SessionCipher, YouTubeClient

from .conftest import needs_ffmpeg
from .fake_youtube import CHANNEL, FakeYouTube

pytestmark = needs_ffmpeg


def _deps(settings_env, **overrides):
    env = dict(settings_env, **overrides)
    return make_deps(load_settings(env))


def _job(d):
    return d.store.list_jobs(limit=1)[0]


def test_dry_run_end_to_end(settings_env):
    d = _deps(settings_env)
    res = pipeline.run_once(d)
    assert res["state"] == "done_dry_run", res
    job = _job(d)
    assert job.video_id == "fixtAI00001"
    an = d.store.get_stage(job.id, "analysis")["analysis"]
    assert an["mode"] == "metadata_only" and an["pacing"] is None and an["limitations"]
    sc = d.store.get_stage(job.id, "script")
    assert sc["prompt"] == "script@v3" and sc["format"] in d.s.content_formats
    qc = d.store.get_stage(job.id, "validate")["qc"]
    assert qc["ok"], qc
    assert qc["width"] == 360 and qc["height"] == 640 and qc["has_audio"] is True  # mock TTS narration
    up = d.store.get_stage(job.id, "upload")
    assert up["dry_run"] and up["would_upload"] is None  # PUBLISH_MADE_FOR_KIDS not chosen -> blocked
    assert any("PUBLISH_MADE_FOR_KIDS" in n for n in up["notes"])
    assert d.video.calls == 3
    assert d.artifacts.exists(f"jobs/{job.id}/final.mp4")
    # same day: no second job (daily limit 1)
    assert pipeline.run_once(d)["status"] == "daily_limit"


def test_restart_between_stages_does_not_regenerate(settings_env, monkeypatch):
    d = _deps(settings_env)
    real = pipeline.stage_render
    boom = {"n": 0}

    def crash_once(dd, job):
        if boom["n"] == 0:
            boom["n"] += 1
            raise RuntimeError("simulated pod OOM-kill during render")
        return real(dd, job)
    monkeypatch.setattr(pipeline, "stage_render", crash_once)
    with pytest.raises(RuntimeError):
        pipeline.run_once(d)
    job = _job(d)
    assert job.state == JobState.GENERATED and d.video.calls == 3
    d2 = _deps(settings_env)  # "new pod": fresh provider objects
    d2.video.calls = 0
    res = pipeline.run_once(d2)
    assert res["state"] == "done_dry_run" and d2.video.calls == 0


def test_generation_unknown_is_not_resent_and_reconciles(settings_env):
    d = _deps(settings_env)
    d.video = MockVideo(d.ff, 192, 320, script=["ok", "unknown"])
    res = pipeline.run_once(d)
    assert res["state"] == "generation_unknown", res
    calls = d.video.calls
    # Running again must not resend scene 1; reconcile polls by request id instead.
    pipeline.advance(d, _job(d))
    assert d.video.calls == calls
    job = pipeline.reconcile_generation(d, _job(d))
    assert job.state == JobState.SCRIPTED
    job = pipeline.advance(d, job)
    assert job.state == JobState.DONE_DRY_RUN
    assert d.video.calls == calls + 1  # only scene 2 generated afterwards; scene 0 and 1 reused


def test_unknown_without_request_id_requires_operator(settings_env):
    d = _deps(settings_env)
    d.video = MockVideo(d.ff, 192, 320, script=["ok", "unknown"])
    pipeline.run_once(d)
    job = _job(d)
    op = [o for o in d.store.ops_for_job(job.id) if o["status"] == "unknown"][0]
    d.video.pending.clear()  # provider cannot find the request
    assert pipeline.reconcile_generation(d, job).state == JobState.GENERATION_UNKNOWN
    with pytest.raises(ValueError, match="resolve unknown"):
        pipeline.retry(d.store, job.id, "generate")
    pipeline.resolve_op(d.store, d.artifacts, op["op_key"], "checked provider dashboard: not billed")
    job = pipeline.advance(d, pipeline.retry(d.store, job.id, "generate"))
    assert job.state == JobState.DONE_DRY_RUN


def test_corrupt_scene_is_regenerated_once_then_review(settings_env):
    d = _deps(settings_env)
    d.video = MockVideo(d.ff, 192, 320, script=["corrupt", "ok", "ok", "ok"])
    assert pipeline.run_once(d)["state"] == "done_dry_run"
    ops = d.store.ops_for_job(_job(d).id)
    assert [o["status"] for o in ops] == ["failed", "succeeded", "succeeded", "succeeded"]
    assert ops[0]["detail"].startswith("bad result")

    d = _deps(dict(settings_env, DATABASE_URL=settings_env["DATABASE_URL"] + "2"))
    d.video = MockVideo(d.ff, 192, 320, script=["corrupt", "corrupt"])
    res = pipeline.run_once(d)
    assert res["state"] == "review_required" and "failed attempts" in res["reason"]


def test_guardrail_rejection_goes_to_review(settings_env):
    d = _deps(settings_env)
    d.video = MockVideo(d.ff, 192, 320, script=["error"])
    res = pipeline.run_once(d)
    assert res["state"] == "review_required" and "rejected" in res["reason"]
    with pytest.raises(ValueError, match="cannot be approved"):
        pipeline.approve(d.store, _job(d).id)


def test_budget_exhaustion_defers_without_state_loss(settings_env):
    d = _deps(settings_env, BUDGET_VIDEO_REQUESTS_PER_DAY="1")
    res = pipeline.run_once(d)
    assert res["state"] == "scripted" and "deferred" in res["reason"]
    ops = d.store.ops_for_job(_job(d).id)
    assert ops[-1]["status"] == "failed" and ops[-1]["detail"].startswith("not sent")


def test_disk_limit_defers(settings_env):
    d = _deps(settings_env, MAX_WORK_DIR_BYTES="1")
    os.makedirs(d.s.work_dir, exist_ok=True)
    with open(os.path.join(d.s.work_dir, "junk"), "wb") as f:
        f.write(b"xx")
    res = pipeline.run_once(d)
    assert res["state"] == "scripted" and "MAX_WORK_DIR_BYTES" in res["reason"]


def test_full_media_analysis_from_licensed_reference(settings_env, tmp_path):
    ref = tmp_path / "ref" / "fixtAI00001"
    ref.mkdir(parents=True)
    FF().run(["-f", "lavfi", "-i", "testsrc2=size=180x320:rate=24:duration=4", "-f", "lavfi", "-i",
              "sine=frequency=440:duration=4", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
              "-c:a", "aac", str(ref / "clip.mp4")])
    (ref / "manifest.json").write_text(json.dumps({"video_id": "fixtAI00001", "license": "test fixture, owned",
                                                   "provided_by": "test-suite", "files": ["clip.mp4"],
                                                   "is_short": True}))
    d = _deps(settings_env, REFERENCE_SOURCE="local", REFERENCE_LOCAL_DIR=str(tmp_path / "ref"))
    assert pipeline.run_once(d)["state"] == "done_dry_run"
    st = d.store.get_stage(_job(d).id, "analysis")
    a = st["analysis"]
    assert a["mode"] == "full_media" and st["candidate_format"] == "confirmed"
    assert any("license='test fixture, owned'" in s for s in a["sources"])
    assert "content not analysed" in a["audio_role"]
    assert a["ai"]["classification_source"] == "combined"


def test_manifest_without_license_falls_back_to_metadata_only(settings_env, tmp_path):
    ref = tmp_path / "ref" / "fixtAI00001"
    ref.mkdir(parents=True)
    (ref / "clip.mp4").write_bytes(b"x")
    (ref / "manifest.json").write_text(json.dumps({"video_id": "fixtAI00001", "files": ["clip.mp4"]}))
    d = _deps(settings_env, REFERENCE_SOURCE="local", REFERENCE_LOCAL_DIR=str(tmp_path / "ref"))
    pipeline.run_once(d)
    assert d.store.get_stage(_job(d).id, "analysis")["analysis"]["mode"] == "metadata_only"


def test_qc_detects_black_and_wrong_format(settings, tmp_path):
    ff = FF()
    black = tmp_path / "black.mp4"
    ff.run(["-f", "lavfi", "-i", "color=c=black:size=192x320:rate=24:duration=3", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(black)])
    rep = check_scene(settings, ff, black, 3)
    assert not rep.ok and any(f.startswith("black_frames") for f in rep.failures)
    land = tmp_path / "land.mp4"
    ff.run(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=20", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(land)])
    rep = check_final(settings, ff, land, expect_audio=True)
    names = {f.split(":")[0] for f in rep.failures}
    assert {"resolution", "aspect_9_16", "audio"} <= names


def test_real_upload_path_with_fake_youtube_private_first(settings_env):
    env = dict(settings_env, DRY_RUN="false", AUTO_PUBLISH="true", PUBLISH_MADE_FOR_KIDS="false",
               PUBLISH_PRIVACY="public", PUBLISH_CONSENT_CHANNEL_ID=CHANNEL, YOUTUBE_CHANNEL_ID=CHANNEL,
               YOUTUBE_CLIENT_ID="cid", YOUTUBE_CLIENT_SECRET="csecret", YOUTUBE_REFRESH_TOKEN="1//refresh-abcdefghijk",
               UPLOAD_URI_KEY=base64.b64encode(os.urandom(32)).decode(),
               YOUTUBE_API_BASE="https://api.fake/youtube/v3", YOUTUBE_UPLOAD_URL="https://api.fake/upload/videos",
               YOUTUBE_TOKEN_URL="https://api.fake/token", UPLOAD_CHUNK_BYTES=str(256 * 1024))
    s = load_settings(env)
    d = make_deps(s)
    fake = FakeYouTube()
    http = httpx.Client(transport=fake.transport())
    d.youtube = YouTubeClient(s, d.store, d.budget, TokenProvider(http, s.youtube_token_url, "cid", "csecret",
                                                                  "1//refresh-abcdefghijk"),
                              http, SessionCipher(s.upload_uri_key.get_secret_value()), sleep=lambda _: None)
    res = pipeline.run_once(d)
    assert res["state"] == "published", res
    meta = next(iter(fake.sessions.values()))["meta"]
    assert meta["status"]["privacyStatus"] == "private"  # first real upload forced private despite consent
    assert meta["status"]["selfDeclaredMadeForKids"] is False and meta["status"]["containsSyntheticMedia"] is True
    assert "сгенерирован" in meta["snippet"]["description"]
    job = _job(d)
    with pytest.raises(ValueError, match="duplicate"):
        pipeline.retry(d.store, job.id, "render")
    pipeline.run_once(d)
    assert fake.inits == 1  # no duplicate upload on rerun


def test_upload_requires_explicit_audience_choice(settings_env):
    s = load_settings(dict(settings_env, DRY_RUN="false"))
    from shorts_factory.config import ConfigError
    from shorts_factory.models import Script
    from shorts_factory.providers.mock import default_mock_responses

    d = make_deps(s.model_copy(update={"dry_run": True}))
    with pytest.raises(ConfigError, match="PUBLISH_MADE_FOR_KIDS"):
        pipeline.build_upload_request(s, d.store, Script.model_validate(default_mock_responses()["Script"]))


def test_operator_can_regenerate_selected_scenes_only(settings_env):
    d = _deps(settings_env)
    assert pipeline.run_once(d)["state"] == "done_dry_run"
    job = _job(d)
    calls = d.video.calls
    with pytest.raises(ValueError):
        pipeline.retry(d.store, job.id, "render", regenerate_scenes=[1])
    job = pipeline.advance(d, pipeline.retry(d.store, job.id, "generate", regenerate_scenes=[1]))
    assert job.state == JobState.DONE_DRY_RUN and d.video.calls == calls + 1


def test_missing_video_endpoint_blocks_with_reason(settings_env):
    d = _deps(settings_env)
    d.video = None  # as in production without NIM_VIDEO_URL
    res = pipeline.run_once(d)
    assert res["state"] == "scripted" and res["reason"].startswith("configuration: video generator")


def test_failed_job_does_not_block_a_new_one_same_day(settings_env):
    d = _deps(settings_env)
    d.video = MockVideo(d.ff, 192, 320, script=["error"])
    assert pipeline.run_once(d)["state"] == "review_required"
    pipeline.reject(d.store, _job(d).id, "bad prompt")
    # the rejected candidate is used; the fixture has no other AI candidate -> no_candidate, not a key clash
    assert pipeline.run_once(d)["status"] == "no_candidate"
