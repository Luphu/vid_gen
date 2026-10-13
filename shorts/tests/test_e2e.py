"""End-to-end pipeline with mock providers, synthetic media and a fake YouTube.

No network, no spend, nothing published. Requires ffmpeg (runs in the Docker test image and CI).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
from datetime import UTC, datetime

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
    assert sc["prompt"].startswith("team@v2(") and sc["format"] in d.s.content_formats
    assert sc["team"]["final"][-1]["scores"] == {"writer": 9, "director": 9, "editor": 9}
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
    ops = [o for o in d.store.ops_for_job(_job(d).id) if o["kind"] == "video_gen"]
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


def test_longest_silence_and_pause_gate(settings, tmp_path):
    ff = FF()
    wav = tmp_path / "n.wav"
    ff.run(["-f", "lavfi", "-i", "sine=f=300:d=1", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2.5",
            "-f", "lavfi", "-i", "sine=f=300:d=1", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.8",
            "-filter_complex", "[0][1][2][3]concat=n=4:v=0:a=1", str(wav)])
    assert 2.3 < ff.longest_silence(wav) < 2.7
    land = tmp_path / "v.mp4"
    ff.run(["-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=16", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(land)])
    bad = check_final(settings, ff, land, expect_audio=False, narration_pause_s=2.6)
    assert any(f.startswith("narration_pauses") for f in bad.failures)
    good = check_final(settings, ff, land, expect_audio=False, narration_pause_s=1.4)
    assert not any(f.startswith("narration_pauses") for f in good.failures)


def test_narration_j_cut_shortens_long_pause(tmp_path):
    from shorts_factory.ff import duration_of
    from shorts_factory.render import build_narration

    ff = FF()
    tones = []
    for i in range(2):
        tones.append(tmp_path / f"t{i}.wav")
        ff.run(["-f", "lavfi", "-i", "sine=f=300:d=1", "-ac", "1", str(tones[i])])
    # phrase 1 ends at 1.0 s, scene 2 starts at 3.0 s: 2.0 s of silence > 1.5 -> phrase 2 starts 0.5 s early
    out = build_narration(ff, [(tones[0], 3.0), (tones[1], 1.2)], tmp_path / "n.wav", max_pause_s=1.5)
    assert 1.35 < ff.longest_silence(out) < 1.65
    assert abs(duration_of(ff.probe(out)) - 4.2) < 0.05
    plain = build_narration(ff, [(tones[0], 3.0), (tones[1], 1.2)], tmp_path / "p.wav")
    assert ff.longest_silence(plain) > 1.9


def test_kids_trend_public_signals_dynamic_edit(settings_env):
    from .conftest import EXAMPLES

    d = _deps(settings_env, TREND_MODE="kids", SELECT_REQUIRE_AI="false", TREND_REQUIRE_MADE_FOR_KIDS="true",
              TREND_FIXTURE_FILE=str(EXAMPLES / "kids_trends_fixture.json"))
    res = pipeline.run_once(d)
    assert res["state"] == "done_dry_run", res
    job = _job(d)
    assert job.video_id == "kidsBBBB002"
    a = d.store.get_stage(job.id, "analysis")["analysis"]
    assert a["mode"] == "public_signals" and a["plot_beats"] and a["pacing"] is None
    assert any(s.startswith("thumbnail:https://i.ytimg.com/") for s in a["sources"])
    edit = d.store.get_stage(job.id, "render")["edit"]
    assert edit["shots"] >= 9 and edit["avg_shot_s"] <= 2.0 and edit["first_shot_s"] == 1.2
    qc = d.store.get_stage(job.id, "validate")["qc"]
    assert any(c["name"] == "pacing" and c["ok"] for c in qc["checks"])
    assert 0 < edit["narration_max_pause_s"] <= 1.5  # mock voice: 11 words ≈ 5 s per 6 s scene
    assert any(c["name"] == "narration_pauses" and c["ok"] for c in qc["checks"])


def test_render_recovers_empty_narration_and_blocks_on_broken_clip(settings_env, monkeypatch):
    d = _deps(settings_env)
    real = pipeline.stage_render
    monkeypatch.setattr(pipeline, "stage_render", lambda dd, j: (_ for _ in ()).throw(RuntimeError("power cut")))
    with pytest.raises(RuntimeError):
        pipeline.run_once(d)
    monkeypatch.setattr(pipeline, "stage_render", real)
    job = _job(d)
    assert job.state == JobState.GENERATED
    root = d.artifacts.root / "jobs" / job.id
    for wav in (root / "audio").glob("*.wav"):
        wav.write_bytes(b"")  # what the SD card left after the power loss
    shutil.rmtree(d.s.work_dir, ignore_errors=True)
    calls = d.video.calls
    job = pipeline.advance(d, job)
    assert job.state == JobState.DONE_DRY_RUN and d.video.calls == calls  # TTS redone, no paid regeneration
    assert all(w.stat().st_size > 1024 for w in (root / "audio").glob("*.wav"))

    d2 = _deps(dict(settings_env, DATABASE_URL=settings_env["DATABASE_URL"] + "2"))
    monkeypatch.setattr(pipeline, "stage_render", lambda dd, j: (_ for _ in ()).throw(RuntimeError("power cut")))
    with pytest.raises(RuntimeError):
        pipeline.run_once(d2)
    monkeypatch.setattr(pipeline, "stage_render", real)
    job2 = _job(d2)
    (d2.artifacts.root / "jobs" / job2.id / "scenes" / "01.mp4").write_bytes(b"")
    shutil.rmtree(d2.s.work_dir, ignore_errors=True)
    job2 = pipeline.advance(d2, job2)
    assert job2.state == JobState.REVIEW_REQUIRED and "scenes [1]" in job2.reason


def test_i2v_chain_keyframe_and_lettering_retry(settings_env):
    from shorts_factory.qc_vision import TextCheck

    d = _deps(settings_env)
    assert d.s.video_mode == "i2v_chain" and d.image is not None
    answers = [True, False, False, False, False]  # keyframe #1 has text -> regenerated; clips clean

    class Vision:
        def frames_content(self, frames):
            return [{"type": "text", "text": "img"}]

        def structured(self, messages, model_cls, *, max_tokens, context=None):
            assert model_cls is TextCheck
            t = answers.pop(0)
            return TextCheck(has_text=t, text_seen="A12" if t else None, where="engraved code"), "v"
    d.vision = Vision()
    starts = []
    real_generate = d.video.generate

    def spy(req, out, on_submitted=None):
        starts.append(req.image.name if req.image else None)
        return real_generate(req, out, on_submitted)
    d.video.generate = spy
    assert pipeline.run_once(d)["state"] == "done_dry_run"
    job = _job(d)
    img_ops = [o for o in d.store.ops_for_job(job.id) if o["kind"] == "image_gen"]
    assert [o["status"] for o in img_ops] == ["failed", "succeeded"] and "lettering" in img_ops[0]["detail"]
    assert starts == ["keyframe.jpg", "start_01.jpg", "start_02.jpg"]  # each scene continues the previous
    gen = d.store.get_stage(job.id, "generate")["scenes"]
    assert [g["start"] for g in gen] == ["keyframe", "previous_clip", "previous_clip"]
    assert d.artifacts.exists(f"jobs/{job.id}/keyframe.jpg")


def test_lettering_in_clip_triggers_one_regeneration(settings_env):
    from shorts_factory.qc_vision import TextCheck

    d = _deps(settings_env)
    answers = [False, True, False, False, False]  # keyframe ok, scene 0 first clip has text

    class Vision:
        def frames_content(self, frames):
            return []

        def structured(self, messages, model_cls, *, max_tokens, context=None):
            t = answers.pop(0)
            return TextCheck(has_text=t, text_seen="XY" if t else None), "v"
    d.vision = Vision()
    assert pipeline.run_once(d)["state"] == "done_dry_run"
    ops = [o for o in d.store.ops_for_job(_job(d).id) if o["kind"] == "video_gen"]
    assert [o["status"] for o in ops][:2] == ["failed", "succeeded"] and "lettering" in ops[0]["detail"]


def test_lettering_on_last_attempt_is_accepted_with_warning(settings_env):
    from shorts_factory.qc_vision import TextCheck

    d = _deps(settings_env, LETTERING_LAST_ATTEMPT="accept")
    answers = [False, True, True, False, False]  # keyframe ok; scene 0 flagged on both attempts

    class Vision:
        def frames_content(self, frames):
            return []

        def structured(self, messages, model_cls, *, max_tokens, context=None):
            t = answers.pop(0)
            return TextCheck(has_text=t, text_seen="QQ" if t else None), "v"
    d.vision = Vision()
    assert pipeline.run_once(d)["state"] == "done_dry_run"
    job = _job(d)
    ops = [o for o in d.store.ops_for_job(job.id) if o["kind"] == "video_gen"]
    assert [o["status"] for o in ops][:2] == ["failed", "succeeded"] and d.video.calls == 4  # no third paid try
    assert d.store.get_stage(job.id, "generate")["warnings"][0].startswith("scene 0: possible lettering")


def test_lettering_on_last_attempt_stops_for_review_by_default(settings_env):
    from shorts_factory.qc_vision import TextCheck

    d = _deps(settings_env)
    answers = [False, True, True]  # keyframe ok; scene 0 flagged on both attempts

    class Vision:
        def frames_content(self, frames):
            return []

        def structured(self, messages, model_cls, *, max_tokens, context=None):
            t = answers.pop(0)
            return TextCheck(has_text=t, text_seen="HEINZ" if t else None), "v"
    d.vision = Vision()
    res = pipeline.run_once(d)
    assert res["state"] == "review_required" and d.video.calls == 2
    job = _job(d)
    assert "lettering on the last attempt" in job.reason and "resolve-op" in job.reason
    assert d.artifacts.exists(f"jobs/{job.id}/review/scene_00.mp4")


def test_script_lab_runs_team_without_jobs_or_media(settings_env, tmp_path):
    from shorts_factory import lab

    from .conftest import EXAMPLES

    d = _deps(settings_env, TREND_MODE="kids", SELECT_REQUIRE_AI="false", TREND_REQUIRE_MADE_FOR_KIDS="true",
              TREND_FIXTURE_FILE=str(EXAMPLES / "kids_trends_fixture.json"))
    pipeline.discover(d)
    refs = lab.pick_references(d, 2)
    assert refs and len({c.channel_id for c in refs}) == len(refs)
    out = lab.run_lab(d, refs, tmp_path / "lab" / "run1")
    assert out["approved"] == len(refs) and d.video.calls == 0 and d.image is not None
    assert d.store.list_jobs(limit=5) == []
    now = datetime.now(UTC).timestamp()
    assert {c["video_id"] for c in d.store.unused_candidates(now)} >= {c.video_id for c in refs}
    md = (tmp_path / "lab" / "run1" / "report.md").read_text()
    assert "Approved" in md and "| # | s | narration" in md and "Main subject:" in md
    assert (tmp_path / "lab" / "analysis").is_dir()  # analysis cached for the next run


def test_missing_key_prop_in_clip_triggers_one_regeneration(settings_env):
    from shorts_factory.qc_vision import PropCheck, TextCheck

    d = _deps(settings_env)
    plan = d.agent.responses["ShotPlan"]
    plan["setting"] += ", a tiny white origami crane on the desk"
    plan["key_props"] = ["a tiny white origami crane"]
    plan["scenes"][0]["must_show"] = ["a tiny white origami crane"]
    seen = iter([True, False, False, True])  # keyframe ok; clip 0: start+mid miss it; retry: visible

    class Vision:
        def frames_content(self, frames):
            return []

        def structured(self, messages, model_cls, *, max_tokens, context=None):
            if model_cls is TextCheck:
                return TextCheck(has_text=False), "v"
            assert model_cls is PropCheck
            return PropCheck(items=[{"name": "a tiny white origami crane", "visible": next(seen)}]), "v"
    d.vision = Vision()
    assert pipeline.run_once(d)["state"] == "done_dry_run"
    job = _job(d)
    ops = [o for o in d.store.ops_for_job(job.id) if o["kind"] == "video_gen"]
    assert ops[0]["status"] == "failed" and "missing" in ops[0]["detail"] and d.video.calls == 4
    script = d.store.get_stage(job.id, "script")["script"]
    assert script["key_props"] == ["a tiny white origami crane"]
