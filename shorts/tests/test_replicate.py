from __future__ import annotations

import json

import httpx
import pytest

from shorts_factory.budget import BudgetGuard
from shorts_factory.config import load_settings
from shorts_factory.models import Script
from shorts_factory.net import BudgetExceeded, OutcomeUnknown, ProviderError
from shorts_factory.providers import replicate as rep
from shorts_factory.providers.mock import default_mock_responses
from shorts_factory.providers.nim_video import VideoRequest

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"v" * 5000
OUT_URL = "https://replicate.delivery/xezq/abc/output.mp4"


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    monkeypatch.setattr(rep, "check_public_https_url", lambda url, allowed_hosts=None: None)


def _s(**kw):
    env = {"REPLICATE_API_TOKEN": "r8_testtoken0000000000000000", "BUDGET_VIDEO_REQUESTS_PER_DAY": "5",
           "REPLICATE_POLL_INTERVAL_S": "0", "NIM_VIDEO_TIMEOUT_S": "0.05"}
    env.update(kw)
    return load_settings(env)


class FakeReplicate:
    def __init__(self, statuses=("processing", "succeeded"), output=OUT_URL, create_error=None, removed=False):
        self.statuses, self.output, self.create_error, self.removed = list(statuses), output, create_error, removed
        self.creates: list[dict] = []
        self.gets = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer r8_testtoken0000000000000000"
        if req.method == "POST":
            if self.create_error:
                raise self.create_error(req)
            self.creates.append(json.loads(req.content))
            assert req.url.path.startswith("/v1/models/") and req.url.path.endswith("/predictions")
            return httpx.Response(201, json={"id": "pred1", "status": "starting"})
        if req.url.host == "replicate.delivery":
            return httpx.Response(200, content=MP4)
        self.gets += 1
        st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]  # last status is sticky
        body = {"id": "pred1", "status": st, "data_removed": self.removed and st == "succeeded"}
        if st == "succeeded":
            body["output"] = None if self.removed else self.output
        if st == "failed":
            body["error"] = "NSFW content detected"
        return httpx.Response(200, json=body)


def _video(fake, **kw):
    s = _s(**kw)
    rc = rep.ReplicateClient(s, client=httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda _: None)
    return rep.ReplicateVideo(s, BudgetGuard(_store(), s.budget), rc=rc)


_STORES = []


def _store():
    from shorts_factory.store import Store

    st = Store("sqlite:///:memory:")
    st.migrate()
    _STORES.append(st)
    return st


REQ = VideoRequest(prompt="paper boat on a pond", num_frames=200, fps=16, resolution="720p", seed=7)


def test_video_input_schema_and_download(tmp_path):
    fake = FakeReplicate()
    v = _video(fake, NIM_VIDEO_TIMEOUT_S="5")
    pid = v.generate(REQ, tmp_path / "s.mp4", on_submitted=lambda r: None)
    assert pid == "pred1" and (tmp_path / "s.mp4").read_bytes() == MP4
    inp = fake.creates[0]["input"]
    assert inp == {"prompt": "paper boat on a pond", "aspect_ratio": "9:16", "resolution": "720p",
                   "num_frames": 121, "frames_per_second": 16, "interpolate_output": True, "go_fast": True,
                   "seed": 7}


def test_create_timeout_is_unknown_and_not_resent(tmp_path):
    fake = FakeReplicate(create_error=lambda r: httpx.ReadTimeout("slow", request=r))
    with pytest.raises(OutcomeUnknown):
        _video(fake).generate(REQ, tmp_path / "s.mp4")


def test_slow_prediction_is_unknown_with_id_then_recovered_by_poll(tmp_path):
    fake = FakeReplicate(statuses=["processing"])
    submitted = []
    v = _video(fake)
    with pytest.raises(OutcomeUnknown) as e:
        v.generate(REQ, tmp_path / "s.mp4", on_submitted=submitted.append)
    assert e.value.request_id == "pred1" and submitted == ["pred1"] and len(fake.creates) == 1
    fake.statuses = ["succeeded"]
    assert v.poll("pred1", tmp_path / "s.mp4") is True and len(fake.creates) == 1


def test_failed_removed_and_foreign_host_are_definite_errors(tmp_path):
    with pytest.raises(ProviderError, match="NSFW"):
        _video(FakeReplicate(statuses=["failed"]), NIM_VIDEO_TIMEOUT_S="5").generate(REQ, tmp_path / "a.mp4")
    v = _video(FakeReplicate(statuses=["succeeded"], removed=True))
    with pytest.raises(ProviderError, match="deleted"):
        v.poll("pred1", tmp_path / "b.mp4")
    with pytest.raises(ProviderError, match="unexpected output host"):
        _video(FakeReplicate(output="https://evil.example/x.mp4"), NIM_VIDEO_TIMEOUT_S="5").generate(
            REQ, tmp_path / "c.mp4")


