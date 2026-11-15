"""Sign-in, sessions and access codes.

Access codes
  12 characters from a 31-letter alphabet without look-alikes, shown as XXXX-XXXX-XXXX (about 59 bits).
  The first 4 characters are a public lookup handle (selector); the full code is checked against a salted
  scrypt hash. Codes are never stored or shown again after they are issued.
  Codes from before this upgrade (6 characters) work once through a keyed lookup and must be replaced.

Brute-force protection
  - 5 wrong codes for one member within 15 minutes pauses that member's code-based sign-in for 15 minutes,
    and the member is emailed.
  - Per-browser-session and per-IP failure limits (IP is a soft signal: it can be shared or spoofed).
  - When failures spike across the whole app, every attempt is slowed down.
  - Throttle checks run before the expensive hash check. Unknown codes still run a dummy hash (equal timing).

Two-step sign-in (MFA)
  Roles listed in MFA_ROLES (default: caretaker in production) and members who opt in must also enter a
  6-digit code emailed to them. Email-only sign-in is not offered to those members.

Sessions
  Random tokens, stored hashed in the database. Idle timeout (SESSION_IDLE_MIN) and absolute lifetime
  (SESSION_MAX_HOURS). Removing a member, resetting their code or "sign out everywhere" ends their sessions.
"""
from __future__ import annotations

import hmac
import re
import secrets
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import config
import crypto
import db
from typedefs import Actor, Member

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SELECTOR_LEN, CODE_LEN, LEGACY_LEN = 4, 12, 6
OTP_LEN = 6


def _setting(name: str) -> int:
    """A sign-in limit from the configuration, kept within safe bounds."""
    defaults = {
        "LOGIN_MAX_FAILURES": (5, 3, 20), "LOCKOUT_MIN": (15, 1, 1440), "CLIENT_MAX_FAILURES": (30, 5, 1000),
        "SESSION_MAX_FAILURES": (10, 3, 100), "GLOBAL_SLOWDOWN_FAILURES": (200, 10, 100000),
        "SESSION_IDLE_MIN": (30, 5, 720), "SESSION_MAX_HOURS": (12, 1, 168), "OTP_TTL_MIN": (10, 2, 30),
        "OTP_MAX_ATTEMPTS": (5, 3, 10), "OTP_MAX_SENDS": (3, 1, 10), "SIGNUP_MAX_PER_HOUR": (5, 1, 100),
    }
    d, lo, hi = defaults[name]
    return config.get_int(name, d, lo, hi)


@dataclass
class Result:
    status: str                    # ok | mfa | rotate | choose | invalid | throttled | unavailable | sent
    token: str = ""
    member: Member | None = None
    message: str = ""
    retry_after: float = 0
    dev_code: str = ""             # development only, when SMTP is not configured
    choices: list[dict[str, Any]] = field(default_factory=list)
    pick_token: str = ""


# ---------------------------------------------------------------- codes
def normalize(code: str | None) -> str:
    """Upper-case a code and drop spaces, dashes, dots and underscores."""
    return re.sub(r"[\s\-_.]", "", code or "").upper()


def fmt(code: str) -> str:
    """Show a code in groups of four: XXXX-XXXX-XXXX."""
    code = normalize(code)
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def legacy_lookup(code: str) -> str:
    """The keyed lookup for an old 6-character code."""
    return crypto.index("legacy-code:" + normalize(code))


def _new_code() -> tuple[str, str, str]:
    """A new code, its public selector and its hash."""
    code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    return code, code[:SELECTOR_LEN], crypto.hash_secret(code)


def create_member(family_id: int, name: str, email: str, role: str,
              c: sqlite3.Connection | None = None) -> tuple[int, str]:
    """Insert a member with a fresh access code. Returns (member_id, code shown once)."""
    for _ in range(20):
        code, sel, h = _new_code()
        try:
            return db.insert_member(family_id, name, email, role, sel, h, c), fmt(code)
        except sqlite3.IntegrityError:  # selector collision; try another code
            continue
    raise RuntimeError("could not allocate an access code")


