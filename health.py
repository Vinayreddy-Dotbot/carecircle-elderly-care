"""Health checks. `python manage.py health` exits non-zero when something is wrong, so a host or cron job can alert.
Streamlit's own /_stcore/health endpoint only says the web server is up; this checks the parts that matter."""
import time

import config
import db


def check():
    checks = []

    def add(name, status, detail):
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

    dead = db.q1("SELECT COUNT(*) AS n FROM outbox WHERE status='dead' AND created_at>=datetime('now','-1 day')")["n"]
    backlog = db.q1("SELECT COUNT(*) AS n FROM outbox WHERE status='pending' AND next_attempt_at<? "
                    "AND created_at<datetime('now','-10 minutes')", (time.time(),))["n"]
    add("email", "warn" if dead or backlog else "ok", f"{dead} undeliverable in 24h, {backlog} waiting over 10 minutes")
    return _summary(checks)


def _summary(checks):
    status = "fail" if any(c["status"] == "fail" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    return {"status": status, "checks": checks}


def reminder_engine_status():
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
