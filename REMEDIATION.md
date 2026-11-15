# CareCircle remediation report

Response to the 65-item review. Each item is marked:

- **Fixed**: the code now handles it, and a test covers it.
- **Mitigated**: the risk is reduced in code; some risk remains, stated in the row.
- **Limitation**: can't be solved in code alone (needs a licensed data source, real users, or is inherent). The app
  now says so where it matters.

**Totals: 40 fixed, 19 mitigated, 6 limitations.** Test suite: 184 tests. 177 pass here; the 7 in
`tests/test_app_streamlit.py` use Streamlit's own AppTest and are skipped when Streamlit isn't installed.
Eleven further problems found during the work are at the end.

## 1. Critical security problems
| # | Issue | Status | What changed |
|---|---|---|---|
| 1 | Plaintext passcodes | Fixed | scrypt hashes with per-member salt (`auth.py`, `crypto.py`). The migration hashes existing codes, drops the column and vacuums the file, so no plaintext remains on disk (`test_migration.py` checks the raw file). |
| 2 | Six-character codes | Fixed | 12 characters from a 31-letter alphabet (~59 bits), shown as `XXXX-XXXX-XXXX`. Old codes work once, then must be replaced. |
| 3 | No rate limiting | Fixed | Failure limits per browser session (10/15 min) and per IP (30/15 min, a soft signal), app-wide slowdown, limits on email codes and new circles. Throttle checks run before the hash. |
| 4 | No lockout | Fixed | 5 wrong codes for a member in 15 min pauses their code sign-in for 15 min and emails them. Residual: someone who knows the first 4 characters of a code could lock that person out; "Email me a code" still works. |
| 5 | Passcodes shown in the Circle screen | Fixed | Codes are shown once, when created or reset, and can't be shown again (only hashes exist). |
| 6 | Elder's code typed in plain sight | Fixed | Masked for everyone, with the show/hide eye to check typing. Role picker removed. README updated. |
| 7 | No reset or recovery | Fixed | Elder/caretaker can give anyone a new code; anyone can change their own; elder and family can sign in by emailed code; `manage.py reset-code` for a server admin. |
| 8 | No MFA | Fixed | Emailed 6-digit code as a second step, required for caretakers in production (`MFA_ROLES`) and optional for others. Residual: weaker than an authenticator app or passkeys, and depends on email. |
| 9 | No session expiry | Fixed | Server-side sessions: 30 min idle, 12 h absolute. Removal, code reset and "Sign out on all devices" end sessions at once. |
| 10 | DB functions don't check permissions | Fixed | `services.py` checks the actor's permission on every action (`authz.py`); every `db.py` query is scoped to the actor's family; updates use column allow-lists. `test_authz.py` tries 9 cross-family actions. |

## 2. Database and privacy
| # | Issue | Status | What changed |
|---|---|---|---|
| 11 | SQLite unsuitable on ephemeral cloud disks | Mitigated | Production refuses a database inside the app folder and refuses to run on Streamlit Community Cloud. WAL mode, encrypted backups. Not done: a PostgreSQL backend. For production, use a host with a persistent volume, or port `db.py` (all SQL is there). |
| 12 | No backup and restore | Fixed | Encrypted backups (`manage.py backup / verify-backup / restore`), automatic backup before every migration, restore keeps a copy of the current database. Scheduling them is an ops task (cron example in DEPLOYMENT.md). |
| 13 | No encryption at rest | Fixed | AES-256-GCM per field for names, emails, medicine details, appointments, notes, logs and email copies; encrypted backups; files 0600; key rotation (`manage.py rotate-key`); `manage.py key-info` shows which key the database expects. Left plain for queries: IDs, dates, times, statuses, stock, mood scores. |
| 14 | No retention or deletion policy | Fixed | Retention periods with a daily purge, "Delete this care circle", data export. Documented in docs/PRIVACY.md. |
| 15 | Sensitive information stored | Mitigated | Data minimised where possible: minimal emails by default, minimal AI context, sign-in codes never stored, no emails in dedupe keys, family role can't see emails. Medical data itself is the app's purpose. |
| 16 | No production/demo separation | Fixed | `APP_ENV` profiles; sample data and time simulation disabled in production; sample families flagged; example.com addresses never emailed. |
| 17 | No breach process | Fixed | Incident runbook in docs/SECURITY.md, plus the tools it uses: security log, code reset, sign-out everywhere, `revoke-sessions`, key rotation, export, and emails to the elder when someone else adds a caretaker, removes a member or resets a code. |