def set_new_code(member_id: int, c: sqlite3.Connection | None = None) -> str:
    """Replace a member's code (and any old 6-character code). Returns the new code, shown once."""
    for _ in range(20):
        code, sel, h = _new_code()
        try:
            db.x("""UPDATE members SET selector=?, code_hash=?, legacy_lookup=NULL, must_rotate=0, locked_until=0,
                    code_changed_at=? WHERE id=?""", (sel, h, time.strftime("%Y-%m-%dT%H:%M:%S"), member_id), c)
            return fmt(code)
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not allocate an access code")


# ---------------------------------------------------------------- rate limiting
def client_buckets(ip: str | None = None, browser_session: str | None = None) -> list[str]:
    """Rate-limit buckets for a client: keyed hashes of its IP address and browser session."""
    out = []
    if ip:
        out.append("ip:" + crypto.index("ip:" + ip)[:24])
    if browser_session:
        out.append("bs:" + crypto.index("bs:" + browser_session)[:24])
    return out


def _count(bucket: str, since: float) -> int:
    """How many events a bucket has had since a time."""
    return db.count("SELECT COUNT(*) AS n FROM rate_events WHERE bucket=? AND at>?", (bucket, since))


def _record(buckets: Iterable[str], now: float) -> None:
    """Record one event in each bucket."""
    with db.tx() as c:
        for b in buckets:
            c.execute("INSERT INTO rate_events(bucket, at) VALUES(?,?)", (b, now))


def _throttled(clients: Iterable[str], now: float) -> int:
    """Seconds to wait if any of the client's buckets has had too many failures, else 0."""
    window = _setting("LOCKOUT_MIN") * 60
    for b in clients:
        limit = _setting("CLIENT_MAX_FAILURES") if b.startswith("ip:") else _setting("SESSION_MAX_FAILURES")
        if _count("fail:" + b, now - window) >= limit:
            return window
    return 0


def _global_slowdown(now: float) -> None:
    """Slow every attempt down while failures spike across the whole app."""
    if _count("fail:global", now - 600) >= _setting("GLOBAL_SLOWDOWN_FAILURES"):
        time.sleep(2)


def _fail(member: Member | None, clients: Sequence[str], now: float) -> None:
    """Count a failed sign-in, and pause a member's code after too many (emailing them)."""
    _record(["fail:global", *("fail:" + b for b in clients)] + ([f"fail:m:{member['id']}"] if member else []), now)
    if not member:
        return
    db.security_event("login_failed", member["family_id"], member["id"])
    window = _setting("LOCKOUT_MIN") * 60
    if _count(f"fail:m:{member['id']}", now - window) >= _setting("LOGIN_MAX_FAILURES"):
        db.x("UPDATE members SET locked_until=? WHERE id=?", (now + window, member["id"]))
        db.security_event("lockout", member["family_id"], member["id"], f"{_setting('LOCKOUT_MIN')} min")
        import notifier
        notifier.security_alert(member, "CareCircle: sign-in paused",
                                f"Hello {member['name']},\n\nSomeone entered a wrong CareCircle access code for your "
                                f"account {_setting('LOGIN_MAX_FAILURES')} times, so sign-in with your code is paused "
                                f"for {_setting('LOCKOUT_MIN')} minutes.\n\nIf this was not you, ask the elder or a "
                                "caretaker to give you a new code from the Circle tab.\n",
                                f"lockout-{member['id']}-{int(now // window)}")


def signup_allowed(clients: Sequence[str], now: float | None = None) -> bool:
    """True if this client may create another care circle this hour."""
    now = now or time.time()
    return all(_count("signup:" + b, now - 3600) < _setting("SIGNUP_MAX_PER_HOUR") for b in clients)


def record_signup(clients: Sequence[str], now: float | None = None) -> None:
    """Count a new care circle against the client's hourly limit."""
    _record(["signup:" + b for b in clients], now or time.time())


# ---------------------------------------------------------------- sign in with an access code
GENERIC_INVALID = "That code didn't work. Check it and try again, or use 'Email me a code'."
GENERIC_THROTTLED = "Too many attempts. Please wait 15 minutes, or use 'Email me a code'."


