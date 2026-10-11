"""Vision-model quality gates on generated images and clip frames (NVIDIA llama-3.2-11b-vision).

Video/image models often invent pseudo-text (engraved codes, signs, captions). On-screen text is added in
editing, so any lettering in a generated frame is a defect.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .ff import FF
from .providers.nim_chat import StructuredOutputError

log = logging.getLogger(__name__)

LETTERING_PROMPT = (
    "Is there any text, letters, numbers, logo, signage or watermark visible anywhere in this image, including "
    "printed, engraved or written on objects? Ignore nothing. Answer ONLY with JSON: "
    '{"has_text": true|false, "where": string|null}')


class TextCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    has_text: bool
    where: str | None = None


def has_lettering(vision, image: Path) -> tuple[bool, str]:
    """(has_text, where). An unusable model answer does not block the pipeline (returns False)."""
    content = [*vision.frames_content([image]), {"type": "text", "text": LETTERING_PROMPT}]
    try:
        out, _ = vision.structured([{"role": "user", "content": content}], TextCheck, max_tokens=200)
    except StructuredOutputError as e:
        log.warning("lettering check inconclusive for %s: %s", image.name, e)
        return False, "inconclusive"
    return out.has_text, (out.where or "")[:200]


def frame_at(ff: FF, clip: Path, out: Path, *, at_s: float | None = None, last: bool = False,
             width: int | None = 448) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    seek = ["-sseof", "-0.15"] if last else ["-ss", f"{at_s or 0:.2f}"]
    vf = ["-vf", f"scale={width}:-2"] if width else []
    ff.run([*seek, "-i", str(clip), "-frames:v", "1", *vf, "-q:v", "3", str(out)])
    return out
