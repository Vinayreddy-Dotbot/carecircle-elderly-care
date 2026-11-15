"""The care assistant: "what needs attention now", worked out from who is looking, the time of day and the care data.

Every suggestion comes with the reason it was made, so people can see the logic and trust it. It is rule-based on
purpose: it works offline, gives the same answer every time, and never guesses about medicine. (The Gemini assistant
in Insights is there for open questions.)

Each suggestion is a dict: level (urgent | todo | info | good), text, why.
"""
import time
from datetime import datetime, timedelta

import clock
import db
import i18n
import interactions

LEVELS = ("urgent", "todo", "info", "good")
GRACE_MIN = 30
LOW_STOCK_DAYS = 7
MAX_ITEMS = 6


def _fmt(t):
    return datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")


def _first(name):
    return (name or "").split()[0] if name else ""


def _day_word(day, today, lang):
    if day == today:
        return i18n.t("day_today", lang)
    if day == today + timedelta(days=1):
        return i18n.t("day_tomorrow", lang)
    return day.strftime("%a %d %b")


def suggestions(actor, now=None):
    now = now or clock.now()
    fid, role, lang = actor["family_id"], actor["role"], actor.get("lang") or "en"
    elder = actor["role"] == "elder"
    carer = role in ("caretaker", "family")
    editor = role in ("elder", "caretaker")
    fam = db.get_family(fid)
    who = _first(fam["elder_name"])
    today = now.date()
    now_t = now.strftime("%H:%M")
    out = []

    def add(level, text, why):
        out.append({"level": level, "text": text, "why": why})

    # 1. the elder asked for help recently
    if carer:
        recent = db.q("SELECT at FROM security_events WHERE family_id=? AND event='help_pressed' AND at>? ORDER BY at DESC",
                      (fid, time.time() - 2 * 3600))
        if recent:
            at = datetime.fromtimestamp(recent[0]["at"]).strftime("%I:%M %p").lstrip("0")
            add("urgent", f"{who} pressed 'I need help' at {at}. Make sure someone has reached them.",
                "The help button was pressed in the last 2 hours.")

    # 2. today's doses: overdue, due now, next
    doses = db.doses_for_day(fid, today.isoformat())
    late = [d for d in doses if d["status"] == "unconfirmed" or
            (d["status"] == "pending" and (now - datetime.fromisoformat(f"{d['scheduled_date']}T{d['scheduled_time']}"))
             > timedelta(minutes=GRACE_MIN))]
    due = [d for d in doses if d["status"] == "pending" and d not in late and
           -15 <= (now - datetime.fromisoformat(f"{d['scheduled_date']}T{d['scheduled_time']}")).total_seconds() / 60 <= GRACE_MIN]
    upcoming = [d for d in doses if d["status"] == "pending" and d["scheduled_time"] > now_t and d not in due]
    if late:
        if elder:
            d = late[0]
            add("urgent", i18n.t("ast_missed", lang, time=_fmt(d["scheduled_time"]), name=d["name"]),
                i18n.t("ast_missed_why", lang))
        else:
            names = ", ".join(f"{d['name']} ({_fmt(d['scheduled_time'])})" for d in late[:3])
            add("urgent", f"{who} hasn't confirmed {len(late)} dose{'s' if len(late) > 1 else ''} today: {names}. "
                          "Give them a call, then mark it on the Today list.",
                f"Doses not confirmed {GRACE_MIN} minutes after their time.")
    for d in due:
        instr = f" ({d['instructions']})" if d["instructions"] else ""
        if elder:
            add("todo", i18n.t("ast_due", lang, name=d["name"], dosage=d["dosage"], instr=instr),
                i18n.t("ast_due_why", lang, time=_fmt(d["scheduled_time"])))
        else:
            add("info", f"{who}'s {d['name']} {d['dosage']} is due at {_fmt(d['scheduled_time'])}.",
                "A dose is due now. CareCircle has reminded them.")
    if upcoming and not due:
        d = upcoming[0]
        if elder:
            add("info", i18n.t("ast_next", lang, name=d["name"], dosage=d["dosage"], time=_fmt(d["scheduled_time"])),
                i18n.t("ast_next_why", lang))
    if doses and all(d["status"] == "taken" for d in doses):
        add("good", i18n.t("ast_all_done", lang) if elder else f"{who} has taken every dose today.",
            i18n.t("ast_all_done_why", lang) if elder else "Every dose today is confirmed as taken.")

    # 3. medicine changes waiting for the elder, or disputed
    open_changes = db.changes(fid)
    waiting = [c for c in open_changes if c["status"] in ("pending", "applied")]
    disputed = [c for c in open_changes if c["status"] == "disputed"]
    if elder and waiting:
        add("todo", i18n.t("ast_changes", lang, n=len(waiting)), i18n.t("ast_changes_why", lang))
    if role == "caretaker" and disputed:
        add("urgent", f"{who} doesn't agree with {len(disputed)} medicine change{'s' if len(disputed) > 1 else ''}. "
                      "Please talk it through with them.", f"{who} tapped 'I don't agree'.")
    elif carer and waiting:
        add("info", f"Waiting for {who} to answer {len(waiting)} medicine change{'s' if len(waiting) > 1 else ''}.",
            "Changes by others are shown to the elder so they stay in charge.")

    # 4. appointments in the next two days
    for a in db.appts(fid, now=now):
        start = datetime.fromisoformat(a["when_at"])
        if start - now > timedelta(days=2):
            break
        day = _day_word(start.date(), today, lang)
        if elder:
            add("todo", i18n.t("ast_appt", lang, doctor=a["doctor"], day=day, time=start.strftime("%I:%M %p").lstrip("0")),
                i18n.t("ast_appt_why", lang))
        else:
            qs = len([q for q in (a["questions"] or "").splitlines() if q.strip()])
            tail = (f" {qs} question{'s' if qs != 1 else ''} to ask so far." if qs
                    else " No questions added yet: add anything you want the doctor to answer.")
            add("todo", f"{who} sees {a['doctor']} {day} at {start.strftime('%I:%M %p').lstrip('0')}.{tail}",
                "Appointment in the next 2 days.")

    # 5. refills, worked out from stock and doses per day
    for m in db.meds(fid):
        if m["stock"] is None:
            continue
        per_day = max(1, len(m["times"].split(",")))
        days = m["stock"] // per_day
        if days <= LOW_STOCK_DAYS or m["stock"] <= (m["refill_at"] or 0):
            if elder:
                add("info", i18n.t("ast_low_stock", lang, name=m["name"], stock=m["stock"]), i18n.t("ast_low_stock_why", lang))
            elif editor:
                add("todo", f"{m['name']} runs out in about {days} day{'s' if days != 1 else ''} "
                            f"({m['stock']} left, {per_day} a day). Order a refill.",
                    f"Less than {LOW_STOCK_DAYS + 1} days of stock left.")

    # 6. wellbeing
    checks = db.checkins(fid, 7, today=today)
    if elder and now.hour >= 9 and not any(c["day"] == today.isoformat() for c in checks):
        add("todo", i18n.t("ast_mood", lang), i18n.t("ast_mood_why", lang))
    if carer and len(checks) >= 3 and sum(c["mood"] for c in checks[-3:]) / 3 <= 2.3:
        add("todo", f"{who}'s mood has been low for the last few check-ins. Worth a call, or a mention to the doctor.",
            "Average of the last 3 mood check-ins is 'Low' or below.")

    # 7. patterns and safety (for the people who can change the plan)
    if editor and not elder:
        adh = db.adherence(fid, 7, today=today)
        for name, (t, n) in adh["per_med"].items():
            if n >= 3 and t / n < 0.7:
                add("info", f"{name} is often not confirmed ({t} of {n} this week). Try moving it to a time that fits "
                            f"{who}'s routine, or pairing it with a meal.", "Less than 70% confirmed over the last 7 days.")
        res = interactions.check_all([m["name"] for m in db.meds(fid)])
        for w in res.warnings[:2]:
            add("todo", f"Check with the pharmacist: {w}", "CareCircle's basic medicine check found this combination.")
        unchecked = [m["name"] for m in db.meds(fid) if not m.get("verified_by")]
        if unchecked:
            add("info", f"{len(unchecked)} medicine{'s' if len(unchecked) > 1 else ''} not yet checked against the "
                        f"prescription: {', '.join(unchecked[:3])}.",
                "Checking each entry against the prescription label catches typing mistakes.")

    # 8. encouragement
    streak = db.adherence(fid, 14, today=today)["streak"]
    if streak >= 3:
        add("good", i18n.t("ast_streak", lang, n=streak) if elder else f"{who} has confirmed every dose for {streak} days in a row.",
            i18n.t("ast_streak_why", lang) if elder else "Days in a row with every dose confirmed.")

    out.sort(key=lambda s: LEVELS.index(s["level"]))
    return out[:MAX_ITEMS]
