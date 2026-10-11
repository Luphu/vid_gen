"""Quality checks with concrete failure reasons. A failed check blocks publication."""

from __future__ import annotations

from pathlib import Path

from .config import Settings
from .ff import FF, FFmpegError, duration_of, stream
from .models import QCCheck, QCReport


def check_scene(s: Settings, ff: FF, path: Path, expected_s: float) -> QCReport:
    checks: list[QCCheck] = []
    if not path.is_file() or path.stat().st_size < 1024:
        return QCReport(ok=False, checks=[QCCheck(name="exists", ok=False, detail="missing or < 1 KiB")])
    try:
        info = ff.probe(path)
    except FFmpegError as e:
        return QCReport(ok=False, checks=[QCCheck(name="probe", ok=False, detail=str(e)[:300])])
    v = stream(info, "video")
    checks.append(QCCheck(name="video_stream", ok=v is not None, detail="" if v else "no video stream"))
    if v is None:
        return QCReport(ok=False, checks=checks)
    dur = duration_of(info)
    checks.append(QCCheck(name="duration", ok=dur >= min(expected_s * 0.8, expected_s - 0.5),
                          detail=f"{dur:.2f}s vs expected {expected_s:.2f}s"))
    err = ff.decode_errors(path)
    checks.append(QCCheck(name="decode", ok=not err, detail=err[:300]))
    if not err and dur > 0:
        br = ff.black_ratio(path, dur)
        checks.append(QCCheck(name="black_frames", ok=br <= s.black_scene_max_ratio,
                              detail=f"black {br:.0%} (max {s.black_scene_max_ratio:.0%})"))
    return QCReport(ok=all(c.ok for c in checks), checks=checks, duration_s=dur,
                    width=v.get("width"), height=v.get("height"))


def check_final(s: Settings, ff: FF, path: Path, expect_audio: bool) -> QCReport:
    checks: list[QCCheck] = []
    size = path.stat().st_size if path.is_file() else 0
    checks.append(QCCheck(name="size", ok=0 < size <= s.max_output_bytes,
                          detail=f"{size} bytes (max {s.max_output_bytes})"))
    try:
        info = ff.probe(path)
    except FFmpegError as e:
        checks.append(QCCheck(name="probe", ok=False, detail=str(e)[:300]))
        return QCReport(ok=False, checks=checks)
    v, a = stream(info, "video"), stream(info, "audio")
    dur = duration_of(info)
    checks.append(QCCheck(name="duration", ok=s.target_min_s - 0.5 <= dur <= s.target_max_s + 0.5,
                          detail=f"{dur:.2f}s, allowed [{s.target_min_s}, {s.target_max_s}]"))
    if v is None:
        checks.append(QCCheck(name="video_stream", ok=False, detail="no video stream"))
        return QCReport(ok=False, checks=checks, duration_s=dur)
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    checks.append(QCCheck(name="resolution", ok=(w, h) == (s.out_width, s.out_height),
                          detail=f"{w}x{h}, expected {s.out_width}x{s.out_height}"))
    checks.append(QCCheck(name="aspect_9_16", ok=h > 0 and abs(w / h - 9 / 16) < 0.01, detail=f"{w}:{h}"))
    checks.append(QCCheck(name="video_codec", ok=v.get("codec_name") == "h264", detail=str(v.get("codec_name"))))
    checks.append(QCCheck(name="pixel_format", ok=v.get("pix_fmt") == "yuv420p", detail=str(v.get("pix_fmt"))))
    if expect_audio:
        checks.append(QCCheck(name="audio", ok=a is not None and a.get("codec_name") == "aac",
                              detail=str(a.get("codec_name")) if a else "missing audio stream"))
    else:
        checks.append(QCCheck(name="audio", ok=a is None, detail="no narration/music configured; video is silent"
                              if a is None else "unexpected audio stream"))
    err = ff.decode_errors(path)
    checks.append(QCCheck(name="decode", ok=not err, detail=err[:300]))
    if not err and dur > 0:
        br = ff.black_ratio(path, dur)
        checks.append(QCCheck(name="black_frames", ok=br <= 0.5, detail=f"black {br:.0%} of video"))
    return QCReport(ok=all(c.ok for c in checks), checks=checks, duration_s=dur, width=w, height=h,
                    has_audio=a is not None)
