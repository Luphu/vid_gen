"""In-memory fake of the YouTube endpoints used by the uploader (resumable protocol)."""

from __future__ import annotations

import json
import re

import httpx

CHANNEL = "UCtestchannel00000000001"


class FakeYouTube:
    def __init__(self, chunk_failures: int = 0, lose_final_response: bool = False, expire_session: bool = False,
                 existing_title: str | None = None, processing: str = "succeeded", thumbnail_status: int = 200):
        self.sessions: dict[str, dict] = {}
        self.videos: dict[str, dict] = {}
        self.inits = 0
        self.puts = 0
        self.chunk_failures = chunk_failures
        self.lose_final_response = lose_final_response
        self.expire_session = expire_session
        self.processing = processing
        self.thumbnail_status = thumbnail_status
        self.thumbnails: dict[str, bytes] = {}
        if existing_title:
            self.videos["existing001"] = {"title": existing_title}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        u = req.url
        if u.path == "/token":
            assert b"grant_type=refresh_token" in req.content
            return httpx.Response(200, json={"access_token": "ya29.fake-access-token", "expires_in": 3600})
        assert req.headers.get("Authorization") == "Bearer ya29.fake-access-token"
        if u.path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"id": CHANNEL, "snippet": {"title": "Test"},
                                                        "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if u.path.endswith("/playlistItems"):
            return httpx.Response(200, json={"items": [
                {"snippet": {"title": v["title"], "publishedAt": "2099-01-01T00:00:00Z",
                             "resourceId": {"videoId": vid}}} for vid, v in self.videos.items()]})
        if u.path.endswith("/videos") and req.method == "GET":
            vid = dict(u.params)["id"]
            if vid not in self.videos:
                return httpx.Response(200, json={"items": []})
            return httpx.Response(200, json={"items": [{"id": vid, "status": {"uploadStatus": "processed",
                                                                               "privacyStatus": "private"},
                                                        "processingDetails": {"processingStatus": self.processing}}]})
        if req.method == "POST" and u.path.endswith("/thumbnails/set"):
            vid = dict(u.params)["videoId"]
            if self.thumbnail_status != 200:
                return httpx.Response(self.thumbnail_status, json={"error": {"message": "custom thumbnails are not "
                                                                                       "available for this channel"}})
            assert vid in self.videos and req.headers["Content-Type"] in ("image/jpeg", "image/png")
            self.thumbnails[vid] = req.content
            return httpx.Response(200, json={"items": [{"default": {"url": "https://i.ytimg.com/x.jpg"}}]})
        if req.method == "POST" and u.path.endswith("/upload/videos"):
            assert dict(u.params)["uploadType"] == "resumable"
            meta = json.loads(req.content)
            assert meta["status"]["privacyStatus"] in ("private", "unlisted", "public")
            self.inits += 1
            sid = f"S{self.inits}"
            self.sessions[sid] = {"size": int(req.headers["X-Upload-Content-Length"]), "got": 0, "meta": meta}
            return httpx.Response(200, headers={"Location": f"https://upload.fake/session?upload_id={sid}"})
        if req.method == "PUT" and u.host == "upload.fake":
            sid = dict(u.params)["upload_id"]
            if self.expire_session:
                return httpx.Response(404)
            s = self.sessions[sid]
            cr = req.headers["Content-Range"]
            if cr.startswith("bytes */"):
                return self._status(sid, s)
            self.puts += 1
            m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
            start, end, total = map(int, m.groups())
            assert start == s["got"], f"chunk starts at {start}, server has {s['got']}"
            assert len(req.content) == end - start + 1
            if self.chunk_failures and start > 0:
                self.chunk_failures -= 1
                raise httpx.WriteError("connection reset", request=req)
            s["got"] = end + 1
            if s["got"] == total:
                vid = f"vid{sid}"
                self.videos[vid] = {"title": s["meta"]["snippet"]["title"], "meta": s["meta"]}
                s["video_id"] = vid
                if self.lose_final_response:
                    self.lose_final_response = False
                    raise httpx.ReadError("response lost", request=req)
            return self._status(sid, s)
        raise AssertionError(f"unexpected {req.method} {u}")

    def _status(self, sid: str, s: dict) -> httpx.Response:
        if s.get("video_id"):
            return httpx.Response(201, json={"id": s["video_id"]})
        if s["got"] == 0:
            return httpx.Response(308)
        return httpx.Response(308, headers={"Range": f"bytes=0-{s['got'] - 1}"})
