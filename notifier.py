"""Email reminders.

queue (deduplicated, encrypted) -> dispatch (claimed atomically, retried with backoff, expires when no longer useful).

Reliability
- Only one process runs the reminder pass at a time: it must hold the "reminders" lease in the database.
  Run it inside the web app (SCHEDULER_MODE=thread) or, better, as `python worker.py` (SCHEDULER_MODE=external).
- Each outbox row is claimed by one dispatcher before sending, so two instances never send the same email.
  A claim that is not finished within 2 minutes (crashed worker) is picked up again: delivery is at-least-once.
- Failed sends are retried with exponential backoff up to EMAIL_MAX_ATTEMPTS, then marked 'dead' and shown to caretakers.
- Time-sensitive emails (dose due, appointment) carry an expiry and are dropped rather than sent late.
- Every pass writes a heartbeat; the app shows caretakers when reminders have stopped running.

Privacy
- By default ("minimal") emails say that something is due without naming medicines, doses or doctors.
  A family can switch to "full" detail in Settings.
- Sign-in codes are sent directly and never stored: the outbox keeps only a redacted record.
"""
import html
import logging
import os
import re
import smtplib
import socket
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr

import clock
import config
import crypto
import db

log = logging.getLogger("carecircle.notifier")

LEAD_MIN = 15          # remind this many minutes before a dose
GRACE_MIN = 30         # a dose becomes "unconfirmed" this long after its time
STALE_MIN = 360        # don't alert about unconfirmed doses older than this (app was off)
DIGEST_HOUR = 20       # evening summary for family
CLAIM_SEC = 120
EMAIL_RE = re.compile(r"^[^@\s<>,;:\"]+@[^@\s<>,;:\"]+\.[^@\s<>,;:\"]{2,}$")
TEST_DOMAINS = ("@example.com", "@example.org", "@example.net", ".test", ".invalid")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def max_attempts():
    return config.get_int("EMAIL_MAX_ATTEMPTS", 5, lo=1, hi=20)


def backoff(attempts):
    return min(60 * 2 ** max(0, attempts - 1), 3600)


def valid_email(e):
    return bool(e and len(e) <= 254 and EMAIL_RE.match(e.strip()))


def smtp_configured():
    return config.smtp_configured()


def _is_test_address(to):
    return (to or "").lower().endswith(TEST_DOMAINS)


def _html(body):
    safe = html.escape(body).replace("\n", "<br>")
    return (f"<div style='font-family:Arial,sans-serif;font-size:18px;line-height:1.5;max-width:560px'>"
            f"<div style='background:#2563eb;color:#fff;padding:10px 16px;border-radius:10px 10px 0 0;font-weight:bold'>"
            f"CareCircle</div><div style='border:1px solid #ddd;border-top:0;padding:16px;border-radius:0 0 10px 10px'>"
            f"{safe}</div></div>")


def _send_smtp(to, subject, body):
    user = config.get("SMTP_USER")
    msg = EmailMessage()
    msg["From"] = formataddr(("CareCircle", config.get("SMTP_FROM") or user or "carecircle@localhost"))
    msg["To"] = to
    msg["Subject"] = re.sub(r"[\r\n]+", " ", subject)[:200]
    msg.set_content(body)
    msg.add_alternative(_html(body), subtype="html")
    host = config.get("SMTP_HOST", "smtp.gmail.com")
    sec = config.get("SMTP_SECURITY", "ssl").lower()
    ctx = ssl.create_default_context()
    timeout = config.get_int("SMTP_TIMEOUT_SEC", 20, lo=5, hi=120)
    if sec == "ssl":
        server = smtplib.SMTP_SSL(host, int(config.get("SMTP_PORT", "465")), timeout=timeout, context=ctx)
    else:
        server = smtplib.SMTP(host, int(config.get("SMTP_PORT", "587" if sec == "starttls" else "25")), timeout=timeout)
        if sec == "starttls":
            server.starttls(context=ctx)
    try:
        if user and config.get("SMTP_PASS"):
            server.login(user, config.get("SMTP_PASS").replace(" ", ""))  # Gmail shows app passwords with spaces
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _describe(e):
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "SMTP login rejected. For Gmail use a 16-character App Password."
    return f"{type(e).__name__}: {str(e)[:200]}"


