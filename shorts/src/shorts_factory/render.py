"""Assembly: per-scene normalisation (one H.264 encode), concat, narration/music mux.

Scenes are scaled to the output frame. Native 9:16 sources are only scaled; other aspect
ratios are letterboxed by default (keeps every object), or cropped around the scene's
``important_region`` with FIT_MODE=crop.
"""

from __future__ import annotations

import math
import textwrap
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .ff import FF, duration_of, filter_path, stream
from .models import Scene, Script


@dataclass
class RenderInputs:
    scenes: list[tuple[Scene, Path]]
    narration: list[Path | None]
    music: Path | None


@dataclass
class Shot:
    start: float   # offset in the generated clip
    length: float  # seconds on screen
    zoom: float    # 1.0 = full frame, >1 = punch-in


def plan_shots(clip_s: float, scene_s: float, first_scene: bool, max_shot: float, first_shot: float,
               punch: float) -> list[Shot]:
    """Split one generated clip into fast cuts taken from different moments, alternating framing."""
    lengths: list[float] = []
    rest = scene_s
    if first_scene and scene_s > first_shot + 0.5:
        lengths.append(first_shot)
        rest -= first_shot
    n = max(1, math.ceil(rest / max_shot - 1e-9))
    lengths += [rest / n] * n
    zooms = [1.0, punch, 1.0 + (punch - 1.0) / 2]
    shots = []
    for i, ln in enumerate(lengths):
        span = max(0.0, clip_s - ln)
        start = span * i / (len(lengths) - 1) if len(lengths) > 1 else 0.0
        shots.append(Shot(start=round(start, 3), length=round(ln, 3), zoom=zooms[i % len(zooms)]))
    return shots


def _geo(s: Settings, src_w: int, src_h: int, scene: Scene, fit: str) -> str:
    W, H = s.out_width, s.out_height
    same_aspect = src_w and src_h and abs(src_w / src_h - W / H) < 0.02
    if same_aspect or fit == "pad":
        return (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
    y = {"top": "0", "bottom": "ih-oh", "center": "(ih-oh)/2"}[scene.important_region]
    return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}:(iw-ow)/2:{y}"


def _zoom(s: Settings, scene: Scene, z: float) -> str:
    if z <= 1.001:
        return ""
    W, H = s.out_width, s.out_height
    cw, ch = int(W / z) // 2 * 2, int(H / z) // 2 * 2
    y = {"top": "0", "bottom": f"{H - ch}", "center": f"{(H - ch) // 2}"}[scene.important_region]
    return f",crop={cw}:{ch}:{(W - cw) // 2}:{y},scale={W}:{H}"


def _text_filter(s: Settings, text_file: Path | None) -> str:
    """drawtext reads the text from a file: untrusted script text never enters the filter syntax."""
    if text_file is None:
        return ""
    longest = max(len(x) for x in text_file.read_text().splitlines())
    size = max(36, min(64, int(s.out_width * 1.5 / max(12, longest))))
    return (f",drawtext=fontfile={filter_path(Path(s.font_file))}:textfile={filter_path(text_file)}"
            f":expansion=none:fontsize={size}:fontcolor=white:borderw=4:bordercolor=black@0.8:line_spacing=8"
            f":x=(w-text_w)/2:y=h*0.72")


