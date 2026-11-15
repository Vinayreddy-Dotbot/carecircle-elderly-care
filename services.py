"""Every user action goes through here.

Each function takes the signed-in actor (from auth.current), checks the permission in authz, validates input,
and calls db with the actor's own family_id. The UI hides buttons people can't use, but these checks are what
actually enforce it, so a crafted request or a UI bug can't edit another family's data or exceed a role.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

import auth
import authz
import clock
import config
import crypto
import db
import interactions
import maintenance
import notifier
from authz import Forbidden, require
from typedefs import Actor, Notice, Row

__all__ = ["Forbidden", "Invalid"]

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Invalid(ValueError):
    pass


# ---------------------------------------------------------------- validation
def _text(value: object, label: str, max_len: int, required: bool = False, multiline: bool = False) -> str:
    """Clean text input: drop control characters, collapse spaces (unless multiline), check length."""
    v = CONTROL.sub("", str(value or "")).strip()
    if not multiline:
        v = " ".join(v.split())
    if required and not v:
        raise Invalid(f"Please enter {label}.")
    if len(v) > max_len:
        raise Invalid(f"{label.capitalize()} is too long (maximum {max_len} characters).")
    return v


def _email(value: str | None, required: bool = False) -> str:
    """A trimmed email address, or '' when none was given and none is required."""
    v = (value or "").strip()
    if not v and not required:
        return ""
    if not notifier.valid_email(v):
        raise Invalid("That doesn't look like a valid email address.")
    return v


def _times(times: Iterable[str] | None) -> list[str]:
    """1 to 8 distinct HH:MM times, sorted."""
    times = sorted(set(times or []))
    if not times:
        raise Invalid("Please choose at least one time.")
    if len(times) > 8:
        raise Invalid("A medicine can have at most 8 times a day.")
    if not all(TIME_RE.match(t) for t in times):
        raise Invalid("Times must look like 08:00.")
    return times


def _date(value: object, label: str) -> str:
    """A date (or date-time, or YYYY-MM-DD text) as YYYY-MM-DD."""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise Invalid(f"{label} is not a valid date.") from None


def _int(value: Any, label: str, lo: int, hi: int) -> int:
    """A whole number in [lo, hi]."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise Invalid(f"{label} must be a whole number.") from None
    if not lo <= v <= hi:
        raise Invalid(f"{label} must be between {lo} and {hi}.")
    return v


def _optional_int(value: Any, label: str, lo: int, hi: int) -> int | None:
    """A whole number in [lo, hi], or None when nothing was entered."""
    return None if value in (None, "") else _int(value, label, lo, hi)


def _now() -> str:
    """The local time now, to the second, as ISO text."""
    return clock.now().isoformat(timespec="seconds")


def _alert_elder(actor: Actor, what: str) -> None:
    """Tell the elder about security-relevant changes someone else made."""
    if actor["role"] == "elder":
        return
    for e in db.members(actor["family_id"], roles=["elder"], with_email=True):
        notifier.security_alert(e, "CareCircle: a change to your care circle",
                                f"Hello {e['name']},\n\n{actor['name']} {what}.\n\nIf you did not expect this, "
                                "open CareCircle, check the Circle tab, and reset any codes you are unsure about.\n",
                                f"elder-alert-{crypto.token()}")


# ---------------------------------------------------------------- circles & members
def create_circle(family_name: str, elder_name: str, elder_email: str | None,
                  invites: Iterable[tuple[str, str, str]] = (), clients: Sequence[str] = (),
                  is_demo: bool = False) -> tuple[int, dict[str, str]]:
    """Returns (family_id, {label: access code}). Codes are shown once."""
    if not auth.signup_allowed(clients):
        raise Invalid("Too many new care circles from this device. Please try again in an hour.")
    family_name = _text(family_name, "the circle name", 80, required=True)
    elder_name = _text(elder_name, "the elderly person's name", 80, required=True)
    elder_email = _email(elder_email)
    clean = []
    for name, email, role in invites:
        name = _text(name, "a name", 80)
        if not name:
            continue
        if role not in ("caretaker", "family"):
            raise Invalid("Invited people must be a caretaker or family member.")
        clean.append((name, _email(email), role))
    with db.tx() as c:
        fid = db.create_family(family_name, elder_name, is_demo, c)
        codes = {}
        _, codes[f"{elder_name} (elder)"] = auth.create_member(fid, elder_name, elder_email, "elder", c)
        for name, email, role in clean:
            _, codes[f"{name} ({role})"] = auth.create_member(fid, name, email, role, c)
        db.log(fid, elder_name, "created the care circle", c)
        db.security_event("circle_created", fid, None, "demo" if is_demo else "", c)
    auth.record_signup(clients)
    return fid, codes


