"""Health checks. `python manage.py health` exits non-zero when something is wrong, so a host or cron job can alert.
Streamlit's own /_stcore/health endpoint only says the web server is up; this checks the parts that matter."""
from __future__ import annotations

import time
from typing import Any

import config
import db


def check() -> dict[str, Any]:
    """Check configuration, database, encryption key, reminder engine and email. Returns status and checks."""
    checks = []

    def add(name: str, status: str, detail: str) -> None:
        """Record one check: ok, warn or fail."""
        checks.append({"check": name, "status": status, "detail": detail})

    errors, warnings = config.validate()
    for e in errors:
        add("config", "fail", e)
    for w in warnings:
        add("config", "warn", w)
    if not errors and not warnings:
        add("config", "ok", f"APP_ENV={config.env()}")

    try:
        v = db.schema_version()
        add("database", "ok" if v == db.SCHEMA_VERSION else "fail", f"schema v{v} (code expects v{db.SCHEMA_VERSION})")
        db.heartbeat("health", time.time(), "write test")
        db.check_key()
        add("encryption key", "ok", "DATA_KEY can read the database")
    except Exception as e:
        add("database", "fail", f"{type(e).__name__}: {e}")
        return _summary(checks)

    mode = config.scheduler_mode()
    if mode == "off":
        add("reminders", "warn", "SCHEDULER_MODE=off: no reminders are being sent")
    else:
        hb = db.get_heartbeat("reminders")
        limit = config.scheduler_interval() * 3 + 60
        if not hb:
            add("reminders", "fail", "the reminder engine has never run" +
                (" (start `python worker.py`)" if mode == "external" else ""))
        else:
            age = time.time() - hb["at"]
            add("reminders", "ok" if age <= limit else "fail", f"last pass {int(age)}s ago ({hb['detail']})")

    dead = db.count("SELECT COUNT(*) AS n FROM outbox WHERE status='dead' AND created_at>=datetime('now','-1 day')")
    backlog = db.count("SELECT COUNT(*) AS n FROM outbox WHERE status='pending' AND next_attempt_at<? "
                       "AND created_at<datetime('now','-10 minutes')", (time.time(),))
    add("email", "warn" if dead or backlog else "ok", f"{dead} undeliverable in 24h, {backlog} waiting over 10 minutes")
    return _summary(checks)


def _summary(checks: list[dict[str, str]]) -> dict[str, Any]:
    """Overall status: the worst of the individual checks."""
    found = {c["status"] for c in checks}
    status = "fail" if "fail" in found else "warn" if "warn" in found else "ok"
    return {"status": status, "checks": checks}


def reminder_engine_status() -> tuple[bool, str]:
    """(ok, message) for the in-app banner."""
    if config.scheduler_mode() == "off":
        return False, "Reminders are switched off on this server (SCHEDULER_MODE=off)."
    hb = db.get_heartbeat("reminders")
    if not hb:
        return False, "The reminder engine hasn't run yet."
    age = time.time() - hb["at"]
    if age > config.scheduler_interval() * 3 + 60:
        return False, f"Reminders last ran {int(age // 60)} minutes ago. Emails may not be going out."
    return True, f"Reminders last ran {int(age)} seconds ago."
