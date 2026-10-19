"""Structured JSON logging.

One line of JSON per event, with a correlation id threaded through every record
belonging to the same inbound request. Secrets are never passed to the logger:
the webhook token lives in the URL path and headers, and neither is logged.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Iterable

_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


REDACTED = "[REDACTED]"


class SecretRedactingFilter(logging.Filter):
    """Removes known secrets from log records, wherever they appear.

    The webhook token travels in the URL path, because TradingView cannot send
    custom headers. That makes it exactly the kind of value a library logs by
    accident while logging a request line. Nothing in this project logs paths,
    but a filter costs nothing and does not depend on every present and future
    dependency behaving. It is defence in depth, not the primary control: the
    primary control is that access logging is off and paths are never logged.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Short strings would redact ordinary text; a real token is long.
        self._secrets = tuple(sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True))

    def _scrub(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        return value

    def _scrub_any(self, value: object) -> object:
        """Scrub a log argument of any type.

        Not only strings: httpx logs its request line with an `httpx.URL`
        object as the argument, and the token is inside that object's `str()`.
        A string-only filter misses it, which is how this was found. The type is
        changed only when a secret is actually present, so ordinary arguments
        keep formatting as they would have.
        """
        if isinstance(value, str):
            return self._scrub(value)
        try:
            text = str(value)
        except Exception:  # pragma: no cover - a __str__ that raises
            return value
        scrubbed = self._scrub(text)
        return scrubbed if scrubbed != text else value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: self._scrub_any(value) for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(self._scrub_any(value) for value in record.args)
            else:
                record.args = self._scrub_any(record.args)
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED:
                record.__dict__[key] = self._scrub_any(value)
        return True


def install_secret_filter(handler: logging.Handler, secrets: Iterable[str]) -> None:
    """Attach redaction to a handler, replacing any redactor already on it.

    Handler-level, not logger-level, and that distinction is load-bearing: a
    filter on a logger is applied only to records logged *on that logger*, not
    to records propagated up from its children. A redactor on the root logger
    would therefore miss everything a dependency logs under its own name, which
    is precisely the case worth defending against.
    """
    handler.filters = [
        existing
        for existing in handler.filters
        if not isinstance(existing, SecretRedactingFilter)
    ]
    handler.addFilter(SecretRedactingFilter(secrets))


def configure_logging(
    level: str = "INFO",
    secrets: Iterable[str] = (),
    stream=None,
) -> logging.Handler:
    """Install the JSON handler and secret redaction. Returns the handler."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Every handler on the root, not only the one installed here, so a file or
    # syslog handler added elsewhere is covered too.
    for existing in root.handlers:
        install_secret_filter(existing, secrets)
    return handler
