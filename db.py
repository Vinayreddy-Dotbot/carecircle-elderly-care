"""SQLite data layer for CareCircle.

Rules this module enforces:
- Every function that reads or changes family data takes the caller's family_id and scopes its SQL by it,
  so a record ID from another family is treated as not found.
- Sensitive text columns (ENCRYPTED_COLUMNS) are encrypted on write and decrypted transparently on read.
- Column names in dynamic UPDATEs come from fixed allow-lists, never from callers.
- Read-modify-write operations run inside BEGIN IMMEDIATE transactions.
- Schema changes are numbered migrations tracked with PRAGMA user_version.
"""
import contextlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import timedelta

import clock
import config
import crypto

log_ = logging.getLogger("carecircle.db")

ROLES = ("elder", "caretaker", "family")
DOSE_STATUSES = ("pending", "taken", "unconfirmed", "missed", "skipped")
MED_STATUSES = ("active", "proposed", "stopped", "declined")
SCHEMA_VERSION = 3

ENCRYPTED_COLUMNS = {
    "families": ("name", "elder_name"),
    "members": ("name", "email"),
    "medications": ("name", "dosage", "instructions", "added_by", "prescriber", "verified_by"),
    "doses": ("marked_by",),
    "appointments": ("doctor", "specialty", "location", "notes", "questions", "added_by"),
    "checkins": ("note",),
    "outbox": ("to_email", "subject", "body", "error"),
    "activity": ("who", "what"),
    "stock_events": ("who", "reason"),
    "med_changes": ("before_json", "after_json", "by_name", "note", "resolved_by"),
}
CHANGE_OPEN = ("pending", "applied", "disputed")   # still need the elder's attention

FAMILY_SETTINGS_DEFAULTS = {
    "email_detail": "minimal",   # minimal: no medicine/doctor names in email; full: everything
    "ai_enabled": False,         # the family must opt in before anything is sent to the AI provider
    "elder_approval": False,     # only the elder can turn this on: medicine changes by others wait for their OK
}

_COL = re.compile(r"^[a-z_]+$")


class NotFound(Exception):
    """The record does not exist or belongs to another family."""


