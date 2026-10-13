"""HTTP helpers: timeouts, bounded retries with jitter, Retry-After, URL safety.

Retry rules distinguish *billable* calls (paid generation) from cheap/idempotent ones:

* connect errors (request never reached the server) and 429 — retried for everything;
* 503 — retried for everything (service refused the request);
* 500/502/504 and read timeouts — retried only for idempotent/cheap calls;
  for billable calls the outcome is *unknown* and the caller must reconcile, never resend blindly.
"""

from __future__ import annotations

import ipaddress
import logging
import random
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Definite failure; safe to record as failed."""

    def __init__(self, msg: str, status: int | None = None, retryable: bool = False):
        super().__init__(msg)
        self.status, self.retryable = status, retryable


class AuthError(ProviderError):
    """401/403: credentials or entitlement problem. Never retried."""


class OutcomeUnknown(RuntimeError):
    """The request may have been processed (and billed); do not resend automatically."""

    def __init__(self, msg: str, request_id: str | None = None):
        super().__init__(msg)
        self.request_id = request_id


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class RetryPolicy:
    max_retries: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    billable: bool = False

    def delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_delay_s * 5)
        exp = min(self.max_delay_s, self.base_delay_s * 2 ** attempt)
        return random.uniform(0, exp)  # full jitter  # noqa: S311


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def send_with_retry(
    send: Callable[[], httpx.Response],
    policy: RetryPolicy,
    *,
    what: str,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    attempt = 0
    while True:
        try:
            resp = send()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # Not sent / not accepted: safe to retry even for billable calls.
            if attempt >= policy.max_retries:
                raise ProviderError(f"{what}: connect failed: {type(e).__name__}", retryable=True) from e
            d = policy.delay(attempt, None)
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError) as e:
            if policy.billable:
                raise OutcomeUnknown(f"{what}: {type(e).__name__} after request was sent") from e
            if attempt >= policy.max_retries:
                raise ProviderError(f"{what}: {type(e).__name__}", retryable=True) from e
            d = policy.delay(attempt, None)
        else:
            s = resp.status_code
            if s in (401, 403):
                raise AuthError(f"{what}: HTTP {s} (check API key / entitlement)", status=s)
            retryable = s in (429, 503) or (s in (500, 502, 504) and not policy.billable)
            if s in (500, 502, 504) and policy.billable:
                rid = resp.headers.get("NVCF-REQID")
                raise OutcomeUnknown(f"{what}: HTTP {s} for billable request", request_id=rid)
            if not retryable:
                return resp
            if attempt >= policy.max_retries:
                raise ProviderError(f"{what}: HTTP {s} after {attempt} retries", status=s, retryable=True)
            retry_after = parse_retry_after(resp.headers.get("Retry-After"))
            d = policy.delay(attempt, retry_after)
            if s == 429 and retry_after is None:  # per-minute quotas (NVIDIA trial): jitter alone is too short
                d = max(d, min(policy.max_delay_s, 5.0 * 2 ** attempt))
            log.warning("%s: HTTP %s, retry %d in %.1fs", what, s, attempt + 1, d)
        attempt += 1
        sleep(d)


def make_client(connect_s: float, read_s: float, **kw) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(connect=connect_s, read=read_s, write=read_s, pool=connect_s),
        follow_redirects=False,
        **kw,
    )


class ConcurrencyLimiter:
    """Per-process cap on concurrent provider calls."""

    def __init__(self, n: int):
        self._sem = threading.BoundedSemaphore(max(1, n))

    def __enter__(self):
        self._sem.acquire()
        return self

    def __exit__(self, *exc):
        self._sem.release()


def check_public_https_url(url: str, allowed_hosts: list[str] | None = None) -> None:
    """Reject non-HTTPS URLs and hosts resolving to private/loopback/link-local addresses (SSRF)."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"only https URLs are allowed: {parts.scheme}")
    host = parts.hostname or ""
    if parts.username or parts.password:
        raise ValueError("credentials in URL are not allowed")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise ValueError(f"host not in allowlist: {host}")
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"{host} resolves to non-public address {ip}")


def download_limited(client: httpx.Client, url: str, dest, max_bytes: int,
                     allowed_hosts: list[str] | None = None) -> int:
    """Stream a public HTTPS URL to disk with a size cap."""
    check_public_https_url(url, allowed_hosts)
    n = 0
    with client.stream("GET", url) as r:
        if r.status_code != 200:
            raise ProviderError(f"download HTTP {r.status_code}", status=r.status_code)
        cl = r.headers.get("Content-Length")
        if cl and int(cl) > max_bytes:
            raise ProviderError(f"download too large: {cl} bytes")
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1024 * 1024):
                n += len(chunk)
                if n > max_bytes:
                    raise ProviderError("download exceeded size limit")
                f.write(chunk)
    return n
