"""Issues 16, 38-41, 64-65: production configuration is checked and demo features stay out of production."""
import os
import stat

import pytest

import config
import crypto
import seed


def _prod(monkeypatch, tmp_path, **extra):
    monkeypatch.setenv("APP_ENV", "production")
    for k, v in extra.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def test_production_refuses_unsafe_settings(monkeypatch, tmp_path):
    _prod(monkeypatch, tmp_path, DATA_KEY=None, CARECIRCLE_DB=None)
    errors, _ = config.validate()
    text = " ".join(errors)
    assert "DATA_KEY" in text and "CARECIRCLE_DB" in text and "SMTP" in text


def test_production_rejects_database_inside_app_folder(monkeypatch, tmp_path):
    _prod(monkeypatch, tmp_path, CARECIRCLE_DB=os.path.join(config.BASE_DIR, "carecircle.db"),
          SMTP_USER="a@mail.example", SMTP_PASS="x")
    errors, _ = config.validate()
    assert any("inside the app folder" in e for e in errors)
    monkeypatch.setenv("ALLOW_EPHEMERAL_DB", "true")
    assert not config.validate()[0]


def test_good_production_config_passes(monkeypatch, tmp_path):
    _prod(monkeypatch, tmp_path, SMTP_USER="a@mail.example", SMTP_PASS="x", SCHEDULER_MODE="external")
    errors, warnings = config.validate()
    assert errors == [] and warnings == []


def test_invalid_key_is_an_error(monkeypatch):
    monkeypatch.setenv("DATA_KEY", "too-short")
    assert any("DATA_KEY is invalid" in e for e in config.validate()[0])


def test_unknown_environment_fails_closed(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    assert config.env() == "production" and not config.demo_features()


def test_settings_page_cannot_write_secrets_outside_development(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / ".env"))
    for envname in ("production", "demo"):
        monkeypatch.setenv("APP_ENV", envname)
        with pytest.raises(PermissionError):
            config.save({"SMTP_USER": "x@mail.example"})
    monkeypatch.setenv("APP_ENV", "development")
    config.save({"SMTP_USER": "x@mail.example"})
    assert stat.S_IMODE(os.stat(tmp_path / ".env").st_mode) == 0o600
    with pytest.raises(ValueError):
        config.save({"DATA_KEY": crypto.generate_key()})
    with pytest.raises(ValueError):
        config.save({"SMTP_USER": "x\nAPP_ENV=development"})


def test_sample_family_is_disabled_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(seed.DemoDisabled):
        seed.seed_demo()


def test_sample_family_is_marked_as_demo():
    import db
    codes = seed.seed_demo(clients=["bs:s"])
    assert set(codes) == {"elder", "caretaker", "family"}
    fam = db.q1("SELECT * FROM families ORDER BY id DESC LIMIT 1")
    assert fam["is_demo"] == 1
    assert all(m["email"].endswith("@example.com") for m in db.members(fam["id"]))


def test_env_file_inline_comments_are_not_part_of_values(tmp_path):
    p = tmp_path / ".env"
    p.write_text("SMTP_PASS=abcd      # 16-char app password\nQUOTED=\"a # b\"\n")
    vals = config._parse(str(p))
    assert vals["SMTP_PASS"] == "abcd" and vals["QUOTED"] == "a # b"


def test_production_is_refused_on_streamlit_community_cloud(monkeypatch, tmp_path):
    _prod(monkeypatch, tmp_path, SMTP_USER="a@mail.example", SMTP_PASS="x")
    monkeypatch.setattr(config, "BASE_DIR", "/mount/src/carecircle")
    assert any("Community Cloud" in e for e in config.validate()[0])
    monkeypatch.setenv("APP_ENV", "demo")
    assert not config.validate()[0]


def test_project_secrets_are_read_wherever_the_app_starts(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".streamlit").mkdir(parents=True)
    key = crypto.generate_key()
    (proj / ".streamlit" / "secrets.toml").write_text(f'DATA_KEY = "{key}"\nSMTP_PORT = 465\n')
    monkeypatch.setattr(config, "SECRETS_PATH", str(proj / ".streamlit" / "secrets.toml"))
    monkeypatch.setattr(config, "use_secrets_files", True)
    monkeypatch.delenv("DATA_KEY")
    monkeypatch.chdir(tmp_path)                     # launched from the parent folder
    assert config.get("DATA_KEY") == key and config.get("SMTP_PORT") == "465"
    assert config.source("DATA_KEY").endswith("secrets.toml")
    assert crypto.key_source().startswith("DATA_KEY in")
    monkeypatch.setenv("DATA_KEY", "from-env")      # environment still wins
    assert config.get("DATA_KEY") == "from-env"


def test_broken_secrets_file_is_a_clear_error(monkeypatch, tmp_path):
    bad = tmp_path / "secrets.toml"
    bad.write_text('DATA_KEY = "unterminated\n')
    monkeypatch.setattr(config, "SECRETS_PATH", str(bad))
    monkeypatch.setattr(config, "use_secrets_files", True)
    monkeypatch.setattr(config, "_secrets_cache", {"stamp": None, "values": {}})
    with pytest.raises(RuntimeError, match="Can't read"):
        config.get("ANYTHING")


def test_secrets_fallback_parser_without_tomllib(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_toml(name, *a, **k):
        if name in ("tomllib", "toml"):
            raise ImportError(name)
        return real_import(name, *a, **k)
    p = tmp_path / "secrets.toml"
    p.write_text('# c\nAPP_ENV = "development"\nDATA_KEY = \'abc=\'  # note\nSMTP_PORT = 465\n[section]\nX = "y"\n')
    monkeypatch.setattr(builtins, "__import__", no_toml)
    assert config._load_toml(str(p)) == {"APP_ENV": "development", "DATA_KEY": "abc=", "SMTP_PORT": "465"}
