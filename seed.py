"""Sample family for demos. Refuses to run in production, and marks everything it creates as demo data."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import clock
import config
import db
import services


class DemoDisabled(Exception):
    pass


def seed_demo(clients: Sequence[str] = ()) -> dict[str, str]:
    """Creates the sample Sharma family with a week of history. Returns dict of access codes."""
    if not config.demo_features():
        raise DemoDisabled("The sample family is disabled in production.")
    fid, codes = services.create_circle(
        "Sharma Family (sample)", "Asha Sharma", "asha.demo@example.com",
        [("Ravi (son)", "ravi.demo@example.com", "caretaker"), ("Meera (daughter)", "meera.demo@example.com", "family")],
        clients=clients, is_demo=True)
    db.update_family_settings(fid, ai_enabled=True)   # made-up data, so the AI assistant is on for demos
    labels = list(codes)
    out = {"elder": codes[labels[0]], "caretaker": codes[labels[1]], "family": codes[labels[2]]}
    start = (clock.today() - timedelta(days=7)).isoformat()
    db.add_med(fid, "Metformin", "500 mg", "Take with food", ["08:00", "20:00"], start, None, 40, 6, "Ravi (son)")
    db.add_med(fid, "Amlodipine", "5 mg", "", ["09:00"], start, None, 18, 5, "Ravi (son)")
    db.add_med(fid, "Vitamin D", "1000 IU", "", ["13:00"], start, None, 25, 5, "Asha Sharma")
    for i in range(7, 0, -1):  # mostly good, evenings a bit weak
        day = (clock.today() - timedelta(days=i)).isoformat()
        for d in db.doses_for_day(fid, day):
            evening = d["scheduled_time"] >= "17:00"
            miss = (evening and i in (5, 3)) or (d["name"] == "Vitamin D" and i == 4)
            db.set_dose(fid, d["id"], "unconfirmed" if miss else "taken", "" if miss else "Asha Sharma",
                        datetime.fromisoformat(f"{day}T{d['scheduled_time']}"))
        db.set_checkin(fid, day, 4 if i % 3 else 3, "")
    nxt = (clock.now() + timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)
    db.add_appt(fid, "Dr. Iyer", "Cardiologist", nxt.isoformat(timespec="minutes"), "Apollo Clinic, Indiranagar",
                "Bring last month's BP log.", "Is the dizziness in the morning related to Amlodipine?", "Ravi (son)")
    return out
