from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from shorts_factory.config import load_settings
from shorts_factory.store import Store

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


def _db_urls(tmp_path: Path) -> list[str]:
    urls = [f"sqlite:///{tmp_path}/t.db"]
    if os.environ.get("TEST_DATABASE_URL"):
        urls.append(os.environ["TEST_DATABASE_URL"])
    return urls


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "postgres":
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            pytest.skip("TEST_DATABASE_URL not set")
        st = Store(url)
        with st.tx() as c:
            for t in ("candidates", "discovery_runs", "jobs", "stage_results", "external_ops", "budget_ledger",
                      "uploads", "events", "locks"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
    else:
        st = Store(f"sqlite:///{tmp_path}/t.db")
    st.migrate()
    yield st
    st.close()


@pytest.fixture
def settings_env(tmp_path):
    fixture = tmp_path / "trends.json"
    fixture.write_text((EXAMPLES / "trends_fixture.json").read_text())
    return {
        "APP_ENV": "test", "DATABASE_URL": f"sqlite:///{tmp_path}/db.sqlite", "WORK_DIR": str(tmp_path / "work"),
        "STORAGE_LOCAL_DIR": str(tmp_path / "storage"), "PROVIDER_MODE": "mock", "TREND_SOURCE": "fixture",
        "TREND_FIXTURE_FILE": str(fixture), "TTS_PROVIDER": "mock", "BUDGET_VIDEO_REQUESTS_PER_DAY": "20",
        "THUMBNAILS": "false",  # enabled explicitly in the thumbnail tests
        "CONTENT_LANGUAGE": "ru", "NARRATION_WORDS_PER_S": "2.2",  # the mock data is Russian
        "TTS_VOICES": '{"Mock_Voice": {"desc": "test voice", "wps": 2.2}}',
        "BUDGET_VIDEO_REQUESTS_PER_JOB": "8", "NIM_VIDEO_RESOLUTION": "256_9_16", "OUT_WIDTH": "360",
        "OUT_HEIGHT": "640", "FONT_FILE": _font(), "BURN_SUBTITLES": "true" if _font() else "false",
        "FFMPEG_THREADS": "2", "VIDEO_PROVIDER": "nvidia",
    }


def _font() -> str:
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        if Path(p).is_file():
            return p
    return ""


@pytest.fixture
def settings(settings_env):
    return load_settings(settings_env)


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())
