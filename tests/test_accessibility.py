"""Accessibility: status is given in words (not only colour or icons), assistant lines say how urgent they are,
charts have a table alternative, the page language follows the reader, and the printable list is a proper table.

Runs app.py with tests/fake_streamlit.py, like test_app_smoke.py."""
import os
import sys

import pytest

import clock
import db
import services

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
LEVEL_LABELS = ("**Urgent:**", "**To do:**", "**Good to know:**", "**Good news:**")


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


def sign_in(st, code):
    import fake_streamlit
    return fake_streamlit.run(st, APP, inputs={"login_code": code}, clicks={"Open"})


@pytest.fixture
def plan(circle):
    """A medicine with one dose taken today, one still to come, and a mood check-in, so every chart has data."""
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "with food", ["00:00", "23:59"], clock.today())
    first = db.doses_for_day(circle["fid"], clock.today().isoformat())[0]
    services.mark_dose(circle["caretaker"], first["id"], "taken")
    services.check_in(circle["elder"], 4)
    return circle


def test_dose_status_is_in_words_and_icons_are_hidden_from_screen_readers(st, plan):
    log = sign_in(st, plan["codes"]["elder"])
    rows = [t for k, t in log if k == "markdown" and "class='dose " in t]
    assert len(rows) == 2
    pending = next(t for t in rows if "dose pending" in t)
    taken = next(t for t in rows if "dose taken" in t)
    assert "Not taken yet" in pending and "Confirmed taken by" in taken
    assert all("<span aria-hidden='true'>" in t and "<small>" not in t for t in rows)


def test_every_assistant_line_says_how_urgent_it_is(st, plan):
    log = sign_in(st, plan["codes"]["caretaker"])
    lines = [t for k, t in log if k == "markdown" and t[:1] in "🔴🟠🔵🟢"]
    assert lines and all(any(label in t for label in LEVEL_LABELS) for t in lines)


def test_charts_have_a_table_alternative(st, plan):
    log = sign_in(st, plan["codes"]["caretaker"])
    charts = [k for k, _ in log if k in ("bar_chart", "line_chart")]
    tables = [k for k, _ in log if k == "table"]
    assert charts and len(tables) == len(charts)


def test_page_language_follows_the_reader(st, plan):
    db.update_member(plan["fid"], plan["elder"]["id"], lang="hi")
    log = sign_in(st, plan["codes"]["elder"])
    scripts = [t for k, t in log if k == "html"]
    assert any('documentElement.lang = "hi"' in s for s in scripts)
    st.session_state.clear()
    log = sign_in(st, plan["codes"]["caretaker"])
    assert any('documentElement.lang = "en"' in s for k, s in log if k == "html")


def test_read_aloud_is_a_real_button_with_a_fallback(st, plan):
    log = sign_in(st, plan["codes"]["elder"])
    speak = next(t for k, t in log if k == "html" and 'id="speak"' in t)
    assert 'type="button"' in speak and "This browser can't read aloud" in speak
    assert '<span aria-hidden="true">🔊</span>' in speak


def test_printable_list_is_a_labelled_table(st, plan):
    saved = []
    st.download_button = lambda label, data, key=None, **k: saved.append((key, data)) or False
    sign_in(st, plan["codes"]["elder"])
    page = next(data for key, data in saved if key == "print_btn")
    assert '<html lang="en">' in page and "<caption>" in page and page.count('scope="col"') == 3
    assert "Metformin" in page