def send_test(to):
    """Send immediately. Returns (ok, error_message)."""
    if not valid_email(to):
        return False, "That doesn't look like a valid email address."
    if not smtp_configured():
        return False, "SMTP isn't configured yet."
    try:
        _send_smtp(to, "CareCircle test email", "This is a test from CareCircle. If you can read this, reminders will reach you.\n")
        return True, ""
    except Exception as e:
        return False, _describe(e)


# ---------------------------------------------------------------- queue
def queue(family_id, member, subject, body, kind, key, expires_in=None, priority=0):
    """Queue one email for a member, once per dedupe key. Returns the outbox id, or None if skipped."""
    to = (member.get("email") or "").strip()
    if not valid_email(to):
        return None
    dedupe = crypto.index(f"dedupe:{key}|m{member['id']}")
    with db.tx() as c:
        if c.execute("SELECT 1 FROM outbox WHERE dedupe_key=?", (dedupe,)).fetchone():
            return None
        return db.insert("outbox", {
            "family_id": family_id, "member_id": member["id"], "to_email": to, "subject": subject, "body": body,
            "kind": kind, "dedupe_key": dedupe, "status": "pending", "priority": priority, "next_attempt_at": 0,
            "expires_at": (time.time() + expires_in) if expires_in else None}, c)


def queue_many(family_id, members, subject, body, kind, key, expires_in=None, priority=0):
    return [i for i in (queue(family_id, m, subject, body, kind, key, expires_in, priority) for m in members) if i]


def send_secret(family_id, to, subject, body, kind, member_id):
    """Send a message containing a secret (sign-in code) straight away. Only a redacted copy is stored."""
    status, err = "simulated", ""
    if config.smtp_configured() and not _is_test_address(to):
        try:
            _send_smtp(to, subject, body)
            status = "sent"
        except Exception as e:
            status, err = "dead", _describe(e)
            log.warning("sign-in code email failed", extra={"family_id": family_id, "kind": kind})
    db.insert("outbox", {"family_id": family_id, "member_id": member_id, "to_email": to, "subject": subject,
                         "body": "[The sign-in code is not stored.]", "kind": kind,
                         "dedupe_key": crypto.index("secret:" + crypto.token()), "status": status, "error": err,
                         "attempts": 1, "sent_at": clock.now().isoformat(timespec="seconds")})
    return status in ("sent", "simulated")


def security_alert(member, subject, body, key):
    return queue(member["family_id"], member, subject, body, "security", key, priority=5)


def dispatch(owner=None, family_id=None, only_ids=None, now=None, limit=100):
    """Claim and send due emails. Returns the number processed."""
    now = now or time.time()
    owner = owner or WORKER_ID
    db.x("""UPDATE outbox SET status='expired' WHERE expires_at IS NOT NULL AND expires_at<?
            AND (status='pending' OR (status='sending' AND claimed_until<?))""", (now, now))
    where = "((status='pending' AND next_attempt_at<=?) OR (status='sending' AND claimed_until<?))"
    args = [now, now]
    if family_id is not None:
        where += " AND family_id=?"
        args.append(family_id)
    if only_ids:
        where += f" AND id IN ({','.join('?' * len(only_ids))})"
        args += list(only_ids)
    with db.tx() as c:
        ids = [r["id"] for r in c.execute(f"SELECT id FROM outbox WHERE {where} ORDER BY priority DESC, id LIMIT ?",
                                          (*args, limit))]
        if ids:
            c.execute(f"UPDATE outbox SET status='sending', claimed_by=?, claimed_until=? WHERE id IN ({','.join('?' * len(ids))})",
                      (owner, now + CLAIM_SEC, *ids))
    if not ids:
        return 0
    rows = db.q(f"SELECT * FROM outbox WHERE claimed_by=? AND status='sending' AND id IN ({','.join('?' * len(ids))})",
                (owner, *ids))
    live = config.smtp_configured()
    allowed = {"status", "error", "sent_at", "attempts", "next_attempt_at", "claimed_by", "claimed_until"}
    for r in rows:
        attempts = (r["attempts"] or 0) + 1
        vals = {"attempts": attempts, "claimed_by": None, "claimed_until": 0, "error": ""}
        if not live or _is_test_address(r["to_email"]):
            vals.update(status="simulated", sent_at=clock.now().isoformat(timespec="seconds"))
        else:
            try:
                _send_smtp(r["to_email"], r["subject"], r["body"])
                vals.update(status="sent", sent_at=clock.now().isoformat(timespec="seconds"))
            except Exception as e:
                gave_up = attempts >= max_attempts()
                vals.update(status="dead" if gave_up else "pending", error=_describe(e),
                            next_attempt_at=time.time() + backoff(attempts))
                log.warning("email send failed", extra={"family_id": r["family_id"], "kind": r["kind"],
                                                        "attempts": attempts, "gave_up": gave_up})
        db.update("outbox", vals, "id=? AND claimed_by=?", (r["id"], owner), allowed)
    return len(rows)


