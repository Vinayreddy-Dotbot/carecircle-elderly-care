"""Issues 5, 6, 54-57, 60: the UI runs for every role, escapes user text, never shows codes again,
and the main flows (sign-in, MFA, code change, adding a medicine) work end to end.

Uses tests/fake_streamlit.py so it runs without the real package; tests/test_app_streamlit.py repeats the key
flows with Streamlit's own AppTest when Streamlit is installed."""
import os
import re
import sys

import pytest

import auth
import clock
import db
import services

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


@pytest.fixture
def st(monkeypatch):
    monkeypatch.setenv("SCHEDULER_MODE", "off")
    saved = {k: sys.modules.get(k) for k in ("streamlit", "streamlit.components", "streamlit.components.v1")}
    import fake_streamlit
    fake = fake_streamlit.install()
    yield fake
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def run(st, **kw):
    import fake_streamlit
    return fake_streamlit.run(st, APP, **kw)


STATIC_ERRORS = ("This permanently deletes the circle",)  # warnings that are always shown on purpose


def errors(log):
    return [t for k, t in log if k == "error" and not t.startswith(STATIC_ERRORS)]


def text(log):
    return "\n".join(t for _, t in log)


def sign_in(st, code):
    return run(st, inputs={"login_code": code}, clicks={"Open"})


def test_login_screen_renders(st):
    log = run(st)
    assert not errors(log) and "CareCircle" in text(log)
    assert ("button", "demo_btn") in log            # sample family only outside production


def test_sample_family_shows_codes_once(st):
    log = run(st, clicks={"demo_btn"})
    assert "Access codes" in text(log) and not errors(log)
    assert "CareCircle doesn't keep a readable copy" in text(log)


def test_wrong_code_shows_generic_error(st):
    log = sign_in(st, "AAAA-BBBB-CCCC")
    assert auth.GENERIC_INVALID in errors(log)


@pytest.mark.parametrize("role", ["elder", "caretaker", "family"])
def test_every_role_sees_its_tabs_without_errors(st, circle, role):
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "with food", ["08:00", "20:00"], clock.today(), stock=4)
    services.add_appointment(circle["caretaker"], "Dr Iyer", "Cardio", clock.now().replace(year=2099), "Clinic", "Q1")
    log = sign_in(st, circle["codes"][role])
    assert not errors(log), errors(log)
    out = text(log)
    assert "Metformin" in out and "Doctor appointments" in out and "Care circle" in out
    if role == "family":
        assert "Reminder emails" not in out and "Privacy and data" not in out
    else:
        assert "Reminder emails" in out and "Privacy and data" in out
    if role == "elder":
        assert "I need help" in out and "call 112" in out
    assert not any(c in out for c in circle["codes"].values())      # codes never shown again


def test_user_text_is_escaped(st, circle):
    services.add_medicine(circle["family"], "<img src=x onerror=alert(1)>", "1", "[click](https://evil.example)",
                          ["08:00"], clock.today())
    services.approve_medicine(circle["caretaker"], db.meds(circle["fid"], ("proposed",))[0]["id"])
    log = sign_in(st, circle["codes"]["elder"])
    rendered = [t for k, t in log if k in ("markdown", "write", "info", "caption", "warning", "subheader")]
    assert not any(re.search(r"(?<!\\)<img", t) for t in rendered)       # only \<img (Markdown) or &lt;img (HTML)
    assert any("&lt;img src=x" in t for t in rendered) and any("\\<img src=x" in t for t in rendered)
    assert not any(re.search(r"(?<!\\)\[click(?<!\\)\]\(", t) for t in rendered)
    spoken = [t for k, t in log if k == "html"]
    assert spoken and all("</script><" not in s.split("<script>")[1].split("</script>")[0] for s in spoken)


def test_add_medicine_with_review_step(st, circle):
    sign_in(st, circle["codes"]["caretaker"])
    log = run(st, inputs={"Medicine name (as on the label)": "Ibuprofen", "Dosage (e.g. 500 mg, 1 tablet)": "400 mg"},
              clicks={"Review"})
    assert "Please check" in text(log) and not db.meds(circle["fid"])
    run(st, clicks={"med_confirm"})
    assert db.meds(circle["fid"])[0]["name"] == "Ibuprofen"


def test_family_suggestion_shows_warning_before_saving(st, circle):
    services.add_medicine(circle["caretaker"], "Warfarin", "5 mg", "", ["08:00"], clock.today())
    sign_in(st, circle["codes"]["family"])
    log = run(st, inputs={"Medicine name (as on the label)": "Ecosprin", "Dosage (e.g. 500 mg, 1 tablet)": "75 mg"},
              clicks={"Review"})
    assert "bleeding" in text(log) and len(db.meds(circle["fid"], ("proposed",))) == 0
    run(st, clicks={"med_confirm"})
    assert db.meds(circle["fid"], ("proposed",))[0]["name"] == "Ecosprin"


def test_mfa_screen_then_app(st, circle, monkeypatch):
    monkeypatch.setenv("MFA_ROLES", "caretaker")
    log = sign_in(st, circle["codes"]["caretaker"])
    assert "Two-step sign-in" in text(log)
    code = st.session_state["dev_code"]
    log = run(st, inputs={"mfa_code": code}, clicks={"Continue"})
    assert "Medication schedule" in text(log) and not errors(log)


