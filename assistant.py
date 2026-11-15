"""The care assistant: "what needs attention now", worked out from who is looking, the time of day and the care data.

Every suggestion comes with the reason it was made, so people can see the logic and trust it. It is rule-based on
purpose: it works offline, gives the same answer every time, and never guesses about medicine. (The Gemini assistant
in Insights is there for open questions.)

Each rule below looks at one part of the care plan and adds zero or more suggestions. A suggestion is a dict:
level (urgent | todo | info | good), text, why.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import clock
import db
import i18n
import interactions
from typedefs import Actor, Row, Suggestion

LEVELS = ("urgent", "todo", "info", "good")
GRACE_MIN = 30          # a dose is late this many minutes after its time (same as the reminder engine)
DUE_SOON_MIN = 15       # a dose counts as "due now" from this many minutes before its time
HELP_WINDOW_HOURS = 2   # how long a press of "I need help" stays on carers' screens
APPT_DAYS = 2           # appointments this close are mentioned
LOW_STOCK_DAYS = 7
LOW_ADHERENCE = 0.7     # per-medicine confirmed rate below which a routine change is suggested
LOW_MOOD = 2.3          # average of the last 3 check-ins at or below this is "low"
MOOD_CHECK_HOUR = 9     # the elder is reminded about the mood check-in after this hour
STREAK_DAYS = 3
MAX_ITEMS = 6


def _fmt(t: str) -> str:
    """'20:00' -> '8:00 PM'."""
    return datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")


def _clock_time(dt: datetime) -> str:
    """A datetime's time of day as '8:00 PM'."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _first(name: str | None) -> str:
    """First name only, for friendlier messages."""
    return (name or "").split()[0] if name else ""


def _plural(n: int, word: str) -> str:
    """'1 dose', '2 doses'."""
    return f"{n} {word}{'s' if n != 1 else ''}"


def _day_word(day: date, today: date, lang: str) -> str:
    """'today', 'tomorrow' or a short date, in the reader's language."""
    if day == today:
        return i18n.t("day_today", lang)
    if day == today + timedelta(days=1):
        return i18n.t("day_tomorrow", lang)
    return day.strftime("%a %d %b")


def _scheduled(d: Row) -> datetime:
    """When a dose is due."""
    return datetime.fromisoformat(f"{d['scheduled_date']}T{d['scheduled_time']}")


@dataclass
class _View:
    """Who is looking, and when: everything the rules need. Rules add suggestions to `out`."""

    family_id: int
    role: str
    lang: str
    who: str              # the elder's first name
    now: datetime
    out: list[Suggestion] = field(default_factory=list)

    @property
    def elder(self) -> bool:
        """The elder is looking at their own care plan."""
        return self.role == "elder"

    @property
    def carer(self) -> bool:
        """A caretaker or family member is looking."""
        return self.role in ("caretaker", "family")

    @property
    def editor(self) -> bool:
        """This person can change the care plan."""
        return self.role in ("elder", "caretaker")

    @property
    def today(self) -> date:
        """The local date being looked at."""
        return self.now.date()

    def add(self, level: str, text: str, why: str) -> None:
        """Add one suggestion with the reason it was made."""
        self.out.append({"level": level, "text": text, "why": why})


# ---------------------------------------------------------------- the rules, in the order people read them
def _help_pressed(v: _View) -> None:
    """1. The elder asked for help recently: carers see it first."""
    if not v.carer:
        return
    recent = db.q("SELECT at FROM security_events WHERE family_id=? AND event='help_pressed' AND at>? ORDER BY at DESC",
                  (v.family_id, time.time() - HELP_WINDOW_HOURS * 3600))
    if recent:
        at = _clock_time(datetime.fromtimestamp(recent[0]["at"]))
        v.add("urgent", f"{v.who} pressed 'I need help' at {at}. Make sure someone has reached them.",
              f"The help button was pressed in the last {HELP_WINDOW_HOURS} hours.")