## 3. Elder safety and medical reliability
| # | Issue | Status | What changed |
|---|---|---|---|
| 18 | Only 13 hardcoded pairs | Mitigated | Rules moved to `data/interactions.json` and generalised to the drug classes the original messages already named (NSAIDs, ACE inhibitors, nitrates, PDE5 inhibitors): 14 rules over 31 ingredients plus brand names. Still small on purpose; no new medical claims were added from memory. |
| 19 | Can't check every interaction | Limitation | The UI now says what it couldn't check, and that no warning doesn't mean safe, both before adding a medicine and in Insights. |
| 20 | Keyword matching misses spellings and brands | Mitigated | Brand names (Ecosprin, Glycomet, Thyronorm...), combination products (Combiflam = ibuprofen + paracetamol), "did you mean" suggestions, word-boundary matching, duplicate-ingredient detection. Unknown brands are reported as unrecognised. |
| 21 | Not connected to a verified database | Limitation | Needs a licensed source (DrugBank, First Databank, Medi-Span) or a pharmacist-curated import of the open DDInter dataset; NLM's free RxNav interaction API was discontinued in January 2024. The data file records `reviewed_by` (currently none), and the UI shows that. |
| 22 | Doesn't replace a pharmacist | Mitigated | Warnings shown before saving, disclaimers, and a per-medicine "checked against the prescription" record. |
| 23 | Schedules depend on what users enter | Mitigated | Validation (dosage required, valid times, end after start, limits), a review step before saving, before→after values in the log, stale doses removed when times change. |
| 24 | Can't confirm a medicine was physically taken | Limitation | Inherent. Wording now says "Confirmed taken by <name>", and the Today screen explains that CareCircle records what people tap. |
| 25 | Automatic "missed" can be wrong | Fixed | Late doses become **unconfirmed**, not missed. Alerts say it doesn't mean missed; adherence reports unconfirmed doses separately; anyone can confirm later. |
| 26 | Stock counts drift | Fixed | Dose status and stock change in one transaction; each dose decrements at most once (`stock_applied`); stock ledger; manual adjustment needs a reason. Concurrency tests. |
| 27 | No emergency response | Mitigated | "I need help" button for the elder sends an immediate high-priority email to everyone, with "It was a mistake"; the emergency number is always on screen; the AI refuses urgent questions. It is not an emergency service and says so. |
| 28 | No verification of medicine changes | Mitigated | "Checked against prescription" (who, when, prescriber), cleared automatically when dose or times change; unchecked medicines are badged; family changes need approval. This records a human check; it doesn't contact a doctor. |

## 4. Reminders and notifications
| # | Issue | Status | What changed |
|---|---|---|---|
| 29 | Scheduler is a thread inside Streamlit | Fixed | `worker.py` runs reminders as a separate process; `SCHEDULER_MODE=thread/external/off`. |
| 30 | No guarantee reminders keep running | Mitigated | Heartbeat each pass; caretakers see a warning when it's stale; `manage.py health` for monitoring; another worker takes over a lapsed lease. Continuous running still depends on the host and monitoring. |
| 31 | Without SMTP, emails are only simulated | Fixed | Production won't start without SMTP; banners in development and demo; health check. |
| 32 | Delivery can fail | Mitigated | Exponential backoff, recovery of crashed claims, dead-letter status shown to caretakers and in the health check. Provider outages remain possible. |
| 33 | Only three retries | Fixed | `EMAIL_MAX_ATTEMPTS` (default 5) with backoff (1, 2, 4, 8 min...). Time-sensitive reminders expire instead of arriving late. |
| 34 | Doesn't guarantee the email is read | Limitation | Inherent. Escalation covers it: a dose not confirmed in the app alerts the whole circle. |
| 35 | Fixed 60-second interval | Fixed | `SCHEDULER_INTERVAL_SEC` (15-3600). |
| 36 | Multiple instances not coordinated | Fixed | Database lease for the reminder pass, and atomic claiming of each email, so two instances never send the same message (tested with 4 parallel dispatchers). |
| 37 | Emails expose medicine and appointment details | Fixed | Minimal content by default; a circle can opt in to full detail. Sign-in codes are never stored in the outbox. |

