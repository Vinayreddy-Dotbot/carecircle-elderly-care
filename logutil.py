"""Structured logging and error reporting.

Logs are JSON lines on stdout (most hosts collect stdout). Only IDs and counts are logged: never access codes,
sign-in codes, tokens, email addresses, names or medical details.
If SENTRY_DSN is set and sentry-sdk is installed, errors are also sent to Sentry with PII collection off.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sys
import time
import traceback
import uuid
from typing import Any

import config

_STD = set(vars(logging.LogRecord("x", 0, "", 0, "", (), None))) | {"message", "asctime"}
_ready = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        """One JSON line: time, level, logger, message, extra fields and any traceback."""
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
               "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        out.update({k: v for k, v in vars(record).items() if k not in _STD and not k.startswith("_")})
        if record.exc_info:
            out["exc"] = "".join(traceback.format_exception(*record.exc_info))[-4000:]
        return json.dumps(out, default=str)


def setup() -> None:
    """Send CareCircle's logs to stdout as JSON lines, and to Sentry when configured. Safe to call repeatedly."""
    global _ready
    if _ready:
        return
    _ready = True
    root = logging.getLogger("carecircle")
    root.setLevel(config.get("LOG_LEVEL", "INFO").upper())
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root.addHandler(h)
    root.propagate = False
    dsn = config.get("SENTRY_DSN")
    if dsn:
        try:
            import sentry_sdk

            def scrub(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
                """Drop request and user details before an event leaves the server."""
                event.pop("request", None)
                event.pop("user", None)
                return event

            sentry_sdk.init(dsn=dsn, send_default_pii=False, include_local_variables=False,
                            traces_sample_rate=0.0, environment=config.env(), before_send=scrub)
        except ImportError:
            root.warning("SENTRY_DSN is set but sentry-sdk is not installed")


def capture(exc: BaseException, where: str = "") -> str:
    """Record an unexpected error. Returns a short reference to show the user."""
    ref = uuid.uuid4().hex[:8].upper()
    logging.getLogger("carecircle.error").error("unhandled error", exc_info=(type(exc), exc, exc.__traceback__),
                                                extra={"ref": ref, "where": where})
    with contextlib.suppress(Exception):   # Sentry is optional; error reporting must never raise
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    return ref
