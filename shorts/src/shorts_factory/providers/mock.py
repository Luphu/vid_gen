"""Mock providers for tests and local dry runs. Forbidden with APP_ENV=prod (see config)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from ..ff import FF
from ..net import OutcomeUnknown, ProviderError
from .nim_video import VideoRequest


class MockChat:
    """Returns canned, schema-valid JSON. ``fail_first`` simulates invalid output once."""

    def __init__(self, responses: dict[str, dict], fail_first: bool = False):
        self.responses, self.fail_first, self.calls = responses, fail_first, 0

    def structured(self, messages: list[dict], model_cls: type[BaseModel], *, max_tokens: int,
                   context: dict | None = None):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            from .nim_chat import StructuredOutputError

            raise StructuredOutputError("mock invalid output")
        data = self.responses[model_cls.__name__]
        return model_cls.model_validate(json.loads(json.dumps(data)), context=context), f"mock-{self.calls}"

    def chat(self, messages: list[dict], *, max_tokens: int, temperature: float = 0.4,
             json_mode: bool = False) -> tuple[str, str | None]:
        self.calls += 1
        return "A grey 3D-cartoon kitten with big eyes holds a brush in a sunny backyard.", f"mock-{self.calls}"

    def frames_content(self, frames: list[Path]) -> list[dict]:
        return [{"type": "text", "text": f"[{len(frames)} frames]"}]

    def image_urls_content(self, urls) -> list[dict]:
        return [{"type": "text", "text": f"[{len(urls)} images]"}]


class MockVideo:
    """Generates a synthetic test clip with ffmpeg (lavfi testsrc2), no network.

    ``script`` lets tests inject behaviours per call: "ok", "unknown", "corrupt", "error".
    """

    name = "mock_video"

    def __init__(self, ff: FF, width: int, height: int, script: list[str] | None = None, budget=None):
        self.ff, self.w, self.h, self.budget = ff, width, height, budget
        self.script = list(script or [])
        self.calls = 0
        self.pending: dict[str, int] = {}

    def generate(self, req: VideoRequest, out: Path, on_submitted=None) -> str | None:
        if self.budget is not None:  # same accounting as the real adapter
            self.budget.reserve("video")
        self.calls += 1
        rid = f"mock-req-{self.calls}"
        action = self.script.pop(0) if self.script else "ok"
        if action == "unknown":
            if on_submitted:
                on_submitted(rid)
            self.pending[rid] = req.num_frames
            raise OutcomeUnknown("mock timeout after send", request_id=rid)
        if action == "error":
            raise ProviderError("mock 422 guardrail rejection", status=422)
        out.parent.mkdir(parents=True, exist_ok=True)
        if action == "corrupt":
            out.write_bytes(b"\x00" * 4096)
            return rid
        dur = req.num_frames / req.fps
        self.ff.run(["-f", "lavfi", "-i", f"testsrc2=size={self.w}x{self.h}:rate={req.fps}:duration={dur:.3f}",
                     "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-threads",
                     str(self.ff.threads), str(out)])
        return rid

    def poll(self, request_id: str, out: Path) -> bool:
        if request_id not in self.pending:
            return False
        frames = self.pending.pop(request_id)
        self.script.insert(0, "ok")
        budget, self.budget = self.budget, None  # recovery is not a new billable request
        try:
            self.generate(VideoRequest("recovered", frames, 24, "720_9_16"), out)
        finally:
            self.budget = budget
        self.calls -= 1
        return True


def default_mock_responses(language: str = "ru") -> dict[str, dict]:
    """Schema-valid canned outputs for dry runs with PROVIDER_MODE=mock."""
    scene = ("The robot unfolds itself out of a blank notebook page on a wooden desk, its paper limbs "
             "creasing open one by one, slow dolly-in from a low angle")
    walk = ("The robot walks across the desk between pencils and stops beside a broken pencil, "
            "camera tracking at desk level")
    fix = "The robot presses the two halves of the pencil together and the crack closes, close-up, slow push-in"
    return {
        "MetaOut": {"topic": "Тест: короткие ИИ-ролики о необычных предметах",
                    "hook": "по названию: обещание неожиданного превращения", "confidence": "low",
                    "audience_interest_hypothesis": "гипотеза: интерес к визуальным трансформациям"},
        "PublicOut": {"topic": "A cartoon kitten learns colours by painting a fence",
                      "hook": "The kitten spills a bucket of paint in the first shot (from thumbnail)",
                      "plot_beats": [
                          {"order": 0, "what_happens": "Kitten finds paint buckets", "evidence": ["thumbnail"],
                           "confidence": "medium"},
                          {"order": 1, "what_happens": "Kitten paints the fence in different colours",
                           "evidence": ["title", "description"], "confidence": "high"}],
                      "characters": ["small grey 3D-cartoon kitten with big eyes"], "setting": "sunny backyard",
                      "visual_description": "Bright 3D cartoon, kitten in centre holding a brush, rainbow fence",
                      "on_screen_text": [], "audience_interest_hypothesis": "colour learning + slapstick",
                      "unknowns": ["ending", "music"], "confidence": "medium"},
        "MediaOut": {"topic": "Тест: превращение предметов", "hook": "первый кадр с крупным объектом",
                     "structure": ["завязка", "превращение", "финал"], "pacing": "быстрая смена сцен",
                     "scenes": [{"t_start": 0, "description": "крупный план предмета", "composition": "центр",
                                 "on_screen_text": None}],
                     "on_screen_text": [], "audience_interest_hypothesis": "гипотеза: эффект неожиданности",
                     "visual_ai_cues": ["слишком гладкие текстуры"], "visual_ai_likelihood": 0.7,
                     "confidence": "medium"},
        "Script": {
            "concept": "Бумажный робот складывается из тетрадного листа и чинит сломанный карандаш",
            "target_audience": "взрослые зрители, интересующиеся ИИ", "language": language,
            "hook": "Что если тетрадь оживёт?",
            "subject": "a palm-sized origami robot folded from plain white paper, square head, two thin arms, "
                       "soft warm glow along its folds",
            "visual_style": "photorealistic macro footage, warm morning window light, shallow depth of field, "
                            "muted natural colours",
            "scenes": [
                {"index": 0, "duration_s": 6, "generation_prompt": scene, "negative_prompt": None,
                 "narration": "Обычная тетрадь. Почти.", "on_screen_text": "Тетрадь оживает",
                 "important_region": "center"},
                {"index": 1, "duration_s": 6, "generation_prompt": walk,
                 "negative_prompt": None, "narration": "Робот находит сломанный карандаш.",
                 "on_screen_text": "Находка", "important_region": "center"},
                {"index": 2, "duration_s": 6, "generation_prompt": fix,
                 "negative_prompt": None, "narration": "И чинит его за секунду.",
                 "on_screen_text": "Готово!", "important_region": "center"},
            ],
            "narration": "Обычная тетрадь. Почти. Робот находит сломанный карандаш. И чинит его за секунду.",
            "title": "Бумажный робот чинит карандаш",
            "description": "Короткая история о бумажном роботе. Видеоряд сгенерирован ИИ.",
            "tags": ["ai", "shorts", "paper robot"],
            "disclosure": {"contains_synthetic_media": True, "made_for_kids": False,
                           "disclosure_text": "Видеоряд сгенерирован ИИ"},
        },
    }
