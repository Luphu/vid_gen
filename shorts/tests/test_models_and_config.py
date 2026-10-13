from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from shorts_factory.config import ConfigError, load_settings
from shorts_factory.log import redact, register_secret
from shorts_factory.models import Analysis, Script, UploadRequest
from shorts_factory.providers.jsonout import extract_json_object
from shorts_factory.providers.mock import default_mock_responses

SCRIPT = default_mock_responses()["Script"]

RU = {"CONTENT_LANGUAGE": "ru", "NARRATION_WORDS_PER_S": "2.2",  # the mock data is Russian
      "TTS_VOICES": '{"Mock_Voice": {"desc": "test voice", "wps": 2.2}}'}


def test_script_valid_and_limits():
    s = Script.model_validate(SCRIPT)
    assert s.total_duration_s == 18
    assert s.check_limits(15, 30, 8.2) == []
    assert s.check_limits(20, 30, 8.2)[0].startswith("total duration")
    assert "exceeds generator limit" in s.check_limits(15, 30, 5.0)[0]


def test_script_rejects_unknown_fields_and_bad_disclosure():
    bad = copy.deepcopy(SCRIPT)
    bad["chain_of_thought"] = "..."
    with pytest.raises(ValidationError):
        Script.model_validate(bad)
    bad = copy.deepcopy(SCRIPT)
    bad["disclosure"]["contains_synthetic_media"] = False
    with pytest.raises(ValidationError):
        Script.model_validate(bad)
    bad = copy.deepcopy(SCRIPT)
    bad["title"] = "<b>clickbait</b>"
    with pytest.raises(ValidationError):
        Script.model_validate(bad)


def test_metadata_only_cannot_describe_media():
    base = dict(mode="metadata_only", video_id="abcdefghijk", topic="topic", sources=["api"])
    Analysis(**base)
    for extra in ({"pacing": "fast"}, {"scene_changes": 4}, {"audio_role": "music"}, {"structure": ["a"]},
                  {"on_screen_text": ["x"]}):
        with pytest.raises(ValidationError):
            Analysis(**base, **extra)
    with pytest.raises(ValidationError):
        Analysis(mode="metadata_only", video_id="abcdefghijk", topic="t", sources=[])


def test_upload_request_publish_at_requires_private():
    kw = dict(title="t", description="d", tags=[], category_id="28", self_declared_made_for_kids=False,
              contains_synthetic_media=True, default_language="ru")
    with pytest.raises(ValidationError):
        UploadRequest(privacy_status="public", publish_at=datetime(2030, 1, 1, tzinfo=UTC), **kw)
    r = UploadRequest(privacy_status="private", publish_at=datetime(2030, 1, 1, tzinfo=UTC), **kw).to_resource()
    assert r["status"]["publishAt"] == "2030-01-01T00:00:00Z"
    assert r["status"]["containsSyntheticMedia"] is True
    assert r["status"]["privacyStatus"] == "private"


def test_extract_json_drops_reasoning():
    out = extract_json_object('<think>secret plan {"x": 1}</think>\nHere: ```json\n{"a": "b}"}\n```')
    assert out == {"a": "b}"}
    assert extract_json_object('</think>{"a": {"b": 2}} trailing') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json_object("no json")


def test_prod_guards():
    with pytest.raises(ConfigError):
        load_settings({"APP_ENV": "prod", "PROVIDER_MODE": "mock", "DATABASE_URL": "postgresql://x/y"})
    with pytest.raises(ConfigError):
        load_settings({"APP_ENV": "prod", "DATABASE_URL": "sqlite:///x.db"})
    with pytest.raises(ConfigError):
        load_settings({"APP_ENV": "prod", "TTS_PROVIDER": "mock", "DATABASE_URL": "postgresql://x/y"})
    s = load_settings({"APP_ENV": "prod", "DATABASE_URL": "postgresql://u:p@h/db"})
    assert s.dry_run is True and s.auto_publish is False
    assert s.budget.video_requests_per_day == 0  # generation needs an explicit budget
    assert s.nim_video_url == ""  # no invented endpoint
    assert s.publish_made_for_kids is None


