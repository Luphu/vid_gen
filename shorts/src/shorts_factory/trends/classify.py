"""Shorts format and AI-content heuristics over metadata.

Neither function produces proof. Outputs carry evidence and a source so reviewers can judge.
"""

from __future__ import annotations

import re

from ..models import AIClassification, Evidence, FormatStatus

SHORTS_MAX_S = 180.0  # YouTube Shorts can be up to 3 minutes
_SHORTS_TAG = re.compile(r"(?i)(^|\s)#shorts?\b")


def parse_iso8601_duration(v: str | None) -> float | None:
    if not v:
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", v)
    if not m:
        return None
    d, h, mi, s = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def classify_format(duration_s: float | None, title: str, description: str, tags: list[str],
                    authoritative_is_short: bool | None = None) -> tuple[FormatStatus, str]:
    if authoritative_is_short is True:
        return FormatStatus.CONFIRMED, "rights-holder manifest declares is_short=true"
    if duration_s is None:
        return FormatStatus.UNKNOWN, "duration unavailable"
    if duration_s > SHORTS_MAX_S:
        return FormatStatus.NOT_SHORT, f"duration {duration_s:.0f}s > {SHORTS_MAX_S:.0f}s"
    text = " ".join([title, description, " ".join("#" + t.lstrip("#") for t in tags)])
    if _SHORTS_TAG.search(text):
        return FormatStatus.PROBABLE, f"duration {duration_s:.0f}s <= 180s and #shorts marker in metadata"
    return FormatStatus.UNKNOWN, f"duration {duration_s:.0f}s <= 180s, no Shorts marker; API has no Shorts flag"


def classify_ai_metadata(title: str, description: str, tags: list[str], keywords: list[str],
                         contains_synthetic_media: bool | None) -> AIClassification:
    evidence: list[Evidence] = []
    if contains_synthetic_media is True:
        evidence.append(Evidence(field="status.containsSyntheticMedia", snippet="true (creator disclosure)"))
    fields = {"title": title, "description": description, "tags": " ".join(tags)}
    for fname, text in fields.items():
        low = text.lower()
        for kw in keywords:
            k = kw.lower()
            i = low.find(k)
            if i >= 0 and _word_boundary(low, i, len(k)):
                evidence.append(Evidence(field=fname, snippet=text[max(0, i - 40): i + len(k) + 40][:300]))
    if not evidence:
        return AIClassification()
    if contains_synthetic_media is True:
        likelihood, source = 0.95, "creator_disclosure"
    else:
        hits_title = sum(1 for e in evidence if e.field == "title")
        likelihood = min(0.85, 0.35 + 0.15 * len(evidence) + 0.1 * hits_title)
        source = "metadata_keywords"
    return AIClassification(ai_likelihood=round(likelihood, 2), evidence=evidence[:10],
                            classification_source=source)


def _word_boundary(text: str, i: int, n: int) -> bool:
    before = text[i - 1] if i > 0 else " "
    after = text[i + n] if i + n < len(text) else " "
    return not before.isalnum() and not after.isalnum()


def merge_ai(meta: AIClassification, vision: AIClassification | None) -> AIClassification:
    if vision is None or vision.ai_likelihood is None:
        return meta
    if meta.ai_likelihood is None:
        return vision
    return AIClassification(
        ai_likelihood=round(max(meta.ai_likelihood, vision.ai_likelihood), 2),
        evidence=(meta.evidence + vision.evidence)[:12],
        classification_source="combined",
    )
