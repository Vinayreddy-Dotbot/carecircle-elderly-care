# Deployment

## Where to host
CareCircle keeps its data in a SQLite file, so the host must give it a **persistent disk**.

| Host | Use for |
|---|---|
| Streamlit Community Cloud | **Demo only** (`APP_ENV=demo`). It has no persistent disk: the database is wiped on every reboot or redeploy and can't be recovered. Production mode refuses to start there. |
| A VM or Docker host with a volume (any cloud, or a small home server) | Production. Put the database on the volume and run `worker.py` alongside. |
| Container platforms with attachable disks (Fly.io volumes, Railway volumes, Render disks) | Production, single instance, database on the disk. |

Running more than one web instance against one SQLite file only works if they share the same disk on the same
machine. To scale beyond that, move `db.py` to PostgreSQL (all SQL lives in that file).

## Production checklist
1. `python manage.py generate-key` and store the output as `DATA_KEY` in your secrets manager. **Keep a second copy
   offline**: without it the data and backups can't be read.
2. Set `APP_ENV=production`, `CARECIRCLE_DB=/data/carecircle.db` (on the persistent disk), SMTP settings,
   `MFA_ROLES=caretaker` (the default), `APP_TZ`, `EMERGENCY_NUMBER`. Optional: `GEMINI_API_KEY` with
   `GEMINI_PAID_TIER=true`, `SENTRY_DSN`.
3. `python manage.py check-config` until it reports no errors, then `python manage.py migrate`.
4. Start the reminder worker as its own process and set `SCHEDULER_MODE=external` for the web app:
   `python worker.py` (systemd, a second container, or a host "background worker"). Several workers are safe: only
   the lease holder sends, and another takes over within ~3 minutes if it stops.
5. Start the app: `streamlit run app.py --server.headless true` behind HTTPS (a reverse proxy such as Caddy or nginx,
   or the platform's TLS).
6. Monitoring: run `python manage.py health` every few minutes (exit code 1 = fail; `--json` for tools). It checks
   configuration, database, encryption key, reminder heartbeat and email delivery.
7. Backups: schedule `python manage.py backup` (encrypted) and copy the files off the server, for example:
   `0 2 * * * cd /app && BACKUP_DIR=/data/backups python manage.py backup && find /data/backups -name '*.ccbak' -mtime +30 -delete`

### Example (systemd)
```
# /etc/systemd/system/carecircle-worker.service
[Service]
WorkingDirectory=/opt/carecircle
EnvironmentFile=/etc/carecircle.env
ExecStart=/opt/carecircle/.venv/bin/python worker.py
Restart=always
User=carecircle
```
The web app gets the same `EnvironmentFile` and `ExecStart=... streamlit run app.py --server.headless true`.

## Upgrades and migrations
On start, `db.init_db()` applies numbered migrations (tracked in `PRAGMA user_version`) inside one transaction,
after writing an encrypted backup `carecircle.db.pre-vN.ccbak`. It refuses to run against a database from a newer
version. To upgrade: back up, deploy the new code, run `python manage.py migrate`, start the app.
The upgrade from the original app hashes every 6-character passcode, encrypts existing records and vacuums the file
so the old plaintext is gone; each person is asked to take a new 12-character code at their next sign-in.

## Restore drill (do this before you need it)
1. `python manage.py verify-backup /data/backups/<file>.ccbak` (decrypts to a temporary file and runs an integrity check).
2. On a staging copy: `CARECIRCLE_DB=/tmp/restore-test.db python manage.py restore <file> --yes`, then start the app
   against it and sign in.
3. In an emergency on production: stop the app and worker, `python manage.py restore <file> --yes` (the current
   database is backed up first), start them again, run `python manage.py health`.

## Demo on Streamlit Community Cloud
Use `.streamlit/secrets.toml.example` as a template in the app's Secrets settings, with `APP_ENV = "demo"` and
`SCHEDULER_MODE = "thread"`. Everything is lost on reboot, which is fine for sample data only.

## Load and multi-user testing
`tests/test_flows.py::test_many_people_at_once` runs 20 simultaneous users against one database. Before a real
launch, also test with real browsers against your host (for example Locust or k6 driving the Streamlit URL), since
Streamlit keeps one websocket per open tab.