def invite(actor: Actor, name: str, email: str | None, role: str) -> str:
    """Add a caretaker or family member. Returns their access code (shown once); the elder is told about new caretakers."""
    require(actor, "member.invite")
    if role not in ("caretaker", "family"):
        raise Invalid("Choose caretaker or family.")
    name = _text(name, "a name", 80, required=True)
    member_id, code = auth.create_member(actor["family_id"], name, _email(email), role)
    db.log(actor["family_id"], actor["name"], f"invited {name} as {role}")
    db.security_event("member_invited", actor["family_id"], member_id, f"role={role} by member {actor['id']}")
    if role == "caretaker":
        _alert_elder(actor, f"added {name} as a caretaker (caretakers can change everything)")
    return code


def remove_member(actor: Actor, member_id: int) -> None:
    """Remove someone from the circle (never the elder, never yourself). The elder is told."""
    require(actor, "member.remove")
    target = db.member_in_family(actor["family_id"], member_id)
    if target["role"] == "elder":
        raise Forbidden("The elder can't be removed from their own care circle.")
    if target["id"] == actor["id"]:
        raise Forbidden("You can't remove yourself. Ask the elder or another caretaker.")
    db.delete_member(actor["family_id"], member_id)
    db.log(actor["family_id"], actor["name"], f"removed {target['name']}")
    db.security_event("member_removed", actor["family_id"], member_id, f"by member {actor['id']}")
    _alert_elder(actor, f"removed {target['name']} from the care circle")


def reset_code(actor: Actor, member_id: int) -> str:
    """Give a member a new access code (shown once). Their old code and sessions stop working."""
    require(actor, "member.reset_code")
    target = db.member_in_family(actor["family_id"], member_id)
    code = auth.reset_code(actor["family_id"], member_id)
    db.log(actor["family_id"], actor["name"], f"gave {target['name']} a new access code")
    db.security_event("code_reset", actor["family_id"], member_id, f"by member {actor['id']}")
    if target["id"] != actor["id"]:
        notifier.security_alert(target, "CareCircle: your access code was changed",
                                f"Hello {target['name']},\n\n{actor['name']} gave you a new CareCircle access code. "
                                "Your old code no longer works. Ask them for the new one.\n",
                                f"reset-{member_id}-{crypto.token()}")
        _alert_elder(actor, f"gave {target['name']} a new access code")
    return code


def update_email(actor: Actor, member_id: int, email: str | None) -> None:
    """Change a member's email (your own, or anyone's for editors). The old address is told."""
    if member_id != actor["id"]:
        require(actor, "member.edit")
    target = db.member_in_family(actor["family_id"], member_id)
    email = _email(email)
    if auth.needs_mfa(target) and not email:
        raise Invalid("This person signs in with an emailed code, so their email can't be removed.")
    if target["email"] and target["email"].lower() != email.lower():  # tell the old address
        notifier.security_alert(target, "CareCircle: your email was changed",
                                f"Hello {target['name']},\n\n{actor['name']} changed the email on your CareCircle "
                                "account. Reminders and sign-in codes will now go to the new address.\n",
                                f"email-change-{member_id}-{crypto.token()}")
    db.update_member(actor["family_id"], member_id, email=email)
    db.log(actor["family_id"], actor["name"], f"updated the email for {target['name']}")
    db.security_event("email_changed", actor["family_id"], member_id, f"by member {actor['id']}")


