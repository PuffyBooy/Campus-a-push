"""Structured logging with secret redaction.

Every log record passes through :class:`RedactingFilter`, which removes registered
secret values (passwords, API keys, storage state) and common credential patterns
(cookies, bearer tokens, query-string keys). Nothing in the bot should log secrets,
but the filter is the safety net.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from datetime import UTC, datetime

_SECRETS: set[str] = set()
_LOCK = threading.Lock()

_PATTERNS = [
    # Cookies, including the TYPO3 backend session cookie be_typo_user
    (re.compile(r"(?i)(be_typo_user|fe_typo_user|PHPSESSID|cookie|set-cookie)(\s*[=:]\s*)([^;\s,\"']+)"), r"\1\2***"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,\"']+"), r"\1***"),
    (re.compile(r"(?i)([?&](?:key|api_key|apikey|token|password)=)[^&\s\"']+"), r"\1***"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "sk-***"),
]


def register_secret(value: str | None) -> None:
    """Register a value that must never appear in logs. Very short values are ignored
    because redacting them would mangle ordinary text."""
    if value and len(value) >= 4:
        with _LOCK:
            _SECRETS.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    with _LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, "***")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if hasattr(record, "context") and isinstance(record.context, dict):
            record.context = json.loads(redact(json.dumps(record.context, default=str)))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if isinstance(getattr(record, "context", None), dict):
            payload.update(record.context)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = getattr(record, "context", None)
        if isinstance(ctx, dict) and ctx:
            base += " " + " ".join(f"{k}={v}" for k, v in ctx.items())
        # Exception text is formatted here, after the filter ran, so redact again.
        return redact(base)


def setup_logging(level: str | None = None, json_logs: bool | None = None) -> None:
    level = level or os.environ.get("BOT_LOG_LEVEL", "INFO")
    if json_logs is None:
        json_logs = os.environ.get("BOT_LOG_FORMAT", "text").lower() == "json"
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        JsonFormatter() if json_logs else TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("urllib3", "httpx", "openai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"typo3_publisher.{name}")


def log_event(logger: logging.Logger, level: int, msg: str, **context) -> None:
    logger.log(level, msg, extra={"context": context})
