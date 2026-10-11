from __future__ import annotations

import base64
import json

import httpx
import pytest

from shorts_factory.budget import BudgetGuard
from shorts_factory.config import ConfigError, load_settings
from shorts_factory.models import Script
from shorts_factory.net import BudgetExceeded, OutcomeUnknown, ProviderError
from shorts_factory.providers.mock import default_mock_responses
from shorts_factory.providers.nim_chat import NimTextClient, StructuredOutputError
from shorts_factory.providers.nim_video import NimVideoClient, VideoRequest

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096
REQ = VideoRequest(prompt="a paper boat on a pond at sunrise", num_frames=97, fps=24, resolution="720_9_16")


def _settings(**kw):
    env = {"NVIDIA_API_KEY": "nvapi-test-key-000000", "NIM_VIDEO_URL": "https://gen.example/v1/infer",
           "BUDGET_VIDEO_REQUESTS_PER_DAY": "5"}
    env.update(kw)
    return load_settings(env)


def _video_client(store, handler, **kw):
    s = _settings(**kw)
    return NimVideoClient(s, BudgetGuard(store, s.budget), client=httpx.Client(transport=httpx.MockTransport(handler)),
                          sleep=lambda _: None)


def test_no_default_video_endpoint(store):
    s = load_settings({"NVIDIA_API_KEY": "nvapi-x-000000"})
    with pytest.raises(ConfigError, match="not configured"):
        NimVideoClient(s, BudgetGuard(store, s.budget))


def test_video_request_schema_and_success(store, tmp_path):
    seen = {}

    def h(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["Authorization"]
        return httpx.Response(200, json={"b64_video": base64.b64encode(VIDEO).decode()},
                              headers={"NVCF-REQID": "rid-1"})

    out = tmp_path / "a.mp4"
    rid = _video_client(store, h, NIM_VIDEO_EXTRA='{"num_inference_steps": 35}').generate(REQ, out)
    assert rid == "rid-1" and out.read_bytes() == VIDEO
    b = seen["body"]
    assert b == {"model_mode": "text2video", "prompt": REQ.prompt, "resolution": "720_9_16", "num_frames": 97,
                 "fps": 24.0, "num_inference_steps": 35}
    assert isinstance(b["num_frames"], int) and seen["auth"].startswith("Bearer nvapi-")


@pytest.mark.parametrize("body,match", [
    ({"error": "x"}, "no b64_video"), ({"b64_video": "@@not-base64@@"}, "not valid base64"),
    ({"b64_video": base64.b64encode(b"tiny").decode()}, "suspiciously small"),
])
def test_video_corrupt_results_are_definite_failures(store, tmp_path, body, match):
    with pytest.raises(ProviderError, match=match):
        _video_client(store, lambda r: httpx.Response(200, json=body)).generate(REQ, tmp_path / "x.mp4")
    assert not (tmp_path / "x.mp4").exists()


def test_video_timeout_is_unknown_not_retried(store, tmp_path):
    calls = []

    def h(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(OutcomeUnknown):
        _video_client(store, h).generate(REQ, tmp_path / "x.mp4")
    assert len(calls) == 1


def test_video_429_then_success(store, tmp_path):
    seq = iter([httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.Response(200, json={"b64_video": base64.b64encode(VIDEO).decode()})])
    _video_client(store, lambda r: next(seq)).generate(REQ, tmp_path / "x.mp4")
    assert (tmp_path / "x.mp4").exists()


def test_async_202_without_status_url_is_unknown_with_request_id(store, tmp_path):
    submitted = []
    with pytest.raises(OutcomeUnknown) as e:
        _video_client(store, lambda r: httpx.Response(202, headers={"NVCF-REQID": "req-42"})).generate(
            REQ, tmp_path / "x.mp4", on_submitted=submitted.append)
    assert e.value.request_id == "req-42" and submitted == ["req-42"]


def test_async_202_polling(store, tmp_path):
    polls = iter([httpx.Response(202), httpx.Response(200, json={"b64_video": base64.b64encode(VIDEO).decode()})])

    def h(req):
        if req.method == "POST":
            return httpx.Response(202, headers={"NVCF-REQID": "req-7"})
        assert req.url.path == "/status/req-7"
        return next(polls)
    c = _video_client(store, h, NIM_VIDEO_ASYNC_STATUS_URL="https://gen.example/status/{request_id}")
    assert c.generate(REQ, tmp_path / "x.mp4") == "req-7"


def test_budget_checked_before_http(store, tmp_path):
    def h(req):
        raise AssertionError("must not send")
    with pytest.raises(BudgetExceeded):
        _video_client(store, h, BUDGET_VIDEO_REQUESTS_PER_DAY="0").generate(REQ, tmp_path / "x.mp4")


def _chat_client(store, contents):
    s = _settings()
    it = iter(contents)
    return NimTextClient(s, BudgetGuard(store, s.budget), client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"id": "c1", "choices": [{"message": {"content": next(it)}}]}))))


def test_text_structured_validation_with_one_repair(store):
    good = json.dumps(default_mock_responses()["Script"], ensure_ascii=False)
    c = _chat_client(store, ["<think>hidden reasoning</think>not json", good])
    script, rid = c.structured([{"role": "user", "content": "x"}], Script, max_tokens=100)
    assert script.title and rid == "c1"
    c = _chat_client(store, ["{}", '{"title": 1}'])
    with pytest.raises(StructuredOutputError):
        c.structured([{"role": "user", "content": "x"}], Script, max_tokens=100)