def set_my_preferences(actor: Actor, lang: str | None = None, large_text: bool | None = None,
                       mfa: bool | None = None) -> None:
    """Change your own language, text size or two-step sign-in."""
    fields: dict[str, Any] = {}
    if lang is not None:
        if lang not in ("en", "hi"):
            raise Invalid("Unknown language.")
        fields["lang"] = lang
    if large_text is not None:
        fields["large_text"] = int(bool(large_text))
    if mfa is not None:
        if mfa and not actor.get("email"):
            raise Invalid("Add your email first, so the sign-in code has somewhere to go.")
        if not mfa and actor["role"] in config.mfa_roles():
            raise Invalid("Two-step sign-in is required for your role.")
        fields["mfa"] = int(bool(mfa))
        db.security_event("mfa_on" if mfa else "mfa_off", actor["family_id"], actor["id"])
    if fields:
        db.update_member(actor["family_id"], actor["id"], **fields)


# ---------------------------------------------------------------- medicines
def _clean_med(actor: Actor, name: str, dosage: str, instructions: str, times: Sequence[str], start: date | str,
               end: date | str | None, stock: int | None, refill_at: int, prescriber: str) -> dict[str, Any]:
    """Validate a new medicine; family members' entries become suggestions."""
    if authz.can(actor, "med.add"):
        status = "active"
    else:
        require(actor, "med.suggest")
        status = "proposed"
    v: dict[str, Any] = {"name": _text(name, "the medicine name", 80, required=True),
                         "dosage": _text(dosage, "the dosage", 40, required=True),
                         "instructions": _text(instructions, "the instructions", 200),
                         "prescriber": _text(prescriber, "the prescriber", 80),
                         "times": _times(times), "start": _date(start, "Start date"),
                         "end": _date(end, "End date") if end else None,
                         "stock": _optional_int(stock, "Doses in stock", 0, 9999) or None,
                         "refill_at": _int(refill_at, "Refill alert", 0, 999), "status": status}
    if v["end"] and v["end"] < v["start"]:
        raise Invalid("The end date is before the start date.")
    return v


def preview_medicine(actor: Actor, name: str, dosage: str, instructions: str, times: Sequence[str], start: date | str,
                     end: date | str | None = None, stock: int | None = None, refill_at: int = 5,
                     prescriber: str = "") -> dict[str, Any]:
    """Validate and run the safety check without saving, so people can review before confirming."""
    v = _clean_med(actor, name, dosage, instructions, times, start, end, stock, refill_at, prescriber)
    v["check"] = interactions.check(v["name"], [m["name"] for m in db.meds(actor["family_id"], ("active", "proposed"))])
    return v


def add_medicine(actor: Actor, name: str, dosage: str, instructions: str, times: Sequence[str], start: date | str,
                 end: date | str | None = None, stock: int | None = None, refill_at: int = 5,
                 prescriber: str = "") -> dict[str, Any]:
    """Editors add directly; family members create a suggestion that needs approval. When the elder has asked to
    approve changes first, a caretaker's new medicine also waits for the elder.
    Returns dict(id, status, check) where check is an interactions.CheckResult."""
    v = preview_medicine(actor, name, dosage, instructions, times, start, end, stock, refill_at, prescriber)
    fid = actor["family_id"]
    status = v["status"]
    if status == "active" and _elder_must_approve(actor):
        status = "proposed"
    med_id = db.add_med(fid, v["name"], v["dosage"], v["instructions"], v["times"], v["start"], v["end"], v["stock"],
                        v["refill_at"], actor["name"], status, v["prescriber"])
    db.log(fid, actor["name"], f"{'added' if status == 'active' else 'suggested'} {v['name']} {v['dosage']}"
                               f" at {', '.join(v['times'])}")
    if actor["role"] != "elder":
        after = {"name": v["name"], "dosage": v["dosage"], "times": ",".join(v["times"]), "instructions": v["instructions"]}
        _record_change(actor, med_id, "add", {}, after, "applied" if status == "active" else "pending")
    return {"id": med_id, "status": status, "check": v["check"]}


# ---------------------------------------------------------------- the elder's say over their medicines
def _elder_must_approve(actor: Actor) -> bool:
    """True when the elder has asked to approve changes by others, and someone else is acting."""
    return actor["role"] != "elder" and db.family_settings(actor["family_id"])["elder_approval"]


def _record_change(actor: Actor, med_id: int, action: str, before: dict[str, Any], after: dict[str, Any],
                   status: str) -> int:
    """Record a change for the elder to see, and email them."""
    cid = db.add_change(actor["family_id"], med_id, action, before, after, actor["id"], actor["name"], status)
    notifier.med_change_notice(actor["family_id"], cid)
    return cid


