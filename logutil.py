"""Structured logging and error reporting.

Logs are JSON lines on stdout (most hosts collect stdout). Only IDs and counts are logged: never access codes,
sign-in codes, tokens, email addresses, names or medical details.
If SENTRY_DSN is set and sentry-sdk is installed, errors are also sent to Sentry with PII collection off.
"""
import json
import logging
import sys
import time
import traceback
import uuid

import config

_STD = set(vars(logging.LogRecord("x", 0, "", 0, "", (), None))) | {"message", "asctime"}
_ready = False


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
               "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        out.update({k: v for k, v in vars(record).items() if k not in _STD and not k.startswith("_")})
        if record.exc_info:
            out["exc"] = "".join(traceback.format_exception(*record.exc_info))[-4000:]
        return json.dumps(out, default=str)


def setup():
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

            def scrub(event, hint):
                event.pop("request", None)
                event.pop("user", None)
                return event

            sentry_sdk.init(dsn=dsn, send_default_pii=False, include_local_variables=False,
                            traces_sample_rate=0.0, environment=config.env(), before_send=scrub)
        except ImportError:
            root.warning("SENTRY_DSN is set but sentry-sdk is not installed")


def capture(exc, where=""):
    """Record an unexpected error. Returns a short reference to show the user."""
    ref = uuid.uuid4().hex[:8].upper()
    logging.getLogger("carecircle.error").error("unhandled error", exc_info=(type(exc), exc, exc.__traceback__),
                                                extra={"ref": ref, "where": where})
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass
    return ref
