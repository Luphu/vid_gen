"""Vision-model quality gates on generated images and clip frames (NVIDIA llama-3.2-11b-vision).

Video/image models often invent pseudo-text (engraved codes, signs, captions). On-screen text is added in
editing, so any lettering in a generated frame is a defect.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .ff import FF
from .providers.nim_chat import StructuredOutputError

log = logging.getLogger(__name__)

LETTERING_PROMPT = (
    "Look for readable text in this image: letters, words or digits printed, engraved, written or displayed "
    "anywhere (signs, screens, clothing, objects, watermark). Shapes, symbols, circles and light patterns are NOT "
    "text. If you report text, copy the exact characters you can read into text_seen. Answer ONLY with JSON: "
    '{"has_text": true|false, "text_seen": string|null, "where": string|null}')
_CHARS = re.compile(r"[0-9A-Za-z\u0400-\u04FF]")


class TextCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    has_text: bool
    text_seen: str | None = None
    where: str | None = None

    @property
    def confirmed(self) -> bool:
        """Only a transcription with real characters counts (the 11B VLM flags light patterns otherwise)."""
        return self.has_text and len(_CHARS.findall(self.text_seen or "")) >= 2


def has_lettering(vision, image: Path) -> tuple[bool, str]:
    """(has_text, where). An unusable model answer does not block the pipeline (returns False)."""
    content = [*vision.frames_content([image]), {"type": "text", "text": LETTERING_PROMPT}]
    try:
        out, _ = vision.structured([{"role": "user", "content": content}], TextCheck, max_tokens=200)
    except StructuredOutputError as e:
        log.warning("lettering check inconclusive for %s: %s", image.name, e)
        return False, "inconclusive"
    if out.has_text and not out.confirmed:
        log.info("lettering not confirmed for %s (no readable characters reported: %r)", image.name, out.where)
    return out.confirmed, f"{(out.text_seen or '')[:60]!r} {(out.where or '')[:120]}".strip()


def frame_at(ff: FF, clip: Path, out: Path, *, at_s: float | None = None, last: bool = False,
             width: int | None = 448) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    seek = ["-sseof", "-0.15"] if last else ["-ss", f"{at_s or 0:.2f}"]
    vf = ["-vf", f"scale={width}:-2"] if width else []
    ff.run([*seek, "-i", str(clip), "-frames:v", "1", *vf, "-q:v", "3", str(out)])
    return out