def delivery_problems(family_id, hours=24):
    """Emails that gave up in the last `hours` (created_at is stored in UTC by SQLite)."""
    return db.q1("SELECT COUNT(*) AS n FROM outbox WHERE family_id=? AND status='dead' AND created_at>=datetime('now', ?)",
                 (family_id, f"-{int(hours)} hours"))["n"]


# ---------------------------------------------------------------- reminder content
def _fmt(t):
    return datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")


def _first(name):
    return (name or "").split()[0] if name else "there"


def _secs_until(target, now):
    return max(60, (target - now).total_seconds())


def _dose_due_email(elder, slot, doses, full):
    if full:
        lines = "\n".join(f"- {d['name']} {d['dosage']}".rstrip() + (f" ({d['instructions']})" if d["instructions"] else "")
                          for d in doses)
        return (f"Time for your {_fmt(slot)} medicine",
                f"Hello {_first(elder)},\n\nIt's time for:\n{lines}\n\nOpen CareCircle and tap 'Taken' once you have it.\n")
    n = len(doses)
    return ("CareCircle: medicine reminder",
            f"Hello {_first(elder)},\n\nIt's time for your {_fmt(slot)} medicine{'s' if n > 1 else ''} ({n}).\n\n"
            "Open CareCircle to see which, and tap 'Taken' once you have it.\n")


def _unconfirmed_email(elder, slot, doses, full):
    what = (", ".join(f"{d['name']} {d['dosage']}".strip() for d in doses) if full
            else f"{len(doses)} medicine{'s' if len(doses) > 1 else ''}")
    return ("CareCircle: a dose hasn't been confirmed",
            f"{_first(elder)} has not confirmed {what} due at {_fmt(slot)}.\n\n"
            "This does not mean it was missed: they may have taken it and not tapped 'Taken'. Please check in with them, "
            "then mark it in CareCircle.\n")


def _refill_email(elder, low, full):
    if full:
        lines = "\n".join(f"- {m['name']}: {m['stock']} doses left" for m in low)
        return "CareCircle: refill needed", f"These medicines for {_first(elder)} are running low:\n{lines}\n\nTime to order a refill.\n"
    return ("CareCircle: refill reminder",
            f"{len(low)} of {_first(elder)}'s medicines {'are' if len(low) > 1 else 'is'} running low. "
            "Open CareCircle to see which, and order a refill.\n")


def _appt_email(elder, a, when, full, soon):
    head = "Appointment in under 2 hours" if soon else "Appointment tomorrow"
    if full:
        body = (f"{_first(elder)} has an appointment with {a['doctor']}"
                f"{(' (' + a['specialty'] + ')') if a['specialty'] else ''} on {when}"
                f"{(' at ' + a['location']) if a['location'] else ''}.\n")
        if a["questions"]:
            body += f"\nQuestions to ask:\n{a['questions']}\n"
        return f"{head}: {a['doctor']}", body
    return f"CareCircle: {head.lower()}", f"{_first(elder)} has a doctor's appointment on {when}. Open CareCircle for the details and questions to ask.\n"


