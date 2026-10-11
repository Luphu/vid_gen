"""Structured logging with secret redaction.

Every log record passes through RedactFilter: registered secret values and common token
shapes (NVIDIA keys, bearer tokens, Google refresh tokens, resumable upload ids) are masked.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time

_PATTERNS = [
    re.compile(r"nvapi-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(r"1//[A-Za-z0-9_\-]{10,}"),           # Google refresh token shape
    re.compile(r"ya29\.[A-Za-z0-9_\-.]{10,}"),        # Google access token shape
    re.compile(r"(?i)(upload_id=)[^&\s\"']+"),        # resumable session id
    re.compile(r"(?i)(key=)[A-Za-z0-9_\-]{20,}"),     # API key in a query string
    re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@\s]+(@)"),
]
_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for s in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(s, "***")
    for p in _PATTERNS:
        text = p.sub(lambda m: (m.group(1) if m.lastindex else "") + "***" + (
            m.group(2) if m.lastindex and m.lastindex >= 2 else ""), text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in ("job_id", "stage", "op_key"):
            if hasattr(record, k):
                out[k] = getattr(record, k)
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    h = logging.StreamHandler(sys.stderr)
    h.addFilter(RedactFilter())
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    h.setFormatter(JsonFormatter() if json_logs else fmt)
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