def describe_change(c: Row) -> str:
    """Plain-language summary, e.g. "changed Metformin: dose 500 mg -> 1000 mg"."""
    b, a = c["before"], c["after"]
    name = a.get("name") or b.get("name") or "a medicine"
    if c["action"] == "add":
        return f"added {name} {a.get('dosage', '')} at {a.get('times', '').replace(',', ', ')}".replace("  ", " ")
    if c["action"] == "stop":
        return f"stopped {name} {b.get('dosage', '')}".strip()
    labels = {"dosage": "dose", "times": "times", "instructions": "instructions", "prescriber": "prescribed by"}
    parts = [f"{labels[k]} {(b.get(k) or '-').replace(',', ', ')} -> {(a.get(k) or '-').replace(',', ', ')}"
             for k in labels if k in a and (b.get(k) or "") != (a.get(k) or "")]
    return f"changed {name}: " + "; ".join(parts)


def changes_for_elder(actor: Actor) -> list[Row]:
    """Changes by others that are waiting for the elder: to approve (pending) or to see and OK (applied, disputed)."""
    require(actor, "view")
    return db.changes(actor["family_id"])


def review_change(actor: Actor, change_id: int, decision: str, note: str = "") -> str:
    """The elder's answer to a change someone else made.
    pending -> approve / decline;  applied or disputed -> ok / disagree."""
    require(actor, "med.review")
    fid = actor["family_id"]
    c = db.get_change(fid, change_id)
    note = _text(note, "a note", 300)
    if c["status"] == "pending" and decision in ("approve", "decline"):
        med = db.get_med(fid, c["med_id"])
        if decision == "approve":
            _apply_change(actor, c, med)
            db.resolve_change(fid, change_id, "approved", actor["name"], note, expect=("pending",))
        else:
            if c["action"] == "add" and med["status"] == "proposed":
                db.update_med(fid, c["med_id"], status="declined")
            db.resolve_change(fid, change_id, "declined", actor["name"], note, expect=("pending",))
            notifier.med_change_reply(fid, change_id)
        db.log(fid, actor["name"], f"{'approved' if decision == 'approve' else 'declined'}: {c['by_name']} {describe_change(c)}")
        return decision
    if c["status"] in ("applied", "disputed") and decision in ("ok", "disagree"):
        if decision == "ok":
            db.resolve_change(fid, change_id, "accepted", actor["name"], note, expect=("applied", "disputed"))
            db.log(fid, actor["name"], f"is OK with: {c['by_name']} {describe_change(c)}")
        else:
            db.resolve_change(fid, change_id, "disputed", actor["name"], note, expect=("applied", "disputed"))
            db.log(fid, actor["name"], f"doesn't agree with: {c['by_name']} {describe_change(c)}")
            notifier.med_change_reply(fid, change_id)
        return decision
    raise Invalid("That change has already been answered. Refresh the page.")


def _apply_change(actor: Actor, c: Row, med: Row) -> None:
    """Carry out a change the elder approved."""
    fid = actor["family_id"]
    if c["action"] == "add":
        if med["status"] != "proposed":
            raise Invalid("That medicine has already been changed by someone else. Refresh the page.")
        db.update_med(fid, med["id"], status="active")
    elif c["action"] == "stop":
        if med["status"] == "active":
            db.stop_med(fid, med["id"])
    else:
        if med["status"] != "active":
            raise Invalid("That medicine is no longer active, so the change can't be applied. Decline it instead.")
        _apply_edit(fid, med, {k: v for k, v in c["after"].items() if k in ("dosage", "times", "instructions", "prescriber")})


def _apply_edit(fid: int, med: Row, new: dict[str, Any]) -> dict[str, Any]:
    """Save changed fields; new times drop doses at times that no longer exist. Returns what changed."""
    changes = {k: v for k, v in new.items() if (med.get(k) or "") != v}
    if not changes:
        return {}
    if "dosage" in changes or "times" in changes:
        changes.update(verified_by="", verified_at=None)
    db.update_med(fid, med["id"], **changes)
    if "times" in changes:  # drop not-yet-due doses at times that no longer exist
        keep = new["times"].split(",")
        db.x(f"DELETE FROM doses WHERE med_id=? AND family_id=? AND status='pending' AND scheduled_date>=? "
             f"AND scheduled_time NOT IN ({','.join('?' * len(keep))})", (med["id"], fid, clock.today().isoformat(), *keep))
    return changes


