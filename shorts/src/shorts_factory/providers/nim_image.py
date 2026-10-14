"""Text-to-image on NVIDIA's hosted API (flux.1-dev), used for video thumbnails.

Checked 2026-10-14 on this account: POST https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev with
{"prompt", "width", "height", "seed", "steps" (>= 5), "cfg_scale", "mode": "base"} returns
{"artifacts": [{"base64": <JPEG>, "finishReason": "SUCCESS", "seed": n}]} in about 6 s for 768x1344.
flux.1-kontext-dev accepts only built-in example images (example_id), so a video frame cannot be edited;
flux.1-schnell did not answer within 120 s; SD3 / SDXL / Bria are not enabled (404).
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from ..budget import BudgetGuard
from ..config import Settings
from ..net import ProviderError, RetryPolicy, make_client, send_with_retry


class NimImageClient:
    name = "nvidia_flux1_dev"

    def __init__(self, s: Settings, budget: BudgetGuard, client: httpx.Client | None = None):
        s.require("nvidia_api_key")
        self.s, self.budget = s, budget
        self.http = client or make_client(s.nim_connect_timeout_s, 180)

    def generate(self, prompt: str, out: Path, seed: int | None = None) -> str | None:
        self.budget.reserve("thumbnail")
        body = {"prompt": prompt[:2000], "width": self.s.thumbnail_width, "height": self.s.thumbnail_height,
                "seed": int(seed or 0) % 2**31, "steps": self.s.thumbnail_steps, "cfg_scale": 3.5, "mode": "base"}
        headers = {"Authorization": f"Bearer {self.s.nvidia_api_key.get_secret_value()}",  # type: ignore[union-attr]
                   "Accept": "application/json"}
        resp = send_with_retry(lambda: self.http.post(self.s.nim_image_url, json=body, headers=headers),
                               RetryPolicy(max_retries=self.s.nim_max_retries), what="flux.1-dev")
        if resp.status_code != 200:
            raise ProviderError(f"flux.1-dev: HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code,
                                retryable=resp.status_code in (429, 503))
        try:
            art = resp.json()["artifacts"][0]
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError("flux.1-dev: unexpected response shape") from e
        if art.get("finishReason") != "SUCCESS":
            raise ProviderError(f"flux.1-dev: finishReason {art.get('finishReason')}")
        data = base64.b64decode(art.get("base64") or "")
        if not data.startswith((b"\xff\xd8", b"\x89PNG")) or len(data) < 1024:
            raise ProviderError("flux.1-dev: not an image")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return resp.headers.get("NVCF-REQID") or str(art.get("seed"))
