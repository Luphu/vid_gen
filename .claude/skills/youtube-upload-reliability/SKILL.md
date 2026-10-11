---
name: youtube-upload-reliability
description: Use when changing YouTube OAuth, resumable upload, publication settings (privacy, publishAt, made-for-kids, synthetic media), duplicate protection, the job state machine or review/retry CLI in shorts/. Covers the no-duplicate guarantees and how to test them without touching a real channel.
---

# YouTube upload reliability

## When to use
- Editing `youtube/oauth.py`, `youtube/upload.py`, `stage_upload`/`stage_publish`/`build_upload_request`,
  `retry`/`approve`/`reject`/`resolve_op`, or publication-related config.

## Verified project rules
1. Upload needs the channel owner's OAuth 2.0 refresh token (scopes `youtube.upload` + `youtube.readonly`).
   API keys/service accounts cannot upload to a normal channel. `auth init` writes the token to a 0600 file,
   never prints it. "Testing" OAuth apps get 7-day refresh tokens.
2. Before every upload: `channels.list mine=true` must equal `YOUTUBE_CHANNEL_ID` (and the consent channel).
3. Always send `privacyStatus` explicitly (API default is public!), `selfDeclaredMadeForKids` from an explicit
   `PUBLISH_MADE_FOR_KIDS`, `containsSyntheticMedia=true`. `publishAt` only with `private`.
4. Defaults: `DRY_RUN=true`, `AUTO_PUBLISH=false` (always private). With consent, the first real upload is still
   private; daily limit `PUBLISH_DAILY_LIMIT`.
5. Resumable protocol: init (no video created) → session URI saved AES-GCM-encrypted *before* bytes → chunks
   (multiple of 256 KiB) → status query `Content-Range: bytes */size` on errors. Completed → `video_id` saved.
6. Unknown outcome after bytes were sent → query session → if expired, search own uploads by title → otherwise
   `upload_unknown`; a new session only with explicit `--new-upload-session` after a human checked Studio.
7. Rerunning any stage after `uploaded` is refused (duplicate protection). `review_required` items without a
   safe resume point cannot be approved, only retried/rejected. QC failures are never published.

## How to verify (no real channel)
```bash
cd shorts && .venv/bin/pytest -q tests/test_youtube_upload.py       # fake resumable server: interruptions,
                                                                    # lost final response, expired session
docker run --rm shorts-test pytest -q tests/test_e2e.py -k "upload or audience"
```
Real verification = private smoke test by the user (README "Приватный smoke-test"); state if it was not run.
