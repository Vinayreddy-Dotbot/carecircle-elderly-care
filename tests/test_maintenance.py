"""Issues 12, 14, 17, 45: backups, restore after failure, retention, export and deletion."""
import os
import time

import pytest

import auth
import clock
import config
import crypto
import db
import maintenance
import services


def test_backup_is_encrypted_and_restores(circle, tmp_path):
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "", ["08:00"], clock.today())
    bak = maintenance.backup(str(tmp_path / "b.ccbak"))
    blob = open(bak, "rb").read()
    assert blob.startswith(maintenance.MAGIC) and b"SQLite format" not in blob and b"Metformin" not in blob
    assert os.stat(bak).st_mode & 0o077 == 0
    assert maintenance.verify_backup(bak)["ok"]

    services.stop_medicine(circle["caretaker"], db.meds(circle["fid"])[0]["id"])   # change after the backup
    assert not db.meds(circle["fid"])
    maintenance.restore(bak)
    assert db.meds(circle["fid"])[0]["name"] == "Metformin"
    assert auth.login_with_code(circle["codes"]["elder"], ["bs:r"]).status == "ok"


def test_restore_after_the_database_is_lost(circle, tmp_path):
    bak = maintenance.backup(str(tmp_path / "b.ccbak"))
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(config.db_path() + suffix):
            os.remove(config.db_path() + suffix)
    maintenance.restore(bak)
    assert db.get_family(circle["fid"])["name"] == "Test Family"


def test_backup_needs_the_right_key(circle, tmp_path, monkeypatch):
    bak = maintenance.backup(str(tmp_path / "b.ccbak"))
    monkeypatch.setenv("DATA_KEY", crypto.generate_key())
    with pytest.raises(crypto.DecryptionError):
        maintenance.verify_backup(bak)


def test_corrupted_backup_is_rejected(circle, tmp_path):
    bak = maintenance.backup(str(tmp_path / "b.ccbak"))
    data = bytearray(open(bak, "rb").read())
    data[-5] ^= 0xFF
    open(bak, "wb").write(bytes(data))
    with pytest.raises(crypto.DecryptionError):
        maintenance.restore(bak)
    assert db.get_family(circle["fid"])    # live database untouched


def test_retention_purge(circle, monkeypatch):
    fid = circle["fid"]
    med = db.add_med(fid, "Old", "1", "", ["08:00"], "2020-01-01", None, None, 5, "x")
    db.x("INSERT INTO doses(med_id,family_id,scheduled_date,scheduled_time) VALUES(?,?,?,?)", (med, fid, "2020-01-02", "08:00"))
    db.x("INSERT INTO doses(med_id,family_id,scheduled_date,scheduled_time) VALUES(?,?,?,?)", (med, fid, clock.today().isoformat(), "08:00"))
    db.x("INSERT INTO outbox(family_id,to_email,subject,body,dedupe_key,status,created_at) VALUES(?,?,?,?,?,?,?)",
         (fid, "a", "s", "b", "old", "sent", "2020-01-01 00:00:00"))
    db.x("INSERT INTO outbox(family_id,to_email,subject,body,dedupe_key,status,created_at) VALUES(?,?,?,?,?,?,?)",
         (fid, "a", "s", "b", "old-pending", "pending", "2020-01-01 00:00:00"))
    db.x("INSERT INTO activity(family_id,who,what,at) VALUES(?,?,?,?)", (fid, "x", "y", "2020-01-01 00:00:00"))
    db.x("INSERT INTO rate_events(bucket,at) VALUES('fail:x', ?)", (time.time() - 3 * 86400,))
    out = maintenance.purge()
    assert out["doses"] == 1 and out["outbox"] == 1 and out["activity"] == 1 and out["rate_events"] == 1
    assert db.q1("SELECT COUNT(*) AS n FROM outbox WHERE dedupe_key='old-pending'")["n"] == 1   # undelivered kept
    monkeypatch.setenv("RETENTION_OUTBOX_DAYS", "1")
    assert maintenance.retention()["RETENTION_OUTBOX_DAYS"] == 1


def test_export_has_only_this_family_and_no_secrets(circle):
    other, _ = services.create_circle("Other", "Kamala", "kamala@mail.test", clients=["bs:o"])
    data = services.export_data(circle["caretaker"])
    assert "Asha Sharma" in data and "asha@mail.test" in data
    assert "Kamala" not in data and "code_hash" not in data and "scrypt$" not in data and crypto.PREFIX not in data


def test_delete_circle_removes_everything_and_only_that(circle):
    other, codes = services.create_circle("Other", "Kamala", "", clients=["bs:o"])
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "", ["08:00"], clock.today())
    services.request_help(circle["elder"])
    with pytest.raises(services.Invalid):
        services.delete_circle(circle["elder"], "wrong name")
    services.delete_circle(circle["elder"], "test family")
    fid = circle["fid"]
    for table in ("members", "medications", "doses", "appointments", "outbox", "activity", "security_events", "checkins"):
        assert db.q1(f"SELECT COUNT(*) AS n FROM {table} WHERE family_id=?", (fid,))["n"] == 0, table
    assert db.q1("SELECT COUNT(*) AS n FROM sessions WHERE family_id=?", (fid,))["n"] == 0
    assert auth.current(circle["elder"]["_token"]) is None
    assert db.get_family(other)["name"] == "Other"