## 5. Streamlit Community Cloud deployment
| # | Issue | Status | What changed |
|---|---|---|---|
| 38 | SQLite lost on restart | Mitigated | See 11: production refuses Community Cloud and the app folder; DEPLOYMENT.md says Community Cloud is demo only and lists hosts with persistent disks. |
| 39 | Default database beside db.py | Fixed | Production requires `CARECIRCLE_DB` outside the app folder. |
| 40 | Settings page writes credentials to .env | Fixed | Only in development; read-only in demo and production; only the three expected keys; single-line values; file mode 0600. |
| 41 | Production secrets needed | Fixed | `config.validate()` at startup (production refuses to start on errors), `manage.py check-config`, `.streamlit/secrets.toml.example`. |
| 42 | No migration process | Fixed | Numbered migrations in one transaction, encrypted backup first, refuses newer schemas, idempotent; tested against a database built like the original app's. |
| 43 | No health check | Fixed | `manage.py health` (exit code, `--json`): config, database, key, reminder heartbeat, email delivery. In-app status for caretakers. |
| 44 | No multi-user testing | Fixed | 20 simultaneous users against one database (`test_flows.py`). Browser-level load testing on the real host is still recommended (DEPLOYMENT.md). |
| 45 | No recovery test | Fixed | Tests restore after the database file is lost, reject a corrupted backup without touching the live database, and show a new worker taking over after a crash. Restore drill in DEPLOYMENT.md. |

## 6. AI assistant
| # | Issue | Status | What changed |
|---|---|---|---|
| 46 | Gemini optional, needs a key | Limitation | By design. The app says why it's unavailable and falls back to a plain schedule. |
| 47 | Answers may be wrong | Mitigated | Every answer labelled as AI-generated and not medical advice; low temperature; answer checks (49). |
| 48 | Answers mistaken for instructions | Mitigated | System prompt forbids dose changes; answers mentioning dose changes or amounts not in the plan are flagged. |
| 49 | No response validation | Mitigated | Heuristic checks for dose-change language and unknown amounts; emergency questions are never sent. Not clinical validation. |
| 50 | Personal data in prompts | Fixed | Off until the circle opts in; prompt holds only medicines and adherence numbers; phone numbers, emails, ID/card numbers, PAN and known names are removed from questions, and the user is told; production also needs `GEMINI_PAID_TIER=true`, since Google's unpaid-tier terms allow human review and ask not to submit personal data. |
| 51 | External service can fail | Mitigated | Timeouts; a quick retry and then a fallback to another current Gemini model when Google is overloaded (HTTP 5xx); automatic switch away from retired models; a circuit breaker after repeated failures; Google's own error shown to the user. |

## 7. User interface and accessibility
| # | Issue | Status | What changed |
|---|---|---|---|
| 52 | Codes hard to remember | Mitigated | Email sign-in, codes ignore case and dashes, no look-alike characters, anyone can be given a new code. Trade-off: 12 characters are harder to remember than 6; test with users (54). |
| 53 | No alternative sign-in | Fixed | "Email me a code" for the elder and family members. |
| 54 | Needs testing with elderly users | Limitation | Needs real people: plan in docs/USABILITY_TEST_PLAN.md. In code: large text on by default for the elder, undo on every dose mark, confirmations before stopping, cancelling, removing or resetting, review step before adding a medicine. |
| 55 | No voice navigation | Mitigated | "Read my medicines aloud" (browser speech, English or Hindi). Voice commands aren't implemented. |
| 56 | No multilingual interface | Mitigated | Hindi for the elder's screens (sign-in, Today, help), with a language switch. Other screens are English. The Hindi needs a native speaker's review. |
| 57 | No offline mode | Mitigated | Download a large-print medicine list to print or keep. A real offline mode isn't possible with Streamlit's server-rendered UI. |

