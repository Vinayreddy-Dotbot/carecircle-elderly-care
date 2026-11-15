"""Issues 25, 29-37, 45: reminder content, retries, expiry, single sender across instances, recovery."""
import threading
import time
from datetime import datetime, timedelta

import pytest

import clock
import config
import db
import health
import notifier
import services


def at(hhmm, days=0):
    return datetime.combine(clock.today() + timedelta(days=days), datetime.strptime(hhmm, "%H:%M").time())


@pytest.fixture
def meds(circle):
    ct = circle["caretaker"]
    services.add_medicine(ct, "Metformin", "500 mg", "with food", ["08:00", "20:00"], clock.today() - timedelta(days=1), stock=5)
    services.add_medicine(ct, "Amlodipine", "5 mg", "", ["08:00"], clock.today() - timedelta(days=1), stock=30)
    return circle


def outbox(kind=None):
    rows = db.q("SELECT * FROM outbox ORDER BY id")
    return [r for r in rows if kind is None or r["kind"] == kind]


def test_minimal_emails_name_no_medicines(meds):
    notifier.run_reminders(now=at("08:05"), family_ids=[meds["fid"]])
    due = outbox("dose_due")
    assert len(due) == 1                              # both 08:00 medicines in one email
    assert due[0]["to_email"] == "asha@mail.test"
    for r in outbox():
        assert "Metformin" not in r["subject"] + r["body"] and "Amlodipine" not in r["subject"] + r["body"]
    assert "8:00 AM medicines (2)" in due[0]["body"]


def test_full_detail_is_opt_in(meds):
    services.update_settings(meds["caretaker"], email_detail="full")
    notifier.run_reminders(now=at("08:05"), family_ids=[meds["fid"]])
    body = outbox("dose_due")[0]["body"]
    assert "Metformin 500 mg (with food)" in body and "Amlodipine 5 mg" in body


def test_late_dose_becomes_unconfirmed_not_missed(meds):
    notifier.run_reminders(now=at("08:45"), family_ids=[meds["fid"]])
    doses = [d for d in db.doses_for_day(meds["fid"], clock.today().isoformat()) if d["scheduled_time"] == "08:00"]
    assert {d["status"] for d in doses} == {"unconfirmed"}
    alert = outbox("dose_unconfirmed")
    assert len(alert) == 3 and "does not mean it was missed" in alert[0]["body"]


def test_yesterdays_leftovers_are_resolved_quietly(meds):
    notifier.run_reminders(now=at("07:00"), family_ids=[meds["fid"]])
    y = db.doses_for_day(meds["fid"], (clock.today() - timedelta(days=1)).isoformat())
    assert y and {d["status"] for d in y} == {"unconfirmed"}
    assert not outbox("dose_unconfirmed")


def test_reminders_are_deduplicated(meds):
    for _ in range(3):
        notifier.run_reminders(now=at("08:05"), family_ids=[meds["fid"]])
    assert len(outbox("dose_due")) == 1


def test_refill_and_digest(meds):
    notifier.run_reminders(now=at("20:30"), family_ids=[meds["fid"]])
    assert outbox("low_stock") and outbox("digest")
    assert "Open CareCircle for details" in outbox("digest")[0]["body"]


def _smtp(monkeypatch, fn):
    monkeypatch.setenv("SMTP_USER", "app@mail.example")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setattr(notifier, "_send_smtp", fn)


def _real_recipient(circle):
    db.update_member(circle["fid"], circle["elder"]["id"], email="asha@mail.example")  # not a test domain


def test_failed_send_backs_off_then_gives_up(meds, monkeypatch):
    monkeypatch.setenv("EMAIL_MAX_ATTEMPTS", "3")

    def boom(*a):
        raise OSError("network down")
    _smtp(monkeypatch, boom)
    _real_recipient(meds)
    notifier.queue(meds["fid"], db.get_member(meds["elder"]["id"]), "s", "b", "test", "k1")
    t = time.time()
    assert notifier.dispatch(now=t) == 1
    row = outbox("test")[0]
    assert row["status"] == "pending" and row["attempts"] == 1 and row["next_attempt_at"] >= t + 59
    assert "network down" in row["error"]
    assert notifier.dispatch(now=t + 1) == 0          # waits for the backoff
    notifier.dispatch(now=t + 61)
    notifier.dispatch(now=t + 10_000)
    row = outbox("test")[0]
    assert row["status"] == "dead" and row["attempts"] == 3
    assert notifier.delivery_problems(meds["fid"]) == 1


def test_expired_reminders_are_dropped_not_sent_late(meds, monkeypatch):
    sent = []
    _smtp(monkeypatch, lambda *a: sent.append(a))
    _real_recipient(meds)
    notifier.queue(meds["fid"], db.get_member(meds["elder"]["id"]), "s", "b", "dose_due", "k2", expires_in=60)
    notifier.dispatch(now=time.time() + 120)
    assert outbox("dose_due")[0]["status"] == "expired" and not sent


