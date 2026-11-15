"""Backups, restore, retention, data export, circle deletion and key rotation.

Backups are consistent SQLite snapshots (online backup API) encrypted with AES-256-GCM using a key derived
from DATA_KEY. A backup is useless without that key, so store the key separately from the backups.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import clock
import config
import crypto
import db
from typedefs import Row

log = logging.getLogger("carecircle.maintenance")
MAGIC = b"CCBK1\n"

RETENTION_DEFAULTS = {  # days
    "RETENTION_DOSE_DAYS": 730,
    "RETENTION_CHECKIN_DAYS": 730,
    "RETENTION_ACTIVITY_DAYS": 365,
    "RETENTION_OUTBOX_DAYS": 30,
    "RETENTION_SECURITY_DAYS": 365,
    "RETENTION_STOCK_DAYS": 730,
    "RETENTION_RECORD_DAYS": 730,   # stopped medicines and past or cancelled appointments
}


def retention() -> dict[str, int]:
    """How many days each kind of data is kept (configurable, 1 day to 100 years)."""
    return {k: config.get_int(k, v, lo=1, hi=36500) for k, v in RETENTION_DEFAULTS.items()}


# ---------------------------------------------------------------- backup / restore
def _snapshot(dest_plain: str) -> None:
    """A consistent copy of the live database, using SQLite's online backup."""
    src = sqlite3.connect(config.db_path())
    dst = sqlite3.connect(dest_plain)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def backup(dest: str | None = None) -> str:
    """Write an encrypted backup. Returns its path."""
    if dest is None:
        folder = config.get("BACKUP_DIR", os.path.dirname(os.path.abspath(config.db_path())))
        dest = os.path.join(folder, f"carecircle-{time.strftime('%Y%m%d-%H%M%S')}.ccbak")
    fd, tmp = tempfile.mkstemp(suffix=".db", dir=os.path.dirname(os.path.abspath(dest)) or ".")
    os.close(fd)
    try:
        _snapshot(tmp)
        with open(tmp, "rb") as f:
            plain = f.read()
    finally:
        _wipe(tmp)
    kid, aead = crypto.backup_key()
    nonce = os.urandom(12)
    blob = MAGIC + kid.encode() + nonce + aead.encrypt(nonce, plain, MAGIC + kid.encode())
    out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(out, "wb") as f:
        f.write(blob)
    log.info("backup written", extra={"bytes": len(blob)})
    return dest


def _wipe(path: str) -> None:
    """Overwrite a temporary plaintext file with zeros, then delete it."""
    with contextlib.suppress(OSError):
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(b"\0" * size)
        os.remove(path)


def decrypt_backup(src: str) -> bytes:
    """The plaintext database inside a backup file, if the right key is available."""
    with open(src, "rb") as f:
        blob = f.read()
    if not blob.startswith(MAGIC):
        raise ValueError("not a CareCircle backup file")
    kid = blob[len(MAGIC):len(MAGIC) + 8].decode()
    nonce = blob[len(MAGIC) + 8:len(MAGIC) + 20]
    aead = crypto.backup_key_for(kid)
    try:
        return aead.decrypt(nonce, blob[len(MAGIC) + 20:], MAGIC + kid.encode())
    except Exception:
        raise crypto.DecryptionError("backup is corrupted or was made with a different key") from None


def verify_backup(src: str) -> dict[str, Any]:
    """Decrypt to a temporary file and run SQLite's integrity check. Returns a summary dict."""
    plain = decrypt_backup(src)
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(plain)
        c = sqlite3.connect(tmp)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            version = c.execute("PRAGMA user_version").fetchone()[0]
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            families = c.execute("SELECT COUNT(*) FROM families").fetchone()[0] if "families" in tables else 0
        finally:
            c.close()
        return {"ok": ok, "schema_version": version, "families": families}
    finally:
        _wipe(tmp)


def restore(src: str) -> dict[str, Any]:
    """Replace the live database with a backup (after verifying it), then apply any newer migrations.
    The current database is first saved as an encrypted backup next to it."""
    info = verify_backup(src)
    if not info["ok"]:
        raise ValueError("backup failed its integrity check")
    if info["schema_version"] > db.SCHEMA_VERSION:
        raise ValueError("backup is from a newer version of CareCircle")
    if os.path.exists(config.db_path()):
        backup(config.db_path() + f".before-restore-{time.strftime('%Y%m%d-%H%M%S')}.ccbak")
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(decrypt_backup(src))
        s, d = sqlite3.connect(tmp), sqlite3.connect(config.db_path())
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
    finally:
        _wipe(tmp)
    db.init_db()
    return info


