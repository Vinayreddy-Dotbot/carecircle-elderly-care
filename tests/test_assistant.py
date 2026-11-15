"""The assistant card: what each person is told depends on who they are, the time, and the care data."""
import time
from datetime import datetime, timedelta

import pytest

import assistant
import clock
import db
import services


def at(hhmm, days=0):
    return datetime.combine(clock.today() + timedelta(days=days), datetime.strptime(hhmm, "%H:%M").time())


@pytest.fixture
def plan(circle):
    services.add_medicine(circle["elder"], "Metformin", "500 mg", "with food", ["08:00", "20:00"], clock.today(), stock=40)
    return circle


def texts(items):
    return [i["text"] for i in items]


def test_every_suggestion_explains_itself(plan):
    items = assistant.suggestions(plan["caretaker"], now=at("08:45"))
    assert items and all(i["why"] and i["level"] in assistant.LEVELS for i in items)


def test_late_dose_is_urgent_and_worded_for_each_person(plan):
    elder = assistant.suggestions(plan["elder"], now=at("08:45"))
    carer = assistant.suggestions(plan["caretaker"], now=at("08:45"))
    assert elder[0]["level"] == "urgent" and "haven't marked your 8:00 AM Metformin" in elder[0]["text"]
    assert carer[0]["level"] == "urgent" and "Asha hasn't confirmed 1 dose today: Metformin (8:00 AM)" in carer[0]["text"]


def test_due_now_then_next_dose(plan):
    due = assistant.suggestions(plan["elder"], now=at("08:05"))
    assert "It's time for Metformin 500 mg (with food)." in texts(due)
    d = next(d for d in db.doses_for_day(plan["fid"], clock.today().isoformat()) if d["scheduled_time"] == "08:00")
    services.mark_dose(plan["elder"], d["id"], "taken")
    later = assistant.suggestions(plan["elder"], now=at("10:00"))
    assert "Next: Metformin 500 mg at 8:00 PM." in texts(later)


def test_all_done_is_celebrated(plan):
    for d in db.doses_for_day(plan["fid"], clock.today().isoformat()):
        services.mark_dose(plan["elder"], d["id"], "taken")
    assert any(i["level"] == "good" and "All of today's medicines are done" in i["text"]
               for i in assistant.suggestions(plan["elder"], now=at("21:00")))


def test_refill_estimate_uses_doses_per_day(plan):
    med = db.meds(plan["fid"])[0]["id"]
    services.adjust_stock(plan["caretaker"], med, 6, "recount")
    carer = texts(assistant.suggestions(plan["caretaker"], now=at("12:00")))
    assert any("Metformin runs out in about 3 days (6 left, 2 a day)" in t for t in carer)
    elder = texts(assistant.suggestions(plan["elder"], now=at("12:00")))
    assert any("Ask your family to order more" in t for t in elder)
    family = texts(assistant.suggestions(plan["family"], now=at("12:00")))
    assert not any("Order a refill" in t for t in family)           # only people who can act on it


def test_appointment_tomorrow(plan):
    services.add_appointment(plan["caretaker"], "Dr Iyer", "", at("11:00", days=1), "Apollo", "")
    carer = texts(assistant.suggestions(plan["family"], now=at("12:00")))
    assert any("Asha sees Dr Iyer tomorrow at 11:00 AM. No questions added yet" in t for t in carer)
    elder = texts(assistant.suggestions(plan["elder"], now=at("12:00")))
    assert "You see Dr Iyer tomorrow at 11:00 AM." in elder


def test_help_button_goes_to_the_top_for_carers(plan):
    services.request_help(plan["elder"])
    first = assistant.suggestions(plan["family"], now=at("12:00"))[0]
    assert first["level"] == "urgent" and "pressed 'I need help'" in first["text"]
    db.x("UPDATE security_events SET at=? WHERE event='help_pressed'", (time.time() - 3 * 3600,))
    assert "pressed 'I need help'" not in " ".join(texts(assistant.suggestions(plan["family"], now=at("12:00"))))


def test_medicine_changes_waiting_and_disputed(plan):
    med = db.meds(plan["fid"])[0]["id"]
    services.edit_medicine(plan["caretaker"], med, "1000 mg", ["08:00", "20:00"], "")
    assert any("1 change(s) to your medicines need your answer" in t
               for t in texts(assistant.suggestions(plan["elder"], now=at("12:00"))))
    c = services.changes_for_elder(plan["elder"])[0]
    services.review_change(plan["elder"], c["id"], "disagree")
    urgent = [i["text"] for i in assistant.suggestions(plan["caretaker"], now=at("12:00")) if i["level"] == "urgent"]
    assert any("doesn't agree with 1 medicine change" in t for t in urgent)


def test_low_mood_and_missing_check_in(plan):
    assert any("How are you feeling today?" in t for t in texts(assistant.suggestions(plan["elder"], now=at("10:00"))))
    for i in range(3):
        db.set_checkin(plan["fid"], (clock.today() - timedelta(days=i)).isoformat(), 2)
    assert any("mood has been low" in t for t in texts(assistant.suggestions(plan["family"], now=at("10:00"))))


def test_pharmacist_check_for_risky_combination(plan):
    services.add_medicine(plan["caretaker"], "Warfarin", "5 mg", "", ["09:00"], clock.today())
    services.add_medicine(plan["caretaker"], "Brufen", "400 mg", "", ["13:00"], clock.today())
    assert any(t.startswith("Check with the pharmacist") and "bleeding" in t
               for t in texts(assistant.suggestions(plan["caretaker"], now=at("07:00"))))


def test_hindi_for_the_elder(plan):
    db.update_member(plan["fid"], plan["elder"]["id"], lang="hi")
    elder = dict(plan["elder"], lang="hi")
    assert "दर्ज नहीं की है" in assistant.suggestions(elder, now=at("08:45"))[0]["text"]


def test_urgent_first_and_at_most_six(plan):
    for i in range(8):
        services.add_medicine(plan["caretaker"], f"Med{i}", "1", "", ["08:00"], clock.today(), stock=2)
    items = assistant.suggestions(plan["caretaker"], now=at("09:00"))
    assert len(items) <= assistant.MAX_ITEMS
    levels = [assistant.LEVELS.index(i["level"]) for i in items]
    assert levels == sorted(levels) and items[0]["level"] == "urgent"


def test_sample_circles_have_the_ai_assistant_on():
    import seed
    seed.seed_demo(clients=["bs:demo"])
    demo = db.q1("SELECT id FROM families ORDER BY id DESC LIMIT 1")["id"]
    assert db.family_settings(demo)["ai_enabled"] is True
    fid, _ = services.create_circle("Real Family", "Kamala", "", clients=["bs:real"])
    assert db.family_settings(fid)["ai_enabled"] is False
