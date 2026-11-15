# Security

## What is protected, and from whom
CareCircle holds health information about an elderly person (medicines, doses, appointments, mood) and contact
details for their family. The main threats it is designed against:

| Threat | Main controls |
|---|---|
| Someone guesses or brute-forces an access code | 59-bit codes, per-member lockout, per-session and per-IP limits, global slowdown, scrypt hashing |
| The database file or a backup is copied | Codes stored only as scrypt hashes; names, emails, medicine details, notes, logs and email copies encrypted with AES-256-GCM; backups encrypted; files 0600; key kept outside the database |
| A member of one circle reaches another circle's data | Every service call checks the actor's role; every DB query is scoped by the actor's family; IDs from other families are "not found" |
| A family member (lowest role) escalates | Role checks in `services.py`, not just hidden buttons; medicines they add are suggestions; all user text escaped before rendering (no HTML/link injection) |
| A removed or compromised member keeps access | Server-side sessions re-validated on every page load; removal, code reset and "sign out on all devices" end sessions immediately |
| Stolen caretaker code | Two-step sign-in (emailed code) required for caretakers in production |
| Health details leaking through email or AI | Minimal email content by default; sign-in codes never stored; AI opt-in, minimised context, redaction, paid tier required in production |

Known limits: emailed one-time codes are weaker than an authenticator app or passkeys (they fall with the email
account). The per-IP limit is a soft signal (shared or spoofed IPs). There has been no external penetration test.

## Controls in detail
- **Access codes** (`auth.py`): 12 characters from a 31-letter alphabet without look-alikes (~59 bits). The first 4
  characters are a lookup handle; the full code is verified against `scrypt(N=2^14, r=8, p=1)` with a random salt.
  Unknown handles still run a dummy hash so timing doesn't reveal which exist.
- **Brute force**: 5 failures for a member in 15 minutes pauses code sign-in for that member for 15 minutes and
  emails them; 10 failures per browser session or 30 per IP in 15 minutes blocks that client; more than 200
  failures app-wide in 10 minutes slows every attempt. All limits are configurable (`LOGIN_MAX_FAILURES`,
  `LOCKOUT_MIN`, `SESSION_MAX_FAILURES`, `CLIENT_MAX_FAILURES`, `GLOBAL_SLOWDOWN_FAILURES`).
- **One-time codes**: 6 digits, 10 minutes, 5 attempts, 3 sends per 15 minutes, stored as keyed hashes. Email sign-in
  answers identically for known and unknown addresses, and isn't offered to members who need two-step sign-in.
- **Sessions**: random 256-bit tokens kept in Streamlit's server-side session state and stored hashed in the
  database. 30 minutes idle (`SESSION_IDLE_MIN`), 12 hours absolute (`SESSION_MAX_HOURS`).
- **Authorisation** (`authz.py`, `services.py`): a permission matrix per role, checked in every service function.
  `db.py` scopes every query by family and only updates allow-listed columns.
- **Encryption at rest** (`crypto.py`, `db.py`): AES-256-GCM per field, key derived from `DATA_KEY` with HKDF;
  blind HMAC index for email lookups. Not encrypted, because queries need them: IDs, dates and times, dose and
  medicine status, stock counts, mood scores (1-5), roles.
- **Output safety**: all user text escaped for Markdown (`esc`) and HTML (`h`); AI output stripped of links, images
  and HTML; email subjects stripped of line breaks.
- **Logging**: JSON logs with IDs and counts only; errors shown to users as a reference code; optional Sentry with
  PII collection off.
- **Supply chain**: CI runs the tests, `bandit` and `pip-audit`.

## Incident response runbook
Run these on the server that holds the database. Each step is logged in the security log.

**A member's code or email account may be compromised**
1. In the app (elder or caretaker): Circle tab > Manage someone's access > **Give a new code**. This ends all their sessions.
   Server: `python manage.py reset-code MEMBER_ID` (find the ID with `python manage.py list-members FAMILY_ID`).
2. If their email account is compromised, change their email in the Circle tab (the old address is notified) or remove them.
3. Review Privacy & data > **Security log** for sign-ins, code resets, invites and exports around the time.

**A caretaker account was misused**
1. Remove the member (Circle tab) or reset their code. Check the who-did-what log for medicine changes and undo them.
2. The elder is emailed automatically when someone else adds a caretaker, removes a member or resets a code.

**The database file, a backup or the server may have been copied**
1. Sign everyone out: `python manage.py revoke-sessions`.
2. Rotate the encryption key: `python manage.py generate-key`; set the new value as `DATA_KEY` and the old one as
   `DATA_KEY_PREVIOUS`; restart; run `python manage.py rotate-key`; then remove `DATA_KEY_PREVIOUS`.
   Old backups stay readable only with the old key: destroy them or keep that key offline.
3. Codes are hashed, but give everyone new codes if the attacker may have had time to attack the hashes offline
   (`reset-code` per member). Rotate the SMTP and Gemini credentials.
4. Use `python manage.py export-family FAMILY_ID` to see exactly what an affected family's record contained.

**Notifying people**
Under India's Digital Personal Data Protection Act 2023 (and GDPR if EU residents are involved), a personal data
breach may have to be reported to the regulator and to affected people. Decide this with whoever operates the
service; this runbook doesn't replace legal advice. Record: what happened, when it was found, which circles were
affected, what was done, and who was told.

## Reporting a vulnerability
Email the maintainer privately rather than opening a public issue.
