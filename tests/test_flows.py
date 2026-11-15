"""Issues 44, 60, 61: the whole journey end to end, many users at once, and the admin tools."""
import json
import threading
import time
from datetime import datetime, timedelta

import auth
import clock
import db
import manage
import notifier
import services
import worker
from conftest import login


def at(hhmm):
    return datetime.combine(clock.today(), datetime.strptime(hhmm, "%H:%M").time())


def test_full_journey(circle):
    ct, fam, elder = circle["caretaker"], circle["family"], circle["elder"]
    # caretaker adds medicines; family suggests one that clashes; caretaker approves
    services.add_medicine(ct, "Warfarin", "5 mg", "", ["08:00"], clock.today(), stock=10)
    services.add_medicine(ct, "Metformin", "500 mg", "with food", ["08:00", "20:00"], clock.today(), stock=10)
    s = services.add_medicine(fam, "Brufen", "400 mg", "", ["13:00"], clock.today())
    assert s["status"] == "proposed" and "bleeding" in s["check"].warnings[0]
    services.approve_medicine(ct, s["id"])
    # reminders: dose due at 08:05 goes to the elder only
    notifier.run_reminders(now=at("08:05"), family_ids=[circle["fid"]])
    due = db.q("SELECT * FROM outbox WHERE kind='dose_due'")
    assert [r["to_email"] for r in due] == ["asha@mail.test"] and due[0]["status"] == "simulated"
    # the elder confirms both 08:00 doses; stock drops by one each
    for d in db.doses_for_day(circle["fid"], clock.today().isoformat()):
        if d["scheduled_time"] == "08:00":
            services.mark_dose(elder, d["id"], "taken")
    assert {m["name"]: m["stock"] for m in db.meds(circle["fid"])} == {"Brufen": None, "Metformin": 9, "Warfarin": 9}
    # the 13:00 dose is never confirmed: family is alerted at 13:40
    notifier.run_reminders(now=at("13:40"), family_ids=[circle["fid"]])
    alerts = db.q("SELECT * FROM outbox WHERE kind='dose_unconfirmed'")
    assert len(alerts) == 3
    # family member checks, confirms it was taken late; adherence reflects it
    d13 = next(d for d in db.doses_for_day(circle["fid"], clock.today().isoformat()) if d["scheduled_time"] == "13:00")
    services.mark_dose(fam, d13["id"], "taken")
    # evening summary to caretaker + family
    notifier.run_reminders(now=at("20:30"), family_ids=[circle["fid"]])
    digest = db.q("SELECT * FROM outbox WHERE kind='digest'")
    assert {r["to_email"] for r in digest} == {"ravi@mail.test", "meera@mail.test"}
    a = db.adherence(circle["fid"], 1)
    assert a["pct"] == 75 and a["unconfirmed"] == 1          # 20:00 Metformin not confirmed yet
    log = " | ".join(x["what"] for x in db.activity(circle["fid"], 50))
    assert "suggested Brufen" in log and "approved Brufen" in log and "confirmed Brufen (13:00) was taken" in log


def test_many_people_at_once(circle):
    """20 simultaneous users signing in, viewing and changing the same circle (SQLite under concurrency)."""
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "", [f"{h:02d}:00" for h in range(6, 14)],
                          clock.today(), stock=500)
    db.add_appt(circle["fid"], "Dr Y", "", "2099-01-01T10:00", "", "", "", "Ravi")
    appt = db.appts(circle["fid"])[0]["id"]
    doses = [d["id"] for d in db.doses_for_day(circle["fid"], clock.today().isoformat())]
    errors, codes = [], list(circle["codes"].values())

    def user(i):
        try:
            a = login(codes[i % 3], f"bs:user{i}")
            for d in doses:
                services.mark_dose(a, d, "taken" if (i + d) % 2 else "pending")
            services.add_question(a, appt, f"question {i}")
            db.doses_for_day(a["family_id"], clock.today().isoformat())
            db.adherence(a["family_id"], 7)
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=user, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    questions = db.get_appt(circle["fid"], appt)["questions"].splitlines()
    assert len(questions) == 20                          # no lost updates
    taken = sum(d["status"] == "taken" for d in db.doses_for_day(circle["fid"], clock.today().isoformat()))
    applied = db.q1("SELECT COALESCE(SUM(stock_applied),0) AS n FROM doses")["n"]
    assert db.meds(circle["fid"])[0]["stock"] == 500 - taken and applied == taken   # stock matches the doses


def test_admin_cli(circle, tmp_path, capsys):
    assert manage.main(["backup", "--out", str(tmp_path / "x.ccbak")]) == 0
    assert manage.main(["verify-backup", str(tmp_path / "x.ccbak")]) == 0
    assert '"ok": true' in capsys.readouterr().out
    assert manage.main(["restore", str(tmp_path / "x.ccbak")]) == 1          # needs --yes
    assert manage.main(["reset-code", str(circle["elder"]["id"])]) == 0
    new = capsys.readouterr().out.strip().split(": ")[-1]
    assert auth.login_with_code(new, ["bs:cli"]).status == "ok"
    assert manage.main(["revoke-sessions"]) == 0
    assert "revoked" in capsys.readouterr().out
    assert auth.current(circle["caretaker"]["_token"]) is None
    assert manage.main(["health", "--json"]) == 1                            # reminders never ran
    assert json.loads(capsys.readouterr().out)["status"] == "fail"
    assert worker.main(["--once"]) == 0
    assert manage.main(["health"]) == 0
    assert manage.main(["export-family", str(circle["fid"]), "--out", str(tmp_path / "e.json")]) == 0
    assert "Asha Sharma" in (tmp_path / "e.json").read_text()


def test_restart_recovery(circle, monkeypatch):
    """After a crash, pending emails are still in the database and another worker takes over the lease."""
    sent = []
    monkeypatch.setenv("SMTP_USER", "a@mail.example")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setattr(notifier, "_send_smtp", lambda *a: sent.append(a))
    db.update_member(circle["fid"], circle["elder"]["id"], email="asha@mail.example")
    notifier.queue(circle["fid"], db.get_member(circle["elder"]["id"]), "s", "b", "test", "restart")
    t = time.time()
    assert db.acquire_lease("reminders", "crashed-instance", 120, t)
    assert not notifier.tick("new-instance")                 # lease still held
    db.x("UPDATE leases SET expires_at=? WHERE name='reminders'", (t - 1,))
    assert notifier.tick("new-instance")                     # takes over once the lease lapses
    assert sent and db.q1("SELECT status FROM outbox WHERE kind='test'")["status"] == "sent"