def _med(actor: Actor, med_id: int, statuses: Sequence[str]) -> Row:
    """A medicine that must still have one of these statuses (guards against two people acting at once)."""
    m = db.get_med(actor["family_id"], med_id)
    if m["status"] not in statuses:
        raise Invalid("That medicine has already been changed by someone else. Refresh the page.")
    return m


def _pending_add(actor: Actor, med_id: int) -> Row | None:
    """The pending 'add' change for a suggested medicine, if there is one."""
    return next((c for c in db.changes(actor["family_id"], ("pending",), med_id) if c["action"] == "add"), None)


def approve_medicine(actor: Actor, med_id: int) -> str | None:
    """Approve a suggested medicine. The elder still sees what a caretaker approved."""
    require(actor, "med.approve")
    if _elder_must_approve(actor):
        raise Forbidden(f"{db.get_family(actor['family_id'])['elder_name']} has asked to approve medicine changes "
                        "themselves, so only they can approve this.")
    m = _med(actor, med_id, ("proposed",))
    c = _pending_add(actor, med_id)
    if actor["role"] == "elder" and c:
        return review_change(actor, c["id"], "approve")
    db.update_med(actor["family_id"], med_id, status="active")
    db.log(actor["family_id"], actor["name"], f"approved {m['name']} (suggested by {m['added_by']})")
    if c:  # a caretaker approved it: the elder still gets to see it and say OK
        db.resolve_change(actor["family_id"], c["id"], "applied", "", expect=("pending",))
        notifier.med_change_notice(actor["family_id"], c["id"], approved_by=actor["name"])
    return None


def decline_medicine(actor: Actor, med_id: int) -> str | None:
    """Decline a suggested medicine."""
    require(actor, "med.approve")
    m = _med(actor, med_id, ("proposed",))
    c = _pending_add(actor, med_id)
    if actor["role"] == "elder" and c:
        return review_change(actor, c["id"], "decline")
    if _elder_must_approve(actor):
        raise Forbidden("Only the elder can answer medicine changes in this care circle.")
    db.update_med(actor["family_id"], med_id, status="declined")
    if c:
        db.resolve_change(actor["family_id"], c["id"], "declined", actor["name"], expect=("pending",))
    db.log(actor["family_id"], actor["name"], f"declined {m['name']}")
    return None


def edit_medicine(actor: Actor, med_id: int, dosage: str, times: Sequence[str], instructions: str,
                  prescriber: str | None = None) -> str | bool:
    """Returns "applied", "pending" (waiting for the elder) or False (nothing changed).
    Changing dosage or times clears 'checked against prescription' so someone checks again."""
    require(actor, "med.edit")
    m = _med(actor, med_id, ("active",))
    new = {"dosage": _text(dosage, "the dosage", 40, required=True), "times": ",".join(_times(times)),
           "instructions": _text(instructions, "the instructions", 200)}
    if prescriber is not None:
        new["prescriber"] = _text(prescriber, "the prescriber", 80)
    diff = {k: v for k, v in new.items() if (m.get(k) or "") != v}
    if not diff:
        return False
    fid = actor["family_id"]
    before = {"name": m["name"], **{k: m.get(k) or "" for k in diff}}
    after = {"name": m["name"], **diff}
    detail = "; ".join(f"{k}: '{m.get(k) or ''}' -> '{v}'" for k, v in diff.items())
    if _elder_must_approve(actor):
        _record_change(actor, med_id, "edit", before, after, "pending")
        db.log(fid, actor["name"], f"asked to change {m['name']} ({detail})")
        return "pending"
    _apply_edit(fid, m, new)
    db.log(fid, actor["name"], f"edited {m['name']} ({detail})")
    if actor["role"] != "elder":
        _record_change(actor, med_id, "edit", before, after, "applied")
    return "applied"


def verify_medicine(actor: Actor, med_id: int, prescriber: str = "") -> None:
    """Record that someone compared this entry with the prescription or the pharmacist's label."""
    require(actor, "med.verify")
    m = _med(actor, med_id, ("active",))
    db.update_med(actor["family_id"], med_id, verified_by=actor["name"], verified_at=_now(),
                  prescriber=_text(prescriber, "the prescriber", 80) or m.get("prescriber") or "")
    db.log(actor["family_id"], actor["name"], f"checked {m['name']} {m['dosage']} against the prescription")


