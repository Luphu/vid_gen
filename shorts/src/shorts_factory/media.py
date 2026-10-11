"""ReferenceMediaSource: where analysable media legitimately comes from.

Only rights-cleared material: originals supplied by the author/user, files in our S3 with a
manifest, or a licensed source with documented terms. The YouTube Data API is NOT a media
source and no downloader/scraper is used.

Layout (local dir or S3 prefix)::

    <video_id>/manifest.json   {"video_id", "license", "provided_by", "files": ["clip.mp4"], "is_short"}
    <video_id>/clip.mp4
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .config import Settings
from .models import MediaManifest
from .storage import S3Store

log = logging.getLogger(__name__)
_ALLOWED_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4a", ".wav", ".mp3", ".srt", ".vtt", ".jpg", ".png"}


@dataclass
class ReferenceMedia:
    manifest: MediaManifest
    video: Path | None
    subtitles: Path | None
    source: str


class ReferenceMediaSource(Protocol):
    name: str

    def fetch(self, video_id: str, dest: Path) -> ReferenceMedia | None: ...


class NoMediaSource:
    name = "none"

    def fetch(self, video_id: str, dest: Path) -> ReferenceMedia | None:
        return None


def _load_manifest(raw: str, video_id: str) -> MediaManifest | None:
    try:
        m = MediaManifest.model_validate_json(raw)
    except (ValidationError, ValueError) as e:
        log.warning("reference manifest for %s is invalid; media not used: %s", video_id, e)
        return None
    if m.video_id != video_id:
        log.warning("manifest video_id mismatch for %s; media not used", video_id)
        return None
    for f in m.files:
        if "/" in f or "\\" in f or f.startswith(".") or Path(f).suffix.lower() not in _ALLOWED_EXT:
            log.warning("manifest for %s lists disallowed file %r; media not used", video_id, f)
            return None
    return m


def _pick(m: MediaManifest, dest: Path, source: str) -> ReferenceMedia:
    video = next((dest / f for f in m.files if Path(f).suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}), None)
    subs = next((dest / f for f in m.files if Path(f).suffix.lower() in {".srt", ".vtt"}), None)
    return ReferenceMedia(m, video, subs, source)


class LocalDirMediaSource:
    name = "local"

    def __init__(self, root: str, max_bytes: int):
        self.root, self.max_bytes = Path(root), max_bytes

    def fetch(self, video_id: str, dest: Path) -> ReferenceMedia | None:
        d = self.root / video_id
        mf = d / "manifest.json"
        if not mf.is_file():
            return None
        m = _load_manifest(mf.read_text(), video_id)
        if not m:
            return None
        dest.mkdir(parents=True, exist_ok=True)
        for f in m.files:
            src = d / f
            if not src.is_file() or src.stat().st_size > self.max_bytes:
                log.warning("reference file %s missing or too large; media not used", f)
                return None
            shutil.copyfile(src, dest / f)
        return _pick(m, dest, f"local:{d}")


class S3MediaSource:
    name = "s3"

    def __init__(self, store: S3Store, prefix: str, max_bytes: int):
        self.store, self.prefix, self.max_bytes = store, prefix, max_bytes

    def fetch(self, video_id: str, dest: Path) -> ReferenceMedia | None:
        base = f"{self.prefix}{video_id}/"
        if not self.store.exists(base + "manifest.json"):
            return None
        dest.mkdir(parents=True, exist_ok=True)
        self.store.get_file(base + "manifest.json", dest / "manifest.json")
        m = _load_manifest((dest / "manifest.json").read_text(), video_id)
        if not m:
            return None
        for f in m.files:
            head = self.store.client.head_object(Bucket=self.store.bucket, Key=self.store._k(base + f))
            if head["ContentLength"] > self.max_bytes:
                log.warning("reference file %s too large; media not used", f)
                return None
            self.store.get_file(base + f, dest / f)
        return _pick(m, dest, f"s3:{base}")


def make_media_source(s: Settings) -> ReferenceMediaSource:
    if s.reference_source == "local":
        return LocalDirMediaSource(s.reference_local_dir, s.max_reference_bytes)
    if s.reference_source == "s3":
        return S3MediaSource(S3Store(s), s.reference_s3_prefix, s.max_reference_bytes)
    return NoMediaSource()


def manifest_example() -> str:
    return json.dumps({"video_id": "abcDEF12345", "license": "CC-BY-4.0, permission email 2026-10-01",
                       "provided_by": "author@example.com", "files": ["clip.mp4"], "is_short": True}, indent=2)
