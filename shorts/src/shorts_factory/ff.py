"""FFmpeg/ffprobe wrappers.

Always argv lists (no shell), bounded threads and timeouts, files on disk only.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


@dataclass
class FF:
    threads: int = 2
    timeout_s: int = 900
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    def available(self) -> bool:
        return bool(shutil.which(self.ffmpeg) and shutil.which(self.ffprobe))

    def run(self, args: list[str], timeout_s: int | None = None) -> str:
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-y", *args]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s or self.timeout_s,
                               check=False)
        except subprocess.TimeoutExpired as e:
            raise FFmpegError(f"ffmpeg timed out after {e.timeout}s") from e
        if p.returncode != 0:
            raise FFmpegError(f"ffmpeg exit {p.returncode}: {p.stderr[-1500:]}")
        return p.stderr

    def query(self, args: list[str]) -> str:
        """Run ffmpeg for information (e.g. -encoders); returns stdout."""
        p = subprocess.run([self.ffmpeg, "-hide_banner", *args], capture_output=True, text=True, timeout=30,
                           check=False)
        return p.stdout

    def probe(self, path: Path) -> dict:
        cmd = [self.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        except subprocess.TimeoutExpired as e:
            raise FFmpegError("ffprobe timed out") from e
        if p.returncode != 0:
            raise FFmpegError(f"ffprobe failed: {p.stderr[-500:]}")
        return json.loads(p.stdout)

    def decode_errors(self, path: Path) -> str:
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-threads", str(self.threads),
               "-i", str(path), "-f", "null", "-"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s, check=False)
        return (p.stderr.strip() or ("" if p.returncode == 0 else f"exit {p.returncode}"))[-1000:]

    def black_ratio(self, path: Path, duration: float) -> float:
        err = self.run(["-threads", str(self.threads), "-i", str(path), "-an",
                        "-vf", "blackdetect=d=0.1:pix_th=0.10", "-f", "null", "-"])
        total = sum(float(m) for m in re.findall(r"black_duration:\s*([\d.]+)", err))
        return min(1.0, total / duration) if duration > 0 else 1.0

    def scene_changes(self, path: Path, threshold: float = 0.3) -> list[float]:
        err = self.run(["-threads", str(self.threads), "-i", str(path), "-an",
                        "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
        return [float(x) for x in re.findall(r"pts_time:([\d.]+)", err)]

    def extract_frames(self, path: Path, out_dir: Path, n: int, size: int, duration: float) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        fps = max(n / max(duration, 0.1), 0.05)
        self.run(["-threads", str(self.threads), "-i", str(path), "-an",
                  "-vf", f"fps={fps:.4f},scale={size}:{size}:force_original_aspect_ratio=decrease",
                  "-frames:v", str(n), "-q:v", "4", str(out_dir / "f%03d.jpg")])
        return sorted(out_dir.glob("f*.jpg"))

    def mean_volume_db(self, path: Path) -> float | None:
        err = self.run(["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"])
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
        return float(m.group(1)) if m else None


def stream(info: dict, kind: str) -> dict | None:
    return next((s for s in info.get("streams", []) if s.get("codec_type") == kind), None)


def duration_of(info: dict) -> float:
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        v = stream(info, "video") or {}
        return float(v.get("duration") or 0)


def filter_path(p: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument (not a shell)."""
    s = str(p)
    for ch in ("\\", ":", "'", ",", "[", "]", ";"):
        s = s.replace(ch, "\\" + ch)
    return s