def stop_medicine(actor: Actor, med_id: int) -> str:
    """Returns "applied" or "pending" (waiting for the elder)."""
    require(actor, "med.stop")
    m = _med(actor, med_id, ("active",))
    snap = {"name": m["name"], "dosage": m["dosage"], "times": m["times"]}
    if _elder_must_approve(actor):
        _record_change(actor, med_id, "stop", snap, {}, "pending")
        db.log(actor["family_id"], actor["name"], f"asked to stop {m['name']}")
        return "pending"
    db.stop_med(actor["family_id"], med_id)
    db.log(actor["family_id"], actor["name"], f"stopped {m['name']}")
    if actor["role"] != "elder":
        _record_change(actor, med_id, "stop", snap, {}, "applied")
    return "applied"


def adjust_stock(actor: Actor, med_id: int, new_stock: int, reason: str) -> None:
    """Correct the stock count, with a reason (refill, recount)."""
    require(actor, "stock.adjust")
    m = _med(actor, med_id, ("active",))
    new_stock = _int(new_stock, "Doses in stock", 0, 9999)
    reason = _text(reason, "a reason", 80, required=True)
    db.adjust_stock(actor["family_id"], med_id, new_stock, actor["name"], reason)
    db.log(actor["family_id"], actor["name"], f"set {m['name']} stock to {new_stock} ({reason})")


# ---------------------------------------------------------------- doses
def mark_dose(actor: Actor, dose_id: int, status: str) -> bool:
    """taken / skipped / missed, or pending to undo. Records who said so: CareCircle can't see the pill being swallowed."""
    require(actor, "dose.mark")
    if status not in ("taken", "skipped", "missed", "pending"):
        raise Invalid("Unknown dose status.")
    d = db.get_dose(actor["family_id"], dose_id)
    if not db.set_dose(actor["family_id"], dose_id, status, actor["name"]):
        return False
    t = d["scheduled_time"]
    what = {"taken": f"confirmed {d['name']} ({t}) was taken", "skipped": f"skipped {d['name']} ({t})",
            "missed": f"recorded {d['name']} ({t}) as missed", "pending": f"undid the mark on {d['name']} ({t})"}[status]
    db.log(actor["family_id"], actor["name"], what)
    return True


# ---------------------------------------------------------------- appointments
def add_appointment(actor: Actor, doctor: str, specialty: str, when: datetime, location: str,
                    questions: str) -> Notice:
    """Add an appointment and email the circle straight away. Returns who was emailed."""
    require(actor, "appt.add")
    if not isinstance(when, datetime):
        raise Invalid("Please choose a date and time.")
    fid = actor["family_id"]
    doctor = _text(doctor, "the doctor's name", 80, required=True)
    appt_id = db.add_appt(fid, doctor, _text(specialty, "the specialty", 60), when.isoformat(timespec="minutes"),
                          _text(location, "the location", 120), "", _text(questions, "the questions", 1000, multiline=True),
                          actor["name"])
    db.log(fid, actor["name"], f"added an appointment with {doctor}")
    return notifier.appointment_notice(fid, appt_id, "added")


def update_appointment(actor: Actor, appt_id: int, notes: str, questions: str) -> None:
    """Change an appointment's notes and questions."""
    require(actor, "appt.edit")
    a = db.get_appt(actor["family_id"], appt_id)
    db.update_appt(actor["family_id"], appt_id, notes=_text(notes, "the notes", 2000, multiline=True),
                   questions=_text(questions, "the questions", 2000, multiline=True))
    db.log(actor["family_id"], actor["name"], f"updated the appointment with {a['doctor']}")


def cancel_appointment(actor: Actor, appt_id: int) -> Notice:
    """Cancel an appointment and email the circle. Returns who was emailed."""
    require(actor, "appt.edit")
    a = db.get_appt(actor["family_id"], appt_id)
    db.update_appt(actor["family_id"], appt_id, status="cancelled")
    db.log(actor["family_id"], actor["name"], f"cancelled the appointment with {a['doctor']}")
    return notifier.appointment_notice(actor["family_id"], appt_id, "cancelled")


