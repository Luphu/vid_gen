# nim — autonomous YouTube Shorts factory on a Raspberry Pi cluster

A pet project that turns a trending kids' Short into an **original** vertical video and uploads it to YouTube
with no human in the loop: trend research → analysis → script by an LLM agent team with critics → video, voice
and editing → automated QC → private upload. It runs on a three-node Raspberry Pi 5 k3s cluster managed with
GitOps; heavy inference is remote (NVIDIA NIM, Replicate).

**Status: stopped.** The pipeline works end to end, but the economics of a new Shorts channel do not
(see [Results](#results-and-why-it-stopped)).

## Pipeline

```
YouTube Data API ─► trend pick ─► analysis ─► agent team ─► keyframe ─► video ─► TTS ─► render ─► QC ─► upload
 (search, stats)    language,     NIM text +  writer        flux       Wan 2.2   MiniMax  FFmpeg   VLM    private,
                    songs/lessons vision      director      (Replicate) i2v chain           cuts,   checks resumable,
                    filtered out              editor                                        J-cuts         thumbnail
                                              + critics, viewer test, fact-check
```

Every stage is idempotent and checkpointed in a database (SQLite locally, PostgreSQL in the cluster), so a job can
resume after a crash, a quota hit or a provider outage. Retryable provider errors defer the job instead of failing it.

### Script by an agent team

`shorts/src/shorts_factory/agents.py`. Three authors on `nemotron-3-ultra` work in phases, each locking the
upstream work: **writer** (story), **director** (shot plan for a video model), **editor** (narration, on-screen
text, title). Each has its own **critic** (`nemotron-3-super`, temperature 0) with a versioned rubric.

- Video is generated only when every critic scores **≥ 8** overall and **≥ 6** on each criterion with no must-fix items.
  Video costs real money, so rejection happens before any spending.
- The director and the editor can escalate to the writer when a story cannot be filmed or its ending does not land.
- A final gate runs all critics together, then a **viewer test**: a separate model retells the video from narration
  and frames only, and the retelling is matched with the intended story. Educational topics also get a **fact-check**.
- Convergence fixes from real runs: critics see their previous verdict (less noise), unchanged approved
  packages skip re-review, and the code, not the LLM, picks among narration length variants so word counts fit the voice's real words per second.
- Deterministic checks back the LLMs: forbidden and text-prone props (screens, packaging, brands), pacing,
  minimum scene length, voice validity, originality versus the reference.

`shorts script-lab` runs the team on N trending references without generating media; reports such as
[docs/script-lab-2026-10-13.md](docs/script-lab-2026-10-13.md) were used to tune prompts cheaply.

### Media and QC

- Keyframe (flux) → Wan 2.2 image-to-video chain for consistent characters; MiniMax TTS with a voice chosen by
  context; FFmpeg render with 1–2 s cuts, zooms, a 1.2 s hook and J-cut narration.
- Vision QC (Llama 3.2 Vision): rejects frames with lettering (brand-like text is a common video-model artifact)
  and checks that the story's key props are actually visible; a bad scene is regenerated, the last attempt goes
  to human review.
- Thumbnails: NVIDIA `flux.1-dev` from the video's climax; if the safety filter blocks a prompt, an LLM rephrases it.
- Upload: resumable YouTube upload with an encrypted session URI, `containsSyntheticMedia` set, private by default.

## Safety and cost controls

- `DRY_RUN=true`, `AUTO_PUBLISH=false`, video budget `0` by default; per-day and per-job budgets for every
  provider bucket; YouTube quota accounting.
- `APP_ENV=prod` refuses to start with mock providers or SQLite, so there is no silent fallback to fakes.
- Secrets only in Kubernetes Secrets or a git-ignored env file; secret values are redacted in logs; OAuth tokens and
  upload URIs are encrypted at rest. `.env.example` contains placeholders only.
- No scraping or downloading of other people's videos: references are analyzed from public metadata and thumbnails
  ([docs/capabilities.md](docs/capabilities.md) documents what each API can and cannot do).

## Infrastructure

Three Raspberry Pi 5 (4 GB) nodes run **k3s** with **Argo CD** (app-of-apps). CI builds arm64 images to GHCR and opens
digest-bump PRs; Argo CD syncs `main`.

```
bootstrap/      Argo CD install (Helm values) and root Application
apps/           Applications: argocd (self-managed), srvmon, shorts
deploy/shorts/  Kustomize base + k3s overlay: CronJob, PostgreSQL
charts/srvmon/  monitoring of a cloud VM: vmagent ─ssh -R─► VictoriaMetrics, vmalert ─► Alertmanager ─► Telegram,
                daily xlsx report, node-exporter on every node
images/         tunnel (autossh) and report images
shorts/         the pipeline (Python 3.13, ~6.5k lines, 145 tests): see shorts/README.md (in Russian)
docs/           cluster setup (homelab.md, in Russian), API capability notes, operations, script-lab reports
```

## Running locally

```bash
cd shorts
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tests && .venv/bin/pytest -q
# dry run: no network, no spending (fixture trends, mock models, synthetic video; needs ffmpeg)
APP_ENV=dev PROVIDER_MODE=mock TTS_PROVIDER=mock TREND_SOURCE=fixture \
TREND_FIXTURE_FILE=examples/trends_fixture.json BUDGET_VIDEO_REQUESTS_PER_DAY=10 \
NIM_VIDEO_RESOLUTION=256_9_16 .venv/bin/shorts run --dry-run
```

The full suite (FFmpeg + PostgreSQL) runs in the `test` target of `shorts/Dockerfile`.

## Results and why it stopped

- Six videos were produced and uploaded, each with a consistent character, key props on screen, timed narration and
  no stray lettering. Most candidate scripts were rejected by the critics before any video spending, which was the point.
- Views were close to zero. A new Shorts channel needs 1,000 subscribers and 10M Shorts views in 90 days to join
  the YouTube Partner Program. Shorts pay cents per thousand views, and YouTube does not monetize mass-produced,
  repetitive content. Every video costs money for video generation, so the project could not pay back within a
  reasonable horizon.
- Platform limits found along the way: the YouTube Data API gives no access to other people's media or captions,
  custom thumbnails need a verified (and, for Shorts, partner) channel, and Raspberry Pi 5 nodes need a proper
  5 V / 5 A power supply to run heavy CronJobs.

What it demonstrates: LLM multi-agent orchestration with measurable gates, cost-aware design around paid APIs,
idempotent job pipelines, vision-model QC, and a small but real GitOps Kubernetes setup.
