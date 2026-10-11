"""Text-to-speech adapters.

The video model produces no audio. Real NVIDIA TTS (Riva/Magpie over gRPC) is not implemented
in this MVP; ``none`` produces a video without narration and the pipeline records that fact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..ff import FF


class TTS(Protocol):
    name: str

    def synthesize(self, text: str, language: str, out_wav: Path, max_duration_s: float) -> Path | None: ...


class NoTTS:
    name = "none"

    def synthesize(self, text: str, language: str, out_wav: Path, max_duration_s: float) -> Path | None:
        return None


class MockTTS:
    """Low tone of plausible speech length; for pipeline tests only."""

    name = "mock"

    def __init__(self, ff: FF):
        self.ff = ff

    def synthesize(self, text: str, language: str, out_wav: Path, max_duration_s: float) -> Path | None:
        if not text.strip():
            return None
        dur = min(max_duration_s, max(1.0, len(text.split()) / 2.5))
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        self.ff.run(["-f", "lavfi", "-i", f"sine=frequency=220:duration={dur:.2f}:sample_rate=48000",
                     "-af", "volume=0.2", "-ac", "1", str(out_wav)])
        return out_wav


def make_tts(name: str, ff: FF) -> TTS:
    return MockTTS(ff) if name == "mock" else NoTTS()
