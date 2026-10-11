from __future__ import annotations

import base64
import os

import httpx
import pytest

from shorts_factory.budget import BudgetGuard
from shorts_factory.config import load_settings
from shorts_factory.models import UploadRequest
from shorts_factory.net import AuthError
from shorts_factory.youtube.oauth import TokenProvider
from shorts_factory.youtube.upload import SessionCipher, UploadUnknown, YouTubeClient

from .fake_youtube import CHANNEL, FakeYouTube

KEY = base64.b64encode(os.urandom(32)).decode()
REQ = UploadRequest(title="Paper robot", description="desc. AI", tags=["a"], category_id="28",
                    privacy_status="private", self_declared_made_for_kids=False, contains_synthetic_media=True,
                    default_language="ru")


def _client(store, fake: FakeYouTube, channel=CHANNEL):
    s = load_settings({"YOUTUBE_API_BASE": "https://api.fake/youtube/v3",
                       "YOUTUBE_UPLOAD_URL": "https://api.fake/upload/videos", "YOUTUBE_TOKEN_URL": "https://api.fake/token",
                       "YOUTUBE_CHANNEL_ID": channel, "UPLOAD_CHUNK_BYTES": str(256 * 1024),
                       "BUDGET_YT_UPLOADS_PER_DAY": "5"})
    http = httpx.Client(transport=fake.transport())
    tokens = TokenProvider(http, s.youtube_token_url, "cid", "csecret", "1//refresh-token-abcdefghijk")
    return YouTubeClient(s, store, BudgetGuard(store, s.budget), tokens, http, SessionCipher(KEY),
                         sleep=lambda _: None)


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "final.mp4"
    p.write_bytes(os.urandom(256 * 1024 * 3 + 1000))  # 4 chunks
    return p


def test_channel_verification(store):
    _client(store, FakeYouTube()).verify_channel()
    with pytest.raises(AuthError, match="refusing to upload"):
        _client(store, FakeYouTube(), channel="UCother").verify_channel()


def test_resumable_upload_happy_path_and_no_duplicate(store, video):
    fake = FakeYouTube()
    c = _client(store, fake)
    vid = c.upload("job1", video, REQ, "jobs/job1/final.mp4")
    assert vid == "vidS1" and fake.inits == 1 and fake.puts == 4
    row = store.get_upload("job1")
    assert row["status"] == "uploaded" and row["session_uri_enc"] is None
    assert fake.sessions["S1"]["meta"]["status"]["containsSyntheticMedia"] is True
    assert c.upload("job1", video, REQ, "k") == "vidS1" and fake.inits == 1  # rerun: no new upload


def test_session_uri_is_encrypted_at_rest(store, video):
    fake = FakeYouTube(chunk_failures=99)
    with pytest.raises(UploadUnknown):
        _client(store, fake).upload("job1", video, REQ, "k")
    row = store.get_upload("job1")
    assert row["session_uri_enc"] and "upload_id" not in row["session_uri_enc"]
    assert "upload.fake" in SessionCipher(KEY).dec(row["session_uri_enc"])


def test_resume_after_interruption_and_process_restart(store, video):
    fake = FakeYouTube(chunk_failures=99)
    with pytest.raises(UploadUnknown):
        _client(store, fake).upload("job1", video, REQ, "k")
    got = fake.sessions["S1"]["got"]
    assert 0 < got < video.stat().st_size
    fake.chunk_failures = 0
    with pytest.raises(UploadUnknown):  # status 'unknown' blocks automatic continuation
        _client(store, fake).upload("job1", video, REQ, "k")
    vid = _client(store, fake).upload("job1", video, REQ, "k", allow_new_session_after_unknown=True)
    assert vid == "vidS1" and fake.inits == 1  # same session resumed from the server offset


def test_transient_chunk_failure_resumes_in_process(store, video):
    fake = FakeYouTube(chunk_failures=1)
    assert _client(store, fake).upload("job1", video, REQ, "k") == "vidS1"
    assert fake.inits == 1


def test_lost_final_response_recovered_by_status_query(store, video):
    fake = FakeYouTube(lose_final_response=True)
    assert _client(store, fake).upload("job1", video, REQ, "k") == "vidS1"
    assert fake.inits == 1 and len(fake.videos) == 1


def test_expired_session_reconciles_against_channel(store, video):
    fake = FakeYouTube(chunk_failures=99)
    with pytest.raises(UploadUnknown):
        _client(store, fake).upload("job1", video, REQ, "k")
    fake.expire_session, fake.chunk_failures = True, 0
    fake.videos["adopted01"] = {"title": REQ.title}  # completed on YouTube side
    vid = _client(store, fake).upload("job1", video, REQ, "k", allow_new_session_after_unknown=True)
    assert vid == "adopted01" and fake.inits == 1


def test_expired_session_without_match_needs_human(store, video):
    fake = FakeYouTube(chunk_failures=99)
    with pytest.raises(UploadUnknown):
        _client(store, fake).upload("job1", video, REQ, "k")
    fake.expire_session = True
    with pytest.raises(UploadUnknown, match="no matching video"):
        _client(store, fake).upload("job1", video, REQ, "k", allow_new_session_after_unknown=True)
    assert fake.inits == 1
    # After the operator verified Studio: explicit new session
    fake.expire_session, fake.chunk_failures = False, 0
    vid = _client(store, fake).upload("job1", video, REQ, "k", allow_new_session_after_unknown=True)
    assert vid == "vidS2" and fake.inits == 2


def test_processing_status(store):
    fake = FakeYouTube(existing_title="x")
    assert _client(store, fake).processing_status("existing001")["processingStatus"] == "succeeded"
    assert _client(store, fake).processing_status("missing")["found"] is False