def _doses(v: _View) -> None:
    """2. Today's doses: late or unconfirmed, due now, next up, or all done."""
    doses = db.doses_for_day(v.family_id, v.today.isoformat())
    pending = [d for d in doses if d["status"] == "pending"]
    late = [d for d in doses if d["status"] == "unconfirmed" or
            (d["status"] == "pending" and v.now - _scheduled(d) > timedelta(minutes=GRACE_MIN))]
    due = [d for d in pending if d not in late and
           -DUE_SOON_MIN <= (v.now - _scheduled(d)).total_seconds() / 60 <= GRACE_MIN]
    upcoming = [d for d in pending if d["scheduled_time"] > v.now.strftime("%H:%M") and d not in due]
    if late:
        _late_doses(v, late)
    for d in due:
        instr = f" ({d['instructions']})" if d["instructions"] else ""
        if v.elder:
            v.add("todo", i18n.t("ast_due", v.lang, name=d["name"], dosage=d["dosage"], instr=instr),
                  i18n.t("ast_due_why", v.lang, time=_fmt(d["scheduled_time"])))
        else:
            v.add("info", f"{v.who}'s {d['name']} {d['dosage']} is due at {_fmt(d['scheduled_time'])}.",
                  "A dose is due now. CareCircle has reminded them.")
    if upcoming and not due and v.elder:
        d = upcoming[0]
        v.add("info", i18n.t("ast_next", v.lang, name=d["name"], dosage=d["dosage"], time=_fmt(d["scheduled_time"])),
              i18n.t("ast_next_why", v.lang))
    if doses and all(d["status"] == "taken" for d in doses):
        v.add("good", i18n.t("ast_all_done", v.lang) if v.elder else f"{v.who} has taken every dose today.",
              i18n.t("ast_all_done_why", v.lang) if v.elder else "Every dose today is confirmed as taken.")


def _late_doses(v: _View, late: list[Row]) -> None:
    """Doses not confirmed after the grace period: a gentle nudge for the elder, a call for carers."""
    if v.elder:
        d = late[0]
        v.add("urgent", i18n.t("ast_missed", v.lang, time=_fmt(d["scheduled_time"]), name=d["name"]),
              i18n.t("ast_missed_why", v.lang))
        return
    names = ", ".join(f"{d['name']} ({_fmt(d['scheduled_time'])})" for d in late[:3])
    v.add("urgent", f"{v.who} hasn't confirmed {_plural(len(late), 'dose')} today: {names}. "
                    "Give them a call, then mark it on the Today list.",
          f"Doses not confirmed {GRACE_MIN} minutes after their time.")


def _med_changes(v: _View) -> None:
    """3. Medicine changes waiting for the elder's answer, or that the elder disagreed with."""
    open_changes = db.changes(v.family_id)
    waiting = [c for c in open_changes if c["status"] in ("pending", "applied")]
    disputed = [c for c in open_changes if c["status"] == "disputed"]
    if v.elder and waiting:
        v.add("todo", i18n.t("ast_changes", v.lang, n=len(waiting)), i18n.t("ast_changes_why", v.lang))
    if v.role == "caretaker" and disputed:
        v.add("urgent", f"{v.who} doesn't agree with {_plural(len(disputed), 'medicine change')}. "
                        "Please talk it through with them.", f"{v.who} tapped 'I don't agree'.")
    elif v.carer and waiting:
        v.add("info", f"Waiting for {v.who} to answer {_plural(len(waiting), 'medicine change')}.",
              "Changes by others are shown to the elder so they stay in charge.")


def _appointments(v: _View) -> None:
    """4. Appointments in the next two days, with how many questions are ready for the doctor."""
    for a in db.appts(v.family_id, now=v.now):
        start = datetime.fromisoformat(a["when_at"])
        if start - v.now > timedelta(days=APPT_DAYS):
            break
        day = _day_word(start.date(), v.today, v.lang)
        if v.elder:
            v.add("todo", i18n.t("ast_appt", v.lang, doctor=a["doctor"], day=day, time=_clock_time(start)),
                  i18n.t("ast_appt_why", v.lang))
            continue
        qs = len([q for q in (a["questions"] or "").splitlines() if q.strip()])
        tail = (f" {_plural(qs, 'question')} to ask so far." if qs
                else " No questions added yet: add anything you want the doctor to answer.")
        v.add("todo", f"{v.who} sees {a['doctor']} {day} at {_clock_time(start)}.{tail}",
              f"Appointment in the next {APPT_DAYS} days.")


