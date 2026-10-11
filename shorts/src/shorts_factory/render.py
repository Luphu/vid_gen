"""Assembly: per-scene normalisation (one H.264 encode), concat, narration/music mux.

Scenes are scaled to the output frame. Native 9:16 sources are only scaled; other aspect
ratios are letterboxed by default (keeps every object), or cropped around the scene's
``important_region`` with FIT_MODE=crop.
"""

from __future__ import annotations

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


def _vf(s: Settings, src_w: int, src_h: int, scene: Scene, text_file: Path | None, fit: str) -> str:
    W, H = s.out_width, s.out_height
    same_aspect = src_w and src_h and abs(src_w / src_h - W / H) < 0.02
    if same_aspect or fit == "pad":
        geo = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
               f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
    else:
        y = {"top": "0", "bottom": "ih-oh", "center": "(ih-oh)/2"}[scene.important_region]
        geo = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}:(iw-ow)/2:{y}"
    chain = [geo, "setsar=1", f"fps={s.out_fps}", f"tpad=stop_mode=clone:stop_duration={scene.duration_s:.3f}"]
    if text_file is not None:
        size = max(36, min(64, int(W * 1.5 / max(12, max(len(x) for x in text_file.read_text().splitlines())))))
        chain.append(
            f"drawtext=fontfile={filter_path(Path(s.font_file))}:textfile={filter_path(text_file)}:expansion=none"
            f":fontsize={size}:fontcolor=white:borderw=4:bordercolor=black@0.8:line_spacing=8"
            f":x=(w-text_w)/2:y=h*0.72"
        )
    return ",".join(chain)


def normalize_scene(s: Settings, ff: FF, src: Path, scene: Scene, dst: Path, work: Path) -> Path:
    v = stream(ff.probe(src), "video") or {}
    text_file = None
    if s.burn_subtitles and scene.on_screen_text.strip():
        text_file = work / f"text_{scene.index}.txt"
        text_file.write_text("\n".join(textwrap.wrap(scene.on_screen_text.strip(), 22)[:3]), encoding="utf-8")
    vf = _vf(s, int(v.get("width") or 0), int(v.get("height") or 0), scene, text_file, s.fit_mode)
    ff.run(["-threads", str(ff.threads), "-i", str(src), "-an", "-vf", vf, "-t", f"{scene.duration_s:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-r", str(s.out_fps), "-g", str(s.out_fps * 2),
            "-x264-params", f"threads={ff.threads}", str(dst)])
    return dst


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


def render(s: Settings, ff: FF, script: Script, inp: RenderInputs, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    norm = [normalize_scene(s, ff, src, sc, work / f"norm_{sc.index:02d}.mp4", work) for sc, src in inp.scenes]
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
    return out


def expected_audio(narration: list[Path | None], music: Path | None) -> bool:
    return any(narration) or music is not None


def probe_duration(ff: FF, p: Path) -> float:
    return duration_of(ff.probe(p))