def test_budget_before_request(tmp_path):
    fake = FakeReplicate()
    with pytest.raises(BudgetExceeded):
        _video(fake, BUDGET_VIDEO_REQUESTS_PER_DAY="0").generate(REQ, tmp_path / "s.mp4")
    assert fake.creates == []


def test_text_adapter_joins_tokens_and_validates():
    script_json = json.dumps(default_mock_responses()["Script"], ensure_ascii=False)
    tokens = [script_json[i:i + 7] for i in range(0, len(script_json), 7)]

    def h(req):
        if req.method == "POST":
            body = json.loads(req.content)["input"]
            assert body["prompt"].startswith("### USER") and body["max_tokens"] <= 16384
            assert req.headers["Prefer"] == "wait=60"
            return httpx.Response(201, json={"id": "t1", "status": "succeeded", "output": tokens})
        raise AssertionError("no polling expected")
    s = _s()
    rc = rep.ReplicateClient(s, client=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda _: None)
    t = rep.ReplicateText(s, BudgetGuard(_store(), s.budget), rc=rc)
    script, rid = t.structured([{"role": "user", "content": "write"}], Script, max_tokens=99999)
    assert rid == "t1" and script.title


def test_flatten_messages_keeps_text_parts_only():
    out = rep.flatten_messages([{"role": "system", "content": "rules"},
                                {"role": "user", "content": [{"type": "text", "text": "hi"},
                                                             {"type": "image_url", "image_url": {"url": "x"}}]}])
    assert "rules" in out and "hi" in out and "image_url" not in out and out.endswith("### ASSISTANT\n")


def test_i2v_input_uses_start_image_and_model(tmp_path):
    img = tmp_path / "start.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"j" * 2000)
    fake = FakeReplicate()
    v = _video(fake, NIM_VIDEO_TIMEOUT_S="5")
    req = VideoRequest(prompt="the robot jumps", num_frames=97, fps=16, resolution="720p", seed=1, image=img)
    assert v.model_for(req) == "wan-video/wan-2.2-i2v-fast"
    v.generate(req, tmp_path / "o.mp4")
    inp = fake.creates[0]["input"]
    assert inp["image"].startswith("data:image/jpeg;base64,") and "aspect_ratio" not in inp
    t2v = VideoRequest(prompt="x", num_frames=81, fps=16, resolution="720p")
    assert v.model_for(t2v) == "wan-video/wan-2.2-t2v-fast"


def test_image_adapter_input_and_budget(tmp_path):
    seen = {}

    def h(req):
        if req.method == "POST":
            seen.update(json.loads(req.content)["input"])
            return httpx.Response(201, json={"id": "img1", "status": "succeeded", "output": [OUT_URL]})
        return httpx.Response(200, content=b"\xff\xd8" + b"i" * 3000)
    s = _s(BUDGET_IMAGE_REQUESTS_PER_DAY="1")
    rc = rep.ReplicateClient(s, client=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda _: None)
    im = rep.ReplicateImage(s, BudgetGuard(_store(), s.budget), rc=rc)
    assert im.generate("a robot", tmp_path / "k.jpg", seed=5) == "img1" and (tmp_path / "k.jpg").stat().st_size > 1000
    assert seen["aspect_ratio"] == "9:16" and seen["output_format"] == "jpg" and seen["seed"] == 5
    with pytest.raises(BudgetExceeded):
        im.generate("again", tmp_path / "k2.jpg")


def test_signed_third_party_outputs_get_no_token(tmp_path):
    seen = []

    def h(req):
        seen.append((req.url.host, req.headers.get("Authorization")))
        return httpx.Response(200, content=b"x" * 2000)
    s = _s()
    rc = rep.ReplicateClient(s, client=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda _: None)
    rc.download("https://abc.r2.cloudflarestorage.com/out.jpg?X-Amz-Signature=1", tmp_path / "a", 10_000)
    rc.download(OUT_URL, tmp_path / "b", 10_000)
    assert seen[0] == ("abc.r2.cloudflarestorage.com", None)
    assert seen[1][1].startswith("Bearer r8_")
    with pytest.raises(ProviderError, match="unexpected output host"):
        rc.download("https://evil.example/x", tmp_path / "c", 10_000)
