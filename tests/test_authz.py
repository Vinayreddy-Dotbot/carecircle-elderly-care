"""Issue 10: permissions are enforced below the UI, and every record lookup is scoped to the caller's family."""
import pytest

import authz
import clock
import db
import services
from authz import Forbidden
from conftest import login


@pytest.fixture
def other(circle):
    fid, codes = services.create_circle("Other Family", "Kamala", "", [("Arjun", "", "caretaker")], clients=["bs:o"])
    arjun = login(next(v for k, v in codes.items() if "caretaker" in k), "bs:other")
    med = services.add_medicine(arjun, "Metformin", "500 mg", "", ["08:00"], clock.today())["id"]
    dose = db.doses_for_day(fid, clock.today().isoformat())[0]["id"]
    db.add_appt(fid, "Dr X", "", "2099-01-01T10:00", "", "", "", "Arjun")
    appt = db.appts(fid)[0]["id"]
    return {"fid": fid, "actor": arjun, "med": med, "dose": dose, "appt": appt,
            "member": db.members(fid, roles=["elder"])[0]["id"]}


def test_family_role_can_only_view_mark_suggest_and_ask(circle):
    fam = circle["family"]
    med = services.add_medicine(fam, "Aspirin", "75 mg", "", ["09:00"], clock.today())
    assert med["status"] == "proposed"
    for call in (lambda: services.approve_medicine(fam, med["id"]),
                 lambda: services.stop_medicine(fam, med["id"]),
                 lambda: services.edit_medicine(fam, med["id"], "1", ["09:00"], ""),
                 lambda: services.invite(fam, "X", "", "family"),
                 lambda: services.remove_member(fam, circle["elder"]["id"]),
                 lambda: services.reset_code(fam, circle["elder"]["id"]),
                 lambda: services.update_email(fam, circle["elder"]["id"], "x@mail.test"),
                 lambda: services.update_settings(fam, email_detail="full"),
                 lambda: services.export_data(fam),
                 lambda: services.delete_circle(fam, "Test Family"),
                 lambda: services.outbox(fam),
                 lambda: services.security_log(fam),
                 lambda: services.check_in(fam, 4),
                 lambda: services.request_help(fam)):
        with pytest.raises(Forbidden):
            call()
    assert not authz.can(fam, "member.view_contact")


def test_family_can_mark_doses_and_add_questions(circle):
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "", ["08:00"], clock.today())
    dose = db.doses_for_day(circle["fid"], clock.today().isoformat())[0]
    assert services.mark_dose(circle["family"], dose["id"], "taken")
    db.add_appt(circle["fid"], "Dr Y", "", "2099-01-01T10:00", "", "", "", "Ravi")
    services.add_question(circle["family"], db.appts(circle["fid"])[0]["id"], "Is the dizziness related?")


@pytest.mark.parametrize("action", ["dose", "approve", "edit", "stop", "appt", "question", "remove", "reset", "email"])
def test_ids_from_another_family_are_not_found(circle, other, action):
    me = circle["caretaker"]
    calls = {
        "dose": lambda: services.mark_dose(me, other["dose"], "taken"),
        "approve": lambda: services.approve_medicine(me, other["med"]),
        "edit": lambda: services.edit_medicine(me, other["med"], "1000 mg", ["08:00"], ""),
        "stop": lambda: services.stop_medicine(me, other["med"]),
        "appt": lambda: services.cancel_appointment(me, other["appt"]),
        "question": lambda: services.add_question(me, other["appt"], "hi"),
        "remove": lambda: services.remove_member(me, other["member"]),
        "reset": lambda: services.reset_code(me, other["member"]),
        "email": lambda: services.update_email(me, other["member"], "evil@mail.test"),
    }
    with pytest.raises(db.NotFound):
        calls[action]()
    assert db.get_med(other["fid"], other["med"])["dosage"] == "500 mg"
    assert db.get_dose(other["fid"], other["dose"])["status"] == "pending"


def test_db_layer_scopes_by_family_too(circle, other):
    with pytest.raises(db.NotFound):
        db.update_med(circle["fid"], other["med"], dosage="9 mg")
    with pytest.raises(db.NotFound):
        db.set_dose(circle["fid"], other["dose"], "taken", "x")
    with pytest.raises(db.NotFound):
        db.delete_member(circle["fid"], other["member"])


def test_update_column_names_come_from_an_allow_list(circle, other):
    with pytest.raises(ValueError):
        db.update_med(other["fid"], other["med"], id=999)
    with pytest.raises(ValueError):
        db.update_appt(other["fid"], other["appt"], **{"status='x' --": 1})
    with pytest.raises(ValueError):
        db.update_member(other["fid"], other["member"], role="caretaker")


def test_elder_cannot_be_removed_and_caretaker_cannot_remove_self(circle):
    with pytest.raises(Forbidden):
        services.remove_member(circle["caretaker"], circle["elder"]["id"])
    with pytest.raises(Forbidden):
        services.remove_member(circle["caretaker"], circle["caretaker"]["id"])


def test_time_simulation_is_off_in_production(circle, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(Forbidden):
        services.run_reminders_now(circle["caretaker"], clock.now())


def test_inputs_are_validated(circle):
    ct = circle["caretaker"]
    with pytest.raises(services.Invalid):
        services.add_medicine(ct, "", "500 mg", "", ["08:00"], clock.today())
    with pytest.raises(services.Invalid):
        services.add_medicine(ct, "X", "", "", ["08:00"], clock.today())
    with pytest.raises(services.Invalid):
        services.add_medicine(ct, "X", "1", "", ["25:00"], clock.today())
    with pytest.raises(services.Invalid):
        services.add_medicine(ct, "X" * 81, "1", "", ["08:00"], clock.today())
    with pytest.raises(services.Invalid):
        services.add_medicine(ct, "X", "1", "", ["08:00"], "2026-02-10", end="2026-02-01")
    with pytest.raises(services.Invalid):
        services.invite(ct, "Y", "not-an-email", "family")
    with pytest.raises(services.Invalid):
        services.invite(ct, "Y", "", "elder")
