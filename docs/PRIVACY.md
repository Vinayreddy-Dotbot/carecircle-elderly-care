# Privacy and data handling

## What CareCircle stores
| Data | Why | Stored as |
|---|---|---|
| Circle name, elder's name, members' names and roles | identify people in the circle | names encrypted |
| Email addresses | reminders and sign-in codes | encrypted + keyed hash for lookup |
| Access codes | sign-in | scrypt hash only (never readable) |
| Medicines: name, dose, instructions, times, stock, prescriber | the care plan | name/dose/instructions/prescriber encrypted |
| Dose history: time, status, who confirmed | adherence | status and times plain, "who" encrypted |
| Appointments: doctor, specialty, place, notes, questions | reminders, doctor visits | encrypted |
| Mood check-ins (1-5) | wellbeing trend | score plain, note encrypted |
| Who-did-what log, security log | accountability, incident response | activity text encrypted; security log holds IDs and event names only |
| Copies of reminder emails | delivery status, troubleshooting | encrypted; sign-in codes are never stored |

Family members (lowest role) cannot see other people's email addresses.

## How long it is kept
Deleted automatically by the daily purge (configurable, in days):

| Data | Default | Setting |
|---|---|---|
| Dose history, mood check-ins | 730 | `RETENTION_DOSE_DAYS`, `RETENTION_CHECKIN_DAYS` |
| Who-did-what log | 365 | `RETENTION_ACTIVITY_DAYS` |
| Copies of sent emails | 30 | `RETENTION_OUTBOX_DAYS` |
| Security log | 365 | `RETENTION_SECURITY_DAYS` |
| Stock history | 730 | `RETENTION_STOCK_DAYS` |
| Stopped medicines, past appointments | 730 | `RETENTION_RECORD_DAYS` |
| Failed-sign-in counters, used one-time codes, ended sessions | 1 | fixed |

Backups contain everything at the time they were taken; keep only as many as you need and delete old ones.

## Access, correction and deletion
- **Copy of the data**: Privacy & data > Prepare a copy (JSON). Server: `python manage.py export-family ID --out file.json`.
- **Correction**: editors can change any record; changes are logged.
- **Deletion**: Privacy & data > Delete this care circle (type the name to confirm). Everyone with an email is told.
  Server: `python manage.py delete-family ID --yes`. Deleted rows are overwritten in the database file
  (`secure_delete`), but they remain in older backups until those expire.

## Email
By default reminders say only that something is due ("your 8:00 AM medicines (2)"), without medicine, dose or
doctor names, because emails are visible on lock screens and to anyone with the inbox. A circle can choose full
detail in Settings.

## AI assistant (optional)
Off until the elder or a caretaker turns it on for the circle. When used, the question and the medicine list
(names, doses, times, confirmation counts) are sent to Google Gemini. Names of people and doctors, places, notes and
emails are not sent; phone numbers, emails, ID/card numbers and known names typed into a question are removed first.
Google's terms for the unpaid Gemini tier allow human review and use for product improvement and ask developers
not to submit personal information, so production requires `GEMINI_PAID_TIER=true` (a key whose Cloud project has
billing enabled). Check Google's current terms before enabling it.