def _refills(v: _View) -> None:
    """5. Refills, worked out from the stock left and how many doses are taken each day."""
    for m in db.meds(v.family_id):
        if m["stock"] is None:
            continue
        per_day = max(1, len(m["times"].split(",")))
        days = m["stock"] // per_day
        if days > LOW_STOCK_DAYS and m["stock"] > (m["refill_at"] or 0):
            continue
        if v.elder:
            v.add("info", i18n.t("ast_low_stock", v.lang, name=m["name"], stock=m["stock"]),
                  i18n.t("ast_low_stock_why", v.lang))
        elif v.editor:
            v.add("todo", f"{m['name']} runs out in about {_plural(days, 'day')} "
                          f"({m['stock']} left, {per_day} a day). Order a refill.",
                  f"Less than {LOW_STOCK_DAYS + 1} days of stock left.")


def _wellbeing(v: _View) -> None:
    """6. The daily mood check-in, and a run of low moods."""
    checks = db.checkins(v.family_id, 7, today=v.today)
    if v.elder and v.now.hour >= MOOD_CHECK_HOUR and not any(c["day"] == v.today.isoformat() for c in checks):
        v.add("todo", i18n.t("ast_mood", v.lang), i18n.t("ast_mood_why", v.lang))
    if v.carer and len(checks) >= 3 and sum(c["mood"] for c in checks[-3:]) / 3 <= LOW_MOOD:
        v.add("todo", f"{v.who}'s mood has been low for the last few check-ins. Worth a call, or a mention to the doctor.",
              "Average of the last 3 mood check-ins is 'Low' or below.")


def _plan_checks(v: _View) -> None:
    """7. Patterns and safety checks, for the caretakers who can change the plan."""
    if not v.editor or v.elder:
        return
    adh = db.adherence(v.family_id, 7, today=v.today)
    for name, (taken, total) in adh["per_med"].items():
        if total >= 3 and taken / total < LOW_ADHERENCE:
            v.add("info", f"{name} is often not confirmed ({taken} of {total} this week). Try moving it to a time that "
                          f"fits {v.who}'s routine, or pairing it with a meal.",
                  f"Less than {round(LOW_ADHERENCE * 100)}% confirmed over the last 7 days.")
    meds = db.meds(v.family_id)
    for w in interactions.check_all([m["name"] for m in meds]).warnings[:2]:
        v.add("todo", f"Check with the pharmacist: {w}", "CareCircle's basic medicine check found this combination.")
    unchecked = [m["name"] for m in meds if not m.get("verified_by")]
    if unchecked:
        v.add("info", f"{_plural(len(unchecked), 'medicine')} not yet checked against the "
                      f"prescription: {', '.join(unchecked[:3])}.",
              "Checking each entry against the prescription label catches typing mistakes.")


def _encouragement(v: _View) -> None:
    """8. A streak of fully confirmed days is worth celebrating."""
    streak = db.adherence(v.family_id, 14, today=v.today)["streak"]
    if streak >= STREAK_DAYS:
        v.add("good", i18n.t("ast_streak", v.lang, n=streak) if v.elder
              else f"{v.who} has confirmed every dose for {streak} days in a row.",
              i18n.t("ast_streak_why", v.lang) if v.elder else "Days in a row with every dose confirmed.")


RULES: tuple[Callable[[_View], None], ...] = (
    _help_pressed, _doses, _med_changes, _appointments, _refills, _wellbeing, _plan_checks, _encouragement)


def suggestions(actor: Actor, now: datetime | None = None) -> list[Suggestion]:
    """What needs attention now for this person, most urgent first (at most MAX_ITEMS)."""
    now = now or clock.now()
    elder_name = db.get_family(actor["family_id"])["elder_name"]
    v = _View(actor["family_id"], actor["role"], actor.get("lang") or "en", _first(elder_name), now)
    for rule in RULES:
        rule(v)
    v.out.sort(key=lambda s: LEVELS.index(s["level"]))
    return v.out[:MAX_ITEMS]
