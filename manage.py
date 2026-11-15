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
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from collections.abc import Callable, Sequence

import auth
import config
import crypto
import db
import health
import logutil
import maintenance

Handler = Callable[[argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface: one sub-command per administration task."""
    p = argparse.ArgumentParser(prog="manage.py", description="CareCircle administration",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("generate-key", "key-info", "check-config", "migrate", "purge", "seed-demo"):
        sub.add_parser(name)
    sub.add_parser("health").add_argument("--json", action="store_true")
    sub.add_parser("backup").add_argument("--out")
    sub.add_parser("verify-backup").add_argument("file")
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.add_argument("--yes", action="store_true")
    sub.add_parser("list-members").add_argument("family_id", type=int)
    sub.add_parser("reset-code").add_argument("member_id", type=int)
    sub.add_parser("revoke-sessions").add_argument("--member", type=int)
    ef = sub.add_parser("export-family")
    ef.add_argument("family_id", type=int)
    ef.add_argument("--out", required=True)
    df = sub.add_parser("delete-family")
    df.add_argument("family_id", type=int)
    df.add_argument("--yes", action="store_true")
    sub.add_parser("rotate-key").add_argument("--force", action="store_true")
    return p


# ---------------------------------------------------------------- commands that work without a database
def cmd_generate_key(a: argparse.Namespace) -> int:
    """Print a new DATA_KEY."""
    print(crypto.generate_key())
    print("Store this as DATA_KEY in your secrets. Keep a copy somewhere safe: without it the data and backups "
          "cannot be read.", file=sys.stderr)
    return 0


def _print_key(label: str, read: Callable[[], str]) -> None:
    """Print one key id, or why it can't be read."""
    try:
        print(f"{label}{read()}")
    except Exception as e:
        print(f"{label}invalid ({e})")


def cmd_key_info(a: argparse.Namespace) -> int:
    """Which key the database expects, and which keys are available. Exit 1 if the database can't be read."""
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
        _print_key("previous key:  ", functools.partial(crypto.kid_of, raw))
    dev = path + ".devkey"
    if os.path.exists(dev):
        def dev_kid() -> str:
            """The development key file's key id, and where the file is."""
            with open(dev, encoding="utf-8") as f:
                return f"{crypto.kid_of(f.read().strip())}  {dev}"
        _print_key("dev key file:  ", dev_kid)
    try:
        db.check_key()
    except Exception as e:
        print(f"status:        CANNOT READ: {e}")
        return 1
    print("status:        OK, the database can be read")
    return 0


def cmd_check_config(a: argparse.Namespace) -> int:
    """List configuration errors and warnings. Exit 1 if there are errors."""
    errors, warnings = config.validate()
    for e in errors:
        print("ERROR  ", e)
    for w in warnings:
        print("WARNING", w)
    print(f"APP_ENV={config.env()}  database={config.db_path()}")
    return 1 if errors else 0


def cmd_migrate(a: argparse.Namespace) -> int:
    """Create or upgrade the database."""
    applied = db.init_db()
    print(f"schema v{db.schema_version()}" + (f" (applied {applied})" if applied else " (already current)"))
    return 0


# ---------------------------------------------------------------- commands that need the database
def cmd_health(a: argparse.Namespace) -> int:
    """Health check for monitoring. Exit 1 on failure."""
    res = health.check()
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        for c in res["checks"]:
            print(f"{c['status'].upper():5} {c['check']:15} {c['detail']}")
        print("overall:", res["status"])
    return 1 if res["status"] == "fail" else 0


def cmd_backup(a: argparse.Namespace) -> int:
    """Write an encrypted backup and print its path."""
    print(maintenance.backup(a.out))
    return 0


def cmd_verify_backup(a: argparse.Namespace) -> int:
    """Decrypt and integrity-check a backup without restoring it."""
    print(json.dumps(maintenance.verify_backup(a.file)))
    return 0


def cmd_restore(a: argparse.Namespace) -> int:
    """Replace the live database with a backup (only with --yes)."""
    if not a.yes:
        print("This replaces the live database (a backup of it is taken first). Re-run with --yes.")
        return 1
    print(json.dumps(maintenance.restore(a.file)))
    return 0


def cmd_purge(a: argparse.Namespace) -> int:
    """Apply the retention periods now."""
    print(json.dumps(maintenance.purge()))
    return 0


def cmd_list_members(a: argparse.Namespace) -> int:
    """Member IDs and roles for one circle. Access codes are not stored, so they can't be listed."""
    for m in db.members(a.family_id):
        print(m["id"], m["role"], m["name"], "(old code, must change)" if m["legacy_lookup"] else "")
    return 0


def cmd_reset_code(a: argparse.Namespace) -> int:
    """Give a locked-out member a new access code, printed once."""
    m = db.get_member(a.member_id)
    if not m:
        print("no such member")
        return 1
    code = auth.reset_code(m["family_id"], m["id"])
    db.security_event("code_reset", m["family_id"], m["id"], "by server admin")
    print(f"New access code for member {m['id']} ({m['role']}): {code}")
    print("Give it to them privately. It is not stored and will not be shown again.", file=sys.stderr)
    return 0


def cmd_revoke_sessions(a: argparse.Namespace) -> int:
    """Sign out one member (--member ID), or everyone."""
    n = auth.revoke_member_sessions(a.member) if a.member else auth.revoke_all_sessions()
    db.security_event("sessions_revoked", None, a.member, "by server admin")
    print(f"revoked {n} session(s)")
    return 0


def cmd_export_family(a: argparse.Namespace) -> int:
    """Write everything held about one circle to a private JSON file."""
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(maintenance.export_family(a.family_id))
    print(a.out)
    return 0


def cmd_delete_family(a: argparse.Namespace) -> int:
    """Permanently delete one circle (only with --yes)."""
    if not a.yes:
        print("This permanently deletes the circle. Re-run with --yes.")
        return 1
    print(f"deleted {maintenance.delete_family(a.family_id)} rows")
    return 0


def cmd_rotate_key(a: argparse.Namespace) -> int:
    """Re-encrypt everything with the current DATA_KEY."""
    print(f"re-encrypted {maintenance.rotate_key(a.force)} rows")
    return 0


def cmd_seed_demo(a: argparse.Namespace) -> int:
    """Create the sample family and print its access codes (not in production)."""
    import seed
    print(json.dumps(seed.seed_demo(), indent=2))
    return 0


NO_DATABASE: dict[str, Handler] = {
    "generate-key": cmd_generate_key, "key-info": cmd_key_info, "check-config": cmd_check_config,
    "migrate": cmd_migrate,
}
WITH_DATABASE: dict[str, Handler] = {
    "health": cmd_health, "backup": cmd_backup, "verify-backup": cmd_verify_backup, "restore": cmd_restore,
    "purge": cmd_purge, "list-members": cmd_list_members, "reset-code": cmd_reset_code,
    "revoke-sessions": cmd_revoke_sessions, "export-family": cmd_export_family, "delete-family": cmd_delete_family,
    "rotate-key": cmd_rotate_key, "seed-demo": cmd_seed_demo,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run one administration command. Returns the process exit code."""
    a = build_parser().parse_args(argv)
    logutil.setup()
    if a.cmd in NO_DATABASE:
        return NO_DATABASE[a.cmd](a)
    db.init_db()   # create or upgrade the database before any command that reads it
    return WITH_DATABASE[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
