"""Issue 42 (and 1, 13): upgrading a database made by the original app."""
import os
import sqlite3

import auth
import config
import db
import maintenance
from conftest import raw_db_bytes

LEGACY = [
    "INSERT INTO families(id,name,elder_name) VALUES(1,'Sharma Family','Asha Sharma')",
    "INSERT INTO families(id,name,elder_name) VALUES(2,'Real Family','Kamala Devi')",
    "INSERT INTO members VALUES(1,1,'Asha Sharma','asha.demo@example.com','elder','ABC234')",
    "INSERT INTO members VALUES(2,1,'Ravi (son)','ravi.demo@example.com','caretaker','XYZ789')",
    "INSERT INTO members VALUES(3,2,'Kamala Devi','kamala@gmail.test','elder','KMN456')",
    "INSERT INTO medications(id,family_id,name,dosage,times,start_date,stock,refill_at,added_by) "
    "VALUES(1,1,'Metformin','500 mg','08:00,20:00','2026-01-01',40,6,'Ravi (son)')",
    "INSERT INTO doses(med_id,family_id,scheduled_date,scheduled_time,status,marked_by) VALUES(1,1,'2026-01-02','08:00','taken','Asha Sharma')",
    "INSERT INTO doses(med_id,family_id,scheduled_date,scheduled_time,status,marked_by) VALUES(1,1,'2026-01-02','20:00','missed','auto')",
    "INSERT INTO appointments(family_id,doctor,when_at,location) VALUES(1,'Dr. Iyer','2099-01-01T11:00','Apollo Clinic')",
    "INSERT INTO outbox(family_id,to_email,subject,body,kind,dedupe_key,status,attempts) "
    "VALUES(1,'ravi.demo@example.com','Missed dose: Metformin','body','dose_missed','dose-missed-2|ravi.demo@example.com','failed',1)",
    "INSERT INTO outbox(family_id,to_email,subject,body,kind,dedupe_key,status,attempts) "
    "VALUES(1,'asha.demo@example.com','x','y','dose_due','dose-due-1|asha.demo@example.com','failed',3)",
    "INSERT INTO activity(family_id,who,what) VALUES(1,'Ravi (son)','added Metformin')",
]


def make_legacy_db(path):
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.executescript(db.SCHEMA_V1)
    c.execute("ALTER TABLE outbox ADD COLUMN attempts INTEGER DEFAULT 0")
    for s in LEGACY:
        c.execute(s)
    c.commit()
    c.close()


def test_upgrade_from_original_app():
    path = config.db_path()
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    make_legacy_db(path)
    assert db.schema_version() == 0

    assert db.init_db() == [1, 2, 3]
    assert db.schema_version() == db.SCHEMA_VERSION

    # passcodes: column gone, plaintext gone from the file (including free pages), old code still works once
    assert "passcode" not in {r["name"] for r in db.q("PRAGMA table_info(members)")}
    raw = raw_db_bytes()
    for secret in (b"ABC234", b"XYZ789", b"KMN456", b"Metformin", b"asha.demo@", b"Apollo Clinic", b"Ravi (son)"):
        assert secret not in raw, secret
    r = auth.login_with_code("abc234", ["bs:m"])
    assert r.status == "rotate"
    new = auth.rotate_own_code(r.token)
    assert auth.login_with_code("ABC234", ["bs:m"]).status == "invalid"
    assert auth.login_with_code(new, ["bs:m"]).status == "ok"

    # data is readable and was transformed
    assert db.get_family(1)["is_demo"] == 1 and db.get_family(2)["is_demo"] == 0
    assert db.meds(1)[0]["name"] == "Metformin"
    statuses = {d["scheduled_time"]: d for d in db.q("SELECT * FROM doses")}
    assert statuses["20:00"]["status"] == "unconfirmed"
    assert statuses["08:00"]["stock_applied"] == 1
    out = {o["kind"]: o for o in db.q("SELECT * FROM outbox")}
    assert out["dose_missed"]["status"] == "pending" and out["dose_due"]["status"] == "dead"
    assert all("@" not in o["dedupe_key"] for o in out.values())

    # an encrypted backup was taken before anything changed
    bak = f"{path}.pre-v{db.SCHEMA_VERSION}.ccbak"
    with open(bak, "rb") as f:
        blob = f.read()
    assert blob.startswith(maintenance.MAGIC) and b"ABC234" not in blob
    info = maintenance.verify_backup(bak)
    assert info == {"ok": True, "schema_version": 0, "families": 2}

    assert db.init_db() == []  # idempotent


def test_newer_schema_is_refused():
    c = db.conn()
    c.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
    c.close()
    import pytest
    with pytest.raises(RuntimeError):
        db.init_db()


def test_wrong_key_is_detected_at_start(monkeypatch):
    import crypto
    import pytest
    monkeypatch.setenv("DATA_KEY", crypto.generate_key())
    with pytest.raises(crypto.DecryptionError):
        db.init_db()
