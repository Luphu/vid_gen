from __future__ import annotations

import time

import httpx
import pytest

from shorts_factory.budget import BudgetGuard
from shorts_factory.config import Budget
from shorts_factory.models import JobState
from shorts_factory.net import (
    AuthError,
    BudgetExceeded,
    OutcomeUnknown,
    ProviderError,
    RetryPolicy,
    check_public_https_url,
    parse_retry_after,
    send_with_retry,
)
from shorts_factory.store import ConcurrencyError


def _seq(*items):
    it = iter(items)

    def send():
        x = next(it)
        if isinstance(x, Exception):
            raise x
        return x
    return send


def test_retry_after_honoured_and_bounded():
    sleeps: list[float] = []
    r = send_with_retry(_seq(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)),
                        RetryPolicy(max_retries=2), what="t", sleep=sleeps.append)
    assert r.status_code == 200 and sleeps == [7.0]
    with pytest.raises(ProviderError) as e:
        send_with_retry(_seq(*[httpx.Response(429)] * 3), RetryPolicy(max_retries=2), what="t",
                        sleep=lambda _: None)
    assert e.value.retryable
    assert parse_retry_after("bogus") is None


def test_billable_never_resent_on_ambiguous_failure():
    req = httpx.Request("POST", "https://x")
    for failure in (httpx.ReadTimeout("t", request=req), httpx.Response(500), httpx.Response(504),
                    httpx.RemoteProtocolError("closed", request=req)):
        calls = []

        def send(f=failure, calls=calls):
            calls.append(1)
            if isinstance(f, Exception):
                raise f
            return f
        with pytest.raises(OutcomeUnknown):
            send_with_retry(send, RetryPolicy(billable=True), what="gen", sleep=lambda _: None)
        assert len(calls) == 1, failure


def test_billable_retries_when_not_accepted():
    req = httpx.Request("POST", "https://x")
    r = send_with_retry(_seq(httpx.ConnectError("refused", request=req), httpx.Response(503),
                             httpx.Response(429), httpx.Response(200)),
                        RetryPolicy(billable=True, max_retries=4), what="gen", sleep=lambda _: None)
    assert r.status_code == 200


def test_cheap_calls_retry_5xx_and_auth_fails_fast():
    r = send_with_retry(_seq(httpx.Response(502), httpx.Response(200)), RetryPolicy(), what="t",
                        sleep=lambda _: None)
    assert r.status_code == 200
    with pytest.raises(AuthError):
        send_with_retry(_seq(httpx.Response(401)), RetryPolicy(), what="t", sleep=lambda _: None)
    assert send_with_retry(_seq(httpx.Response(422)), RetryPolicy(), what="t").status_code == 422


@pytest.mark.parametrize("url", ["http://example.com/a", "https://127.0.0.1/x", "https://10.1.2.3/x",
                                 "https://169.254.169.254/latest", "https://user:pw@example.com/",
                                 "file:///etc/passwd", "https://[::1]/"])
def test_ssrf_guard(url):
    with pytest.raises(ValueError):
        check_public_https_url(url)


def test_job_transitions_atomic_and_idempotent(store):
    job, created = store.create_job("2026-10-11:1", "vid00000001", True)
    again, created2 = store.create_job("2026-10-11:1", "vid00000001", True)
    assert created and not created2 and again.id == job.id
    j2 = store.transition(job, JobState.ANALYZED, stage="analysis", payload={"x": 1})
    assert store.get_stage(job.id, "analysis") == {"x": 1}
    with pytest.raises(ConcurrencyError):
        store.transition(job, JobState.ANALYZED)  # stale version
    store.transition(j2, JobState.SCRIPTED, stage="script", payload={"s": 1})
    with pytest.raises(ConcurrencyError):  # second runner with the same stale view
        store.transition(j2, JobState.GENERATED, stage="generate", payload={"dup": True})
    assert store.get_stage(job.id, "generate") is None  # rolled back with the failed transition
    assert store.get_job(job.id).state == JobState.SCRIPTED
    assert [e[2] for e in store.events(job.id)] == ["discovered", "analyzed", "scripted"]


def test_budget_reservation_is_capped(store):
    g = BudgetGuard(store, Budget(video_requests_per_day=2))
    g.reserve("video")
    g.reserve("video")
    with pytest.raises(BudgetExceeded):
        g.reserve("video")
    with pytest.raises(BudgetExceeded, match="set BUDGET_"):
        BudgetGuard(store, Budget(video_requests_per_day=0)).reserve("video")


def test_lock_is_exclusive_and_expires(store):
    assert store.acquire_lock("p", "a", 60)
    assert not store.acquire_lock("p", "b", 60)
    assert store.acquire_lock("p", "a", 60)  # renew
    store.release_lock("p", "a")
    assert store.acquire_lock("p", "b", 0.01)
    time.sleep(0.05)
    assert store.acquire_lock("p", "c", 60)  # expired lease taken over


def test_purge_respects_retention(store):
    now = time.time()
    store.upsert_candidate("oldvid00001", {"title": "x"}, "unknown", None, now - 100, now - 1)
    store.upsert_candidate("usedvid0001", {"title": "y"}, "unknown", None, now - 100, now - 1)
    store.mark_candidate_used("usedvid0001", "job1")
    store.upsert_candidate("newvid00001", {"title": "z"}, "unknown", None, now, now + 1000)
    res = store.purge_expired(now)
    assert res["candidates_deleted"] == 1 and res["candidates_scrubbed"] == 1
    assert store.get_candidate("usedvid0001") == {} and store.get_candidate("newvid00001")


def test_429_without_retry_after_waits_long_enough_for_a_minute_quota():
    import httpx

    from shorts_factory.net import RetryPolicy, send_with_retry

    waits = []
    replies = iter([httpx.Response(429), httpx.Response(429), httpx.Response(200)])
    resp = send_with_retry(lambda: next(replies), RetryPolicy(max_retries=4), what="t", sleep=waits.append)
    assert resp.status_code == 200 and waits[0] >= 5 and waits[1] >= 10
