"""Issues 23-26, 28, 61: dose status, stock accuracy under repeated and concurrent updates, verification."""
import threading

import clock
import db
import services


def _med(circle, stock=10, times=("08:00",)):
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "", list(times), clock.today(), stock=stock)
    med = db.meds(circle["fid"])[0]
    return med, db.doses_for_day(circle["fid"], clock.today().isoformat())


def test_taken_twice_counts_once_and_undo_restores(circle):
    med, doses = _med(circle)
    d = doses[0]["id"]
    assert services.mark_dose(circle["elder"], d, "taken")
    assert not services.mark_dose(circle["family"], d, "taken")      # already taken: no double count
    assert db.get_med(circle["fid"], med["id"])["stock"] == 9
    services.mark_dose(circle["elder"], d, "pending")
    assert db.get_med(circle["fid"], med["id"])["stock"] == 10
    services.mark_dose(circle["elder"], d, "skipped")
    services.mark_dose(circle["elder"], d, "taken")
    assert db.get_med(circle["fid"], med["id"])["stock"] == 9
    reasons = [e["reason"] for e in db.stock_events(circle["fid"], med["id"])]
    assert reasons.count("dose taken") == 2 and reasons.count("dose un-marked") == 1


def test_stock_at_zero_does_not_go_negative_or_drift_up(circle):
    med, doses = _med(circle, stock=1, times=("08:00", "20:00"))
    a, b = doses[0]["id"], doses[1]["id"]
    services.mark_dose(circle["elder"], a, "taken")
    services.mark_dose(circle["elder"], b, "taken")
    assert db.get_med(circle["fid"], med["id"])["stock"] == 0
    services.mark_dose(circle["elder"], b, "pending")   # b never decremented, so undo must not add one
    assert db.get_med(circle["fid"], med["id"])["stock"] == 0


def test_concurrent_marks_of_the_same_dose(circle):
    med, doses = _med(circle, stock=50)
    d, errors = doses[0]["id"], []

    def tap(actor):
        try:
            services.mark_dose(actor, d, "taken")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=tap, args=(circle[r],)) for r in ("elder", "caretaker", "family") * 6]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert db.get_med(circle["fid"], med["id"])["stock"] == 49


def test_concurrent_marks_of_different_doses(circle):
    times = [f"{h:02d}:00" for h in range(6, 14)]
    med, doses = _med(circle, stock=100, times=times)
    errors = []

    def tap(dose_id, actor):
        try:
            services.mark_dose(actor, dose_id, "taken")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=tap, args=(d["id"], circle[r])) for d in doses for r in ("elder", "family")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert db.get_med(circle["fid"], med["id"])["stock"] == 100 - len(times)


def test_changing_times_removes_doses_at_old_times(circle):
    med, _ = _med(circle, times=("08:00",))
    services.edit_medicine(circle["caretaker"], med["id"], "500 mg", ["09:00"], "")
    times = [d["scheduled_time"] for d in db.doses_for_day(circle["fid"], clock.today().isoformat())]
    assert times == ["09:00"]


def test_dose_change_clears_prescription_check(circle):
    med, _ = _med(circle)
    services.verify_medicine(circle["caretaker"], med["id"], "Dr Rao")
    assert db.get_med(circle["fid"], med["id"])["verified_by"] == "Ravi"
    services.edit_medicine(circle["caretaker"], med["id"], "500 mg", ["08:00"], "after food")   # instructions only
    assert db.get_med(circle["fid"], med["id"])["verified_by"] == "Ravi"
    services.edit_medicine(circle["caretaker"], med["id"], "1000 mg", ["08:00"], "after food")
    m = db.get_med(circle["fid"], med["id"])
    assert m["verified_by"] == "" and m["verified_at"] is None
    log = db.activity(circle["fid"], 1)[0]["what"]
    assert "'500 mg' -> '1000 mg'" in log


def test_activity_log_records_undo(circle):
    _, doses = _med(circle)
    services.mark_dose(circle["family"], doses[0]["id"], "taken")
    services.mark_dose(circle["family"], doses[0]["id"], "pending")
    whats = [a["what"] for a in db.activity(circle["fid"], 2)]
    assert whats[0].startswith("undid") and whats[1].startswith("confirmed")


def test_adherence_reports_unconfirmed_separately(circle):
    _, doses = _med(circle, times=("08:00", "20:00"))
    services.mark_dose(circle["elder"], doses[0]["id"], "taken")
    db.auto_unconfirm(circle["fid"], doses[1]["id"], clock.now())
    a = db.adherence(circle["fid"], 1)
    assert a["pct"] == 50 and a["unconfirmed"] == 1
