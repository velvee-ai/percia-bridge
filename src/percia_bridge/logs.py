"""Structured logging with central secret redaction. Every line goes through redact()."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

_secrets: set[str] = set()

# Order matters: specific patterns first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    (re.compile(r"(?i)(authorization[:=]\s*)[^\r\n]+"), r"\1***"),
    (re.compile(r"\bpc_[A-Za-z0-9_-]{8,}"), "pc_***"),
    (re.compile(r"(?i)\b(rtsps?|https?|rtmp)://[^/\s:@]+:[^/\s@]*@"), r"\1://***:***@"),
    (re.compile(r"(?i)\b(rtsps?|https?)://[^/\s:@]+@"), r"\1://***@"),
    (re.compile(r"(?i)((?:password|passwd|token|api[_-]?key|secret)[\"']?\s*[:=]\s*[\"']?)[^\s\"',&}]+"), r"\1***"),
    (re.compile(r"/cam/[A-Za-z0-9_-]{24,}"), "/cam/***"),
]


def register_secret(value: str | None) -> None:
    """Remember a literal secret (API key, camera token, password) so it is masked wherever it shows up."""
    if value and len(value) >= 4:
        _secrets.add(value)


def clear_secrets() -> None:
    _secrets.clear()


def redact(text: str) -> str:
    for s in sorted(_secrets, key=len, reverse=True):
        text = text.replace(s, "***")
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def strip_credentials(url: str) -> str:
    """rtsp://user:pass@host/path -> rtsp://host/path (for printing)."""
    return re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]*@", r"\1", url)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        data.update(getattr(record, "fields", {}))
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(data, default=str, separators=(",", ":")))


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        line = f"{time.strftime('%H:%M:%S', time.localtime(record.created))} {record.levelname:<7} {record.getMessage()}"
        if extra:
            line += f" ({extra})"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return redact(line)


log = logging.getLogger("percia_bridge")


def setup_logging(level: str = "info", json_lines: bool | None = None, stream=None) -> None:
    stream = stream or sys.stderr
    if json_lines is None:
        json_lines = not getattr(stream, "isatty", lambda: False)()
    h = logging.StreamHandler(stream)
    h.setFormatter(_JsonFormatter() if json_lines else _TextFormatter())
    log.handlers[:] = [h]
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.propagate = False


def event(level: int, name: str, **fields: Any) -> None:
    """Structured event: a stable name plus key/value fields."""
    log.log(level, name, extra={"fields": fields})