def login_with_code(code: str | None, clients: Sequence[str] = (), now: float | None = None) -> Result:
    """Sign in with an access code, with throttling and equal timing for unknown codes."""
    now = now or time.time()
    if _throttled(clients, now):
        return Result("throttled", message=GENERIC_THROTTLED, retry_after=_setting("LOCKOUT_MIN") * 60)
    _global_slowdown(now)
    c = normalize(code)
    member = None
    if len(c) == CODE_LEN and all(ch in ALPHABET for ch in c):
        member = db.q1("SELECT * FROM members WHERE selector=?", (c[:SELECTOR_LEN],))
    elif len(c) == LEGACY_LEN:
        member = db.q1("SELECT * FROM members WHERE legacy_lookup=?", (legacy_lookup(c),))
    if member and (member["locked_until"] or 0) > now:
        _record(["fail:global", *("fail:" + b for b in clients)], now)
        return Result("throttled", message=GENERIC_THROTTLED, retry_after=member["locked_until"] - now)
    ok = crypto.verify_secret(c, member["code_hash"] if member else None)   # runs a full hash even for unknown codes
    if member is None or not ok:
        _fail(member, clients, now)
        return Result("invalid", message=GENERIC_INVALID)
    db.x("DELETE FROM rate_events WHERE bucket=?", (f"fail:m:{member['id']}",))
    db.security_event("login_code_ok", member["family_id"], member["id"], "legacy code" if len(c) == LEGACY_LEN else "")
    return _begin(member, now)


def needs_mfa(member: Member) -> bool:
    """True if this member must also enter an emailed code."""
    return member["role"] in config.mfa_roles() or bool(member.get("mfa"))


def _begin(member: Member, now: float) -> Result:
    """Start a session after the first factor: straight in, a new code first, or the second step."""
    if needs_mfa(member):
        if not member["email"]:
            db.security_event("mfa_unavailable", member["family_id"], member["id"])
            return Result("unavailable", message="Your role needs a code sent to your email, but no email is on file. "
                                                 "Ask the elder or a caretaker to add your email in the Circle tab.")
        tok = create_session(member, "mfa", now)
        dev = _send_otp(member, "mfa", crypto.token_hash(tok), now)
        return Result("mfa", token=tok, member=member, dev_code=dev or "",
                      message="We emailed you a 6-digit code. It expires in 10 minutes.")
    stage = "rotate" if member["must_rotate"] else "full"
    tok = create_session(member, stage, now)
    return Result("rotate" if stage == "rotate" else "ok", token=tok, member=member)


# ---------------------------------------------------------------- sessions
def create_session(member: Member, stage: str, now: float | None = None) -> str:
    """Store a new session (only its hash) and return the token for the browser."""
    now = now or time.time()
    tok = crypto.token()
    ttl = _setting("OTP_TTL_MIN") * 60 if stage == "mfa" else _setting("SESSION_MAX_HOURS") * 3600
    db.x("INSERT INTO sessions(token_hash,member_id,family_id,stage,created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?,?)",
         (crypto.token_hash(tok), member["id"], member["family_id"], stage, now, now, now + ttl))
    if stage == "full":
        db.x("UPDATE members SET last_login_at=? WHERE id=?", (time.strftime("%Y-%m-%dT%H:%M:%S"), member["id"]))
    return tok


