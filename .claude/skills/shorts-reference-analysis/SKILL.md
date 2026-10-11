---
name: shorts-reference-analysis
description: Use when changing trend discovery (YouTube Data API search), Shorts format detection, AI-content classification, reference media sources, analysis prompts or originality checks in shorts/. Covers what the API can and cannot tell, metadata_only vs full_media, untrusted input and originality rules.
---

# Trend discovery and reference analysis

## When to use
- Editing `trends/`, `media.py`, `analysis.py`, `scripting.py`, `prompts/*.md`, selection logic in `pipeline.py`.

## Verified project rules
1. YouTube Data API: `search.list` (type=video, order=viewCount, publishedAfter, regionCode,
   relevanceLanguage, bounded pages) + `videos.list` (snippet, contentDetails, statistics, status).
   Quota (2026 docs): search.list and videos.insert have their own 100-calls/day buckets; others 10 000 units.
   API key goes in the `X-Goog-Api-Key` header, never the URL.
2. `videoDuration=short` means < 4 min — **not** Shorts. Format is `confirmed` only from a rights-holder
   manifest, `probable` = ≤ 180 s + `#shorts` marker, `unknown` otherwise, `not_short` > 180 s.
3. AI classification is a heuristic: `ai_likelihood` + `evidence[]` + `classification_source`. No signal →
   `ai_likelihood=null` (absence is not proof). Model opinions are labelled `vision_model`. Ambiguous → review.
4. No derived metrics from API data (no views/hour, engagement ratios, viral scores). Show search scope and
   `fetched_at`; never claim a global maximum. Retain API data ≤ `YOUTUBE_DATA_RETENTION_DAYS` (25), `purge`.
5. The Data API is not a media source. Media only via `ReferenceMediaSource` with a `manifest.json`
   (`license`, `provided_by`, `files`). No scraping/downloaders.
6. `metadata_only` must not describe frames, pacing, structure, on-screen text, speech or music — enforced by
   the `Analysis` validator; limitations are written by code, not the model.
7. Scripts are original: shingle-overlap and title checks vs. the reference; borrow theme/format only.
   Prompts are versioned files (`id`/`version` header); store the prompt ref with each stage result.

## How to verify
```bash
cd shorts && .venv/bin/pytest -q tests/test_classify_and_trends.py tests/test_models_and_config.py tests/test_examples.py
docker build --platform linux/arm64 --target test -t shorts-test . && docker run --rm shorts-test \
  pytest -q tests/test_e2e.py -k "full_media or manifest"
```