def test_two_dispatchers_never_send_the_same_email(meds, monkeypatch):
    sent, lock = [], threading.Lock()

    def slow(to, subject, body):
        time.sleep(0.02)
        with lock:
            sent.append(subject)
    _smtp(monkeypatch, slow)
    _real_recipient(meds)
    elder = db.get_member(meds["elder"]["id"])
    for i in range(20):
        notifier.queue(meds["fid"], elder, f"s{i}", "b", "test", f"k{i}")
    threads = [threading.Thread(target=notifier.dispatch, kwargs={"owner": f"w{i}"}) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(sent) == sorted(f"s{i}" for i in range(20))


def test_crashed_claim_is_picked_up_again(meds, monkeypatch):
    sent = []
    _smtp(monkeypatch, lambda *a: sent.append(a))
    _real_recipient(meds)
    i = notifier.queue(meds["fid"], db.get_member(meds["elder"]["id"]), "s", "b", "test", "k3")
    db.x("UPDATE outbox SET status='sending', claimed_by='dead-worker', claimed_until=? WHERE id=?", (time.time() - 1, i))
    notifier.dispatch()
    assert sent and outbox("test")[0]["status"] == "sent"


def test_only_one_instance_holds_the_reminder_lease():
    t = time.time()
    assert db.acquire_lease("reminders", "a", 120, t)
    assert not db.acquire_lease("reminders", "b", 120, t + 1)
    assert db.acquire_lease("reminders", "a", 120, t + 60)     # holder renews
    assert db.acquire_lease("reminders", "b", 120, t + 200)    # holder stopped: another takes over


def test_tick_runs_once_per_lease_and_writes_heartbeat(meds):
    assert notifier.tick("worker-1")
    assert not notifier.tick("worker-2")
    ok, msg = health.reminder_engine_status()
    assert ok, msg
    assert db.get_heartbeat("purge")


def test_stale_heartbeat_is_reported(meds):
    db.heartbeat("reminders", time.time() - 3600, "old")
    ok, msg = health.reminder_engine_status()
    assert not ok and "minutes ago" in msg
    assert health.check()["status"] == "fail"


def test_simulation_touches_only_the_callers_family(meds):
    fid2, codes = services.create_circle("Other", "Kamala", "kamala@mail.test", clients=["bs:o"])
    db.add_med(fid2, "Aspirin", "75 mg", "", ["08:00"], clock.today().isoformat(), None, None, 5, "x")
    services.run_reminders_now(meds["caretaker"], at("08:45"))
    other = db.doses_for_day(fid2, clock.today().isoformat())
    assert {d["status"] for d in other} == {"pending"}
    assert not [r for r in outbox() if r["family_id"] == fid2]


def test_help_alert_goes_to_carers_immediately(meds):
    res = services.request_help(meds["elder"])
    assert res["recipients"] == 2 and res["delivered"] == 2 and res["simulated"]
    rows = outbox("help")
    assert {r["to_email"] for r in rows} == {"ravi@mail.test", "meera@mail.test"}
    assert all(r["priority"] == 10 for r in rows)
    services.cancel_help(meds["elder"])
    assert len(outbox("help_cancel")) == 2


def test_scheduler_interval_is_configurable(monkeypatch):
    monkeypatch.setenv("SCHEDULER_INTERVAL_SEC", "300")
    assert config.scheduler_interval() == 300
    monkeypatch.setenv("SCHEDULER_INTERVAL_SEC", "1")
    assert config.scheduler_interval() == 15


def test_header_injection_is_neutralised(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg):
            captured["subject"] = msg["Subject"]

        def quit(self):
            pass
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("SMTP_USER", "a@mail.example")
    monkeypatch.setenv("SMTP_PASS", "x")
    notifier._send_smtp("b@mail.example", "Hi\r\nBcc: evil@mail.example", "body")
    assert "\n" not in captured["subject"] and "\r" not in captured["subject"]


def _real(circle, monkeypatch, sent):
    monkeypatch.setenv("SMTP_USER", "app@mail.example")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setattr(notifier, "_send_smtp", lambda to, subject, body: sent.append((to, subject, body)))
    db.update_member(circle["fid"], circle["elder"]["id"], email="asha@mail.example")


def test_new_appointment_is_emailed_straight_away(circle, monkeypatch):
    sent = []
    _real(circle, monkeypatch, sent)
    when = clock.now().replace(second=0, microsecond=0) + timedelta(days=3)
    res = services.add_appointment(circle["caretaker"], "Dr Iyer", "Cardiology", when, "Apollo Clinic", "Dizziness?")
    assert res["recipients"] == 3 and res["sent"] == 1 and res["simulated_n"] == 2
    to, subject, body = sent[0]
    assert to == "asha@mail.example" and subject == "CareCircle: doctor's appointment added"
    assert "Dr Iyer" not in subject + body and "24 hours and 2 hours before" in body       # minimal by default
    # the regular 24-hour reminder still comes later
    notifier.run_reminders(now=when - timedelta(hours=20), family_ids=[circle["fid"]])
    assert [s for s in sent if "appointment tomorrow" in s[1]]


def test_appointment_tomorrow_is_not_emailed_twice(circle, monkeypatch):
    sent = []
    _real(circle, monkeypatch, sent)
    services.update_settings(circle["caretaker"], email_detail="full")
    when = clock.now().replace(second=0, microsecond=0) + timedelta(hours=20)
    services.add_appointment(circle["caretaker"], "Dr Iyer", "", when, "Apollo Clinic", "")
    notifier.run_reminders(family_ids=[circle["fid"]])
    assert len(sent) == 1 and sent[0][1].startswith("Appointment added: Dr Iyer")


def test_cancelled_appointment_is_emailed(circle, monkeypatch):
    sent = []
    _real(circle, monkeypatch, sent)
    when = clock.now().replace(second=0, microsecond=0) + timedelta(days=5)
    services.add_appointment(circle["caretaker"], "Dr Rao", "", when, "", "")
    appt = db.appts(circle["fid"])[0]["id"]
    res = services.cancel_appointment(circle["caretaker"], appt)
    assert res["sent"] == 1 and sent[-1][1] == "CareCircle: doctor's appointment cancelled"


def test_past_appointment_sends_nothing(circle):
    res = services.add_appointment(circle["caretaker"], "Dr Old", "", clock.now() - timedelta(days=1), "", "")
    assert res["recipients"] == 0 and not outbox("appt_added")