def test_secrets_not_in_repr_and_redacted():
    s = load_settings({"NVIDIA_API_KEY": "nvapi-supersecretvalue123", "BUDGET_TEXT_REQUESTS_PER_DAY": "3"})
    assert "supersecret" not in repr(s)
    assert s.budget.text_requests_per_day == 3
    register_secret("my-refresh-token-value")
    msg = ("key nvapi-abcdefghijk Bearer ya29.abcdefghijklmnop my-refresh-token-value "
           "https://x/upload?upload_id=SESSION123&part=1 postgresql://u:pw@h/db")
    out = redact(msg)
    for leaked in ("abcdefghijk", "ya29.abc", "my-refresh-token-value", "SESSION123", ":pw@"):
        assert leaked not in out, out


def test_env_parsing_by_type():
    s = load_settings({"PUBLISH_PRIVACY": "unlisted", "PUBLISH_MADE_FOR_KIDS": "false", "TREND_KEYWORDS": "a, b",
                       "NIM_VIDEO_EXTRA": '{"num_inference_steps": 35}', "DRY_RUN": "false", "TARGET_MAX_S": "25"})
    assert s.publish_privacy == "unlisted" and s.publish_made_for_kids is False
    assert s.trend_keywords == ["a", "b"] and s.nim_video_extra == {"num_inference_steps": 35}
    assert s.dry_run is False and s.target_max_s == 25.0
    with pytest.raises(ConfigError):
        load_settings({"DRY_RUN": "maybe"})


def test_config_errors_do_not_echo_secrets():
    with pytest.raises(ConfigError) as e:
        load_settings({"APP_ENV": "prod", "DATABASE_URL": "sqlite:///x.db", "NVIDIA_API_KEY": "nvapi-leakcheck-123456",
                       "YOUTUBE_REFRESH_TOKEN": "1//leakcheck-refresh-abcdef"})
    assert "leakcheck" not in str(e.value) and "PostgreSQL" in str(e.value)


@pytest.mark.parametrize("bad", [
    "Split screen: the dull pebble on the left and the glowing one on the right",
    "The same stone now glows softly in the forest",
    "A wooden sign with the word HELLO stands next to the robot",
    "Camera pans over the robot while captions appear",
])
def test_scene_prompt_rejects_what_video_models_cannot_do(bad):
    data = copy.deepcopy(SCRIPT)
    data["scenes"][1]["generation_prompt"] = bad
    with pytest.raises(ValidationError, match="generation_prompt must not contain"):
        Script.model_validate(data)


def test_compose_video_prompt_repeats_subject_and_style_in_every_scene():
    from shorts_factory.scripting import compose_video_prompt

    s = Script.model_validate(SCRIPT)
    prompts = [compose_video_prompt(s, sc) for sc in s.scenes]
    for p, sc in zip(prompts, s.scenes, strict=True):
        assert p.startswith(sc.generation_prompt.rstrip("."))
        assert f"Main subject: {s.subject}" in p and f"Style: {s.visual_style}" in p
        assert "no lettering" in p and "9:16" in p
    assert len(set(prompts)) == len(prompts)


def test_texture_is_not_mistaken_for_text():
    data = copy.deepcopy(SCRIPT)
    data["scenes"][0]["generation_prompt"] = "Macro shot of the robot's paper texture as light sweeps across it slowly"
    Script.model_validate(data)


def test_visual_fields_must_be_english_and_state_is_composed():
    from shorts_factory.scripting import compose_video_prompt

    data = copy.deepcopy(SCRIPT)
    data["subject"] = "небольшой кусок бурого лимонита"
    with pytest.raises(ValidationError, match="English"):
        Script.model_validate(data)
    data = copy.deepcopy(SCRIPT)
    data["scenes"][2]["subject_state"] = "now standing upright, its folds glowing brighter"
    s = Script.model_validate(data)
    assert "Main subject: " + s.subject.rstrip(".") + ", now standing upright" in compose_video_prompt(s, s.scenes[2])


def test_plan_shots_is_fast_and_covers_scene():
    from shorts_factory.render import plan_shots

    shots = plan_shots(clip_s=5.06, scene_s=6.0, first_scene=True, max_shot=2.0, first_shot=1.2, punch=1.15)
    assert shots[0].length == 1.2 and abs(sum(x.length for x in shots) - 6.0) < 0.01
    assert all(x.length <= 2.0 for x in shots) and len({x.zoom for x in shots}) > 1
    assert all(0 <= x.start <= 5.06 - x.length + 1e-6 for x in shots)
    assert [x.start for x in shots] == sorted(x.start for x in shots)
    one = plan_shots(clip_s=5.06, scene_s=1.5, first_scene=False, max_shot=2.0, first_shot=1.2, punch=1.15)
    assert len(one) == 1 and one[0].length == 1.5


