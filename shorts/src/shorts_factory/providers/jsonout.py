"""Extract a JSON object from model output, dropping any reasoning text.

Reasoning (``<think>…</think>``, ``reasoning_content``) is discarded and never stored.
"""

from __future__ import annotations

import json
import re

_THINK = re.compile(r"(?is)<think>.*?</think>")
_FENCE = re.compile(r"(?s)```(?:json)?\s*(\{.*?\})\s*```")


def strip_reasoning(text: str) -> str:
    text = _THINK.sub("", text)
    if "</think>" in text:  # opening tag cut off by the server
        text = text.split("</think>", 1)[1]
    return text.strip()


def extract_json_object(text: str) -> dict:
    text = strip_reasoning(text)
    m = _FENCE.search(text)
    if m:
        return json.loads(m.group(1))
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object in model output")