def normalize_scene(s: Settings, ff: FF, src: Path, scene: Scene, dst: Path, work: Path,
                    first_scene: bool = False) -> list[Shot]:
    """One H.264 encode per scene: fast cuts from the clip + framing + on-screen text."""
    info = ff.probe(src)
    v = stream(info, "video") or {}
    text_file = None
    if s.burn_subtitles and scene.on_screen_text.strip():
        text_file = work / f"text_{scene.index}.txt"
        text_file.write_text("\n".join(textwrap.wrap(scene.on_screen_text.strip(), 22)[:3]), encoding="utf-8")
    shots = plan_shots(duration_of(info) or scene.duration_s, scene.duration_s, first_scene, s.edit_max_shot_s,
                       s.edit_first_shot_s, s.edit_punch_in)
    geo = _geo(s, int(v.get("width") or 0), int(v.get("height") or 0), scene, s.fit_mode)
    parts = [f"[0:v]split={len(shots)}" + "".join(f"[s{i}]" for i in range(len(shots)))] if len(shots) > 1 \
        else ["[0:v]null[s0]"]
    for i, sh in enumerate(shots):
        parts.append(f"[s{i}]trim=start={sh.start:.3f}:duration={sh.length:.3f},setpts=PTS-STARTPTS,{geo}"
                     f"{_zoom(s, scene, sh.zoom)},setsar=1,fps={s.out_fps}[c{i}]")
    parts.append("".join(f"[c{i}]" for i in range(len(shots))) + f"concat=n={len(shots)}:v=1:a=0[cat]")
    parts.append(f"[cat]tpad=stop_mode=clone:stop_duration={scene.duration_s:.3f}"
                 f"{_text_filter(s, text_file)}[out]")
    ff.run(["-threads", str(ff.threads), "-i", str(src), "-an", "-filter_complex", ";".join(parts),
            "-map", "[out]", "-t", f"{scene.duration_s:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-r", str(s.out_fps), "-g", str(s.out_fps * 2),
            "-x264-params", f"threads={ff.threads}", str(dst)])
    return shots


def build_narration(ff: FF, parts: list[tuple[Path | None, float]], dst: Path) -> Path | None:
    """Concatenate per-scene narration, each padded/trimmed to its scene duration."""
    if not any(p for p, _ in parts):
        return None
    args: list[str] = []
    filters = []
    for i, (p, d) in enumerate(parts):
        if p is None:
            args += ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=mono"]
        else:
            args += ["-i", str(p)]
        filters.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=mono,apad,atrim=0:{d:.3f},"
                       f"asetpts=PTS-STARTPTS[a{i}]")
    graph = ";".join(filters) + ";" + "".join(f"[a{i}]" for i in range(len(parts))) + \
        f"concat=n={len(parts)}:v=0:a=1[out]"
    ff.run([*args, "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", str(dst)])
    return dst


def render(s: Settings, ff: FF, script: Script, inp: RenderInputs, work: Path) -> tuple[Path, list[float],
                                                                                       float | None]:
    """Returns the final file, the on-screen length of every shot (for the pacing check) and the longest
    pause of the voice-over (None without narration)."""
    work.mkdir(parents=True, exist_ok=True)
    norm, shot_lengths = [], []
    for k, (sc, src) in enumerate(inp.scenes):
        dst = work / f"norm_{sc.index:02d}.mp4"
        shot_lengths += [sh.length for sh in normalize_scene(s, ff, src, sc, dst, work, first_scene=k == 0)]
        norm.append(dst)
    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in norm))
    video_only = work / "video_only.mp4"
    ff.run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(video_only)])
    total = sum(sc.duration_s for sc, _ in inp.scenes)

    narration = build_narration(ff, [(p, sc.duration_s) for p, (sc, _) in zip(inp.narration, inp.scenes,
                                                                                strict=True)],
                                work / "narration.wav")
    out = work / "final.mp4"
    args = ["-i", str(video_only)]
    audio_inputs = []
    if narration:
        args += ["-i", str(narration)]
        audio_inputs.append(f"[{len(audio_inputs) + 1}:a]aresample=48000,volume=1.0[n]")
    if inp.music:
        args += ["-stream_loop", "-1", "-i", str(inp.music)]
        audio_inputs.append(f"[{len(audio_inputs) + 1}:a]aresample=48000,volume=0.18[m]")
    if audio_inputs:
        labels = "".join(x[x.rfind("["):] for x in audio_inputs)
        mix = (f"{labels}amix=inputs={len(audio_inputs)}:duration=longest:normalize=0" if len(audio_inputs) > 1
               else f"{labels}anull")
        graph = ";".join(audio_inputs) + f";{mix},apad,atrim=0:{total:.3f}[a]"
        args += ["-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    else:
        args += ["-map", "0:v", "-c:v", "copy", "-an"]
    ff.run([*args, "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)])
    return out, shot_lengths, (ff.longest_silence(narration) if narration else None)


def expected_audio(narration: list[Path | None], music: Path | None) -> bool:
    return any(narration) or music is not None


def probe_duration(ff: FF, p: Path) -> float:
    return duration_of(ff.probe(p))
