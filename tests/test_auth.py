"""Issues 1-9, 52-53: hashed longer codes, rate limiting, lockout, recovery, MFA, session expiry."""
import math
import re
import time

import pytest

import auth
import config
import db
import services
from conftest import login, raw_db_bytes


def test_codes_are_long_and_from_the_safe_alphabet(circle):
    for code in circle["codes"].values():
        assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
        assert set(auth.normalize(code)) <= set(auth.ALPHABET)
    assert math.log2(len(auth.ALPHABET) ** auth.CODE_LEN) > 59


def test_codes_are_not_stored_anywhere_in_the_database_file(circle):
    cols = {r["name"] for r in db.q("PRAGMA table_info(members)")}
    assert "passcode" not in cols
    raw = raw_db_bytes()
    for code in circle["codes"].values():
        assert auth.normalize(code).encode() not in raw
        assert code.encode() not in raw
    for m in db.members(circle["fid"]):
        assert m["code_hash"].startswith("scrypt$")


def test_login_ignores_case_dashes_and_spaces(circle):
    code = circle["codes"]["elder"]
    for variant in (code.lower(), code.replace("-", ""), " " + code.replace("-", " ") + " "):
        assert auth.login_with_code(variant, ["bs:variants"]).status == "ok"


def test_wrong_code_gets_a_generic_message(circle):
    r = auth.login_with_code("ZZZZ-ZZZZ-ZZZZ", ["bs:x"])
    assert r.status == "invalid" and r.message == auth.GENERIC_INVALID and not r.token


def _wrong_secret(code):
    c = auth.normalize(code)
    return c[:4] + ("A" * 8 if c[4:] != "A" * 8 else "B" * 8)


def test_five_wrong_codes_pause_that_member_even_with_the_right_code(circle):
    code, t = circle["codes"]["family"], time.time()
    for i in range(5):
        assert auth.login_with_code(_wrong_secret(code), [f"bs:attacker{i}"], now=t + i).status == "invalid"
    r = auth.login_with_code(code, ["bs:owner"], now=t + 10)
    assert r.status == "throttled" and not r.token
    events = [e["event"] for e in db.security_events(circle["fid"])]
    assert "lockout" in events
    alert = db.q("SELECT * FROM outbox WHERE kind='security' AND member_id=?", (circle["family"]["id"],))
    assert alert and "paused" in alert[0]["body"]


def test_lockout_expires(circle):
    code, t = circle["codes"]["family"], time.time()
    for i in range(5):
        auth.login_with_code(_wrong_secret(code), [f"bs:a{i}"], now=t)
    assert auth.login_with_code(code, ["bs:o"], now=t + 60).status == "throttled"
    assert auth.login_with_code(code, ["bs:o"], now=t + 16 * 60).status == "ok"


def test_browser_session_throttle_blocks_guessing(circle):
    t = time.time()
    for i in range(10):
        auth.login_with_code(f"ZZZ{i}-ZZZZ-ZZZZ", ["bs:guesser"], now=t)
    assert auth.login_with_code(circle["codes"]["elder"], ["bs:guesser"], now=t).status == "throttled"
    assert auth.login_with_code(circle["codes"]["elder"], ["bs:someone-else"], now=t).status == "ok"


def test_throttle_runs_before_the_hash(circle, monkeypatch):
    t = time.time()
    for i in range(10):
        auth.login_with_code("ZZZZ-ZZZZ-ZZZZ", ["bs:flood"], now=t)
    import crypto
    monkeypatch.setattr(crypto, "verify_secret", lambda *a: pytest.fail("hash ran while throttled"))
    assert auth.login_with_code("ZZZZ-ZZZZ-ZZZZ", ["bs:flood"], now=t).status == "throttled"


def test_unknown_selector_still_costs_a_hash(monkeypatch):
    import crypto
    calls = []
    real = crypto.verify_secret
    monkeypatch.setattr(crypto, "verify_secret", lambda s, h: calls.append(h) or real(s, h))
    auth.login_with_code("ZZZZ-ZZZZ-ZZZZ", ["bs:t"])
    assert calls == [None]  # verify_secret(None) runs a dummy scrypt


def test_idle_and_absolute_session_expiry(circle):
    tok, t = circle["elder"]["_token"], time.time()
    assert auth.current(tok, now=t + 29 * 60)
    assert auth.current(tok, now=t + 29 * 60 + 31 * 60) is None   # idle > 30 min
    s = auth.create_session(db.get_member(circle["elder"]["id"]), "full", now=t)
    for k in range(1, 80):
        if auth.current(s, now=t + k * 600) is None:
            assert k * 600 > 12 * 3600 - 600
            break
    else:
        pytest.fail("session never expired")


def test_removed_member_is_signed_out_at_once(circle):
    services.remove_member(circle["caretaker"], circle["family"]["id"])
    assert auth.current(circle["family"]["_token"]) is None
    assert auth.login_with_code(circle["codes"]["family"], ["bs:x"]).status == "invalid"