def test_public_signals_cannot_claim_editing_or_frames():
    base = dict(mode="public_signals", video_id="abcdefghijk", topic="t", sources=["api"],
                plot_beats=[{"order": 0, "what_happens": "cat paints", "evidence": ["thumbnail"]}])
    Analysis(**base)
    with pytest.raises(ValidationError):
        Analysis(**base, pacing="fast")
    with pytest.raises(ValidationError):
        Analysis(**dict(base, plot_beats=[{"order": 0, "what_happens": "x", "evidence": ["frames"]}]))
    with pytest.raises(ValidationError):
        Analysis(**dict(base, plot_beats=[{"order": 0, "what_happens": "x", "evidence": []}]))


class _Seq:
    """Fake structured client returning prepared objects in order."""

    def __init__(self, *outs):
        self.outs, self.calls = list(outs), []

    def structured(self, messages, model_cls, *, max_tokens, context=None):
        self.calls.append(messages)
        return model_cls.model_validate(self.outs.pop(0)), "r"


def _fc(*verdicts):
    return {"claims": [{"text": f"c{i}", "verdict": v, "reason": "r"} for i, v in enumerate(verdicts)]}


def test_fact_check_repairs_then_rejects():
    from datetime import UTC, datetime

    from shorts_factory.config import load_settings
    from shorts_factory.models import Candidate
    from shorts_factory.scripting import ScriptRejected, generate_script

    s = load_settings({**RU, "SCRIPT_PUNCH_UP": "false"})
    cand = Candidate(video_id="abcdefghijk", title="Котик красит забор", channel_id="UCx",
                     published_at=datetime.now(UTC), fetched_at=datetime.now(UTC))
    an = Analysis(mode="metadata_only", video_id="abcdefghijk", topic="t", sources=["api"])
    text = _Seq(SCRIPT, SCRIPT)
    checker = _Seq(_fc("false", "story"), _fc("story", "true"))
    script, _ = generate_script(s, text, cand, an, "x", checker=checker)
    assert len(text.calls) == 2 and "fact-checker rejected" in text.calls[1][-1]["content"]
    assert text.calls[1][-2]["role"] == "assistant"
    with pytest.raises(ScriptRejected, match="fact-check"):
        generate_script(s, _Seq(SCRIPT, SCRIPT), cand, an, "x", checker=_Seq(_fc("uncertain"), _fc("false")))
    no_check = load_settings({**RU, "FACT_CHECK": "false", "SCRIPT_PUNCH_UP": "false"})
    generate_script(no_check, _Seq(SCRIPT), cand, an, "x", checker=_Seq())


def test_keyframe_prompt_has_subject_setting_style():
    from shorts_factory.scripting import compose_keyframe_prompt

    s = Script.model_validate(SCRIPT)
    p = compose_keyframe_prompt(s)
    assert s.subject.rstrip(".") in p and s.setting in p and "no lettering" in p and "9:16" in p


@pytest.mark.parametrize("field,bad", [("setting", "a lab with holographic displays and drones"),
                                       ("setting", "a classroom with posters on the walls"),
                                       ("subject", "a boy in a jacket with a name tag")])
def test_text_prone_props_rejected(field, bad):
    data = copy.deepcopy(SCRIPT)
    data[field] = bad
    with pytest.raises(ValidationError, match="fake lettering"):
        Script.model_validate(data)


def test_lettering_needs_transcription():
    from shorts_factory.qc_vision import TextCheck

    assert not TextCheck(has_text=True, where="on the blue circle").confirmed
    for meta in ["No text found", "I'm not able to identify the people in this image",
                 "The boy is wearing a lab coat and goggles, and he is holding"]:
        assert not TextCheck(has_text=True, text_seen=meta).confirmed, meta
    assert TextCheck(has_text=True, text_seen="NO 17").confirmed
    assert not TextCheck(has_text=True, text_seen="~~", where="x").confirmed
    assert TextCheck(has_text=True, text_seen="NAATS", where="vest").confirmed
    assert TextCheck(has_text=True, text_seen="Ок 1", where="x").confirmed
    assert not TextCheck(has_text=False, text_seen="ABC").confirmed