def current(tok: str | None, now: float | None = None) -> Actor | None:
    """The signed-in actor for this token, or None. Re-reads the member every time, so role changes and removals apply at once."""
    if not tok:
        return None
    now = now or time.time()
    th = crypto.token_hash(tok)
    row = db.q1("""SELECT s.*, m.name, m.role, m.email, m.lang, m.large_text, m.mfa, m.must_rotate,
                          f.name AS family_name, f.elder_name, f.is_demo
                   FROM sessions s JOIN members m ON m.id=s.member_id AND m.family_id=s.family_id
                   JOIN families f ON f.id=m.family_id WHERE s.token_hash=?""", (th,))
    if not row or row["revoked"]:
        return None
    if now > row["expires_at"] or now - row["last_seen"] > _setting("SESSION_IDLE_MIN") * 60:
        db.x("UPDATE sessions SET revoked=1 WHERE token_hash=?", (th,))
        db.security_event("session_expired", row["family_id"], row["member_id"])
        return None
    if now - row["last_seen"] > 30:
        db.x("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, th))
    return {"id": row["member_id"], "member_id": row["member_id"], "family_id": row["family_id"],
            "role": row["role"], "name": row["name"], "email": row["email"], "lang": row["lang"] or "en",
            "large_text": bool(row["large_text"]), "mfa": bool(row["mfa"]), "family_name": row["family_name"],
            "elder_name": row["elder_name"], "is_demo": bool(row["is_demo"]), "stage": row["stage"],
            "token_hash": th, "expires_at": row["expires_at"]}


def _set_stage(th: str, stage: str, now: float) -> None:
    """Move a session to another stage and give it a full lifetime."""
    ttl = _setting("SESSION_MAX_HOURS") * 3600
    db.x("UPDATE sessions SET stage=?, expires_at=?, last_seen=? WHERE token_hash=?", (stage, now + ttl, now, th))


def sign_out(tok: str | None) -> None:
    """End this session."""
    if tok:
        db.x("UPDATE sessions SET revoked=1 WHERE token_hash=?", (crypto.token_hash(tok),))


def revoke_member_sessions(member_id: int, keep_token_hash: str | None = None) -> int:
    """End a member's sessions (except one, if given). Returns how many."""
    return db.x("UPDATE sessions SET revoked=1 WHERE member_id=? AND token_hash!=?", (member_id, keep_token_hash or ""))


def revoke_all_sessions() -> int:
    """Sign everyone out. Returns how many sessions ended."""
    return db.x("UPDATE sessions SET revoked=1 WHERE revoked=0")


# ---------------------------------------------------------------- one-time codes
def _otp_hash(subject: str, purpose: str, challenge: str, code: str) -> str:
    """How a one-time code is stored: a keyed hash bound to who, why and which challenge."""
    return crypto.index(f"otp:{subject}:{purpose}:{challenge}:{code}")


def _otp_create(subject: str, purpose: str, challenge: str, now: float) -> str | None:
    """A new one-time code (older ones stop working), or None after too many sends."""
    sends = db.count("SELECT COUNT(*) AS n FROM otp_codes WHERE subject=? AND purpose=? AND created_at>?",
                     (subject, purpose, now - 900))
    if sends >= _setting("OTP_MAX_SENDS"):
        return None
    db.x("UPDATE otp_codes SET used=1 WHERE subject=? AND purpose=? AND used=0", (subject, purpose))
    code = f"{secrets.randbelow(10 ** OTP_LEN):0{OTP_LEN}d}"
    db.x("INSERT INTO otp_codes(subject,purpose,challenge,code_hash,created_at,expires_at) VALUES(?,?,?,?,?,?)",
         (subject, purpose, challenge, _otp_hash(subject, purpose, challenge, code), now, now + _setting("OTP_TTL_MIN") * 60))
    return code


def _otp_verify(purpose: str, challenge: str, code: str | None, now: float) -> str | None:
    """Returns the subject on success. Each code allows OTP_MAX_ATTEMPTS tries."""
    code = re.sub(r"\D", "", code or "")
    row = db.q1("""SELECT * FROM otp_codes WHERE challenge=? AND purpose=? AND used=0 AND expires_at>?
                   ORDER BY id DESC LIMIT 1""", (challenge, purpose, now))
    if not row or row["attempts"] >= _setting("OTP_MAX_ATTEMPTS"):
        return None
    if len(code) == OTP_LEN and hmac.compare_digest(row["code_hash"], _otp_hash(row["subject"], purpose, challenge, code)):
        db.x("UPDATE otp_codes SET used=1 WHERE id=?", (row["id"],))
        return row["subject"]
    db.x("UPDATE otp_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
    return None


def _otp_exhausted(purpose: str, challenge: str, now: float) -> bool:
    """True when no attempts are left for this challenge."""
    row = db.q1("SELECT attempts FROM otp_codes WHERE challenge=? AND purpose=? AND used=0 AND expires_at>? "
                "ORDER BY id DESC LIMIT 1", (challenge, purpose, now))
    return not row or row["attempts"] >= _setting("OTP_MAX_ATTEMPTS")


def _dev_visible() -> bool:
    """Show one-time codes on screen only in local development without email."""
    return config.env() == "development" and not config.smtp_configured()


def _send_otp(member: Member, purpose: str, challenge: str, now: float) -> str | None:
    """Email a sign-in code. Returns it only for local development, '' otherwise, None if throttled."""
    import notifier
    code = _otp_create(f"m:{member['id']}", purpose, challenge, now)
    if not code:
        return None
    notifier.send_secret(member["family_id"], member["email"], "Your CareCircle sign-in code",
                         f"Hello {member['name']},\n\nYour CareCircle sign-in code is {code}. It expires in "
                         f"{_setting('OTP_TTL_MIN')} minutes.\n\nIf you did not try to sign in, someone may know "
                         "your access code: ask the elder or a caretaker for a new one.\n", "otp_mfa", member["id"])
    return code if _dev_visible() else ""


def verify_mfa(tok: str, code: str | None, now: float | None = None) -> Result:
    """Check the emailed code for the second sign-in step."""
    now = now or time.time()
    s = current(tok, now)
    if not s or s["stage"] != "mfa":
        return Result("invalid", message="That sign-in has expired. Start again.")
    subject = _otp_verify("mfa", s["token_hash"], code, now)
    if subject != f"m:{s['member_id']}":
        db.security_event("mfa_failed", s["family_id"], s["member_id"])
        if _otp_exhausted("mfa", s["token_hash"], now):
            sign_out(tok)
            return Result("invalid", message="Too many wrong codes. Start again.")
        return Result("invalid", message="That code didn't match. Check the latest email and try again.")
    db.security_event("mfa_ok", s["family_id"], s["member_id"])
    member = db.get_member(s["member_id"]) or {}
    stage = "rotate" if s.get("must_rotate") or member.get("must_rotate") else "full"
    _set_stage(s["token_hash"], stage, now)
    db.x("UPDATE members SET last_login_at=? WHERE id=?", (time.strftime("%Y-%m-%dT%H:%M:%S"), s["member_id"]))
    return Result("rotate" if stage == "rotate" else "ok", token=tok)


def resend_mfa(tok: str, now: float | None = None) -> Result:
    """Send a new code for the second sign-in step (limited per 15 minutes)."""
    now = now or time.time()
    s = current(tok, now)
    if not s or s["stage"] != "mfa":
        return Result("invalid", message="That sign-in has expired. Start again.")
    member = db.get_member(s["member_id"])
    if not member:
        return Result("invalid", message="That sign-in has expired. Start again.")
    dev = _send_otp(member, "mfa", s["token_hash"], now)
    if dev is None:
        return Result("throttled", message="We've sent several codes already. Wait 15 minutes and try again.")
    return Result("sent", dev_code=dev, message="We sent a new code.")


# ---------------------------------------------------------------- sign in by email (alternative and recovery)
EMAIL_SENT = ("If that email belongs to someone in a care circle, we've sent a 6-digit code to it. "
              "It expires in 10 minutes.")


def _email_eligible(idx: str) -> list[Member]:
    """Members with this email who may sign in by email (not those who need two-step sign-in)."""
    rows = db.q("""SELECT m.*, f.name AS family_name FROM members m JOIN families f ON f.id=m.family_id
                   WHERE m.email_index=?""", (idx,)) if idx else []
    return [m for m in rows if not needs_mfa(m)]


def email_signin_start(email: str | None, clients: Sequence[str] = (), now: float | None = None) -> Result:
    """Always answers the same way, so it cannot be used to find out which emails are registered."""
    now = now or time.time()
    challenge = crypto.token()
    if _throttled(clients, now) or not all(_count("emailreq:" + b, now - 900) < 5 for b in clients):
        return Result("throttled", message=GENERIC_THROTTLED)
    _record(["emailreq:" + b for b in clients], now)
    idx = crypto.email_index(email)
    eligible = _email_eligible(idx)
    dev = ""
    if eligible:
        code = _otp_create(f"e:{idx}", "email_signin", crypto.token_hash(challenge), now)
        if code:
            import notifier
            notifier.send_secret(eligible[0]["family_id"], (email or "").strip(), "Your CareCircle sign-in code",
                                 f"Your CareCircle sign-in code is {code}. It expires in {_setting('OTP_TTL_MIN')} "
                                 "minutes.\n\nIf you did not ask for this, you can ignore this email.\n",
                                 "otp_email", eligible[0]["id"])
            db.security_event("email_code_sent", eligible[0]["family_id"], eligible[0]["id"])
            dev = code if _dev_visible() else ""
    return Result("sent", token=challenge, message=EMAIL_SENT, dev_code=dev)


def email_signin_verify(challenge: str | None, code: str | None, clients: Sequence[str] = (),
                        now: float | None = None) -> Result:
    """Check an emailed sign-in code; ask which circle if the email is in several."""
    now = now or time.time()
    if _throttled(clients, now):
        return Result("throttled", message=GENERIC_THROTTLED)
    subject = _otp_verify("email_signin", crypto.token_hash(challenge or ""), code, now)
    if not subject or not subject.startswith("e:"):
        _record(["fail:global", *("fail:" + b for b in clients)], now)
        return Result("invalid", message="That code didn't match or has expired.")
    eligible = _email_eligible(subject[2:])
    if not eligible:
        return Result("invalid", message="That code didn't match or has expired.")
    if len(eligible) == 1:
        db.security_event("login_email_ok", eligible[0]["family_id"], eligible[0]["id"])
        return _begin(eligible[0], now)
    pick = crypto.token()
    db.x("INSERT INTO otp_codes(subject,purpose,challenge,code_hash,created_at,expires_at) VALUES(?,?,?,?,?,?)",
         (subject, "pick", crypto.token_hash(pick), "-", now, now + 300))
    return Result("choose", pick_token=pick,
                  choices=[{"id": m["id"], "family_name": m["family_name"], "role": m["role"], "name": m["name"]}
                           for m in eligible])


def email_signin_pick(pick_token: str | None, member_id: int, now: float | None = None) -> Result:
    """Finish email sign-in for the circle the person chose."""
    now = now or time.time()
    row = db.q1("SELECT * FROM otp_codes WHERE challenge=? AND purpose='pick' AND used=0 AND expires_at>?",
                (crypto.token_hash(pick_token or ""), now))
    if not row:
        return Result("invalid", message="That sign-in has expired. Start again.")
    m = next((m for m in _email_eligible(row["subject"][2:]) if m["id"] == member_id), None)
    if not m:
        return Result("invalid", message="That choice isn't available.")
    db.x("UPDATE otp_codes SET used=1 WHERE id=?", (row["id"],))
    db.security_event("login_email_ok", m["family_id"], m["id"])
    return _begin(m, now)


# ---------------------------------------------------------------- changing codes
def rotate_own_code(tok: str, now: float | None = None) -> str:
    """The signed-in member takes a new code (shown once). Ends their other sessions."""
    now = now or time.time()
    s = current(tok, now)
    if not s or s["stage"] not in ("full", "rotate"):
        raise PermissionError("not signed in")
    code = set_new_code(s["member_id"])
    revoke_member_sessions(s["member_id"], keep_token_hash=s["token_hash"])
    _set_stage(s["token_hash"], "full", now)
    db.security_event("code_changed", s["family_id"], s["member_id"], "by self")
    return code


def reset_code(family_id: int, member_id: int) -> str:
    """Issue a new code for a member of this family and end their sessions. Permission is checked by services."""
    db.member_in_family(family_id, member_id)
    code = set_new_code(member_id)
    revoke_member_sessions(member_id)
    return code
