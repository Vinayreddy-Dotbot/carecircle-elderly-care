"""Issues 13, 15, 17: encryption at rest and key rotation."""
import os

import pytest

import auth
import clock
import config
import crypto
import db
import maintenance
import services
from conftest import raw_db_bytes


def test_round_trip_and_random_nonces():
    a, b = crypto.enc("Metformin"), crypto.enc("Metformin")
    assert a != b and a.startswith(crypto.PREFIX)
    assert crypto.dec(a) == crypto.dec(b) == "Metformin"
    assert crypto.enc("") == "" and crypto.enc(None) is None


def test_wrong_key_cannot_decrypt(monkeypatch):
    v = crypto.enc("secret")
    monkeypatch.setenv("DATA_KEY", crypto.generate_key())
    with pytest.raises(crypto.DecryptionError):
        crypto.dec(v)


def test_tampering_is_detected():
    v = crypto.enc("secret")
    bad = v[:-4] + ("AAAA" if not v.endswith("AAAA") else "BBBB")
    with pytest.raises(crypto.DecryptionError):
        crypto.dec(bad)


def test_sensitive_values_are_not_in_the_database_file(circle):
    ct = circle["caretaker"]
    services.add_medicine(ct, "Levothyroxine", "50 mcg", "Empty stomach", ["06:30"], clock.today(), prescriber="Dr Rao")
    services.add_appointment(ct, "Dr Venkatesh", "Endocrinology", clock.now().replace(year=2099), "Manipal Hospital",
                             "Ask about thyroid levels")
    services.invite(ct, "Priya Nair", "priya.nair@mail.test", "family")
    dose = db.doses_for_day(circle["fid"], clock.today().isoformat())[0]
    services.mark_dose(circle["elder"], dose["id"], "taken")
    raw = raw_db_bytes()
    for s in ("Levothyroxine", "Empty stomach", "Dr Rao", "Dr Venkatesh", "Manipal Hospital", "thyroid",
              "Priya Nair", "priya.nair@", "asha@mail.test", "Asha Sharma", "Test Family"):
        assert s.encode() not in raw, s
    assert db.meds(circle["fid"])[0]["name"] == "Levothyroxine"


def test_production_requires_a_key(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DATA_KEY")
    with pytest.raises(RuntimeError):
        crypto.enc("x")


def test_development_key_file_is_private(monkeypatch, tmp_path):
    import os
    import stat
    monkeypatch.delenv("DATA_KEY")
    monkeypatch.setenv("CARECIRCLE_DB", str(tmp_path / "dev.db"))
    crypto.enc("x")
    mode = stat.S_IMODE(os.stat(str(tmp_path / "dev.db") + ".devkey").st_mode)
    assert mode == 0o600


def test_key_rotation(circle, monkeypatch):
    old = config.get("DATA_KEY")
    new = crypto.generate_key()
    monkeypatch.setenv("DATA_KEY", new)
    monkeypatch.setenv("DATA_KEY_PREVIOUS", old)
    assert db.get_family(circle["fid"])["name"] == "Test Family"   # old values still readable
    assert maintenance.rotate_key() > 0
    monkeypatch.delenv("DATA_KEY_PREVIOUS")
    assert db.get_family(circle["fid"])["name"] == "Test Family"
    assert db.members(circle["fid"])[0]["email"] == "asha@mail.test"
    db.check_key()
    r = auth.email_signin_start("asha@mail.test", ["bs:k"])    # blind index was rebuilt with the new key
    assert r.dev_code
    assert auth.login_with_code(circle["codes"]["elder"], ["bs:k"]).status == "ok"


def test_rotation_refuses_while_old_codes_remain(circle):
    db.x("UPDATE members SET legacy_lookup='x' WHERE id=?", (circle["elder"]["id"],))
    with pytest.raises(RuntimeError):
        maintenance.rotate_key()


@pytest.fixture
def dev_db(tmp_path, monkeypatch):
    """A database created in development without DATA_KEY, so it uses a .devkey file."""
    monkeypatch.delenv("DATA_KEY")
    monkeypatch.setenv("CARECIRCLE_DB", str(tmp_path / "dev.db"))
    crypto._dev_cache.clear()
    db.init_db()
    fid, _ = services.create_circle("Dev Family", "Asha", "asha@mail.test", clients=["bs:dev"])
    return fid, crypto.key_id()


def test_setting_data_key_after_first_run_keeps_data_readable(dev_db, monkeypatch, capsys):
    fid, dev_kid = dev_db
    monkeypatch.setenv("DATA_KEY", crypto.generate_key())      # the situation that used to fail at start-up
    db.init_db()
    assert db.get_family(fid)["name"] == "Dev Family"
    assert any("rotate-key" in w for w in config.validate()[1])
    services.create_circle("New Family", "Kamala", "", clients=["bs:dev2"])   # new data uses DATA_KEY
    assert maintenance.rotate_key() > 0
    os.remove(config.db_path() + ".devkey")
    crypto._dev_cache.clear()
    db.init_db()
    assert db.get_family(fid)["name"] == "Dev Family"
    import manage
    assert manage.main(["key-info"]) == 0
    assert "OK, the database can be read" in capsys.readouterr().out


def test_lost_key_file_gives_a_message_naming_both_keys(dev_db):
    fid, dev_kid = dev_db
    os.remove(config.db_path() + ".devkey")
    crypto._dev_cache.clear()
    with pytest.raises(crypto.DecryptionError) as err:
        db.init_db()
    msg = str(err.value)
    assert dev_kid in msg and crypto.key_id() in msg and "development key file" in msg


def test_production_does_not_fall_back_to_the_dev_key_file(dev_db, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATA_KEY", crypto.generate_key())
    with pytest.raises(crypto.DecryptionError):
        db.check_key()


def test_old_key_as_previous_key_works(circle, monkeypatch, capsys):
    old = config.get("DATA_KEY")
    monkeypatch.setenv("DATA_KEY", crypto.generate_key())
    with pytest.raises(crypto.DecryptionError):
        db.check_key()
    import manage
    assert manage.main(["key-info"]) == 1
    assert "CANNOT READ" in capsys.readouterr().out
    monkeypatch.setenv("DATA_KEY_PREVIOUS", old)
    db.check_key()
