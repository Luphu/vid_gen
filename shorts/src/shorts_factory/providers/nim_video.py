"""Cosmos3 Generator adapter (``POST /v1/infer``, model_mode=text2video).

Request/response follow the documented NIM schema (see docs/capabilities.md §1.3):
strict types, unknown fields rejected, response ``{"b64_video": "<raw base64 MP4 (VP9)>"}``.

The hosted cosmos3-nano URL is not documented, so NIM_VIDEO_URL has no default.
Optional async handling: HTTP 202 + ``NVCF-REQID`` header, polled at
NIM_VIDEO_ASYNC_STATUS_URL (``{request_id}`` placeholder) only if configured.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from ..budget import BudgetGuard
from ..config import ConfigError, Settings
from ..net import ConcurrencyLimiter, OutcomeUnknown, ProviderError, RetryPolicy, make_client, send_with_retry

log = logging.getLogger(__name__)


@dataclass
class VideoRequest:
    prompt: str
    num_frames: int
    fps: int
    resolution: str
    negative_prompt: str | None = None
    seed: int | None = None
    image: Path | None = None  # start frame for image-to-video (i2v_chain)


class VideoGenerator(Protocol):
    name: str

    def generate(self, req: VideoRequest, out: Path, on_submitted=None) -> str | None:
        """Write MP4 to ``out``; return provider request id if any.

        Raises OutcomeUnknown when the request may have been billed but no result is in hand.
        ``on_submitted(request_id)`` is called as soon as an async request id is known.
        """
        ...

    def poll(self, request_id: str, out: Path) -> bool:
        """Try to recover a previously submitted async result. True if written."""
        ...


class NimVideoClient:
    name = "nim_cosmos3_generator"

    def __init__(self, s: Settings, budget: BudgetGuard, client: httpx.Client | None = None,
                 limiter: ConcurrencyLimiter | None = None, sleep=time.sleep):
        if not s.nim_video_url:
            raise ConfigError("video generator endpoint not configured (NIM_VIDEO_URL); "
                              "hosted cosmos3-nano URL is not documented — see docs/capabilities.md §1.3")
        self.s, self.budget = s, budget
        self.http = client or make_client(s.nim_connect_timeout_s, s.nim_video_timeout_s)
        self.limiter = limiter or ConcurrencyLimiter(s.nim_max_concurrency)
        self.sleep = sleep

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.s.nvidia_api_key:
            h["Authorization"] = f"Bearer {self.s.nvidia_api_key.get_secret_value()}"
        return h

    def build_body(self, req: VideoRequest) -> dict:
        body: dict = {
            "model_mode": "text2video",
            "prompt": req.prompt[:20000],
            "resolution": req.resolution,
            "num_frames": int(req.num_frames),
            "fps": float(req.fps),
        }
        if req.negative_prompt:
            body["negative_prompt"] = req.negative_prompt[:20000]
        if req.seed is not None:
            body["seed"] = int(req.seed)
        for k, v in self.s.nim_video_extra.items():  # e.g. {"num_inference_steps": 35}
            if k in body:
                raise ConfigError(f"NIM_VIDEO_EXTRA must not override {k}")
            body[k] = v
        return body

    def generate(self, req: VideoRequest, out: Path, on_submitted=None) -> str | None:
        if not (25 <= req.num_frames <= self.s.nim_video_max_frames):  # documented 720p tier range
            raise ProviderError(f"num_frames {req.num_frames} outside [25, {self.s.nim_video_max_frames}]")
        body = self.build_body(req)
        self.budget.reserve("video")
        with self.limiter:
            resp = send_with_retry(
                lambda: self.http.post(self.s.nim_video_url, json=body, headers=self._headers()),
                RetryPolicy(max_retries=self.s.nim_max_retries, billable=True), what="cosmos3 generate",
                sleep=self.sleep,
            )
        rid = resp.headers.get("NVCF-REQID")
        if resp.status_code == 202:
            if not rid:
                raise OutcomeUnknown("202 Accepted without NVCF-REQID")
            if on_submitted:
                on_submitted(rid)
            if not self.s.nim_video_async_status_url:
                raise OutcomeUnknown("async 202 received but NIM_VIDEO_ASYNC_STATUS_URL is not configured",
                                     request_id=rid)
            if self.poll(rid, out, wait=True):
                return rid
            raise OutcomeUnknown("async generation did not finish within NIM_VIDEO_TIMEOUT_S", request_id=rid)
        if resp.status_code != 200:
            # 4xx other than 401/403/429 are definite rejections (validation, guardrails).
            raise ProviderError(f"cosmos3: HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code)
        self._write_video(resp.content, out)
        return rid

    def poll(self, request_id: str, out: Path, wait: bool = False) -> bool:
        if not self.s.nim_video_async_status_url:
            return False
        url = self.s.nim_video_async_status_url.format(request_id=request_id)
        deadline = time.monotonic() + (self.s.nim_video_timeout_s if wait else 0)
        while True:
            resp = send_with_retry(lambda: self.http.get(url, headers=self._headers()),
                                   RetryPolicy(max_retries=self.s.nim_max_retries), what="cosmos3 status",
                                   sleep=self.sleep)
            if resp.status_code == 200:
                self._write_video(resp.content, out)
                return True
            if resp.status_code != 202:
                raise ProviderError(f"cosmos3 status: HTTP {resp.status_code}: {resp.text[:300]}",
                                    status=resp.status_code)
            if time.monotonic() >= deadline:
                return False
            self.sleep(5.0)

    def _write_video(self, raw: bytes, out: Path) -> None:
        if len(raw) > self.s.nim_max_response_bytes:
            raise ProviderError(f"cosmos3 response too large: {len(raw)} bytes")
        try:
            data = json.loads(raw)
            b64 = data.get("b64_video")
        except (ValueError, AttributeError) as e:
            raise ProviderError("cosmos3: response is not JSON") from e
        if not b64 or not isinstance(b64, str):
            raise ProviderError("cosmos3: response has no b64_video")
        try:
            video = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ProviderError("cosmos3: b64_video is not valid base64") from e
        if len(video) < 1024:
            raise ProviderError(f"cosmos3: video payload suspiciously small ({len(video)} bytes)")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part")
        tmp.write_bytes(video)
        tmp.replace(out)