def test_legacy_code_forces_new_code(st, circle):
    db.x("UPDATE members SET must_rotate=1 WHERE id=?", (circle["elder"]["id"],))
    log = sign_in(st, circle["codes"]["elder"])
    assert "Time for a new, stronger access code" in text(log)
    log = run(st, clicks={"rotate_go"})
    new = st.session_state["new_code"]
    assert new in text(log)
    log = run(st, inputs={"rotate_ack": True}, clicks={"rotate_done"})
    assert "Medicines" in text(log)
    assert auth.login_with_code(new, ["bs:x"]).status == "ok"


def test_expired_session_returns_to_login(st, circle):
    st.session_state["token"] = "not-a-real-token"
    log = run(st)
    assert "You were signed out" in text(log)


def test_reset_code_from_circle_tab(st, circle):
    sign_in(st, circle["codes"]["caretaker"])
    fam = db.get_member(circle["family"]["id"])
    st.session_state["mgsel"] = fam
    log = run(st, inputs={"mgsel": fam, f"rcok{fam['id']}": True}, clicks={"rcbtn"})
    issued = st.session_state["issued_code"][1]
    assert issued in text(log)
    assert auth.login_with_code(circle["codes"]["family"], ["bs:x"]).status == "invalid"


def test_production_with_bad_config_refuses_to_start(st, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DATA_KEY")
    log = run(st)
    assert any("can't start" in e for e in errors(log))
    assert ("button", "demo_btn") not in log


def test_remaining_branches(st, circle):
    """Help button, Hindi, settings, export, simulation and delete all run without errors."""
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "", ["08:00"], clock.today())
    log = sign_in(st, circle["codes"]["elder"])
    log = run(st, clicks={"help_btn"})
    assert not errors(log) and db.q("SELECT 1 FROM outbox WHERE kind='help'")
    log = run(st, clicks={"help_cancel"})
    assert "We told everyone" in text(log)
    log = run(st, inputs={"lang": "hi"})
    log = run(st)
    assert "दवाइयाँ" in text(log) and not errors(log)
    log = run(st, inputs={"set_detail": "full", "set_ai": True}, clicks={"set_save"})
    assert db.family_settings(circle["fid"]) == {"email_detail": "full", "ai_enabled": True, "elder_approval": False}
    log = run(st, clicks={"export_prep"})
    assert any(k == "download" and t.startswith("export_dl") for k, t in log)
    log = run(st, clicks={"simrun"})
    assert not errors(log)
    log = run(st, inputs={"del_confirm": "Test Family"}, clicks={"del_btn"})
    assert "were deleted" in text(log) and not db.family_ids()


def test_email_sign_in_through_the_ui(st, circle):
    run(st, inputs={"email_addr": "meera@mail.test"}, clicks={"Email me a code"})
    code = st.session_state["email_challenge"]["dev"]
    log = run(st, inputs={"email_otp": code}, clicks={"Sign in"})
    assert "Medication schedule" in text(log) and not errors(log)


def test_wrong_key_shows_how_to_fix_it(st, circle, monkeypatch):
    import crypto
    monkeypatch.setenv("DATA_KEY", crypto.generate_key())
    log = run(st)
    out = text(log)
    assert "can't read its database" in out and "DATA_KEY_PREVIOUS" in out and "key-info" in out
    assert ("button", "demo_btn") not in log


def test_adding_an_appointment_says_who_was_emailed(st, circle):
    sign_in(st, circle["codes"]["caretaker"])
    log = run(st, inputs={"Doctor": "Dr Test"}, clicks={"Add"})
    out = text(log)
    assert "Appointment added." in out and "simulated" in out and not errors(log)
    assert db.appts(circle["fid"])[0]["doctor"] == "Dr Test"


def test_elder_answers_changes_on_the_today_screen(st, circle):
    med = services.add_medicine(circle["elder"], "Metformin", "500 mg", "", ["08:00"], clock.today())["id"]
    services.edit_medicine(circle["caretaker"], med, "1000 mg", ["08:00"], "")
    log = sign_in(st, circle["codes"]["elder"])
    assert "Changes to your medicines" in text(log) and "dose 500 mg \\-\\> 1000 mg" in text(log)
    cid = services.changes_for_elder(circle["elder"])[0]["id"]
    log = run(st, inputs={f"chn{cid}": "Doctor didn't say this"}, clicks={f"chno{cid}"})
    assert db.get_change(circle["fid"], cid)["status"] == "disputed"
    assert not [e for e in errors(log) if "agree" not in e]          # disputed changes are shown in red on purpose
    st.session_state.clear()
    log = sign_in(st, circle["codes"]["caretaker"])
    assert "doesn't agree" in text(log) and "Only Asha can change this" in text(log)


def test_caretaker_sees_approval_mode(st, circle):
    med = services.add_medicine(circle["elder"], "Metformin", "500 mg", "", ["08:00"], clock.today())["id"]
    services.update_settings(circle["elder"], elder_approval=True)
    sign_in(st, circle["codes"]["caretaker"])
    log = run(st, inputs={}, clicks={"Save changes"})
    st.session_state.clear()
    log = sign_in(st, circle["codes"]["caretaker"])
    assert "has asked to approve medicine changes" in text(log)


def test_assistant_card_shows_on_today(st, circle):
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "", ["00:05"], clock.today())
    log = sign_in(st, circle["codes"]["caretaker"])
    out = text(log)
    assert "What needs attention now" in out and "Why:" in out and not errors(log)
