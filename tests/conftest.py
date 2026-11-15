import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import crypto  # noqa: E402

CLEAR = ["SMTP_USER", "SMTP_PASS", "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_FROM", "GEMINI_API_KEY",
         "GEMINI_MODEL", "GEMINI_PAID_TIER", "GEMINI_BASE_URL", "MFA_ROLES", "DATA_KEY_PREVIOUS", "SCHEDULER_MODE",
         "ALLOW_EPHEMERAL_DB", "ALLOW_UI_SECRETS", "AI_ENABLED", "EMAIL_MAX_ATTEMPTS", "BACKUP_DIR"]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    config._loaded = True  # never read a developer's .env or secrets.toml during tests
    monkeypatch.setattr(config, "use_secrets_files", False)
    for k in CLEAR:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CARECIRCLE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATA_KEY", crypto.generate_key())
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_TZ", "Asia/Kolkata")
    import db
    import insights
    insights._breaker.update(failures=0, open_until=0.0, last_error="")
    insights._model_cache.clear()
    db.init_db()
    yield tmp_path


def _code(codes, role):
    return next(v for k, v in codes.items() if k.endswith(f"({role})"))


@pytest.fixture
def circle():
    """A care circle with an elder, a caretaker and a family member, all signed in."""
    import services
    fid, codes = services.create_circle(
        "Test Family", "Asha Sharma", "asha@mail.test",
        [("Ravi", "ravi@mail.test", "caretaker"), ("Meera", "meera@mail.test", "family")], clients=["bs:fixture"])
    out = {"fid": fid, "codes": {r: _code(codes, r) for r in ("elder", "caretaker", "family")}}
    for role in ("elder", "caretaker", "family"):
        out[role] = login(out["codes"][role])
    return out


def login(code, bucket="bs:login-helper"):
    import auth
    r = auth.login_with_code(code, [bucket])
    assert r.status == "ok", r.message
    a = auth.current(r.token)
    a["_token"] = r.token
    return a


def raw_db_bytes():
    data = b""
    for suffix in ("", "-wal"):
        p = config.db_path() + suffix
        if os.path.exists(p):
            with open(p, "rb") as f:
                data += f.read()
    return data
