"""OAuth 2.0 for the channel owner (installed-app loopback flow with PKCE).

* ``auth init`` runs once on the owner's computer, opens the consent page and writes the
  refresh token to a 0600 file — it is never printed. The owner then loads it into a Secret.
* Scopes: ``youtube.upload`` (videos.insert) + ``youtube.readonly`` (channels.list mine=true,
  processing status of own videos). Nothing broader.
* API keys and service accounts cannot upload to a regular channel.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import httpx

from ..net import AuthError, ProviderError, RetryPolicy, send_with_retry

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


class TokenProvider:
    """Exchanges the refresh token for short-lived access tokens; caches until expiry."""

    def __init__(self, client: httpx.Client, token_url: str, client_id: str, client_secret: str,
                 refresh_token: str):
        self.http, self.url = client, token_url
        self._form = {"client_id": client_id, "client_secret": client_secret,
                      "refresh_token": refresh_token, "grant_type": "refresh_token"}
        self._token: str | None = None
        self._exp = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        resp = send_with_retry(lambda: self.http.post(self.url, data=self._form),
                               RetryPolicy(max_retries=3), what="oauth refresh")
        if resp.status_code in (400, 401):
            err = _safe_json(resp).get("error", "")
            raise AuthError(f"OAuth refresh failed ({err}). Refresh token revoked/expired? Apps in 'Testing'"
                            " publishing status get refresh tokens that expire after 7 days."
                            " Re-run `shorts auth init`.",
                            status=resp.status_code)
        if resp.status_code != 200:
            raise ProviderError(f"OAuth refresh HTTP {resp.status_code}", status=resp.status_code)
        data = resp.json()
        self._token, self._exp = data["access_token"], time.time() + float(data.get("expires_in", 3600))
        return self._token

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


def run_local_auth(client_id: str, client_secret: str, out_file: Path, token_url: str,
                   open_browser: bool = True, timeout_s: int = 300) -> None:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab.")

        def log_message(self, *a):  # silence: the URL contains the code
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state,
    })
    print("Open this URL as the CHANNEL OWNER and grant access:\n" + url, flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    srv.timeout = 1
    deadline = time.time() + timeout_s
    while "code" not in result and "error" not in result and time.time() < deadline:
        srv.handle_request()  # ignores stray requests (favicon) whose state does not match
    srv.server_close()
    if "code" not in result:
        raise AuthError(f"no authorization code received ({result.get('error', 'timeout')})")
    with httpx.Client(timeout=30) as c:
        resp = c.post(token_url, data={"code": result["code"], "client_id": client_id,
                                       "client_secret": client_secret, "redirect_uri": redirect,
                                       "grant_type": "authorization_code", "code_verifier": verifier})
    if resp.status_code != 200:
        raise AuthError(f"token exchange failed: HTTP {resp.status_code} {_safe_json(resp).get('error', '')}")
    data = resp.json()
    if "refresh_token" not in data:
        raise AuthError("no refresh_token returned (revoke previous grant and retry with prompt=consent)")
    granted = set(data.get("scope", "").split())
    if not set(SCOPES) <= granted:
        raise AuthError(f"missing scopes: {sorted(set(SCOPES) - granted)}")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": data["refresh_token"], "scope": data.get("scope")}, f)
    print(f"Refresh token written to {out_file} (mode 0600). It was not printed.")