## 8. Code quality and testing
| # | Issue | Status | What changed |
|---|---|---|---|
| 58 | No test suite | Fixed | 184 tests in `tests/` (`pytest`). |
| 59 | No security testing | Fixed | Tests for hashing, brute force, lockout, MFA, session expiry, cross-family access, column injection, HTML/link escaping, email header injection, encryption at rest and key rotation; `bandit` and `pip-audit` in CI. No external penetration test yet. |
| 60 | No end-to-end testing | Fixed | Full journey test (add → suggest → approve → reminder → confirm → alert → summary); UI flows run through a Streamlit stand-in; `test_app_streamlit.py` repeats key flows with Streamlit's AppTest. |
| 61 | No concurrency testing | Fixed | Concurrent dose marking, question appends and dispatching; WAL and `BEGIN IMMEDIATE` transactions. |
| 62 | No error monitoring | Fixed | JSON logs without personal data, error reference codes shown to users, optional Sentry with PII off. |
| 63 | External failures need handling | Fixed | SMTP and Gemini timeouts, retries and backoff, circuit breaker; one family's bad data can't stop reminders for others. |
| 64 | Demo seed could run in production | Fixed | Refuses outside development and demo; sample data flagged. |
| 65 | No production vs demo configuration | Fixed | Three documented profiles (README, `.env.example`, `secrets.toml.example`). |

## Further problems found and fixed
| | Problem | Fix |
|---|---|---|
| A | **Cross-circle:** any circle's elder or caretaker could overwrite the server-wide SMTP and Gemini keys in Settings, routing every family's reminders through their own mailbox. | Server keys are read-only outside local development. |
| B | **Cross-circle:** "Simulate a time" and "Run reminder check now" processed every family, so one user could mark other families' doses as missed and send them false alerts. | Limited to the user's own circle; simulation is demo-only. |
| C | **Stored HTML/link injection:** medicine names, instructions and names were rendered as raw HTML/Markdown. A family member (lowest role) could inject markup or links shown to the elder. | All user text escaped for Markdown and HTML; AI output stripped of links. |
| D | Removed members kept access until they signed out (the session held a copy of their record). | Sessions re-checked against the database on every page load. |
| E | The login role picker confirmed whether a code was valid ("This passcode belongs to the elder"). | Picker removed; one generic error. |
| F | Two people tapping "Taken" at once could double-decrement stock (read and write on separate connections). | Single transaction plus per-dose flag. |
| G | Undo wasn't logged; changing a medicine's times left today's doses at the old times; the edit form would crash for any stored time not on the half-hour grid. | Fixed, with tests. |
| H | Email dedupe keys stored addresses in plain text. | Keyed hashes. |
| I | The `.env` parser kept inline comments as part of values, so the shipped `.env.example` line would have produced a broken password. | Parser fixed. |
| J | Any editor could send "test" emails to any address through the server's SMTP account. | Test email goes only to the signed-in person's own address. |
| K | Activity and email timestamps were UTC but shown as local time. | Converted to `APP_TZ` for display. |

## Not verified here
- **The UI wasn't run in real Streamlit** (it couldn't be installed in the environment used for this work). It was
  executed through `tests/fake_streamlit.py` for every role and the main flows. Run `pytest` after
  `pip install -r requirements.txt -r requirements-dev.txt` to run `tests/test_app_streamlit.py` too, and click
  through once before deploying.
- `bandit` and `pip-audit` are configured in CI but weren't run here.
- The Hindi text and the interaction list need review by a native speaker and a pharmacist.
