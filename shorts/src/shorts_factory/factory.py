"""Wire concrete adapters from settings. Mock adapters only when PROVIDER_MODE=mock (never prod)."""

from __future__ import annotations

from .budget import BudgetGuard
from .config import ConfigError, Settings
from .ff import FF
from .log import register_secret
from .media import make_media_source
from .net import ConcurrencyLimiter, make_client
from .pipeline import Deps
from .providers.tts import make_tts
from .storage import make_store
from .store import Store


def register_secrets(s: Settings) -> None:
    for v in (s.nvidia_api_key, s.replicate_api_token, s.youtube_api_key, s.youtube_client_secret,
              s.youtube_refresh_token, s.s3_secret_access_key, s.upload_uri_key, s.database_url):
        register_secret(v.get_secret_value() if v else None)


def make_deps(s: Settings, store: Store | None = None) -> Deps:
    register_secrets(s)
    store = store or Store(s.database_url.get_secret_value())
    store.migrate()
    budget = BudgetGuard(store, s.budget)
    ff = FF(threads=s.ffmpeg_threads, timeout_s=s.ffmpeg_timeout_s)
    d = Deps(s=s, store=store, budget=budget, artifacts=make_store(s), media=make_media_source(s), ff=ff,
             tts=make_tts(s.tts_provider, ff))
    if s.tts_provider == "replicate" and s.provider_mode != "mock":
        from .providers.replicate import ReplicateTTS

        d.tts = ReplicateTTS(s, budget, ff)

    if s.trend_source == "fixture":
        from .trends.fixture import FixtureTrendSource

        if not s.trend_fixture_file:
            raise ConfigError("TREND_SOURCE=fixture requires TREND_FIXTURE_FILE")
        d.trends = FixtureTrendSource(s.trend_fixture_file, s.ai_keywords)
    else:
        from .trends.youtube import YouTubeSearchSource

        tokens = None
        if not s.youtube_api_key and s.youtube_refresh_token and s.youtube_client_secret:
            from .youtube.oauth import TokenProvider

            tokens = TokenProvider(make_client(10, 30), s.youtube_token_url, s.youtube_client_id,
                                   s.youtube_client_secret.get_secret_value(),
                                   s.youtube_refresh_token.get_secret_value())
        d.trends = YouTubeSearchSource(s, budget, make_client(10, 30), tokens)

    if s.provider_mode == "mock":
        from .providers.mock import MockChat, MockVideo, default_mock_responses

        chat = MockChat(default_mock_responses(s.content_language))
        d.text = d.vision = chat
        d.video = MockVideo(ff, *_gen_size(s), budget=budget)
    else:
        from .providers.nim_chat import NimTextClient, NimVisionClient
        from .providers.nim_video import NimVideoClient

        limiter = ConcurrencyLimiter(s.nim_max_concurrency)
        d.vision = NimVisionClient(s, budget, limiter=limiter) if s.nvidia_api_key else None
        if s.text_provider == "replicate":
            from .providers.replicate import ReplicateText

            d.text = ReplicateText(s, budget, limiter=limiter) if s.replicate_api_token else None
        else:
            d.text = NimTextClient(s, budget, limiter=limiter)
        if s.video_provider == "replicate":
            from .providers.replicate import ReplicateVideo

            d.video = ReplicateVideo(s, budget, limiter=limiter) if s.replicate_api_token else None
        else:
            d.video = NimVideoClient(s, budget, limiter=limiter) if s.nim_video_url else None

    if not s.dry_run:
        d.youtube = make_youtube(s, store, budget)
    elif s.youtube_refresh_token:
        d.youtube = make_youtube(s, store, budget)  # publish-stage status checks of earlier real jobs
    return d


def make_youtube(s: Settings, store: Store, budget: BudgetGuard):
    from .youtube.oauth import TokenProvider
    from .youtube.upload import SessionCipher, YouTubeClient

    s.require("youtube_client_id", "youtube_client_secret", "youtube_refresh_token", "youtube_channel_id",
              "upload_uri_key")
    http = make_client(10, 120)
    tokens = TokenProvider(http, s.youtube_token_url, s.youtube_client_id,
                           s.youtube_client_secret.get_secret_value(),  # type: ignore[union-attr]
                           s.youtube_refresh_token.get_secret_value())  # type: ignore[union-attr]
    return YouTubeClient(s, store, budget, tokens, http,
                         SessionCipher(s.upload_uri_key.get_secret_value()))  # type: ignore[union-attr]


def _gen_size(s: Settings) -> tuple[int, int]:
    sizes = {"256_9_16": (192, 320), "480_9_16": (480, 832), "720_9_16": (720, 1280), "480p": (480, 832),
             "720p": (720, 1280)}
    return sizes.get(s.gen_resolution, (s.out_width, s.out_height))
