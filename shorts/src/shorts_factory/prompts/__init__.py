"""Versioned prompt templates (``$var`` placeholders, string.Template semantics)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from string import Template


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    text: str

    @property
    def ref(self) -> str:
        return f"{self.id}@v{self.version}"

    def render(self, **values) -> str:
        return Template(self.text).substitute(**{k: str(v) for k, v in values.items()})


def load(name: str) -> Prompt:
    raw = resources.files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"---\nid: (\w+)\nversion: (\d+)\n---\n", raw)
    if not m:
        raise ValueError(f"prompt {name} lacks a version header")
    return Prompt(m.group(1), int(m.group(2)), raw[m.end():])


def untrusted(obj: object, limit: int = 6000) -> str:
    """Serialise untrusted data; JSON-escaping keeps it from closing the data block."""
    s = json.dumps(obj, ensure_ascii=False, default=str)
    s = s.replace("</untrusted_data>", "<\\/untrusted_data>")
    return s[:limit]
