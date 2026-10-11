"""Trend discovery sources."""

from __future__ import annotations

from typing import Protocol

from ..models import Candidate, SearchScope


class TrendSource(Protocol):
    name: str

    def discover(self) -> tuple[SearchScope, list[Candidate]]:
        """Return the exact search scope and the candidates found in it."""
        ...
