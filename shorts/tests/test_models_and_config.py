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