def add_question(actor: Actor, appt_id: int, text: str) -> None:
    """Anyone in the circle can add a question for the doctor."""
    require(actor, "appt.question")
    text = _text(text, "a question", 300, required=True)
    db.append_question(actor["family_id"], appt_id, f"- {text} ({actor['name']})")
    db.log(actor["family_id"], actor["name"], "added a question for the doctor")


# ---------------------------------------------------------------- elder actions
def check_in(actor: Actor, mood: int) -> None:
    """The elder's mood today, from 1 (not good) to 5 (great)."""
    require(actor, "checkin.set")
    mood = _int(mood, "Mood", 1, 5)
    db.set_checkin(actor["family_id"], clock.today().isoformat(), mood)
    db.log(actor["family_id"], actor["name"], "did the mood check-in")


def request_help(actor: Actor) -> Notice:
    """The elder pressed 'I need help': email every caretaker and family member now."""
    require(actor, "help.request")
    db.log(actor["family_id"], actor["name"], "pressed 'I need help'")
    db.security_event("help_pressed", actor["family_id"], actor["id"])
    return notifier.help_alert(actor["family_id"], actor)


def cancel_help(actor: Actor) -> int:
    """The elder says the help alert was pressed by mistake."""
    require(actor, "help.request")
    db.log(actor["family_id"], actor["name"], "said the help alert was a mistake")
    return notifier.help_cancel(actor["family_id"], actor)


# ---------------------------------------------------------------- settings, data, logs
def update_settings(actor: Actor, **values: Any) -> None:
    """Change circle settings. Only the elder can change whether they approve medicine changes first."""
    require(actor, "family.settings")
    if "elder_approval" in values:
        current = db.family_settings(actor["family_id"])["elder_approval"]
        if bool(values["elder_approval"]) != current:
            require(actor, "family.elder_settings")
        values["elder_approval"] = bool(values["elder_approval"])
    if "email_detail" in values and values["email_detail"] not in ("minimal", "full"):
        raise Invalid("Unknown email detail level.")
    if "ai_enabled" in values:
        values["ai_enabled"] = bool(values["ai_enabled"])
    db.update_family_settings(actor["family_id"], **values)
    db.log(actor["family_id"], actor["name"], "changed settings: " + ", ".join(f"{k}={v}" for k, v in values.items()))


def export_data(actor: Actor) -> str:
    """Everything held about this circle, as JSON. The download is logged."""
    require(actor, "family.export")
    db.log(actor["family_id"], actor["name"], "downloaded a copy of the care circle's data")
    db.security_event("data_exported", actor["family_id"], actor["id"])
    return maintenance.export_family(actor["family_id"])


def delete_circle(actor: Actor, confirm_name: str | None) -> int:
    """Permanently delete the circle and everything in it. Everyone with an email is told."""
    require(actor, "family.delete")
    fam = db.get_family(actor["family_id"])
    if (confirm_name or "").strip().lower() != (fam["name"] or "").strip().lower():
        raise Invalid("Type the circle's name exactly to confirm.")
    people = db.members(actor["family_id"], with_email=True)
    n = maintenance.delete_family(actor["family_id"])
    for p in people:  # sent directly: nothing about the circle may remain in the outbox
        notifier.send_direct(p["email"], "CareCircle: care circle deleted",
                             f"Hello {p['name']},\n\n{actor['name']} deleted the care circle \"{fam['name']}\" "
                             "and all of its data from CareCircle.\n")
    return n


def outbox(actor: Actor, limit: int = 50) -> list[Row]:
    """The circle's most recent emails."""
    require(actor, "outbox.view")
    return db.q("SELECT * FROM outbox WHERE family_id=? ORDER BY id DESC LIMIT ?", (actor["family_id"], limit))


def security_log(actor: Actor, limit: int = 50) -> list[Row]:
    """The circle's most recent security events."""
    require(actor, "audit.view")
    return db.security_events(actor["family_id"], limit)


def run_reminders_now(actor: Actor, when: datetime | None = None) -> int:
    """Editors can run their own circle's reminder check now. Simulating another time is a demo-only tool."""
    require(actor, "outbox.view")
    if when is not None:
        require(actor, "demo.simulate")
        if not config.demo_features():
            raise Forbidden("Time simulation is only available in development and demo mode.")
    return notifier.run_reminders(now=when, family_ids=[actor["family_id"]])
