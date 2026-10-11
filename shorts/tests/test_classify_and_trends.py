from __future__ import annotations

import json

import httpx

from shorts_factory.budget import BudgetGuard
from shorts_factory.config import load_settings
from shorts_factory.models import FormatStatus
from shorts_factory.pipeline import select_candidate
from shorts_factory.trends.classify import classify_ai_metadata, classify_format, parse_iso8601_duration
from shorts_factory.trends.youtube import YouTubeSearchSource

KW = load_settings({}).ai_keywords


def test_duration_parse():
    assert parse_iso8601_duration("PT59S") == 59
    assert parse_iso8601_duration("PT2M30S") == 150
    assert parse_iso8601_duration("PT1H") == 3600
    assert parse_iso8601_duration("P0D") == 0
    assert parse_iso8601_duration("bogus") is None


def test_format_classification_is_not_videoduration_short():
    # 3m50s is "short" for search.list (<4 min) but cannot be a Short (>3 min).
    assert classify_format(230, "x", "", [])[0] == FormatStatus.NOT_SHORT
    assert classify_format(40, "x #shorts", "", [])[0] == FormatStatus.PROBABLE
    assert classify_format(40, "x", "", ["Shorts"])[0] == FormatStatus.PROBABLE
    assert classify_format(40, "x", "", [])[0] == FormatStatus.UNKNOWN
    assert classify_format(None, "x #shorts", "", [])[0] == FormatStatus.UNKNOWN
    assert classify_format(40, "x", "", [], authoritative_is_short=True)[0] == FormatStatus.CONFIRMED


def test_ai_classification_is_evidence_based():
    none = classify_ai_metadata("My cat", "phone video", [], KW, None)
    assert none.ai_likelihood is None and none.classification_source == "none"  # absence != proof
    partial = classify_ai_metadata("#aitools contest", "", [], KW, None)
    assert partial.ai_likelihood is None  # keyword '#ai' must not match inside '#aitools'
    strong = classify_ai_metadata("Dragon made with AI", "sora clip", ["ai generated"], KW, None)
    assert strong.ai_likelihood and strong.ai_likelihood >= 0.6
    assert {e.field for e in strong.evidence} >= {"title", "description", "tags"}
    disclosed = classify_ai_metadata("dragon", "", [], KW, True)
    assert disclosed.classification_source == "creator_disclosure" and disclosed.ai_likelihood == 0.95


def test_select_candidate_prefers_confident_ai_and_skips_long(settings):
    from shorts_factory.trends.fixture import FixtureTrendSource

    _, cands = FixtureTrendSource(settings.trend_fixture_file, settings.ai_keywords).discover()
    chosen = select_candidate(settings, cands)
    assert chosen and chosen.video_id == "fixtAI00001"
    assert select_candidate(settings, [c for c in cands if c.video_id != "fixtAI00001"]) is None


def _yt_handler(calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        assert "key=" not in str(req.url), "API key must be sent in a header"
        assert req.headers["X-Goog-Api-Key"] == "test-api-key-123456"
        if req.url.path.endswith("/search"):
            q = dict(req.url.params)
            assert q["type"] == "video" and q["order"] == "viewCount" and q["videoDuration"] == "short"
            assert q["publishedAfter"].endswith("Z") and q["regionCode"] == "DE" and q["relevanceLanguage"] == "de"
            items = [{"id": {"videoId": "vidAAAAAAA1"}}, {"id": {"videoId": "vidBBBBBBB2"}}]
            body = {"items": items, "nextPageToken": "P2"} if "pageToken" not in q else {"items": [
                {"id": {"videoId": "vidCCCCCCC3"}}]}
            return httpx.Response(200, json=body)
        ids = dict(req.url.params)["id"].split(",")
        return httpx.Response(200, json={"items": [{
            "id": i, "snippet": {"title": f"t {i} #shorts", "description": "made with ai", "channelId": "UCx",
                                 "publishedAt": "2026-10-10T00:00:00Z", "tags": []},
            "contentDetails": {"duration": "PT30S"}, "statistics": {"viewCount": "10"}, "status": {}} for i in ids]})
    return handler


def test_youtube_search_scope_dedup_pagination_quota(store):
    s = load_settings({"YOUTUBE_API_KEY": "test-api-key-123456", "TREND_KEYWORDS": "a,b", "TREND_REGION": "DE",
                       "TREND_LANGUAGE": "de", "TREND_MAX_PAGES": "2", "TREND_MAX_RESULTS": "10"})
    calls: list = []
    src = YouTubeSearchSource(s, BudgetGuard(store, s.budget), httpx.Client(transport=httpx.MockTransport(
        _yt_handler(calls))))
    scope, cands = src.discover()
    assert [c.video_id for c in cands] == ["vidAAAAAAA1", "vidBBBBBBB2", "vidCCCCCCC3"]  # dedup across keywords
    assert scope.pages_fetched == 4 and scope.region == "DE" and scope.fetched_at
    assert all(c.format_status == FormatStatus.PROBABLE for c in cands)
    usage = BudgetGuard(store, s.budget).usage()
    assert usage["yt_search"].startswith("4/") and usage["yt_units"].startswith("1/")
    assert not hasattr(cands[0], "views_per_hour")
    json.dumps(scope.model_dump(mode="json"))


def test_youtube_search_respects_quota_budget(store):
    s = load_settings({"YOUTUBE_API_KEY": "test-api-key-123456", "BUDGET_YT_SEARCH_CALLS_PER_DAY": "1",
                       "TREND_MAX_PAGES": "3", "TREND_REGION": "DE", "TREND_LANGUAGE": "de"})
    src = YouTubeSearchSource(s, BudgetGuard(store, s.budget),
                              httpx.Client(transport=httpx.MockTransport(_yt_handler([]))))
    import pytest

    from shorts_factory.net import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        src.discover()


def test_youtube_search_falls_back_to_oauth(store):
    class Tok:
        def headers(self):
            return {"Authorization": "Bearer ya29.test-token"}

    seen = []

    def h(req):
        seen.append(req.headers.get("Authorization"))
        assert "X-Goog-Api-Key" not in req.headers
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [{"id": {"videoId": "vidAAAAAAA1"}}]})
        return httpx.Response(200, json={"items": []})
    s = load_settings({"TREND_KEYWORDS": "a", "TREND_REGION": "DE", "TREND_LANGUAGE": "de"})
    YouTubeSearchSource(s, BudgetGuard(store, s.budget), httpx.Client(transport=httpx.MockTransport(h)),
                        Tok()).discover()
    assert seen and all(x == "Bearer ya29.test-token" for x in seen)
