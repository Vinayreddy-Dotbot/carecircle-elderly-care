"""Server administration. Run on the machine (or container) that holds the database.

    python manage.py generate-key                 new DATA_KEY for your secrets
    python manage.py key-info                     which key the database expects, and which keys are available
    python manage.py check-config                 show configuration problems
    python manage.py migrate                      create or upgrade the database
    python manage.py health                       health check (exit 1 on failure); add --json for monitoring
    python manage.py backup [--out FILE]          encrypted backup
    python manage.py verify-backup FILE           decrypt and integrity-check a backup without restoring it
    python manage.py restore FILE --yes           replace the database with a backup
    python manage.py purge                        apply retention periods now
    python manage.py list-members FAMILY_ID       member IDs and roles (no codes: they are not stored)
    python manage.py reset-code MEMBER_ID         new access code for a locked-out member (printed once)
    python manage.py revoke-sessions [--member ID]  sign out one member, or everyone
    python manage.py export-family FAMILY_ID --out FILE
    python manage.py delete-family FAMILY_ID --yes
    python manage.py rotate-key [--force]         re-encrypt with DATA_KEY (old key in DATA_KEY_PREVIOUS)
    python manage.py seed-demo                    sample family (not in production)
"""
import argparse
import json
import os
import sys

import auth
import config
import crypto
import db
import health
import logutil
import maintenance


def main(argv=None):
    p = argparse.ArgumentParser(prog="manage.py", description="CareCircle administration",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate-key")
    sub.add_parser("key-info")
    sub.add_parser("check-config")
    sub.add_parser("migrate")
    h = sub.add_parser("health")
    h.add_argument("--json", action="store_true")
    b = sub.add_parser("backup")
    b.add_argument("--out")
    vb = sub.add_parser("verify-backup")
    vb.add_argument("file")
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.add_argument("--yes", action="store_true")
    sub.add_parser("purge")
    lm = sub.add_parser("list-members")
    lm.add_argument("family_id", type=int)
    rc = sub.add_parser("reset-code")
    rc.add_argument("member_id", type=int)
    rs = sub.add_parser("revoke-sessions")
    rs.add_argument("--member", type=int)
    ef = sub.add_parser("export-family")
    ef.add_argument("family_id", type=int)
    ef.add_argument("--out", required=True)
    df = sub.add_parser("delete-family")
    df.add_argument("family_id", type=int)
    df.add_argument("--yes", action="store_true")
    rk = sub.add_parser("rotate-key")
    rk.add_argument("--force", action="store_true")
    sub.add_parser("seed-demo")
    a = p.parse_args(argv)
    logutil.setup()

    if a.cmd == "generate-key":
        print(crypto.generate_key())
        print("Store this as DATA_KEY in your secrets. Keep a copy somewhere safe: without it the data and backups "
              "cannot be read.", file=sys.stderr)
        return 0
    if a.cmd == "key-info":
        path = config.db_path()
        print(f"database:      {path}" + ("" if os.path.exists(path) else "  (does not exist yet)"))
        if os.path.exists(path):
            print(f"schema:        v{db.schema_version()}")
            print(f"data key:      {db.expected_key_id() or 'none yet (new or pre-upgrade database)'}")
        try:
            print(f"key in use:    {crypto.key_id()}  from {crypto.key_source()}")
        except Exception as e:
            print(f"key in use:    unavailable ({e})")
        for raw in crypto.previous_keys():
            try:
                print(f"previous key:  {crypto.kid_of(raw)}")
            except Exception as e:
                print(f"previous key:  invalid ({e})")
        dev = path + ".devkey"
        if os.path.exists(dev):
            try:
                print(f"dev key file:  {crypto.kid_of(open(dev).read().strip())}  {dev}")
            except Exception as e:
                print(f"dev key file:  invalid ({e})")
        try:
            db.check_key()
            print("status:        OK, the database can be read")
            return 0
        except Exception as e:
            print(f"status:        CANNOT READ: {e}")
            return 1
    if a.cmd == "check-config":
        errors, warnings = config.validate()
        for e in errors:
            print("ERROR  ", e)
        for w in warnings:
            print("WARNING", w)
        print(f"APP_ENV={config.env()}  database={config.db_path()}")
        return 1 if errors else 0
    if a.cmd == "migrate":
        applied = db.init_db()
        print(f"schema v{db.schema_version()}" + (f" (applied {applied})" if applied else " (already current)"))
        return 0

    db.init_db()
    if a.cmd == "health":
        res = health.check()
        if a.json:
            print(json.dumps(res, indent=2))
        else:
            for c in res["checks"]:
                print(f"{c['status'].upper():5} {c['check']:15} {c['detail']}")
            print("overall:", res["status"])
        return 1 if res["status"] == "fail" else 0
    if a.cmd == "backup":
        print(maintenance.backup(a.out))
        return 0
    if a.cmd == "verify-backup":
        print(json.dumps(maintenance.verify_backup(a.file)))
        return 0
    if a.cmd == "restore":
        if not a.yes:
            print("This replaces the live database (a backup of it is taken first). Re-run with --yes.")
            return 1
        print(json.dumps(maintenance.restore(a.file)))
        return 0
    if a.cmd == "purge":
        print(json.dumps(maintenance.purge()))
        return 0
    if a.cmd == "list-members":
        for m in db.members(a.family_id):
            print(m["id"], m["role"], m["name"], "(old code, must change)" if m["legacy_lookup"] else "")
        return 0
    if a.cmd == "reset-code":
        m = db.get_member(a.member_id)
        if not m:
            print("no such member")
            return 1
        code = auth.reset_code(m["family_id"], m["id"])
        db.security_event("code_reset", m["family_id"], m["id"], "by server admin")
        print(f"New access code for member {m['id']} ({m['role']}): {code}")
        print("Give it to them privately. It is not stored and will not be shown again.", file=sys.stderr)
        return 0
    if a.cmd == "revoke-sessions":
        n = auth.revoke_member_sessions(a.member) if a.member else auth.revoke_all_sessions()
        db.security_event("sessions_revoked", None, a.member, "by server admin")
        print(f"revoked {n} session(s)")
        return 0
    if a.cmd == "export-family":
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(maintenance.export_family(a.family_id))
        print(a.out)
        return 0
    if a.cmd == "delete-family":
        if not a.yes:
            print("This permanently deletes the circle. Re-run with --yes.")
            return 1
        print(f"deleted {maintenance.delete_family(a.family_id)} rows")
        return 0
    if a.cmd == "rotate-key":
        print(f"re-encrypted {maintenance.rotate_key(a.force)} rows")
        return 0
    if a.cmd == "seed-demo":
        import seed
        print(json.dumps(seed.seed_demo(), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