# ---------------------------------------------------------------- connections
def conn():
    c = sqlite3.connect(config.db_path(), timeout=15, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA secure_delete=ON")   # overwrite deleted content instead of leaving it in free pages
    c.execute("PRAGMA busy_timeout=15000")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


@contextlib.contextmanager
def tx():
    """A write transaction that takes the database write lock up front."""
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(Exception):
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def _decrypt_row(row):
    return {k: crypto.dec(row[k]) for k in row.keys()}


def q(sql, args=(), c=None):
    own = c is None
    c = c or conn()
    try:
        return [_decrypt_row(r) for r in c.execute(sql, args).fetchall()]
    finally:
        if own:
            c.close()


def q1(sql, args=(), c=None):
    rows = q(sql, args, c)
    return rows[0] if rows else None


def x(sql, args=(), c=None):
    own = c is None
    c = c or conn()
    try:
        cur = c.execute(sql, args)
        return cur.lastrowid if cur.lastrowid else cur.rowcount
    finally:
        if own:
            c.close()


def _check_cols(cols):
    for col in cols:
        if not _COL.match(col):
            raise ValueError(f"bad column name {col!r}")


def _enc_values(table, values):
    enc_cols = ENCRYPTED_COLUMNS.get(table, ())
    return {k: (crypto.enc(v) if k in enc_cols and isinstance(v, str) else v) for k, v in values.items()}


def insert(table, values, c=None):
    _check_cols([table, *values])
    vals = _enc_values(table, values)
    sql = f"INSERT INTO {table}({','.join(vals)}) VALUES({','.join('?' * len(vals))})"
    own = c is None
    c = c or conn()
    try:
        return c.execute(sql, tuple(vals.values())).lastrowid
    finally:
        if own:
            c.close()


def update(table, values, where, args, allowed, c=None):
    bad = set(values) - set(allowed)
    if bad:
        raise ValueError(f"cannot update {', '.join(sorted(bad))} on {table}")
    if not values:
        return 0
    _check_cols([table, *values])
    vals = _enc_values(table, values)
    sql = f"UPDATE {table} SET {', '.join(f'{k}=?' for k in vals)} WHERE {where}"
    own = c is None
    c = c or conn()
    try:
        return c.execute(sql, (*vals.values(), *args)).rowcount
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------- schema & migrations
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS families(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, elder_name TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS members(
  id INTEGER PRIMARY KEY, family_id INTEGER NOT NULL REFERENCES families(id),
  name TEXT NOT NULL, email TEXT DEFAULT '', role TEXT NOT NULL,
  passcode TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS medications(
  id INTEGER PRIMARY KEY, family_id INTEGER NOT NULL REFERENCES families(id),
  name TEXT NOT NULL, dosage TEXT DEFAULT '', instructions TEXT DEFAULT '',
  times TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT,
  stock INTEGER, refill_at INTEGER DEFAULT 5, status TEXT DEFAULT 'active', added_by TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS doses(
  id INTEGER PRIMARY KEY, med_id INTEGER NOT NULL REFERENCES medications(id),
  family_id INTEGER NOT NULL, scheduled_date TEXT NOT NULL, scheduled_time TEXT NOT NULL,
  status TEXT DEFAULT 'pending', marked_by TEXT DEFAULT '', marked_at TEXT,
  UNIQUE(med_id, scheduled_date, scheduled_time));
CREATE TABLE IF NOT EXISTS appointments(
  id INTEGER PRIMARY KEY, family_id INTEGER NOT NULL REFERENCES families(id),
  doctor TEXT NOT NULL, specialty TEXT DEFAULT '', when_at TEXT NOT NULL,
  location TEXT DEFAULT '', notes TEXT DEFAULT '', questions TEXT DEFAULT '',
  status TEXT DEFAULT 'scheduled', added_by TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS checkins(
  id INTEGER PRIMARY KEY, family_id INTEGER NOT NULL, day TEXT NOT NULL,
  mood INTEGER NOT NULL, note TEXT DEFAULT '', UNIQUE(family_id, day));
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, family_id INTEGER NOT NULL, to_email TEXT NOT NULL,
  subject TEXT NOT NULL, body TEXT NOT NULL, kind TEXT DEFAULT '',
  dedupe_key TEXT NOT NULL UNIQUE, status TEXT DEFAULT 'pending',
  error TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, sent_at TEXT);
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY, family_id INTEGER NOT NULL, who TEXT NOT NULL,
  what TEXT NOT NULL, at TEXT DEFAULT CURRENT_TIMESTAMP)
"""

MEMBERS_V2 = """
CREATE TABLE members_v2(
  id INTEGER PRIMARY KEY,
  family_id INTEGER NOT NULL REFERENCES families(id),
  name TEXT NOT NULL,
  email TEXT DEFAULT '',
  email_index TEXT DEFAULT '',
  role TEXT NOT NULL CHECK(role IN ('elder','caretaker','family')),
  selector TEXT UNIQUE,
  code_hash TEXT NOT NULL,
  legacy_lookup TEXT UNIQUE,
  must_rotate INTEGER DEFAULT 0,
  mfa INTEGER DEFAULT 0,
  locked_until REAL DEFAULT 0,
  lang TEXT DEFAULT 'en',
  large_text INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  code_changed_at TEXT,
  last_login_at TEXT)
"""

NEW_TABLES_V2 = """
CREATE TABLE IF NOT EXISTS rate_events(id INTEGER PRIMARY KEY, bucket TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_rate_events ON rate_events(bucket, at);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, member_id INTEGER NOT NULL, family_id INTEGER NOT NULL,
  stage TEXT NOT NULL DEFAULT 'full', created_at REAL NOT NULL, last_seen REAL NOT NULL,
  expires_at REAL NOT NULL, revoked INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_sessions_member ON sessions(member_id);
CREATE TABLE IF NOT EXISTS otp_codes(
  id INTEGER PRIMARY KEY, subject TEXT NOT NULL, purpose TEXT NOT NULL, challenge TEXT NOT NULL,
  code_hash TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
  attempts INTEGER DEFAULT 0, used INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_otp_challenge ON otp_codes(challenge);
CREATE TABLE IF NOT EXISTS security_events(
  id INTEGER PRIMARY KEY, at REAL NOT NULL, family_id INTEGER, member_id INTEGER,
  event TEXT NOT NULL, detail TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_security_family ON security_events(family_id, at);
CREATE TABLE IF NOT EXISTS stock_events(
  id INTEGER PRIMARY KEY, med_id INTEGER NOT NULL, family_id INTEGER NOT NULL, delta INTEGER NOT NULL,
  new_stock INTEGER, reason TEXT DEFAULT '', who TEXT DEFAULT '', dose_id INTEGER,
  at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS heartbeats(name TEXT PRIMARY KEY, at REAL NOT NULL, detail TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_doses_family_day ON doses(family_id, scheduled_date);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_activity_family ON activity(family_id, id)
"""


def _exec_script(c, script):
    for stmt in script.split(";"):
        if stmt.strip():
            c.execute(stmt)


def _cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def _add_col(c, table, col, decl):
    if col not in _cols(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _migrate_v1(c):
    _exec_script(c, SCHEMA_V1)
    _add_col(c, "outbox", "attempts", "INTEGER DEFAULT 0")


def _migrate_v2(c):
    """Security upgrade: hashed access codes, encryption at rest, sessions, rate limits, reliable outbox."""
    import auth  # local import: auth depends on db

    _add_col(c, "families", "is_demo", "INTEGER DEFAULT 0")
    _add_col(c, "families", "settings", "TEXT DEFAULT '{}'")

    # members: replace the plaintext passcode column. Old 6-character codes keep working once,
    # through a keyed lookup, and the member must take a new code at that sign-in.
    c.execute(MEMBERS_V2)
    for m in c.execute("SELECT * FROM members").fetchall():
        code = auth.normalize(m["passcode"])
        c.execute("""INSERT INTO members_v2(id,family_id,name,email,email_index,role,code_hash,legacy_lookup,
                     must_rotate,large_text) VALUES(?,?,?,?,?,?,?,?,1,?)""",
                  (m["id"], m["family_id"], m["name"], m["email"] or "", crypto.email_index(m["email"]),
                   m["role"], crypto.hash_secret(code), auth.legacy_lookup(code), int(m["role"] == "elder")))
    c.execute("DROP TABLE members")
    c.execute("ALTER TABLE members_v2 RENAME TO members")
    c.execute("CREATE INDEX IF NOT EXISTS ix_members_family ON members(family_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_members_email ON members(email_index)")

    # sample families created by the old demo button used example.com addresses
    for f in c.execute("SELECT id FROM families").fetchall():
        emails = [r["email"] or "" for r in c.execute("SELECT email FROM members WHERE family_id=?", (f["id"],))]
        if emails and all(e.lower().endswith("@example.com") for e in emails):
            c.execute("UPDATE families SET is_demo=1 WHERE id=?", (f["id"],))

    for col, decl in (("prescriber", "TEXT DEFAULT ''"), ("verified_by", "TEXT DEFAULT ''"), ("verified_at", "TEXT"),
                      ("created_at", "TEXT"), ("updated_at", "TEXT")):
        _add_col(c, "medications", col, decl)

    _add_col(c, "doses", "stock_applied", "INTEGER DEFAULT 0")
    c.execute("UPDATE doses SET status='unconfirmed' WHERE status='missed' AND marked_by='auto'")
    c.execute("UPDATE doses SET stock_applied=1 WHERE status='taken'")

    for col, decl in (("member_id", "INTEGER"), ("priority", "INTEGER DEFAULT 0"), ("next_attempt_at", "REAL DEFAULT 0"),
                      ("claimed_by", "TEXT"), ("claimed_until", "REAL DEFAULT 0"), ("expires_at", "REAL")):
        _add_col(c, "outbox", col, decl)
    c.execute("UPDATE outbox SET status='pending' WHERE status='failed' AND attempts<3")
    c.execute("UPDATE outbox SET status='dead' WHERE status='failed'")
    for r in c.execute("SELECT id, dedupe_key FROM outbox").fetchall():  # old keys contained email addresses
        c.execute("UPDATE outbox SET dedupe_key=? WHERE id=?", (crypto.index("legacy-dedupe:" + r["dedupe_key"]), r["id"]))

    _exec_script(c, NEW_TABLES_V2)
    _encrypt_plaintext(c)
    c.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('key_check',?)", (crypto.enc("carecircle"),))


def _encrypt_plaintext(c):
    existing = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, cols in ENCRYPTED_COLUMNS.items():
        if table not in existing:  # created by a later migration
            continue
        for r in c.execute(f"SELECT id, {', '.join(cols)} FROM {table}").fetchall():
            vals = {col: crypto.enc(r[col]) for col in cols
                    if isinstance(r[col], str) and r[col] and not crypto.is_encrypted(r[col])}
            if vals:
                c.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), r["id"]))


def _migrate_v3(c):
    """Medicine changes made by someone other than the elder, for the elder to see, accept, dispute or approve."""
    _exec_script(c, """
CREATE TABLE IF NOT EXISTS med_changes(
  id INTEGER PRIMARY KEY, family_id INTEGER NOT NULL, med_id INTEGER NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('add','edit','stop')),
  before_json TEXT DEFAULT '', after_json TEXT DEFAULT '',
  by_member INTEGER, by_name TEXT DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('pending','applied','accepted','disputed','approved','declined')),
  note TEXT DEFAULT '', resolved_by TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT);
CREATE INDEX IF NOT EXISTS ix_med_changes_family ON med_changes(family_id, status)
""")


MIGRATIONS = [(1, _migrate_v1), (2, _migrate_v2), (3, _migrate_v3)]


def schema_version(c=None):
    own = c is None
    c = c or conn()
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        if own:
            c.close()


def _has_data(c):
    try:
        return c.execute("SELECT COUNT(*) FROM members").fetchone()[0] > 0
    except sqlite3.OperationalError:
        return False


def _restrict_permissions():
    for suffix in ("", "-wal", "-shm"):
        p = config.db_path() + suffix
        if os.path.exists(p):
            with contextlib.suppress(OSError):
                os.chmod(p, 0o600)


def init_db():
    """Create or upgrade the database. Safe to call on every start."""
    path = config.db_path()
    if not os.path.exists(path):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    c = conn()
    try:
        c.execute("PRAGMA journal_mode=WAL")
        current = schema_version(c)
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema v{current} is newer than this code (v{SCHEMA_VERSION}). Deploy the newer code.")
        pending = [(v, fn) for v, fn in MIGRATIONS if v > current]
        if not pending:
            check_key(c)
            return []
        if _has_data(c):
            import maintenance
            maintenance.backup(f"{path}.pre-v{SCHEMA_VERSION}.ccbak")  # encrypted copy before changing anything
        c.execute("PRAGMA foreign_keys=OFF")  # table rebuilds; must be set outside the transaction
        applied = []
        c.execute("BEGIN IMMEDIATE")
        try:
            for v, fn in pending:
                fn(c)
                c.execute(f"PRAGMA user_version={int(v)}")
                applied.append(v)
            problems = c.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"foreign key problems after migration: {len(problems)}")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("PRAGMA foreign_keys=ON")
        # remove old plaintext (passcodes, names) from free pages and the WAL
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log_.info("database migrated", extra={"versions": applied})
        return applied
    finally:
        c.close()
        _restrict_permissions()


def expected_key_id(c=None):
    """The key id this database's data was first encrypted with (None for a new or pre-upgrade database)."""
    own = c is None
    c = c or conn()
    try:
        row = c.execute("SELECT value FROM app_meta WHERE key='key_check'").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        if own:
            c.close()
    return crypto.stored_kid(row["value"]) if row else None


def check_key(c=None):
    """Fail fast, with a message that names both keys, if the current keys can't read this database."""
    own = c is None
    c = c or conn()
    try:
        row = c.execute("SELECT value FROM app_meta WHERE key='key_check'").fetchone()
    finally:
        if own:
            c.close()
    if not row:
        return
    try:
        ok = crypto.dec(row["value"]) == "carecircle"
    except crypto.DecryptionError:
        ok = False
    if not ok:
        raise crypto.DecryptionError(crypto.mismatch_message(crypto.stored_kid(row["value"])))


# ---------------------------------------------------------------- families
def create_family(name, elder_name, is_demo=False, c=None):
    return insert("families", {"name": name, "elder_name": elder_name, "is_demo": int(is_demo)}, c)


def get_family(family_id):
    f = q1("SELECT * FROM families WHERE id=?", (family_id,))
    if not f:
        raise NotFound("family")
    return f


def family_ids(include_demo=True):
    sql = "SELECT id FROM families" + ("" if include_demo else " WHERE is_demo=0")
    return [r["id"] for r in q(sql)]


def family_settings(family_id):
    raw = get_family(family_id).get("settings") or "{}"
    try:
        stored = json.loads(raw)
    except ValueError:
        stored = {}
    return {**FAMILY_SETTINGS_DEFAULTS, **{k: v for k, v in stored.items() if k in FAMILY_SETTINGS_DEFAULTS}}


def update_family_settings(family_id, **values):
    bad = set(values) - set(FAMILY_SETTINGS_DEFAULTS)
    if bad:
        raise ValueError(f"unknown settings: {', '.join(sorted(bad))}")
    with tx() as c:
        row = c.execute("SELECT settings FROM families WHERE id=?", (family_id,)).fetchone()
        if not row:
            raise NotFound("family")
        current = json.loads(row["settings"] or "{}")
        current.update(values)
        c.execute("UPDATE families SET settings=? WHERE id=?", (json.dumps(current), family_id))


def delete_family_rows(family_id):
    """Remove every row belonging to a family. Returns the number of rows deleted."""
    with tx() as c:
        member_ids = [r["id"] for r in c.execute("SELECT id FROM members WHERE family_id=?", (family_id,))]
        n = 0
        for mid in member_ids:
            n += c.execute("DELETE FROM sessions WHERE member_id=?", (mid,)).rowcount
            n += c.execute("DELETE FROM otp_codes WHERE subject=?", (f"m:{mid}",)).rowcount
        for table in ("doses", "stock_events", "med_changes", "medications", "appointments", "checkins", "outbox",
                      "activity", "security_events", "members"):
            n += c.execute(f"DELETE FROM {table} WHERE family_id=?", (family_id,)).rowcount
        n += c.execute("DELETE FROM families WHERE id=?", (family_id,)).rowcount
    return n


# ---------------------------------------------------------------- members
MEMBER_FIELDS = {"name", "email", "email_index", "lang", "large_text", "mfa"}


def insert_member(family_id, name, email, role, selector, code_hash, c=None):
    if role not in ROLES:
        raise ValueError("bad role")
    return insert("members", {
        "family_id": family_id, "name": name, "email": email or "", "email_index": crypto.email_index(email),
        "role": role, "selector": selector, "code_hash": code_hash, "large_text": int(role == "elder"),
        "code_changed_at": clock.now().isoformat(timespec="seconds"),
    }, c)


def get_member(member_id, c=None):
    return q1("SELECT * FROM members WHERE id=?", (member_id,), c)


def member_in_family(family_id, member_id):
    m = q1("SELECT * FROM members WHERE id=? AND family_id=?", (member_id, family_id))
    if not m:
        raise NotFound("member")
    return m


def members(family_id, roles=None, with_email=False):
    rows = q("SELECT * FROM members WHERE family_id=? ORDER BY id", (family_id,))
    if roles:
        rows = [r for r in rows if r["role"] in roles]
    if with_email:
        rows = [r for r in rows if r["email"]]
    return rows


def update_member(family_id, member_id, **fields):
    if "email" in fields:
        fields["email_index"] = crypto.email_index(fields["email"])
    n = update("members", fields, "id=? AND family_id=?", (member_id, family_id), MEMBER_FIELDS)
    if not n:
        raise NotFound("member")


def delete_member(family_id, member_id):
    with tx() as c:
        n = c.execute("DELETE FROM members WHERE id=? AND family_id=? AND role!='elder'", (member_id, family_id)).rowcount
        if not n:
            raise NotFound("member")
        c.execute("DELETE FROM sessions WHERE member_id=?", (member_id,))
        c.execute("DELETE FROM otp_codes WHERE subject=?", (f"m:{member_id}",))


# ---------------------------------------------------------------- activity & security log
def log(family_id, who, what, c=None):
    insert("activity", {"family_id": family_id, "who": who, "what": what}, c)


def activity(family_id, limit=30):
    return q("SELECT * FROM activity WHERE family_id=? ORDER BY id DESC LIMIT ?", (family_id, limit))


def security_event(event, family_id=None, member_id=None, detail="", c=None):
    """Security log. Never put codes, tokens or email addresses in `detail`."""
    insert("security_events", {"at": time.time(), "family_id": family_id, "member_id": member_id,
                               "event": event, "detail": detail[:300]}, c)


def security_events(family_id, limit=50):
    return q("SELECT * FROM security_events WHERE family_id=? ORDER BY id DESC LIMIT ?", (family_id, limit))


# ---------------------------------------------------------------- medications
MED_FIELDS = {"name", "dosage", "instructions", "times", "start_date", "end_date", "stock", "refill_at", "status",
              "prescriber", "verified_by", "verified_at", "updated_at"}


def _sort_meds(rows):
    return sorted(rows, key=lambda m: (m["name"] or "").lower())


def add_med(family_id, name, dosage, instructions, times, start, end, stock, refill_at, added_by,
            status="active", prescriber=""):
    if status not in MED_STATUSES:
        raise ValueError("bad status")
    now = clock.now().isoformat(timespec="seconds")
    return insert("medications", {
        "family_id": family_id, "name": name.strip(), "dosage": dosage, "instructions": instructions,
        "times": ",".join(sorted(set(times))), "start_date": start, "end_date": end, "stock": stock,
        "refill_at": refill_at, "status": status, "added_by": added_by, "prescriber": prescriber,
        "created_at": now, "updated_at": now})


def get_med(family_id, med_id):
    m = q1("SELECT * FROM medications WHERE id=? AND family_id=?", (med_id, family_id))
    if not m:
        raise NotFound("medicine")
    return m


def meds(family_id, statuses=("active",)):
    ph = ",".join("?" * len(statuses))
    return _sort_meds(q(f"SELECT * FROM medications WHERE family_id=? AND status IN ({ph})", (family_id, *statuses)))


def update_med(family_id, med_id, **fields):
    if "status" in fields and fields["status"] not in MED_STATUSES:
        raise ValueError("bad status")
    fields.setdefault("updated_at", clock.now().isoformat(timespec="seconds"))
    if not update("medications", fields, "id=? AND family_id=?", (med_id, family_id), MED_FIELDS):
        raise NotFound("medicine")


def stop_med(family_id, med_id):
    with tx() as c:
        if not c.execute("UPDATE medications SET status='stopped', updated_at=? WHERE id=? AND family_id=?",
                         (clock.now().isoformat(timespec="seconds"), med_id, family_id)).rowcount:
            raise NotFound("medicine")
        c.execute("DELETE FROM doses WHERE med_id=? AND family_id=? AND status='pending'", (med_id, family_id))


def adjust_stock(family_id, med_id, new_stock, who, reason):
    with tx() as c:
        row = c.execute("SELECT stock FROM medications WHERE id=? AND family_id=?", (med_id, family_id)).fetchone()
        if not row:
            raise NotFound("medicine")
        old = row["stock"] or 0
        c.execute("UPDATE medications SET stock=? WHERE id=?", (new_stock, med_id))
        insert("stock_events", {"med_id": med_id, "family_id": family_id, "delta": (new_stock or 0) - old,
                                "new_stock": new_stock, "reason": reason, "who": who}, c)


def stock_events(family_id, med_id, limit=20):
    return q("SELECT * FROM stock_events WHERE family_id=? AND med_id=? ORDER BY id DESC LIMIT ?",
             (family_id, med_id, limit))


# ---------------------------------------------------------------- medicine changes for the elder
def add_change(family_id, med_id, action, before, after, by_member, by_name, status, c=None):
    return insert("med_changes", {"family_id": family_id, "med_id": med_id, "action": action,
                                  "before_json": json.dumps(before), "after_json": json.dumps(after),
                                  "by_member": by_member, "by_name": by_name, "status": status}, c)


def _change_row(r):
    r["before"] = json.loads(r.pop("before_json") or "{}")
    r["after"] = json.loads(r.pop("after_json") or "{}")
    return r


def get_change(family_id, change_id):
    r = q1("SELECT * FROM med_changes WHERE id=? AND family_id=?", (change_id, family_id))
    if not r:
        raise NotFound("change")
    return _change_row(r)


def changes(family_id, statuses=CHANGE_OPEN, med_id=None):
    ph = ",".join("?" * len(statuses))
    sql = f"SELECT * FROM med_changes WHERE family_id=? AND status IN ({ph})"
    args = [family_id, *statuses]
    if med_id is not None:
        sql += " AND med_id=?"
        args.append(med_id)
    return [_change_row(r) for r in q(sql + " ORDER BY id", args)]


def resolve_change(family_id, change_id, status, resolved_by, note="", expect=None):
    """Move a change to a new status. `expect` guards against two people resolving it at once."""
    where, args = "id=? AND family_id=?", [change_id, family_id]
    if expect:
        where += f" AND status IN ({','.join('?' * len(expect))})"
        args += list(expect)
    n = update("med_changes", {"status": status, "resolved_by": resolved_by, "note": note,
                               "resolved_at": clock.now().isoformat(timespec="seconds")}, where, args,
               {"status", "resolved_by", "note", "resolved_at"})
    if not n:
        raise NotFound("change")


# ---------------------------------------------------------------- doses
def ensure_doses(family_id, day):
    """Create dose rows for every active medicine scheduled on `day` (YYYY-MM-DD)."""
    with tx() as c:
        for m in q("SELECT id, times, start_date, end_date FROM medications WHERE family_id=? AND status='active'",
                   (family_id,), c):
            if m["start_date"] > day or (m["end_date"] and m["end_date"] < day):
                continue
            for t in m["times"].split(","):
                c.execute("INSERT OR IGNORE INTO doses(med_id,family_id,scheduled_date,scheduled_time) VALUES(?,?,?,?)",
                          (m["id"], family_id, day, t))


def doses_for_day(family_id, day):
    ensure_doses(family_id, day)
    rows = q("""SELECT d.*, m.name, m.dosage, m.instructions FROM doses d
                JOIN medications m ON m.id=d.med_id
                WHERE d.family_id=? AND d.scheduled_date=? AND m.status='active'""", (family_id, day))
    return sorted(rows, key=lambda d: (d["scheduled_time"], (d["name"] or "").lower()))


def get_dose(family_id, dose_id):
    d = q1("""SELECT d.*, m.name, m.dosage FROM doses d JOIN medications m ON m.id=d.med_id
              WHERE d.id=? AND d.family_id=?""", (dose_id, family_id))
    if not d:
        raise NotFound("dose")
    return d


def set_dose(family_id, dose_id, status, who, now=None):
    """Change a dose's status and keep the pill count in step, atomically.

    Stock is decremented at most once per dose (tracked by doses.stock_applied), so repeated clicks,
    two people marking at the same moment, or undo/redo cannot drift the count.
    Returns False if the dose already had this status.
    """
    if status not in DOSE_STATUSES:
        raise ValueError("bad status")
    now = now or clock.now()
    with tx() as c:
        row = c.execute("""SELECT d.status, d.stock_applied, d.med_id, m.stock FROM doses d
                           JOIN medications m ON m.id=d.med_id WHERE d.id=? AND d.family_id=?""",
                        (dose_id, family_id)).fetchone()
        if not row:
            raise NotFound("dose")
        if row["status"] == status:
            return False
        applied = row["stock_applied"]
        delta = 0
        if status == "taken" and not applied and row["stock"] is not None and row["stock"] > 0:
            delta, applied = -1, 1
        elif status != "taken" and applied:
            delta, applied = 1, 0
        update("doses", {"status": status, "marked_by": who, "marked_at": now.isoformat(timespec="seconds"),
                         "stock_applied": applied}, "id=?", (dose_id,),
               {"status", "marked_by", "marked_at", "stock_applied"}, c)
        if delta:
            c.execute("UPDATE medications SET stock=stock+? WHERE id=?", (delta, row["med_id"]))
            new = c.execute("SELECT stock FROM medications WHERE id=?", (row["med_id"],)).fetchone()["stock"]
            insert("stock_events", {"med_id": row["med_id"], "family_id": family_id, "delta": delta, "new_stock": new,
                                    "reason": "dose taken" if delta < 0 else "dose un-marked", "who": who,
                                    "dose_id": dose_id}, c)
    return True


def auto_unconfirm(family_id, dose_id, now):
    """Scheduler: a pending dose past its grace period becomes 'unconfirmed' (not 'missed': nobody knows yet)."""
    return x("UPDATE doses SET status='unconfirmed', marked_by='', marked_at=? WHERE id=? AND family_id=? AND status='pending'",
             (now.isoformat(timespec="seconds"), dose_id, family_id))


# ---------------------------------------------------------------- appointments
APPT_FIELDS = {"doctor", "specialty", "when_at", "location", "notes", "questions", "status"}


def add_appt(family_id, doctor, specialty, when_at, location, notes, questions, added_by):
    return insert("appointments", {"family_id": family_id, "doctor": doctor.strip(), "specialty": specialty,
                                   "when_at": when_at, "location": location, "notes": notes,
                                   "questions": questions, "added_by": added_by})


def get_appt(family_id, appt_id):
    a = q1("SELECT * FROM appointments WHERE id=? AND family_id=?", (appt_id, family_id))
    if not a:
        raise NotFound("appointment")
    return a


def appts(family_id, upcoming_only=True, now=None):
    now = (now or clock.now()).isoformat(timespec="minutes")
    if upcoming_only:
        return q("SELECT * FROM appointments WHERE family_id=? AND status='scheduled' AND when_at>=? ORDER BY when_at",
                 (family_id, now))
    return q("SELECT * FROM appointments WHERE family_id=? AND status='scheduled' ORDER BY when_at DESC", (family_id,))


def update_appt(family_id, appt_id, **fields):
    if not update("appointments", fields, "id=? AND family_id=?", (appt_id, family_id), APPT_FIELDS):
        raise NotFound("appointment")


def append_question(family_id, appt_id, line):
    """Atomic append so two people adding questions at once don't overwrite each other."""
    with tx() as c:
        row = q1("SELECT questions FROM appointments WHERE id=? AND family_id=?", (appt_id, family_id), c)
        if not row:
            raise NotFound("appointment")
        text = (row["questions"] + "\n" if row["questions"] else "") + line
        update("appointments", {"questions": text}, "id=?", (appt_id,), {"questions"}, c)


# ---------------------------------------------------------------- check-ins
def set_checkin(family_id, day, mood, note=""):
    x("""INSERT INTO checkins(family_id,day,mood,note) VALUES(?,?,?,?)
         ON CONFLICT(family_id,day) DO UPDATE SET mood=excluded.mood, note=excluded.note""",
      (family_id, day, int(mood), crypto.enc(note)))


def checkins(family_id, days=14, today=None):
    since = ((today or clock.today()) - timedelta(days=days)).isoformat()
    return q("SELECT * FROM checkins WHERE family_id=? AND day>=? ORDER BY day", (family_id, since))


# ---------------------------------------------------------------- stats
def adherence(family_id, days=7, today=None):
    """Confirmed-taken rate over resolved doses. 'unconfirmed' doses count as not confirmed and are reported separately."""
    today = today or clock.today()
    start = (today - timedelta(days=days - 1)).isoformat()
    rows = q("""SELECT d.scheduled_date, d.status, m.name FROM doses d JOIN medications m ON m.id=d.med_id
                WHERE d.family_id=? AND d.scheduled_date>=? AND d.scheduled_date<=? AND d.status!='pending'""",
             (family_id, start, today.isoformat()))
    taken = sum(r["status"] == "taken" for r in rows)
    per_day, per_med = {}, {}
    for r in rows:
        d = per_day.setdefault(r["scheduled_date"], [0, 0])
        d[1] += 1
        d[0] += r["status"] == "taken"
        m = per_med.setdefault(r["name"], [0, 0])
        m[1] += 1
        m[0] += r["status"] == "taken"
    streak = 0
    for i in range(0, 60):
        t = per_day.get((today - timedelta(days=i)).isoformat())
        if t is None:
            if i == 0:
                continue  # today may not have resolved doses yet
            break
        if t[0] == t[1]:
            streak += 1
        else:
            break
    return {"pct": round(100 * taken / len(rows)) if rows else None, "streak": streak, "per_day": per_day,
            "per_med": per_med, "unconfirmed": sum(r["status"] == "unconfirmed" for r in rows)}


# ---------------------------------------------------------------- leases & heartbeats (scheduler coordination)
def acquire_lease(name, owner, ttl, now):
    with tx() as c:
        row = c.execute("SELECT owner, expires_at FROM leases WHERE name=?", (name,)).fetchone()
        if row and row["owner"] != owner and row["expires_at"] > now:
            return False
        c.execute("INSERT INTO leases(name,owner,expires_at) VALUES(?,?,?) "
                  "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at",
                  (name, owner, now + ttl))
        return True


def release_lease(name, owner):
    x("DELETE FROM leases WHERE name=? AND owner=?", (name, owner))


def heartbeat(name, at, detail=""):
    x("INSERT INTO heartbeats(name,at,detail) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET at=excluded.at, detail=excluded.detail",
      (name, at, detail))


def get_heartbeat(name):
    return q1("SELECT * FROM heartbeats WHERE name=?", (name,))