def test_reset_code_issues_new_code_and_ends_old_sessions(circle):
    new = services.reset_code(circle["caretaker"], circle["family"]["id"])
    assert auth.current(circle["family"]["_token"]) is None
    assert auth.login_with_code(circle["codes"]["family"], ["bs:x"]).status == "invalid"
    assert login(new)["id"] == circle["family"]["id"]
    assert db.q("SELECT 1 FROM outbox WHERE kind='security' AND member_id=?", (circle["family"]["id"],))


def test_rotate_own_code_keeps_this_session_only(circle):
    other = login(circle["codes"]["elder"], "bs:other-device")
    new = auth.rotate_own_code(circle["elder"]["_token"])
    assert auth.current(circle["elder"]["_token"])
    assert auth.current(other["_token"]) is None
    assert auth.login_with_code(circle["codes"]["elder"], ["bs:x"]).status == "invalid"
    assert auth.login_with_code(new, ["bs:x"]).status == "ok"


# ---------------------------------------------------------------- MFA
def test_caretaker_needs_emailed_code_when_mfa_is_on(circle, monkeypatch):
    monkeypatch.setenv("MFA_ROLES", "caretaker")
    r = auth.login_with_code(circle["codes"]["caretaker"], ["bs:m"])
    assert r.status == "mfa" and r.dev_code
    assert auth.current(r.token)["stage"] == "mfa"
    assert auth.verify_mfa(r.token, "000000" if r.dev_code != "000000" else "111111").status == "invalid"
    assert auth.verify_mfa(r.token, r.dev_code).status == "ok"
    assert auth.current(r.token)["stage"] == "full"
    stored = db.q("SELECT body FROM outbox WHERE kind='otp_mfa'")
    assert stored and r.dev_code not in stored[0]["body"]


def test_mfa_code_attempts_are_limited(circle, monkeypatch):
    monkeypatch.setenv("MFA_ROLES", "caretaker")
    r = auth.login_with_code(circle["codes"]["caretaker"], ["bs:m"])
    wrong = "123456" if r.dev_code != "123456" else "654321"
    for _ in range(5):
        auth.verify_mfa(r.token, wrong)
    assert auth.current(r.token) is None
    assert auth.verify_mfa(r.token, r.dev_code).status == "invalid"


def test_mfa_without_email_is_refused(circle, monkeypatch):
    monkeypatch.setenv("MFA_ROLES", "caretaker")
    db.update_member(circle["fid"], circle["caretaker"]["id"], email="")
    assert auth.login_with_code(circle["codes"]["caretaker"], ["bs:m"]).status == "unavailable"


def test_mfa_is_default_for_caretakers_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert config.mfa_roles() == {"caretaker"}


def test_otp_sends_are_limited(circle, monkeypatch):
    monkeypatch.setenv("MFA_ROLES", "caretaker")
    r = auth.login_with_code(circle["codes"]["caretaker"], ["bs:m"])
    assert auth.resend_mfa(r.token).status == "sent"
    assert auth.resend_mfa(r.token).status == "sent"
    assert auth.resend_mfa(r.token).status == "throttled"


# ---------------------------------------------------------------- email sign-in
def test_email_sign_in_for_the_elder(circle):
    r = auth.email_signin_start("ASHA@mail.test ", ["bs:e"])
    assert r.status == "sent" and r.dev_code
    done = auth.email_signin_verify(r.token, r.dev_code, ["bs:e"])
    assert done.status == "ok" and auth.current(done.token)["role"] == "elder"


def test_email_sign_in_does_not_reveal_unknown_addresses(circle):
    known = auth.email_signin_start("asha@mail.test", ["bs:e1"])
    unknown = auth.email_signin_start("nobody@mail.test", ["bs:e2"])
    assert known.message == unknown.message and unknown.token and not unknown.dev_code
    assert auth.email_signin_verify(unknown.token, "123456", ["bs:e2"]).status == "invalid"


def test_email_sign_in_is_not_offered_to_mfa_roles(circle, monkeypatch):
    monkeypatch.setenv("MFA_ROLES", "caretaker")
    r = auth.email_signin_start("ravi@mail.test", ["bs:e"])
    assert r.status == "sent" and not r.dev_code


def test_email_in_two_circles_lets_you_choose(circle):
    fid2, codes2 = services.create_circle("Second", "Gopal", "", [("Meera", "meera@mail.test", "family")], clients=["bs:c2"])
    r = auth.email_signin_start("meera@mail.test", ["bs:e"])
    pick = auth.email_signin_verify(r.token, r.dev_code, ["bs:e"])
    assert pick.status == "choose" and {c["family_name"] for c in pick.choices} == {"Test Family", "Second"}
    assert auth.email_signin_pick(pick.pick_token, circle["elder"]["id"]).status == "invalid"
    chosen = next(c for c in pick.choices if c["family_name"] == "Second")
    s = auth.email_signin_pick(pick.pick_token, chosen["id"])
    assert s.status == "ok" and auth.current(s.token)["family_id"] == fid2


def test_signups_are_rate_limited():
    for i in range(5):
        services.create_circle(f"F{i}", "E", "", clients=["bs:spam"])
    with pytest.raises(services.Invalid):
        services.create_circle("F6", "E", "", clients=["bs:spam"])
