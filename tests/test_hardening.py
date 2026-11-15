"""Safe defaults on a public host, and the small helpers added for them."""
import os
import tomllib

import pytest

import auth
import config
import crypto
import db
import insights
import notifier

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- Streamlit Community Cloud is public
def test_community_cloud_defaults_to_demo_mode(monkeypatch):
    monkeypatch.delenv("APP_ENV")
    monkeypatch.setattr(config, "BASE_DIR", "/mount/src/carecircle-elderly-care")
    assert config.on_community_cloud() and config.env() == "demo"
    assert not config.ui_can_save_secrets()           # nobody can write server keys from the browser
    assert not auth._dev_visible()                    # sign-in codes are never shown on screen
    assert config.demo_features()                     # the sample family still works for visitors


def test_explicit_app_env_still_wins_on_community_cloud(monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", "/mount/src/carecircle-elderly-care")
    monkeypatch.setenv("APP_ENV", "development")
    assert config.env() == "development"


def test_local_machine_defaults_to_development(monkeypatch):
    monkeypatch.delenv("APP_ENV")
    monkeypatch.setattr(config, "BASE_DIR", "/home/someone/carecircle")
    assert config.env() == "development"


def test_streamlit_hides_error_details_and_developer_options():
    with open(os.path.join(ROOT, ".streamlit", "config.toml"), "rb") as f:
        cfg = tomllib.load(f)
    assert cfg["client"]["showErrorDetails"] == "none" and cfg["client"]["toolbarMode"] == "viewer"
    assert cfg["server"]["enableXsrfProtection"] is True
    assert cfg["browser"]["gatherUsageStats"] is False


# ---------------------------------------------------------------- secrets
def test_only_scrypt_hashes_are_accepted():
    good = crypto.hash_secret("ABCD-EFGH-JKLM")
    assert crypto.verify_secret("ABCD-EFGH-JKLM", good)
    forged = good.replace("scrypt$", "md5$", 1)
    assert not crypto.verify_secret("ABCD-EFGH-JKLM", forged)
    assert not crypto.verify_secret("ABCD-EFGH-JKLM", "not a hash")
    assert not crypto.verify_secret("ABCD-EFGH-JKLM", None)


# ---------------------------------------------------------------- direct emails (circle deletion)
def test_send_direct_skips_without_email_or_for_test_addresses(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "_send_smtp", lambda *a: sent.append(a))
    assert not notifier.send_direct("asha@mail.com", "s", "b")           # email isn't set up
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASS", "abcd efgh ijkl mnop")
    assert not notifier.send_direct("asha@example.com", "s", "b")        # test domain
    assert notifier.send_direct("asha@mail.com", "s", "b") and sent == [("asha@mail.com", "s", "b")]


def test_send_direct_reports_failure_instead_of_hiding_it(monkeypatch, caplog):
    def fail(*a):
        raise OSError("connection refused")
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASS", "abcd efgh ijkl mnop")
    monkeypatch.setattr(notifier, "_send_smtp", fail)
    caplog.set_level("WARNING", logger="carecircle.notifier")
    assert notifier.send_direct("asha@mail.com", "s", "b") is False
    assert any("direct email failed" in r.message for r in caplog.records)


def test_deleting_a_circle_tells_everyone_directly(circle, monkeypatch):
    import services
    sent = []
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASS", "abcd efgh ijkl mnop")
    monkeypatch.setattr(notifier, "_send_smtp", lambda to, subject, body: sent.append(to))
    for role in ("elder", "caretaker", "family"):           # the fixture's .test addresses are never emailed
        db.update_member(circle["fid"], circle[role]["id"], email=f"{role}@mail.com")
    services.delete_circle(circle["elder"], "Test Family")
    assert sorted(sent) == ["caretaker@mail.com", "elder@mail.com", "family@mail.com"]
    assert not db.q("SELECT 1 FROM outbox")             # nothing about the deleted circle is kept


# ---------------------------------------------------------------- small helpers
def test_count_helper(circle):
    assert db.count("SELECT COUNT(*) AS n FROM members WHERE family_id=?", (circle["fid"],)) == 3
    assert db.count("SELECT COUNT(*) AS n FROM members WHERE family_id=?", (-1,)) == 0


def test_whole_numbers_and_optional_numbers():
    import services
    assert services._int("7", "Mood", 1, 9) == 7
    assert services._optional_int("", "Stock", 0, 9) is None and services._optional_int(None, "Stock", 0, 9) is None
    assert services._optional_int("3", "Stock", 0, 9) == 3
    with pytest.raises(services.Invalid):
        services._int("", "Mood", 1, 5)
    with pytest.raises(services.Invalid):
        services._optional_int("12", "Stock", 0, 9)


def test_reset_model_cache():
    insights._model_cache["m"] = "gemini-9.9-flash"
    insights.reset_model_cache()
    assert insights._model_cache == {}
