"""Script lab: run analysis + the agent team on several references and write a report.

For prompt tuning only: no jobs are created, references are not marked used, and no video, image or speech
is generated (only NVIDIA text/vision calls, counted in the text/vision budgets).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .agents import TeamRejected, write_with_team
from .analysis import analyze_public_signals
from .config import ConfigError
from .models import Analysis, Candidate
from .net import ProviderError
from .pipeline import Deps, select_candidate
from .providers.nim_chat import StructuredOutputError
from .scripting import compose_keyframe_prompt, compose_video_prompt, pick_format

log = logging.getLogger(__name__)
_CRITERION = re.compile(r"^(\w+) scored (\d+)/10")


def pick_references(d: Deps, count: int, video_ids: list[str] | None = None,
                    include_used: bool = False, skip: set[str] | None = None) -> list[Candidate]:
    if video_ids:
        found = [d.store.get_candidate(v) for v in video_ids]
        missing = [v for v, c in zip(video_ids, found, strict=True) if c is None]
        if missing:
            raise ConfigError(f"unknown references (run `shorts discover` first): {missing}")
        return [Candidate.model_validate(c) for c in found if c]
    now = datetime.now(UTC).timestamp()
    pool = [Candidate.model_validate(x) for x in d.store.unused_candidates(now)]
    if include_used:
        with d.store.tx() as c:
            rows = c.execute("SELECT data FROM candidates WHERE used_by_job IS NOT NULL").fetchall()
        pool += [Candidate.model_validate(json.loads(r[0])) for r in rows]
    pool = [c for c in pool if c.video_id not in (skip or set())]
    picked: list[Candidate] = []
    while len(picked) < count:
        avoid = {c.channel_id for c in picked}  # one reference per channel: more variety
        cand = select_candidate(d.s, [c for c in pool if c not in picked], avoid)
        if cand is None:
            break
        picked.append(cand)
    return picked


def _analysis(d: Deps, cand: Candidate, cache: Path) -> Analysis:
    f = cache / f"{cand.video_id}.json"
    if f.is_file():
        return Analysis.model_validate(json.loads(f.read_text()))
    stored = d.store.analysis_for_video(cand.video_id)
    if stored:
        return Analysis.model_validate(stored)
    if d.vision is None or d.analyst is None or not cand.thumbnail_url:
        raise ConfigError("public-signals analysis needs NVIDIA_API_KEY and a thumbnail")
    titles = d.trends.channel_recent_titles(cand.channel_id) if hasattr(d.trends, "channel_recent_titles") else []
    analysis, _ = analyze_public_signals(cand, d.vision, d.analyst, titles)
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(analysis.model_dump_json(indent=1))
    return analysis


def run_lab(d: Deps, refs: list[Candidate], out: Path) -> dict:
    if d.agent is None or d.critic is None:
        raise ConfigError("script lab needs NVIDIA_API_KEY (agents and critics run on NVIDIA NIM)")
    out.mkdir(parents=True, exist_ok=True)
    cache = out.parent / "analysis"
    results = []
    for cand in refs:
        t0 = time.monotonic()
        res: dict = {"video_id": cand.video_id, "title": cand.title, "views": cand.view_count,
                     "channel": cand.channel_title}
        try:
            analysis = _analysis(d, cand, cache)
            fmt = pick_format(d.s, cand.video_id)
            res |= {"format": fmt, "analysis": analysis.model_dump(mode="json")}
            script, ref, report = write_with_team(d.s, d.agent, d.critic, cand, analysis, fmt, checker=d.analyst)
            res |= {"approved": True, "prompt": ref, "team": report, "script": script.model_dump(mode="json"),
                    "keyframe_prompt": compose_keyframe_prompt(script),
                    "video_prompts": [compose_video_prompt(script, sc) for sc in script.scenes]}
        except TeamRejected as e:
            res |= {"approved": False, "reason": str(e)[:500], "team": e.report}
        except (StructuredOutputError, ConfigError, ProviderError) as e:  # one reference must not stop the lab
            res |= {"approved": False, "reason": f"{type(e).__name__}: {e}"[:500]}
        res["minutes"] = round((time.monotonic() - t0) / 60, 1)
        (out / f"{cand.video_id}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        log.info("lab %s: %s in %.1f min", cand.video_id, "approved" if res["approved"] else "rejected",
                 res["minutes"])
        results.append(res)
    summary = summarize(results)
    (out / "report.md").write_text(render_markdown(results, summary))
    return summary | {"report": str(out / "report.md")}


def _trail(entries: list[dict]) -> str:
    return "→".join(str(e.get("score", "✗")) for e in entries) or "—"


def summarize(results: list[dict]) -> dict:
    weak: Counter = Counter()
    for r in results:
        team = r.get("team") or {}
        for entries in (team.get("phases") or {}).values():
            for e in entries:
                for m in e.get("must_fix", []):
                    if (hit := _CRITERION.match(m)):
                        weak[hit.group(1)] += 1
    done = [r for r in results if r["approved"]]
    return {"references": len(results), "approved": len(done),
            "avg_minutes": round(sum(r["minutes"] for r in results) / max(1, len(results)), 1),
            "weak_criteria": dict(weak.most_common(8))}


def render_markdown(results: list[dict], summary: dict) -> str:
    lines = [f"# Script lab — {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", "",
             f"Approved {summary['approved']} of {summary['references']}, average {summary['avg_minutes']} min "
             "per reference.", "", "Criteria scored below the minimum (count over all rounds): "
             + (", ".join(f"{k} ×{v}" for k, v in summary["weak_criteria"].items()) or "none"), ""]
    for r in results:
        team = r.get("team") or {}
        phases = team.get("phases") or {}
        lines += [f"## {'✅' if r['approved'] else '❌'} {r['title']}",
                  f"`{r['video_id']}` · {r.get('views') or '?'} views · format: {r.get('format', '?')} · "
                  f"{r['minutes']} min", "",
                  "Scores: " + " · ".join(f"{role} {_trail(phases.get(role, []))}" for role in
                                          ("writer", "director", "editor"))
                  + (" · final " + " / ".join(str(v) for v in team["final"][-1]["scores"].values())
                     if team.get("final") else ""), ""]
        if team.get("escalations"):
            lines += ["Escalated to the writer: the director could not film the story.", ""]
        sc = r.get("script")
        if sc:
            lines += [f"**{sc['title']}** — {sc['hook']}", "", f"Trend link: {sc['trend_link']}", "",
                      f"Hero: {sc['subject']}", "", f"Location: {sc['setting']}", "",
                      "| # | s | narration | on screen | video prompt |", "|---|---|---|---|---|"]
            for s, vp in zip(sc["scenes"], r["video_prompts"], strict=True):
                lines.append(f"| {s['index']} | {s['duration_s']} | {s['narration']} | {s['on_screen_text']} | "
                             f"{vp[:220].replace('|', '/')}… |")
            lines.append("")
        else:
            lines += [f"Rejected: {r.get('reason', '')}", ""]
        for role, entries in phases.items():
            last = entries[-1] if entries else {}
            notes = last.get("must_fix") or last.get("checks") or []
            if notes:
                lines.append(f"- last {role} notes: " + " | ".join(str(n)[:200] for n in notes[:4]))
            for s in (last.get("suggestions") or [])[:2]:
                lines.append(f"- {role} suggestion: {s[:200]}")
        lines.append("")
    return "\n".join(lines)
