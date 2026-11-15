"""The key UI flows with Streamlit's own test runner. Skipped when Streamlit isn't installed.

Run locally with:  pip install -r requirements.txt -r requirements-dev.txt && pytest tests/test_app_streamlit.py
"""
import os

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

import services  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


@pytest.fixture(autouse=True)
def no_scheduler(monkeypatch):
    monkeypatch.setenv("SCHEDULER_MODE", "off")


def app():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    return at


def by_label(widgets, label):
    return next(w for w in widgets if w.label == label)


def sign_in(code):
    at = app()
    at.text_input(key="login_code").input(code)
    next(b for b in at.button if b.label == "Open").click()
    at.run()
    assert not at.exception
    return at


def test_login_screen():
    at = app()
    assert any("CareCircle" in t.value for t in at.title)


def test_wrong_code():
    at = sign_in("AAAA-BBBB-CCCC")
    assert any("didn't work" in e.value for e in at.error)


@pytest.mark.parametrize("role", ["elder", "caretaker", "family"])
def test_role_tabs(circle, role):
    at = sign_in(circle["codes"][role])
    labels = [t.label for t in at.tabs]
    assert "Today" in labels and "Medications" in labels
    assert ("Emails" in labels) == (role != "family")
    assert not any(c in str(at) for c in circle["codes"].values())


def test_elder_sees_help_button(circle):
    at = sign_in(circle["codes"]["elder"])
    assert at.button(key="help_btn")
    at.button(key="help_btn").click().run()
    assert not at.exception


def test_add_medicine(circle):
    at = sign_in(circle["codes"]["caretaker"])
    by_label(at.text_input, "Medicine name (as on the label)").input("Metformin")
    by_label(at.text_input, "Dosage (e.g. 500 mg, 1 tablet)").input("500 mg")
    next(b for b in at.button if b.label == "Review").click()
    at.run()
    at.button(key="med_confirm").click().run()
    import db
    assert db.meds(circle["fid"])[0]["name"] == "Metformin"
