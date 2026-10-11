"""Replicate adapters: text (LLM), video (Wan 2.2 T2V) and TTS (MiniMax speech).

HTTP API (https://replicate.com/docs/reference/http, checked 2026-10-11):
* ``POST /v1/models/{owner}/{name}/predictions`` with ``{"input": {...}}`` -> prediction ``id``, ``status``;
* ``GET /v1/predictions/{id}``; statuses starting|processing|succeeded|failed|canceled;
* outputs are HTTPS URLs on replicate.delivery that need the Authorization header;
* API-created predictions' inputs/outputs are deleted after ONE HOUR -> download immediately.

Model input schemas (from each model's API page, checked 2026-10-11):
* wan-video/wan-2.2-t2v-fast: prompt, num_frames 81..121, aspect_ratio "16:9"|"9:16", resolution "480p"|"720p",
  frames_per_second 5..30 (priced by duration at 16 fps), interpolate_output, go_fast, seed. Output: uri.
* qwen/qwen3-235b-a22b-instruct-2507: prompt, max_tokens (<=16384), temperature, top_p. Output: string[].
* minimax/speech-02-turbo: text (<=10000 chars), voice_id, speed 0.5..2, audio_format, sample_rate,
  language_boost. Output: uri.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..budget import BudgetGuard
from ..config import Settings
from ..ff import FF, duration_of
from ..net import (
    ConcurrencyLimiter,
    OutcomeUnknown,
    ProviderError,
    RetryPolicy,
    check_public_https_url,
    make_client,
    send_with_retry,
)
from .nim_chat import _ChatBase
from .nim_video import VideoRequest

log = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "canceled"}


class ReplicateClient:
    def __init__(self, s: Settings, client: httpx.Client | None = None, sleep=time.sleep):
        s.require("replicate_api_token")
        self.s, self.sleep = s, sleep
        self.http = client or make_client(s.nim_connect_timeout_s, 90)

    def _headers(self, wait: int | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.s.replicate_api_token.get_secret_value()}",  # type: ignore[union-attr]
             "Content-Type": "application/json"}
        if wait:
            h["Prefer"] = f"wait={wait}"
        return h

    def create(self, model: str, inp: dict, *, billable: bool, wait: int | None = None) -> dict:
        owner, name = model.split("/", 1)
        url = f"{self.s.replicate_api_base}/models/{owner}/{name}/predictions"
        resp = send_with_retry(lambda: self.http.post(url, json={"input": inp}, headers=self._headers(wait)),
                               RetryPolicy(max_retries=self.s.nim_max_retries, billable=billable),
                               what=f"replicate {model}", sleep=self.sleep)
        if resp.status_code not in (200, 201, 202):
            raise ProviderError(f"replicate {model}: HTTP {resp.status_code}: {resp.text[:300]}",
                                status=resp.status_code, retryable=resp.status_code == 429)
        return resp.json()

    def get(self, pid: str) -> dict:
        resp = send_with_retry(lambda: self.http.get(f"{self.s.replicate_api_base}/predictions/{pid}",
                                                     headers=self._headers()),
                               RetryPolicy(max_retries=self.s.nim_max_retries), what="replicate get",
                               sleep=self.sleep)
        if resp.status_code != 200:
            raise ProviderError(f"replicate get {pid}: HTTP {resp.status_code}", status=resp.status_code)
        return resp.json()

    def wait(self, pred: dict, timeout_s: float) -> dict:
        deadline = time.monotonic() + timeout_s
        while pred.get("status") not in TERMINAL:
            if time.monotonic() >= deadline:
                return pred
            self.sleep(self.s.replicate_poll_interval_s)
            pred = self.get(pred["id"])
        return pred

    @staticmethod
    def check_terminal(pred: dict) -> None:
        if pred.get("status") in ("failed", "canceled"):
            raise ProviderError(f"replicate prediction {pred.get('id')} {pred['status']}: "
                                f"{str(pred.get('error'))[:300]}")

    def download(self, url: str, dest: Path, max_bytes: int) -> Path:
        host = urlsplit(url).hostname or ""

        def under(suffix: str) -> bool:
            return host == suffix or host.endswith("." + suffix)
        own = under(self.s.replicate_output_host_suffix)
        signed = any(under(x) for x in self.s.replicate_signed_output_suffixes)
        if not (own or signed):
            raise ProviderError(f"unexpected output host {host!r}")
        check_public_https_url(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp, n = dest.with_suffix(dest.suffix + ".part"), 0
        # The API token goes only to Replicate's own delivery host, never to pre-signed third-party storage.
        headers = {"Authorization": self._headers()["Authorization"]} if own else {}
        with self.http.stream("GET", url, headers=headers, timeout=httpx.Timeout(30, read=300)) as r:
            if r.status_code != 200:
                raise ProviderError(f"replicate output download HTTP {r.status_code}", status=r.status_code)
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    n += len(chunk)
                    if n > max_bytes:
                        raise ProviderError("replicate output exceeds size limit")
                    f.write(chunk)
        tmp.replace(dest)
        return dest


def _single_uri(output) -> str:
    if isinstance(output, list):
        output = output[0] if output else None
    if not isinstance(output, str) or not output.startswith("https://"):
        raise ProviderError(f"replicate output is not a file URL: {str(output)[:120]}")
    return output


class ReplicateVideo:
    name = "replicate_wan22_t2v"

    def __init__(self, s: Settings, budget: BudgetGuard, rc: ReplicateClient | None = None,
                 limiter: ConcurrencyLimiter | None = None):
        self.s, self.budget, self.rc = s, budget, rc or ReplicateClient(s)
        self.limiter = limiter or ConcurrencyLimiter(s.nim_max_concurrency)

    def model_for(self, req: VideoRequest) -> str:
        return self.s.replicate_i2v_model if req.image is not None else self.s.replicate_video_model

    def build_input(self, req: VideoRequest) -> dict:
        lo, hi = self.s.replicate_video_min_frames, self.s.replicate_video_max_frames
        inp = {"prompt": req.prompt[:4000], "resolution": req.resolution,
               "num_frames": max(lo, min(hi, int(req.num_frames))), "frames_per_second": int(req.fps),
               "interpolate_output": True, "go_fast": True}
        if req.image is not None:  # i2v: aspect ratio comes from the image
            inp["image"] = data_url(req.image)
        else:
            inp["aspect_ratio"] = "9:16"
        if req.seed is not None:
            inp["seed"] = int(req.seed)
        inp.update(self.s.replicate_video_extra)
        return inp

    def generate(self, req: VideoRequest, out: Path, on_submitted=None) -> str | None:
        self.budget.reserve("video")
        with self.limiter:
            pred = self.rc.create(self.model_for(req), self.build_input(req), billable=True)
        pid = pred.get("id")
        if not pid:
            raise OutcomeUnknown("replicate create returned no prediction id")
        if on_submitted:
            on_submitted(pid)
        pred = self.rc.wait(pred, self.s.nim_video_timeout_s)
        if pred.get("status") not in TERMINAL:
            raise OutcomeUnknown(f"replicate prediction still {pred.get('status')} after timeout", request_id=pid)
        self.rc.check_terminal(pred)
        self.rc.download(_single_uri(pred.get("output")), out, self.s.nim_max_response_bytes)
        return pid

    def poll(self, request_id: str, out: Path) -> bool:
        pred = self.rc.get(request_id)
        if pred.get("status") not in TERMINAL:
            return False
        self.rc.check_terminal(pred)
        if pred.get("data_removed") or not pred.get("output"):
            raise ProviderError(f"replicate {request_id}: output already deleted (kept 1 hour)")
        self.rc.download(_single_uri(pred["output"]), out, self.s.nim_max_response_bytes)
        return True


def data_url(path: Path, max_bytes: int = 8 * 1024 * 1024) -> str:
    """Replicate accepts files as data URLs; keep start frames small (JPEG)."""
    import base64

    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise ProviderError(f"start image too large: {len(raw)} bytes")
    mime = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


class ReplicateImage:
    """Text-to-image keyframes (black-forest-labs/flux-schnell: prompt, aspect_ratio, output_format, seed; uri[])."""

    name = "replicate_flux_schnell"

    def __init__(self, s: Settings, budget: BudgetGuard, rc: ReplicateClient | None = None):
        self.s, self.budget, self.rc = s, budget, rc or ReplicateClient(s)

    def generate(self, prompt: str, out: Path, seed: int | None = None) -> str | None:
        self.budget.reserve("image")
        inp = {"prompt": prompt[:2000], "aspect_ratio": "9:16", "output_format": "jpg", "output_quality": 92,
               "num_outputs": 1}
        if seed is not None:
            inp["seed"] = int(seed)
        pred = self.rc.create(self.s.replicate_image_model, inp, billable=False, wait=60)
        pred = self.rc.wait(pred, 180)
        if pred.get("status") not in TERMINAL:
            raise ProviderError("replicate image timed out", retryable=True)
        self.rc.check_terminal(pred)
        self.rc.download(_single_uri(pred.get("output")), out, 20 * 1024**2)
        return pred.get("id")


def flatten_messages(messages: list[dict]) -> str:
    """Prompt-only LLMs on Replicate take a single string."""
    parts = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        parts.append(f"### {m['role'].upper()}\n{content}")
    parts.append("### ASSISTANT\n")
    return "\n\n".join(parts)


class ReplicateText(_ChatBase):
    """Same structured-output contract as NimTextClient (validate + one repair)."""

    bucket = "text"

    def __init__(self, s: Settings, budget: BudgetGuard, rc: ReplicateClient | None = None, **kw):
        super().__init__(s, budget, url="", model=s.replicate_text_model, **kw)
        self.rc = rc or ReplicateClient(s)

    def chat(self, messages: list[dict], *, max_tokens: int, temperature: float = 0.4,
             json_mode: bool = False) -> tuple[str, str | None]:
        self.budget.reserve(self.bucket)
        with self.limiter:
            pred = self.rc.create(self.model, {"prompt": flatten_messages(messages),
                                               "max_tokens": min(max_tokens, 16384),
                                               "temperature": temperature}, billable=False, wait=60)
            pred = self.rc.wait(pred, self.s.nim_read_timeout_s)
        if pred.get("status") not in TERMINAL:
            raise ProviderError(f"replicate text prediction {pred.get('id')} timed out", retryable=True)
        self.rc.check_terminal(pred)
        out = pred.get("output")
        text = "".join(out) if isinstance(out, list) else str(out or "")
        return text, pred.get("id")


class ReplicateTTS:
    name = "replicate_minimax_speech"

    def __init__(self, s: Settings, budget: BudgetGuard, ff: FF, rc: ReplicateClient | None = None):
        self.s, self.budget, self.ff, self.rc = s, budget, ff, rc or ReplicateClient(s)

    def synthesize(self, text: str, language: str, out_wav: Path, max_duration_s: float) -> Path | None:
        if not text.strip():
            return None
        self.budget.reserve("tts")
        pred = self.rc.create(self.s.replicate_tts_model, {
            "text": text[:2000], "voice_id": self.s.replicate_tts_voice, "speed": 1.05,
            "audio_format": "wav", "sample_rate": 32000, "channel": "mono", "language_boost": "Automatic",
        }, billable=False, wait=60)
        pred = self.rc.wait(pred, 180)
        if pred.get("status") not in TERMINAL:
            raise ProviderError("replicate tts timed out", retryable=True)
        self.rc.check_terminal(pred)
        raw = self.rc.download(_single_uri(pred.get("output")), out_wav.with_suffix(".src.wav"), 50 * 1024**2)
        dur = duration_of(self.ff.probe(raw))
        tempo = 1.0
        if dur > max_duration_s - 0.2:  # fit narration into the scene; beyond 1.35x it is cut in render
            tempo = min(1.35, dur / max(0.5, max_duration_s - 0.2))
        self.ff.run(["-i", str(raw), "-af", f"atempo={tempo:.3f}", "-ar", "48000", "-ac", "1", str(out_wav)])
        raw.unlink(missing_ok=True)
        return out_wav
