"""Adapters for OpenAI-compatible NIM chat endpoints: text (script) and vision (analysis)."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..budget import BudgetGuard
from ..config import Settings
from ..net import ConcurrencyLimiter, ProviderError, RetryPolicy, make_client, send_with_retry
from .jsonout import extract_json_object

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    """Model output did not validate after the allowed repair attempt."""


class _ChatBase:
    bucket = "text"
    extra_body: dict = {}

    def __init__(self, s: Settings, budget: BudgetGuard, url: str, model: str,
                 client: httpx.Client | None = None, limiter: ConcurrencyLimiter | None = None):
        self.s, self.budget, self.url, self.model = s, budget, url, model
        self.http = client or make_client(s.nim_connect_timeout_s, s.nim_read_timeout_s)
        self.limiter = limiter or ConcurrencyLimiter(s.nim_max_concurrency)

    def _headers(self) -> dict:
        self.s.require("nvidia_api_key")
        return {"Authorization": f"Bearer {self.s.nvidia_api_key.get_secret_value()}",  # type: ignore[union-attr]
                "Accept": "application/json"}

    def chat(self, messages: list[dict], *, max_tokens: int, temperature: float = 0.4,
             json_mode: bool = False) -> tuple[str, str | None]:
        body: dict = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                      "temperature": temperature, "stream": False}
        body.update(self.extra_body)
        if json_mode and self.s.nim_text_response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        self.budget.reserve(self.bucket)
        with self.limiter:
            resp = send_with_retry(lambda: self.http.post(self.url, json=body, headers=self._headers()),
                                   RetryPolicy(max_retries=self.s.nim_max_retries), what=f"{self.model} chat")
        if resp.status_code != 200:
            raise ProviderError(f"{self.model}: HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code)
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"{self.model}: unexpected response shape") from e
        return content, data.get("id") or resp.headers.get("NVCF-REQID")

    def structured(self, messages: list[dict], model_cls: type[M], *, max_tokens: int,
                   context: dict | None = None) -> tuple[M, str | None]:
        """Ask for JSON, validate with pydantic, allow one repair round with the validation error."""
        content, rid = self.chat(messages, max_tokens=max_tokens, json_mode=True)
        for attempt in range(2):
            try:
                obj = extract_json_object(content)
                return model_cls.model_validate(obj, context=context), rid
            except (ValueError, ValidationError) as e:
                if attempt == 1:
                    raise StructuredOutputError(f"{model_cls.__name__} invalid after repair: {str(e)[:500]}") from e
                log.warning("%s output invalid, asking for one repair: %s", model_cls.__name__, str(e)[:200])
                repair = messages + [
                    {"role": "assistant", "content": content[:6000]},
                    {"role": "user", "content": "The JSON above is invalid: " + str(e)[:1500]
                     + "\nReturn only the corrected JSON object, no commentary."},
                ]
                content, rid = self.chat(repair, max_tokens=max_tokens, json_mode=True)
        raise AssertionError("unreachable")


class NimTextClient(_ChatBase):
    bucket = "text"

    def __init__(self, s: Settings, budget: BudgetGuard, **kw):
        super().__init__(s, budget, s.nim_text_url, s.nim_text_model, **kw)
        if s.nim_text_disable_thinking:  # reasoning tokens are billed and discarded anyway
            self.extra_body = {"chat_template_kwargs": {"enable_thinking": False}}


class NimVisionClient(_ChatBase):
    bucket = "vision"

    def __init__(self, s: Settings, budget: BudgetGuard, **kw):
        super().__init__(s, budget, s.nim_vision_url, s.nim_vision_model, **kw)

    def frames_content(self, frames: list[Path]) -> list[dict]:
        urls = ["data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode() for p in frames]
        if self.s.nim_vision_input == "video_frames":
            return [{"type": "video_frames", "video_frames": urls}]
        return [{"type": "image_url", "image_url": {"url": u}} for u in urls]
