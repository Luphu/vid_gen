"""Fixture TrendSource for local dry runs and tests (no network, no quota)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..models import Candidate, SearchScope
from .classify import classify_ai_metadata, classify_format


class FixtureTrendSource:
    name = "fixture"

    def __init__(self, path: str, ai_keywords: list[str]):
        self.path, self.ai_keywords = Path(path), ai_keywords

    def discover(self) -> tuple[SearchScope, list[Candidate]]:
        now = datetime.now(UTC)
        raw = json.loads(self.path.read_text())
        out = []
        for i, item in enumerate(raw["items"]):
            fmt, why = classify_format(item.get("duration_s"), item["title"], item.get("description", ""),
                                       item.get("tags", []))
            ai = classify_ai_metadata(item["title"], item.get("description", ""), item.get("tags", []),
                                      self.ai_keywords, item.get("contains_synthetic_media"))
            out.append(Candidate(**item, format_status=fmt, format_reason=why, ai=ai, fetched_at=now,
                                 search_rank=i))
        scope = SearchScope(keywords=["fixture"], channel_allowlist=[], region="--", language="--",
                            published_after=now - timedelta(days=3), max_results=len(out), fetched_at=now,
                            pages_fetched=0)
        return scope, out

    def channel_recent_titles(self, channel_id: str, n: int = 15) -> list[str]:
        return []
