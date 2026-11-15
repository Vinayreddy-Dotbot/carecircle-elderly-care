"""One source of 'now'. Cloud servers run in UTC, so reminders must use the family's timezone (default India)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import config


def _tz() -> ZoneInfo | None:
    """The family's timezone (APP_TZ, default Asia/Kolkata), or None if it isn't a valid name."""
    try:
        return ZoneInfo(config.get("APP_TZ", "Asia/Kolkata"))
    except Exception:
        return None


def now() -> datetime:
    """The local time now, without tzinfo (all stored times are local)."""
    z = _tz()
    return datetime.now(z).replace(tzinfo=None) if z else datetime.now()


def today() -> date:
    """The local date today."""
    return now().date()


def from_utc(text: object) -> str:
    """SQLite CURRENT_TIMESTAMP values are UTC 'YYYY-MM-DD HH:MM:SS'. Return local 'YYYY-MM-DD HH:MM'."""
    try:
        dt = datetime.fromisoformat(str(text).replace(" ", "T")).replace(tzinfo=timezone.utc)
        z = _tz()
        return (dt.astimezone(z) if z else dt).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(text or "")[:16]
