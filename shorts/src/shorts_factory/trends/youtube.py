"""TrendSource over the official YouTube Data API v3 (search.list + videos.list).

* type=video, order=viewCount, publishedAfter window, regionCode, relevanceLanguage;
* videoDuration=short narrows to < 4 minutes — NOT a Shorts filter (see classify.py);
* bounded pagination, quota accounted in BudgetGuard before each call;
* no derived metrics (no views/hour, engagement ratios, viral scores).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import httpx

from ..budget import BudgetGuard
from ..config import ConfigError, Settings
from ..models import Candidate, FormatStatus, SearchScope
from ..net import ProviderError, RetryPolicy, send_with_retry
from .classify import classify_ai_metadata, classify_format, parse_iso8601_duration

log = logging.getLogger(__name__)


class YouTubeSearchSource:
    name = "youtube_data_api"

    def __init__(self, s: Settings, budget: BudgetGuard, client: httpx.Client, tokens=None):
        """Auth: API key if configured, otherwise the channel owner's OAuth token (youtube.readonly)."""
        self.s, self.budget, self.http, self.tokens = s, budget, client, tokens

    def _auth(self) -> dict:
        if self.s.youtube_api_key:
            # Key in a header, not the query string, so it never lands in URL logs.
            return {"X-Goog-Api-Key": self.s.youtube_api_key.get_secret_value()}
        if self.tokens is not None:
            return self.tokens.headers()
        raise ConfigError("missing configuration: YOUTUBE_API_KEY (or YouTube OAuth credentials)")

    def _get(self, path: str, params: dict, bucket: str) -> dict:
        hdr = self._auth()
        self.budget.reserve(bucket)
        resp = send_with_retry(lambda: self.http.get(f"{self.s.youtube_api_base}/{path}", params=params,
                                                      headers=hdr),
                               RetryPolicy(max_retries=3), what=f"youtube {path}")
        if resp.status_code != 200:
            raise ProviderError(f"youtube {path}: HTTP {resp.status_code}: {_err(resp)}", status=resp.status_code)
        return resp.json()

    def discover(self) -> tuple[SearchScope, list[Candidate]]:
        s = self.s
        now = datetime.now(UTC)
        after = (now - timedelta(hours=s.trend_window_hours)).replace(microsecond=0)
        scope = SearchScope(keywords=s.trend_keywords, channel_allowlist=s.trend_channel_allowlist,
                            region=s.trend_region, language=s.trend_language, published_after=after,
                            max_results=s.trend_max_results, fetched_at=now)
        queries: list[dict] = []
        base = {
            "part": "snippet", "type": "video", "order": "viewCount", "videoDuration": "short",
            "publishedAfter": after.isoformat().replace("+00:00", "Z"), "regionCode": s.trend_region,
            "relevanceLanguage": s.trend_language, "maxResults": min(50, s.trend_max_results),
            "safeSearch": "strict",
        }
        if s.trend_channel_allowlist:
            for ch in s.trend_channel_allowlist:
                for kw in s.trend_keywords or [""]:
                    queries.append(dict(base, channelId=ch, **({"q": kw} if kw else {})))
        else:
            queries = [dict(base, q=kw) for kw in s.trend_keywords]

        ranked: list[str] = []
        for q in queries:
            token = None
            for _page in range(max(1, s.trend_max_pages)):
                if token:
                    q["pageToken"] = token
                data = self._get("search", q, "yt_search")
                scope.pages_fetched += 1
                for item in data.get("items", []):
                    vid = item.get("id", {}).get("videoId")
                    if vid and vid not in ranked:
                        ranked.append(vid)
                token = data.get("nextPageToken")
                if not token or len(ranked) >= s.trend_max_results:
                    break
            if len(ranked) >= s.trend_max_results:
                break
        ranked = ranked[: s.trend_max_results]

        candidates: list[Candidate] = []
        for i in range(0, len(ranked), 50):
            batch = ranked[i:i + 50]
            data = self._get("videos", {"part": "snippet,contentDetails,statistics,status,topicDetails",
                                        "id": ",".join(batch), "maxResults": 50}, "yt_units")
            by_id = {it["id"]: it for it in data.get("items", [])}
            for vid in batch:
                if vid in by_id:
                    candidates.append(self._to_candidate(by_id[vid], ranked.index(vid), now))
        if s.trend_mode == "kids":
            candidates = self._kids_top(candidates)
        log.info("discovered %d candidates in scope region=%s lang=%s after=%s",
                 len(candidates), s.trend_region, s.trend_language, after.isoformat())
        return scope, candidates

    def _kids_top(self, cands: list[Candidate]) -> list[Candidate]:
        """Top N by raw view count among Shorts-length videos declared made for kids (no derived metrics)."""
        pool = [c for c in cands if c.format_status != FormatStatus.NOT_SHORT
                and (c.made_for_kids is True or not self.s.trend_require_made_for_kids)
                and language_ok(c, self.s.trend_languages)]
        pool.sort(key=lambda c: c.view_count or 0, reverse=True)
        top = pool[: self.s.trend_top_n]
        for i, c in enumerate(top):
            c.search_rank = i
        log.info("kids trends: %d made-for-kids short candidates of %d fetched, keeping top %d by views",
                 len(pool), len(cands), len(top))
        return top

    def channel_recent_titles(self, channel_id: str, n: int = 15) -> list[str]:
        """Titles of the channel's latest uploads: reveals the series format (2 quota units)."""
        ch = self._get("channels", {"part": "contentDetails", "id": channel_id}, "yt_units").get("items", [])
        if not ch:
            return []
        uploads = ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        items = self._get("playlistItems", {"part": "snippet", "playlistId": uploads, "maxResults": n},
                          "yt_units").get("items", [])
        return [it["snippet"]["title"][:150] for it in items if it.get("snippet", {}).get("title")]

    def _to_candidate(self, it: dict, rank: int, fetched_at: datetime) -> Candidate:
        sn, cd, st, stats = it.get("snippet", {}), it.get("contentDetails", {}), it.get("status", {}), \
            it.get("statistics", {})
        dur = parse_iso8601_duration(cd.get("duration"))
        tags = sn.get("tags", []) or []
        fmt, why = classify_format(dur, sn.get("title", ""), sn.get("description", ""), tags)
        synth = st.get("containsSyntheticMedia")
        ai = classify_ai_metadata(sn.get("title", ""), sn.get("description", ""), tags, self.s.ai_keywords, synth)
        return Candidate(
            video_id=it["id"], title=sn.get("title", ""), description=sn.get("description", ""),
            channel_id=sn.get("channelId", ""), channel_title=sn.get("channelTitle", ""),
            published_at=sn.get("publishedAt"), duration_s=dur, tags=tags,
            view_count=_int(stats.get("viewCount")), like_count=_int(stats.get("likeCount")),
            comment_count=_int(stats.get("commentCount")),
            default_language=sn.get("defaultLanguage") or sn.get("defaultAudioLanguage"),
            contains_synthetic_media=synth, made_for_kids=st.get("madeForKids"),
            thumbnail_url=_best_thumbnail(sn.get("thumbnails", {})),
            topic_categories=[u.rsplit("/", 1)[-1].replace("_", " ")
                              for u in it.get("topicDetails", {}).get("topicCategories", [])][:8],
            format_status=fmt, format_reason=why, ai=ai,
            fetched_at=fetched_at, search_rank=rank,
        )


_SCRIPTS = {"ru": re.compile(r"[\u0400-\u04FF]"), "uk": re.compile(r"[\u0400-\u04FF]"),
            "en": re.compile(r"^[\x00-\x7F\u2000-\u2BFF\U0001F000-\U0001FAFF\uFE0F]*$")}


def language_ok(c: Candidate, langs: list[str]) -> bool:
    """Metadata audio language if declared, otherwise the script of the title."""
    if not langs:
        return True
    if c.default_language:
        return c.default_language.split("-")[0].lower() in langs
    if any(lang in _SCRIPTS and _SCRIPTS[lang].search(c.title) for lang in langs):
        return True
    return False


def _best_thumbnail(thumbs: dict) -> str | None:
    for k in ("maxres", "standard", "high", "medium", "default"):
        url = thumbs.get(k, {}).get("url")
        if url and url.startswith("https://i.ytimg.com/"):
            return url
    return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _err(resp: httpx.Response) -> str:
    try:
        e = resp.json().get("error", {})
        reasons = ",".join(x.get("reason", "") for x in e.get("errors", []))
        return f"{e.get('message', '')[:200]} [{reasons}]"
    except ValueError:
        return resp.text[:200]