# ---------------------------------------------------------------- the reminder pass
def _family_pass(fid, now):
    fam = db.get_family(fid)
    elder = fam["elder_name"]
    full = db.family_settings(fid)["email_detail"] == "full"
    elder_rcpt = db.members(fid, roles=["elder"], with_email=True)
    everyone = db.members(fid, with_email=True)
    carers = db.members(fid, roles=["caretaker", "family"], with_email=True)
    day = now.date().isoformat()

    # yesterday's leftovers (app was off): resolve silently so adherence stays honest
    for d in db.doses_for_day(fid, (now.date() - timedelta(days=1)).isoformat()):
        if d["status"] == "pending":
            db.auto_unconfirm(fid, d["id"], now)

    due, late = {}, {}
    for d in db.doses_for_day(fid, day):
        if d["status"] != "pending":
            continue
        sched = datetime.fromisoformat(f"{d['scheduled_date']}T{d['scheduled_time']}")
        mins = (now - sched).total_seconds() / 60
        if -LEAD_MIN <= mins < GRACE_MIN:
            due.setdefault(d["scheduled_time"], []).append(d)
        elif mins >= GRACE_MIN:
            db.auto_unconfirm(fid, d["id"], now)
            if mins < STALE_MIN:
                late.setdefault(d["scheduled_time"], []).append(d)
    for slot, doses in due.items():
        subject, body = _dose_due_email(elder, slot, doses, full)
        deadline = datetime.fromisoformat(f"{day}T{slot}") + timedelta(minutes=GRACE_MIN)
        queue_many(fid, elder_rcpt, subject, body, "dose_due", f"dose-due-{fid}-{day}-{slot}",
                   expires_in=_secs_until(deadline, now))
    for slot, doses in late.items():
        subject, body = _unconfirmed_email(elder, slot, doses, full)
        deadline = datetime.fromisoformat(f"{day}T{slot}") + timedelta(minutes=STALE_MIN)
        queue_many(fid, everyone, subject, body, "dose_unconfirmed", f"dose-unconfirmed-{fid}-{day}-{slot}",
                   expires_in=_secs_until(deadline, now))

    low = [m for m in db.meds(fid) if m["stock"] is not None and m["stock"] <= (m["refill_at"] or 5)]
    if low:
        subject, body = _refill_email(elder, low, full)
        ids = "-".join(str(m["id"]) for m in sorted(low, key=lambda m: m["id"]))
        queue_many(fid, carers + elder_rcpt, subject, body, "low_stock", f"lowstock-{fid}-{day}-{ids}")

    for a in db.appts(fid, now=now):
        start = datetime.fromisoformat(a["when_at"])
        hrs = (start - now).total_seconds() / 3600
        when = start.strftime("%a %d %b, %I:%M %p")
        if 0 < hrs <= 2:
            subject, body = _appt_email(elder, a, when, full, True)
            queue_many(fid, everyone, subject, body, "appt_2h", f"appt2h-{a['id']}", expires_in=_secs_until(start, now))
        elif 2 < hrs <= 24:
            subject, body = _appt_email(elder, a, when, full, False)
            queue_many(fid, everyone, subject, body, "appt_24h", f"appt24h-{a['id']}", expires_in=_secs_until(start, now))

    if now.hour >= DIGEST_HOUR:
        from insights import daily_summary
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        queue_many(fid, carers, f"CareCircle: today's summary for {_first(elder)}", daily_summary(fid, now.date(), full=full),
                   "digest", f"digest-{fid}-{day}", expires_in=_secs_until(midnight, now))


def run_reminders(now=None, family_ids=None):
    """One reminder pass. `now` simulates a time (demo); `family_ids` limits it to some families."""
    now = now or clock.now()
    ids = db.family_ids() if family_ids is None else list(family_ids)
    for fid in ids:
        try:
            _family_pass(fid, now)
        except Exception:  # one family's bad data must not stop everyone else's reminders
            log.exception("reminder pass failed for a family", extra={"family_id": fid})
    return dispatch(family_id=ids[0] if family_ids is not None and len(ids) == 1 else None)