def test_stored_scripts_load_without_newer_generation_rules():
    data = copy.deepcopy(SCRIPT)
    data["setting"] = "a lab with holographic displays"
    with pytest.raises(ValidationError):
        Script.model_validate(data)
    assert Script.model_validate(data, context={"stored": True}).setting.startswith("a lab")


def test_punch_up_then_voice_over_density_is_enforced():
    from datetime import UTC, datetime

    from shorts_factory.config import load_settings
    from shorts_factory.models import Candidate
    from shorts_factory.scripting import ScriptRejected, generate_script, narration_range

    s = load_settings({**RU, "TTS_PROVIDER": "mock", "FACT_CHECK": "false"})
    assert narration_range(s, 6) == (10, 16)  # 2.2 words/s, pause <= 1.5 s
    cand = Candidate(video_id="abcdefghijk", title="Котик красит забор", channel_id="UCx",
                     published_at=datetime.now(UTC), fetched_at=datetime.now(UTC))
    an = Analysis(mode="metadata_only", video_id="abcdefghijk", topic="t", sources=["api"])
    terse = copy.deepcopy(SCRIPT)
    terse["scenes"][1]["narration"] = "Робот находит карандаш."  # 3 words for 6 s -> 4.6 s of silence
    text = _Seq(terse, {"critique": ["weak hook"], "script": terse}, SCRIPT)
    script, ref = generate_script(s, text, cand, an, "x")
    assert ref == "script@v6+punch_up@v1" and script.scenes[1].narration == SCRIPT["scenes"][1]["narration"]
    assert "story editor" in text.calls[1][-1]["content"] and text.calls[1][-2]["role"] == "assistant"
    assert "scene 1 narration has 3 words, needs 10-16" in text.calls[2][-1]["content"]
    near = copy.deepcopy(SCRIPT)
    near["scenes"][1]["narration"] = "Робот замечает сломанный карандаш и понимает: тот скоро пропадёт."  # 9 of 10
    generate_script(s, _Seq(near, {"critique": [], "script": near}, near), cand, an, "x")  # repaired once, kept
    with pytest.raises(ScriptRejected, match="scene 1 narration"):
        generate_script(s, _Seq(terse, {"critique": [], "script": terse}, terse), cand, an, "x")
    silent = load_settings({**RU, "FACT_CHECK": "false", "SCRIPT_PUNCH_UP": "false"})
    generate_script(silent, _Seq(terse), cand, an, "x")  # no TTS -> no voice-over rule


@pytest.mark.parametrize("bad", ["The subject squeezes a ketchup packet", "The subject opens a soda can"])
def test_packaged_goods_rejected_at_generation_but_old_scripts_load(bad):
    data = copy.deepcopy(SCRIPT)
    data["scenes"][0]["generation_prompt"] = bad + ", slow push-in, warm light"
    with pytest.raises(ValidationError, match="packaged goods"):
        Script.model_validate(data)
    Script.model_validate(data, context={"stored": True})


def test_verb_can_is_not_a_packaged_good():
    data = copy.deepcopy(SCRIPT)
    data["scenes"][0]["generation_prompt"] = "The subject can barely reach the shelf, stretching up, slow push-in"
    Script.model_validate(data)


@pytest.mark.parametrize("bad", ["eyes fixed on the monitor", "types on the keyboard", "grabs a sticky note"])
def test_text_prone_props_rejected_in_scene_prompts(bad):
    data = copy.deepcopy(SCRIPT)
    data["scenes"][0]["generation_prompt"] = "The subject leans forward, " + bad + ", slow push-in, warm light"
    with pytest.raises(ValidationError, match="fake lettering"):
        Script.model_validate(data)
    Script.model_validate(data, context={"stored": True})


def test_key_props_go_into_prompts_and_reject_packaged_goods():
    from shorts_factory.scripting import compose_keyframe_prompt, compose_video_prompt

    data = copy.deepcopy(SCRIPT)
    data["key_props"] = ["a tiny white origami crane"]
    data["scenes"][0]["must_show"] = ["a tiny white origami crane"]
    s = Script.model_validate(data)
    assert "Clearly visible in the scene: a tiny white origami crane" in compose_keyframe_prompt(s)
    assert "Clearly visible in the shot: a tiny white origami crane" in compose_video_prompt(s, s.scenes[0])
    assert "Clearly visible" not in compose_video_prompt(s, s.scenes[1])
    data["key_props"] = ["a soda can"]
    with pytest.raises(ValidationError):
        Script.model_validate(data)
