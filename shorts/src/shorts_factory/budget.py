"""Request budgets persisted in the database (survive restarts and retries)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .config import Budget
from .net import BudgetExceeded
from .store import Store

_PT = ZoneInfo("America/Los_Angeles")  # YouTube quota resets at midnight Pacific Time

BUCKETS = {
    "nim_text": ("text_requests_per_day", "utc"),
    "nim_vision": ("vision_requests_per_day", "utc"),
    "nim_video": ("video_requests_per_day", "utc"),
    "yt_search": ("yt_search_calls_per_day", "pt"),
    "yt_units": ("yt_units_per_day", "pt"),
    "yt_upload": ("yt_uploads_per_day", "pt"),
}


def day_key(tz: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return (now.astimezone(_PT) if tz == "pt" else now.astimezone(UTC)).strftime("%Y-%m-%d")


class BudgetGuard:
    def __init__(self, store: Store, budget: Budget):
        self.store, self.budget = store, budget

    def limit(self, bucket: str) -> int:
        return int(getattr(self.budget, BUCKETS[bucket][0]))

    def reserve(self, bucket: str, n: int = 1) -> None:
        """Reserve before the call. A reserved unit is spent even if the call later fails."""
        limit = self.limit(bucket)
        if limit <= 0:
            raise BudgetExceeded(
                f"budget for {bucket} is 0: set BUDGET_{BUCKETS[bucket][0].upper()} explicitly to enable it"
            )
        if not self.store.reserve_budget(day_key(BUCKETS[bucket][1]), bucket, n, limit):
            raise BudgetExceeded(f"daily budget exhausted for {bucket} (limit {limit})")

    def usage(self) -> dict[str, str]:
        out = {}
        for bucket, (_field, tz) in BUCKETS.items():
            used = self.store.budget_usage(day_key(tz)).get(bucket, 0)
            out[bucket] = f"{used}/{self.limit(bucket)} ({tz} day {day_key(tz)})"
        return out