def help_alert(family_id, elder_member):
    """The elder pressed 'I need help'. Email every caretaker and family member now."""
    carers = db.members(family_id, roles=["caretaker", "family"], with_email=True)
    bucket = int(time.time() // 300)
    stamp = clock.now().strftime("%I:%M %p").lstrip("0")
    ids = queue_many(family_id, carers, f"URGENT: {_first(elder_member['name'])} pressed 'I need help'",
                     f"{elder_member['name']} pressed 'I need help' in CareCircle at {stamp}.\n\n"
                     f"Please call or visit them now. If you cannot reach them, call the emergency number "
                     f"({config.get('EMERGENCY_NUMBER', '112')}).\n\n"
                     "CareCircle does not contact emergency services itself.\n",
                     "help", f"help-{family_id}-{bucket}", priority=10)
    if ids:
        dispatch(only_ids=ids)
    sent = db.q(f"SELECT status FROM outbox WHERE id IN ({','.join('?' * len(ids))})", ids) if ids else []
    return {"recipients": len(carers), "delivered": sum(r["status"] in ("sent", "simulated") for r in sent),
            "simulated": any(r["status"] == "simulated" for r in sent)}


def appointment_notice(family_id, appt_id, change):
    """Email the circle straight away when an appointment is added or cancelled.

    An 'added' notice for an appointment within the next 24 h (or 2 h) takes the place of the scheduled
    24 h (or 2 h) reminder, so nobody gets two emails about the same thing at once."""
    fam = db.get_family(family_id)
    a = db.get_appt(family_id, appt_id)
    full = db.family_settings(family_id)["email_detail"] == "full"
    elder = _first(fam["elder_name"])
    start = datetime.fromisoformat(a["when_at"])
    now = clock.now()
    hrs = (start - now).total_seconds() / 3600
    when = start.strftime("%a %d %b, %I:%M %p")
    people = db.members(family_id, with_email=True)
    if change == "added":
        if hrs <= 0:
            return {"recipients": 0, "delivered": 0, "simulated": False}
        key = f"appt2h-{appt_id}" if hrs <= 2 else f"appt24h-{appt_id}" if hrs <= 24 else f"appt-added-{appt_id}"
        if full:
            subject = f"Appointment added: {a['doctor']} on {when}"
            body = (f"A doctor's appointment for {elder} was added:\n\n{a['doctor']}"
                    f"{(' (' + a['specialty'] + ')') if a['specialty'] else ''}\n{when}"
                    f"{chr(10) + a['location'] if a['location'] else ''}\n")
            if a["questions"]:
                body += f"\nQuestions to ask:\n{a['questions']}\n"
        else:
            subject = "CareCircle: doctor's appointment added"
            body = f"A doctor's appointment for {elder} was added for {when}.\n\nOpen CareCircle for the details.\n"
        body += "\nCareCircle will also remind you 24 hours and 2 hours before.\n" if hrs > 24 else ""
        ids = queue_many(family_id, people, subject, body, "appt_added", key,
                         expires_in=_secs_until(start, now), priority=5)
    else:
        subject = (f"Appointment cancelled: {a['doctor']} on {when}" if full
                   else "CareCircle: doctor's appointment cancelled")
        body = f"The doctor's appointment for {elder} on {when} was cancelled.\n"
        ids = queue_many(family_id, people, subject, body, "appt_cancelled", f"appt-cancelled-{appt_id}", priority=5)
    if ids:
        dispatch(only_ids=ids)
    rows = db.q(f"SELECT status FROM outbox WHERE id IN ({','.join('?' * len(ids))})", ids) if ids else []
    n = {st: sum(r["status"] == st for r in rows) for st in ("sent", "simulated")}
    return {"recipients": len(people), "queued": len(ids), "sent": n["sent"], "simulated_n": n["simulated"],
            "delivered": n["sent"] + n["simulated"], "simulated": n["simulated"] > 0}


def _send_now(family_id, people, subject, body, kind):
    ids = queue_many(family_id, people, subject, body, kind, f"{kind}-{crypto.token()}", priority=5)
    if ids:
        dispatch(only_ids=ids)
    return len(ids)


def med_change_notice(family_id, change_id, approved_by=None):
    """Tell the elder that someone else changed (or wants to change) one of their medicines."""
    from services import describe_change
    c = db.get_change(family_id, change_id)
    elders = db.members(family_id, roles=["elder"], with_email=True)
    if not elders:
        return 0
    full = db.family_settings(family_id)["email_detail"] == "full"
    first = _first(elders[0]["name"])
    pending = c["status"] == "pending"
    who = f"{approved_by} approved {c['by_name']}'s suggestion and" if approved_by else c["by_name"]
    if c["action"] == "add" and pending and approved_by is None:
        what = "suggested a new medicine for you" if not full else describe_change(c).replace("added", "suggested adding", 1)
    else:
        what = (describe_change(c) if full else
                {"add": "added a medicine", "edit": "changed one of your medicines", "stop": "stopped one of your medicines"}[c["action"]])
    if pending:
        subject = "CareCircle: a medicine change needs your OK"
        ask = "Nothing changes until you approve it. Open CareCircle and choose Approve or Decline."
    else:
        subject = "CareCircle: a change to your medicines"
        ask = "Open CareCircle to see the change, and tap OK, or 'I don't agree' if it isn't what you want."
    body = f"Hello {first},\n\n{who} {what}.\n\n{ask}\n"
    return _send_now(family_id, elders, subject, body, "med_change")


def med_change_reply(family_id, change_id):
    """Tell the person who made a change, and the caretakers, that the elder disagreed with it or declined it."""
    from services import describe_change
    c = db.get_change(family_id, change_id)
    fam = db.get_family(family_id)
    first = _first(fam["elder_name"])
    full = db.family_settings(family_id)["email_detail"] == "full"
    people = [m for m in db.members(family_id, with_email=True)
              if m["role"] == "caretaker" or m["id"] == c["by_member"]]
    people = [m for m in people if m["role"] != "elder"]
    verb = "doesn't agree with" if c["status"] == "disputed" else "declined"
    subject = f"CareCircle: {first} {verb} a medicine change"
    detail = f" ({describe_change(c)})" if full else ""
    note = f'\n\nTheir note: "{c["note"]}"' if full and c["note"] else ""
    body = (f"{first} {verb} the change {c['by_name']} made to their medicines{detail}.{note}\n\n"
            + ("The change has already taken effect. Please talk it through with them, and change it back if needed.\n"
               if c["status"] == "disputed" else "Nothing was changed.\n"))
    return _send_now(family_id, people, subject, body, "med_change_reply")


def help_cancel(family_id, elder_member):
    carers = db.members(family_id, roles=["caretaker", "family"], with_email=True)
    ids = queue_many(family_id, carers, f"CareCircle: {_first(elder_member['name'])}'s help alert was a mistake",
                     f"{elder_member['name']} says the 'I need help' alert was pressed by mistake. "
                     "You may still want to give them a call.\n", "help_cancel", f"help-cancel-{crypto.token()}", priority=10)
    if ids:
        dispatch(only_ids=ids)
    return len(ids)


# ---------------------------------------------------------------- scheduler
def tick(owner=WORKER_ID):
    """Run one pass if this process holds the reminder lease. Returns True if it ran."""
    interval = config.scheduler_interval()
    if not db.acquire_lease("reminders", owner, max(120, interval * 3), time.time()):
        return False
    started = time.time()
    run_reminders()
    db.heartbeat("reminders", time.time(), f"{owner} took {time.time() - started:.1f}s")
    hb = db.get_heartbeat("purge")
    if not hb or time.time() - hb["at"] > 86400:
        import maintenance
        maintenance.purge()
        db.heartbeat("purge", time.time(), owner)
    return True


_started = False
_lock = threading.Lock()


def start_scheduler(interval=None):
    """Background thread inside the web app. Safe to call repeatedly."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    def loop():
        while True:
            try:
                tick()
            except Exception:
                log.exception("reminder loop error")
            time.sleep(interval or config.scheduler_interval())

    threading.Thread(target=loop, daemon=True, name="carecircle-reminders").start()
