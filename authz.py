"""Who may do what. Checked in services.py on every operation, independently of what the UI shows."""

EDITOR = {
    "view", "dose.mark", "med.add", "med.suggest", "med.edit", "med.stop", "med.approve", "med.verify",
    "stock.adjust", "appt.add", "appt.edit", "appt.question", "member.view_contact", "member.invite",
    "member.remove", "member.reset_code", "member.edit", "outbox.view", "family.settings", "family.export",
    "family.delete", "audit.view", "ai.use", "demo.simulate",
}

PERMISSIONS = {
    # The elder owns the record. Mood check-ins and the help button are theirs alone.
    "elder": EDITOR | {"checkin.set", "help.request", "med.review", "family.elder_settings"},
    # Caretakers manage the plan day to day.
    "caretaker": set(EDITOR),
    # Family can see the plan, confirm doses, suggest medicines (for approval) and add doctor questions.
    "family": {"view", "dose.mark", "med.suggest", "appt.question", "ai.use"},
}


class Forbidden(Exception):
    pass


def can(actor, perm):
    return bool(actor) and perm in PERMISSIONS.get(actor.get("role"), set())


def require(actor, perm):
    if not can(actor, perm):
        raise Forbidden(f"Your role ({(actor or {}).get('role', 'signed out')}) can't do that.")
