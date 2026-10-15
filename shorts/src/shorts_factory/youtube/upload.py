"""Resumable upload to YouTube with crash recovery and duplicate protection.

Protocol (YouTube resumable upload):
1. POST {upload_url}?uploadType=resumable&part=snippet,status with metadata -> 200 + Location (session URI).
   Starting a session creates no video, so a lost init response is safe to redo.
2. PUT chunks with Content-Range; 308 + Range = bytes received so far; 200/201 = video resource.
3. Status query: PUT with ``Content-Range: bytes */<size>`` and empty body.

The session URI is a bearer capability: stored AES-GCM-encrypted, never logged.
Once bytes were sent and the outcome is not known, a new upload is never started
automatically: the session is queried, then the channel's uploads are reconciled, and
otherwise the job goes to ``upload_unknown`` for a human decision.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..budget import BudgetGuard
from ..config import ConfigError, Settings
from ..models import UploadRequest
from ..net import AuthError, ProviderError, RetryPolicy, send_with_retry
from ..store import Store
from .oauth import TokenProvider

log = logging.getLogger(__name__)


class UploadUnknown(RuntimeError):
    """Outcome cannot be established automatically; human reconciliation required."""


class SessionCipher:
    def __init__(self, key_b64: str):
        key = base64.b64decode(key_b64)
        if len(key) != 32:
            raise ConfigError("UPLOAD_URI_KEY must be 32 random bytes, base64-encoded")
        self._aes = AESGCM(key)

    def enc(self, plain: str) -> str:
        nonce = os.urandom(12)
        return base64.b64encode(nonce + self._aes.encrypt(nonce, plain.encode(), b"upload-session")).decode()

    def dec(self, token: str) -> str:
        raw = base64.b64decode(token)
        return self._aes.decrypt(raw[:12], raw[12:], b"upload-session").decode()


class YouTubeClient:
    def __init__(self, s: Settings, store: Store, budget: BudgetGuard, tokens: TokenProvider,
                 client: httpx.Client, cipher: SessionCipher, sleep=time.sleep):
        self.s, self.store, self.budget, self.tokens, self.http = s, store, budget, tokens, client
        self.cipher, self.sleep = cipher, sleep

    # ---- read-only API --------------------------------------------------------------------
    def _api_get(self, path: str, params: dict) -> dict:
        self.budget.reserve("yt_units")
        resp = send_with_retry(lambda: self.http.get(f"{self.s.youtube_api_base}/{path}", params=params,
                                                     headers=self.tokens.headers()),
                               RetryPolicy(max_retries=3), what=f"youtube {path}", sleep=self.sleep)
        if resp.status_code != 200:
            raise ProviderError(f"youtube {path}: HTTP {resp.status_code}: {resp.text[:300]}",
                                status=resp.status_code)
        return resp.json()

    def my_channel(self) -> dict:
        items = self._api_get("channels", {"part": "id,snippet,contentDetails", "mine": "true"}).get("items", [])
        if len(items) != 1:
            raise AuthError(f"OAuth token maps to {len(items)} channels; expected exactly 1")
        return items[0]

    def verify_channel(self) -> dict:
        self.s.require("youtube_channel_id")
        ch = self.my_channel()
        if ch["id"] != self.s.youtube_channel_id:
            raise AuthError(f"token belongs to channel {ch['id']}, not configured YOUTUBE_CHANNEL_ID "
                            f"{self.s.youtube_channel_id}; refusing to upload")
        return ch

    def set_thumbnail(self, video_id: str, image: Path) -> None:
        """thumbnails.set (scope youtube.upload, ~50 quota units). YouTube may refuse it for Shorts or for channels
        without custom-thumbnail access: the caller treats any error as non-fatal."""
        self.budget.reserve("yt_units", 50)
        data = image.read_bytes()
        ctype = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
        resp = send_with_retry(lambda: self.http.post(
            self.s.youtube_thumbnail_url, params={"videoId": video_id, "uploadType": "media"}, content=data,
            headers={**self.tokens.headers(), "Content-Type": ctype}),
            RetryPolicy(max_retries=2), what="youtube thumbnails.set", sleep=self.sleep)
        if resp.status_code != 200:
            raise ProviderError(f"thumbnails.set: HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code)

    def processing_status(self, video_id: str) -> dict:
        items = self._api_get("videos", {"part": "status,processingDetails", "id": video_id}).get("items", [])
        if not items:
            return {"found": False}
        it = items[0]
        return {"found": True, "uploadStatus": it.get("status", {}).get("uploadStatus"),
                "privacyStatus": it.get("status", {}).get("privacyStatus"),
                "processingStatus": it.get("processingDetails", {}).get("processingStatus"),
                "failureReason": it.get("status", {}).get("failureReason")
                or it.get("status", {}).get("rejectionReason")}

    def find_recent_upload(self, title: str, since: datetime) -> str | None:
        """Look for a video with the same title uploaded after ``since`` on our channel."""
        uploads = self.my_channel()["contentDetails"]["relatedPlaylists"]["uploads"]
        data = self._api_get("playlistItems", {"part": "snippet", "playlistId": uploads, "maxResults": 25})
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            pub = sn.get("publishedAt")
            if sn.get("title") == title and pub and datetime.fromisoformat(pub.replace("Z", "+00:00")) >= since:
                return sn.get("resourceId", {}).get("videoId")
        return None

    # ---- upload ---------------------------------------------------------------------------
    def upload(self, job_id: str, path: Path, req: UploadRequest, file_key: str,
               allow_new_session_after_unknown: bool = False) -> str:
        size = path.stat().st_size
        row = self.store.get_upload(job_id)
        if row and row["video_id"]:
            return row["video_id"]
        if row is None:
            self.store.save_upload(job_id, status="initiating", file_key=file_key, file_size=size,
                                   request=req.model_dump_json(), detail="{}")
            row = self.store.get_upload(job_id)
        assert row is not None
        if row["file_size"] != size:
            raise UploadUnknown("rendered file size changed since the upload session started")
        if row["status"] == "unknown" and not allow_new_session_after_unknown:
            raise UploadUnknown(f"upload outcome unknown: {self._detail(job_id).get('note')}")

        session = self.cipher.dec(row["session_uri_enc"]) if row["session_uri_enc"] else None
        offset = 0
        if session:
            state, offset, vid = self._query(session, size)
            if state == "done":
                return self._done(job_id, vid)
            if state == "expired":
                return self._reconcile_expired(job_id, req)
        else:
            if not self._detail(job_id).get("budget_reserved"):
                self.budget.reserve("yt_upload")
                self._set(job_id, budget_reserved=True)
            session = self._init_session(req, size)
            self.store.save_upload(job_id, status="in_progress", session_uri_enc=self.cipher.enc(session))
        return self._send(job_id, session, path, size, offset, req)

    def _init_session(self, req: UploadRequest, size: int) -> str:
        url = f"{self.s.youtube_upload_url}?uploadType=resumable&part=snippet,status"
        headers = {**self.tokens.headers(), "Content-Type": "application/json; charset=UTF-8",
                   "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"}
        resp = send_with_retry(lambda: self.http.post(url, content=json.dumps(req.to_resource()), headers=headers),
                               RetryPolicy(max_retries=3), what="youtube upload init", sleep=self.sleep)
        if resp.status_code != 200 or "Location" not in resp.headers:
            raise ProviderError(f"upload init HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code)
        return resp.headers["Location"]

    def _query(self, session: str, size: int) -> tuple[str, int, str | None]:
        try:
            resp = self.http.put(session, headers={**self.tokens.headers(), "Content-Range": f"bytes */{size}"},
                                 content=b"")
        except httpx.TransportError as e:
            raise UploadUnknown(f"cannot query upload session: {type(e).__name__}") from e
        if resp.status_code in (200, 201):
            return "done", size, resp.json().get("id")
        if resp.status_code == 308:
            return "partial", _next_offset(resp), None
        if resp.status_code in (404, 410):
            return "expired", 0, None
        raise UploadUnknown(f"upload session query HTTP {resp.status_code}")

    def _send(self, job_id: str, session: str, path: Path, size: int, offset: int, req: UploadRequest) -> str:
        chunk = self.s.upload_chunk_bytes
        failures = 0
        with path.open("rb") as f:
            while True:
                f.seek(offset)
                data = f.read(chunk)
                end = offset + len(data) - 1
                headers = {**self.tokens.headers(), "Content-Type": "video/mp4",
                           "Content-Range": f"bytes {offset}-{end}/{size}"}
                try:
                    resp = self.http.put(session, content=data, headers=headers)
                    code = resp.status_code
                except httpx.TransportError as e:
                    code, resp = None, None
                    log.warning("upload chunk transport error %s; querying session", type(e).__name__)
                if code in (200, 201):
                    return self._done(job_id, resp.json().get("id"))
                if code == 308:
                    offset, failures = _next_offset(resp), 0
                    continue
                if code in (401,):
                    self.tokens._exp = 0  # force refresh, then query
                if code in (404, 410):
                    return self._reconcile_expired(job_id, req)
                if code is not None and code < 500 and code not in (401, 408, 429):
                    self._set(job_id, status="failed", note=f"HTTP {code}: {resp.text[:300]}")
                    raise ProviderError(f"upload rejected: HTTP {code}", status=code)
                failures += 1
                if failures > self.s.nim_max_retries:
                    self._set(job_id, status="unknown", note="too many chunk failures; session kept")
                    raise UploadUnknown("upload interrupted; session saved, rerun to resume")
                self.sleep(min(60, 2 ** failures))
                state, offset, vid = self._query(session, size)
                if state == "done":
                    return self._done(job_id, vid)
                if state == "expired":
                    return self._reconcile_expired(job_id, req)

    def _detail(self, job_id: str) -> dict:
        row = self.store.get_upload(job_id) or {}
        try:
            return json.loads(row.get("detail") or "{}")
        except ValueError:
            return {}

    def _set(self, job_id: str, status: str | None = None, **detail) -> None:
        """Merge into the JSON detail (never overwrite flags such as budget_reserved)."""
        merged = self._detail(job_id) | detail
        fields = {"detail": json.dumps(merged)}
        if status:
            fields["status"] = status
        self.store.save_upload(job_id, **fields)

    def _done(self, job_id: str, video_id: str | None) -> str:
        if not video_id:
            self._set(job_id, status="unknown", note="upload completed without video id")
            raise UploadUnknown("upload completed but response had no video id")
        self.store.save_upload(job_id, status="uploaded", video_id=video_id, session_uri_enc=None)
        log.info("upload complete: video %s", video_id)
        return video_id

    def _reconcile_expired(self, job_id: str, req: UploadRequest) -> str:
        row = self.store.get_upload(job_id) or {}
        started = row.get("created_at") or time.time() - 7 * 86400
        since = datetime.fromtimestamp(started).astimezone() - timedelta(minutes=5)
        vid = self.find_recent_upload(req.title, since)
        if vid:
            log.warning("upload session expired, but matching video %s found on channel; adopting it", vid)
            return self._done(job_id, vid)
        self.store.save_upload(job_id, session_uri_enc=None)
        self._set(job_id, status="unknown", note="session expired/invalid and no matching upload found on channel")
        raise UploadUnknown("upload session expired and no matching video found; verify in YouTube Studio, "
                            "then `shorts retry <job> --stage upload --new-upload-session`")


def _next_offset(resp: httpx.Response) -> int:
    m = re.match(r"bytes=0-(\d+)", resp.headers.get("Range", ""))
    return int(m.group(1)) + 1 if m else 0
