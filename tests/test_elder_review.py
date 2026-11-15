"""The elder stays at the centre: they hear about medicine changes made by others, can say they don't agree,
and can choose to approve changes before they start."""
import pytest

import authz
import clock
import db
import notifier
import services
from authz import Forbidden


@pytest.fixture
def mail(circle, monkeypatch):
    sent = []
    monkeypatch.setenv("SMTP_USER", "app@mail.example")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setattr(notifier, "_send_smtp", lambda to, subject, body: sent.append((to, subject, body)))
    for role, addr in (("elder", "asha@mail.example"), ("caretaker", "ravi@mail.example"), ("family", "meera@mail.example")):
        db.update_member(circle["fid"], circle[role]["id"], email=addr)
    return sent


def _med(circle, actor="elder"):
    return services.add_medicine(circle[actor], "Metformin", "500 mg", "", ["08:00"], clock.today())["id"]


def test_caretaker_change_starts_now_and_elder_is_told(circle, mail):
    med = _med(circle)
    assert services.edit_medicine(circle["caretaker"], med, "1000 mg", ["08:00"], "") == "applied"
    assert db.get_med(circle["fid"], med)["dosage"] == "1000 mg"
    [c] = services.changes_for_elder(circle["elder"])
    assert c["status"] == "applied" and services.describe_change(c) == "changed Metformin: dose 500 mg -> 1000 mg"
    to, subject, body = mail[-1]
    assert to == "asha@mail.example" and subject == "CareCircle: a change to your medicines"
    assert "Metformin" not in body and "I don't agree" in body                      # minimal by default
    services.review_change(circle["elder"], c["id"], "ok")
    assert services.changes_for_elder(circle["elder"]) == []


def test_elder_disagreeing_tells_the_person_and_caretakers(circle, mail):
    services.update_settings(circle["elder"], email_detail="full")
    med = _med(circle)
    services.stop_medicine(circle["caretaker"], med)
    [c] = services.changes_for_elder(circle["elder"])
    services.review_change(circle["elder"], c["id"], "disagree", "The doctor said to keep taking it")
    reply = [m for m in mail if "doesn't agree" in m[1]]
    assert {m[0] for m in reply} == {"ravi@mail.example"}
    assert "stopped Metformin" in reply[0][2] and "keep taking it" in reply[0][2]
    assert db.changes(circle["fid"], ("disputed",))                                  # flagged for everyone
    services.review_change(circle["elder"], c["id"], "ok")                           # sorted out later
    assert not db.changes(circle["fid"], ("disputed",))


def test_elders_own_changes_need_no_review(circle, mail):
    med = _med(circle)
    services.edit_medicine(circle["elder"], med, "250 mg", ["09:00"], "")
    assert services.changes_for_elder(circle["elder"]) == [] and not mail


def test_only_the_elder_can_switch_on_approval_first(circle):
    with pytest.raises(Forbidden):
        services.update_settings(circle["caretaker"], elder_approval=True)
    services.update_settings(circle["elder"], elder_approval=True)
    with pytest.raises(Forbidden):
        services.update_settings(circle["caretaker"], elder_approval=False)
    services.update_settings(circle["caretaker"], email_detail="full", elder_approval=True)   # unchanged value is fine
    assert db.family_settings(circle["fid"])["elder_approval"]


def test_with_approval_first_nothing_changes_until_the_elder_approves(circle, mail):
    med = _med(circle)
    services.update_settings(circle["elder"], elder_approval=True)
    assert services.edit_medicine(circle["caretaker"], med, "1000 mg", ["08:00", "20:00"], "") == "pending"
    assert db.get_med(circle["fid"], med)["dosage"] == "500 mg"
    assert mail[-1][1] == "CareCircle: a medicine change needs your OK"
    new = services.add_medicine(circle["caretaker"], "Aspirin", "75 mg", "", ["09:00"], clock.today())
    assert new["status"] == "proposed"
    with pytest.raises(Forbidden):
        services.approve_medicine(circle["caretaker"], new["id"])
    assert services.stop_medicine(circle["caretaker"], med) == "pending"
    edit, add, stop = services.changes_for_elder(circle["elder"])
    services.review_change(circle["elder"], edit["id"], "approve")
    m = db.get_med(circle["fid"], med)
    assert m["dosage"] == "1000 mg" and m["times"] == "08:00,20:00"
    services.review_change(circle["elder"], add["id"], "decline", "Ask the doctor first")
    assert db.get_med(circle["fid"], new["id"])["status"] == "declined"
    assert any("declined" in s for _, s, _ in mail)
    services.review_change(circle["elder"], stop["id"], "approve")
    assert db.get_med(circle["fid"], med)["status"] == "stopped"
    with pytest.raises(services.Invalid):                                            # already answered
        services.review_change(circle["elder"], stop["id"], "approve")


def test_family_suggestion_approved_by_caretaker_still_reaches_the_elder(circle, mail):
    s = services.add_medicine(circle["family"], "Crocin", "500 mg", "", ["21:00"], clock.today())
    assert services.changes_for_elder(circle["elder"])[0]["status"] == "pending"
    services.approve_medicine(circle["caretaker"], s["id"])
    [c] = services.changes_for_elder(circle["elder"])
    assert c["status"] == "applied" and "approved Meera's suggestion" in mail[-1][2]


def test_elder_approving_a_suggestion_from_the_medicines_tab(circle):
    s = services.add_medicine(circle["family"], "Crocin", "500 mg", "", ["21:00"], clock.today())
    services.approve_medicine(circle["elder"], s["id"])
    assert db.get_med(circle["fid"], s["id"])["status"] == "active"
    assert services.changes_for_elder(circle["elder"]) == []


def test_only_the_elder_reviews_and_only_in_their_circle(circle):
    med = _med(circle)
    services.edit_medicine(circle["caretaker"], med, "1000 mg", ["08:00"], "")
    [c] = services.changes_for_elder(circle["elder"])
    for role in ("caretaker", "family"):
        with pytest.raises(Forbidden):
            services.review_change(circle[role], c["id"], "ok")
    assert not authz.can(circle["caretaker"], "med.review")
    _, codes = services.create_circle("Other", "Kamala", "", clients=["bs:o"])
    from conftest import login
    other_elder = login(next(v for k, v in codes.items() if "elder" in k), "bs:oe")
    with pytest.raises(db.NotFound):
        services.review_change(other_elder, c["id"], "ok")


def test_changes_are_deleted_with_the_circle(circle):
    med = _med(circle)
    services.edit_medicine(circle["caretaker"], med, "1000 mg", ["08:00"], "")
    services.delete_circle(circle["elder"], "Test Family")
    assert db.q1("SELECT COUNT(*) AS n FROM med_changes")["n"] == 0