# ---------------------------------------------------------------- retention
def purge(now: float | None = None) -> dict[str, int]:
    """Delete data older than the retention periods. Returns counts per table."""
    r = retention()
    now = now or time.time()
    out: dict[str, int] = {}

    def run(name: str, sql: str, args: Sequence[Any]) -> None:
        """Run one delete and count the rows."""
        out[name] = out.get(name, 0) + db.x(sql, args)

    run("doses", "DELETE FROM doses WHERE scheduled_date < date('now', ?)", (f"-{r['RETENTION_DOSE_DAYS']} days",))
    run("checkins", "DELETE FROM checkins WHERE day < date('now', ?)", (f"-{r['RETENTION_CHECKIN_DAYS']} days",))
    run("activity", "DELETE FROM activity WHERE at < datetime('now', ?)", (f"-{r['RETENTION_ACTIVITY_DAYS']} days",))
    run("outbox", "DELETE FROM outbox WHERE status IN ('sent','simulated','dead','expired') AND created_at < datetime('now', ?)",
        (f"-{r['RETENTION_OUTBOX_DAYS']} days",))
    run("security_events", "DELETE FROM security_events WHERE at < ?", (now - r["RETENTION_SECURITY_DAYS"] * 86400,))
    run("stock_events", "DELETE FROM stock_events WHERE at < datetime('now', ?)", (f"-{r['RETENTION_STOCK_DAYS']} days",))
    cutoff = (clock.now() - timedelta(days=r["RETENTION_RECORD_DAYS"])).isoformat(timespec="minutes")
    run("medications", """DELETE FROM medications WHERE status IN ('stopped','declined') AND COALESCE(updated_at, start_date) < ?
                          AND NOT EXISTS (SELECT 1 FROM doses d WHERE d.med_id=medications.id)""", (cutoff,))
    run("appointments", "DELETE FROM appointments WHERE when_at < ?", (cutoff,))
    run("med_changes", "DELETE FROM med_changes WHERE status IN ('accepted','approved','declined') AND resolved_at < ?",
        (cutoff,))
    run("rate_events", "DELETE FROM rate_events WHERE at < ?", (now - 86400,))
    run("sessions", "DELETE FROM sessions WHERE (revoked=1 OR expires_at < ?) AND last_seen < ?", (now, now - 86400))
    run("otp_codes", "DELETE FROM otp_codes WHERE expires_at < ?", (now - 86400,))
    log.info("retention purge", extra={"deleted": out})
    return out


# ---------------------------------------------------------------- export & delete
def export_family(family_id: int) -> str:
    """Everything CareCircle holds about one care circle, as JSON (decrypted). Access codes are not included."""
    def rows(sql: str, args: Sequence[Any] = (family_id,)) -> list[Row]:
        """Rows for this family."""
        return db.q(sql, args)

    fam = db.get_family(family_id)
    data: dict[str, Any] = {
        "exported_at": clock.now().isoformat(timespec="seconds"),
        "family": {"name": fam["name"], "elder_name": fam["elder_name"], "created_at": fam["created_at"],
                   "settings": db.family_settings(family_id)},
        "members": [{k: m[k] for k in ("id", "name", "email", "role", "lang", "created_at", "last_login_at")}
                    for m in db.members(family_id)],
        "medications": rows("SELECT * FROM medications WHERE family_id=?"),
        "doses": rows("SELECT id, med_id, scheduled_date, scheduled_time, status, marked_by, marked_at "
                      "FROM doses WHERE family_id=?"),
        "stock_events": rows("SELECT * FROM stock_events WHERE family_id=?"),
        "medicine_changes": rows("SELECT med_id, action, before_json, after_json, by_name, status, note, resolved_by, "
                                 "created_at, resolved_at FROM med_changes WHERE family_id=?"),
        "appointments": rows("SELECT * FROM appointments WHERE family_id=?"),
        "checkins": rows("SELECT day, mood, note FROM checkins WHERE family_id=?"),
        "activity": rows("SELECT who, what, at FROM activity WHERE family_id=? ORDER BY id"),
        "emails": rows("SELECT to_email, subject, body, kind, status, created_at, sent_at FROM outbox WHERE family_id=?"),
        "security_events": rows("SELECT at, member_id, event, detail FROM security_events WHERE family_id=?"),
    }
    for m in data["medications"]:
        m.pop("family_id", None)
    return json.dumps(data, indent=2, default=str)


def delete_family(family_id: int) -> int:
    """Delete a circle and everything in it. Returns the number of rows deleted."""
    n = db.delete_family_rows(family_id)
    db.security_event("family_deleted", None, None, f"family {family_id}, {n} rows")
    with contextlib.suppress(Exception):
        c = db.conn()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    return n


# ---------------------------------------------------------------- key rotation
def rotate_key(force: bool = False) -> int:
    """Re-encrypt every encrypted value with the current DATA_KEY. The old key must be in DATA_KEY_PREVIOUS.
    Members still on a pre-upgrade 6-character code would lose it (its lookup is keyed); refuse unless force."""
    legacy = db.count("SELECT COUNT(*) AS n FROM members WHERE legacy_lookup IS NOT NULL")
    if legacy and not force:
        raise RuntimeError(f"{legacy} member(s) still use an old 6-character code. Ask them to sign in once, "
                           "or run with --force and give them new codes.")
    changed = 0
    with db.tx() as c:
        for table, cols in db.ENCRYPTED_COLUMNS.items():
            for r in c.execute(f"SELECT id, {', '.join(cols)} FROM {table}").fetchall():
                vals = {col: crypto.enc(crypto.dec(r[col])) for col in cols if crypto.needs_reencrypt(r[col])}
                if vals:
                    changed += 1
                    c.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), r["id"]))
        for r in c.execute("SELECT id, email FROM members").fetchall():
            c.execute("UPDATE members SET email_index=? WHERE id=?", (crypto.email_index(crypto.dec(r["email"])), r["id"]))
        if force:
            c.execute("UPDATE members SET legacy_lookup=NULL WHERE legacy_lookup IS NOT NULL")
        c.execute("UPDATE otp_codes SET used=1 WHERE used=0")
        c.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('key_check',?)", (crypto.enc("carecircle"),))
    c = db.conn()
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
    finally:
        c.close()
    db.security_event("key_rotated", None, None, f"{changed} rows")
    with contextlib.suppress(Exception):
        crypto._dev_cache.clear()
    return changed
