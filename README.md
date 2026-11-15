# CareCircle 💊

Looking after an elderly parent is rarely one person's job. There's the person themselves, who wants to stay
in charge of their own life. There's usually someone nearby who handles the day-to-day: the pills, the pharmacy,
the doctor visits. And there's family further away who worry, and who'd help if they only knew what was going on.

CareCircle gives all of them one shared, private place for medicines, doses and doctor visits. It reminds people by
email before things are missed, tells the family when something needs a hand, and keeps the elderly person at the
centre of every decision about their own care.

> Built for the **PromptWars × Catalyst** challenge: *Elderly Care & Medication Management Platform*.

---

## Who it's for

Meet the sample family that ships with the app:

- **Asha** (the elder) wants to know what to take and when, in large text, in Hindi if she prefers, and to have the
  final say when someone changes her medicines.
- **Ravi**, her son (the caretaker), manages the plan: adds medicines, keeps track of refills, books the cardiologist.
- **Meera**, her daughter (family), lives in another city. She wants to know Asha is OK, chip in with questions for
  the doctor, and suggest things without stepping on anyone's toes.

Each of them signs in with their own private access code and sees the app shaped around what they can and should do:

| | Asha (elder) | Ravi (caretaker) | Meera (family) |
|---|---|---|---|
| See the plan, confirm doses, add questions for the doctor | ✔ | ✔ | ✔ |
| Add, change or stop medicines; manage appointments | ✔ | ✔ | suggests only |
| Invite people, give new codes, settings, export or delete data | ✔ | ✔ | |
| Mood check-in, "I need help", answer medicine changes, "approve changes first" | ✔ | | |

## A day with CareCircle

- **7:45 AM.** Asha gets an email: *"It's time for your 8:00 AM medicine (1)."* She opens the app, sees which one,
  and taps **Taken**. The pill count goes down by one.
- **11:00 AM.** Ravi adds an appointment with Dr Iyer for Tuesday. Everyone gets an email straight away, and again
  24 hours and 2 hours before. Meera adds her question: *"Is the morning dizziness from Amlodipine?"*
- **2:00 PM.** Ravi changes a dose because the doctor said so. Asha is told, sees it on her screen and taps **OK**.
  If she disagreed, Ravi would hear about it at once. If Asha wants, she can switch on *"approve changes first"*,
  and then nothing about her medicines changes without her.
- **8:00 PM.** Ravi and Meera get a short summary of the day.
- **8:30 PM.** Asha's evening tablet hasn't been confirmed. Nobody assumes she missed it: it's marked
  *"not confirmed"* and the family gets a gentle email, so someone can just ask.

## The smart parts, and the logic behind them

**The care assistant ("What needs attention now").** The first thing everyone sees is a short list worked out
from *who is looking*, *what time it is*, and *the care data*. Every line says why it's there:

| Situation | Asha sees | Ravi (caretaker) and Meera (family) see |
|---|---|---|
| A dose is 30+ minutes late | "You haven't marked your 8:00 AM Metformin yet. If you've taken it, tap Taken below." | "Asha hasn't confirmed 1 dose today: Metformin (8:00 AM). Give them a call..." |
| A dose is due now | "It's time for Metformin 500 mg (with food)." | "Asha's Metformin 500 mg is due at 8:00 AM." |
| Stock is getting low | "Your Metformin is running low (6 left). Ask your family to order more." | Ravi: "Metformin runs out in about 3 days (6 left, 2 a day). Order a refill." |
| Appointment in the next 2 days | "You see Dr Iyer tomorrow at 11:00 AM." | "Asha sees Dr Iyer tomorrow at 11:00 AM. No questions added yet: add anything you want the doctor to answer." |
| The help button was pressed | | Goes straight to the top, in red |
| Mood low three check-ins running | | "Worth a call, or a mention to the doctor." |
| A risky medicine combination | | Ravi: "Check with the pharmacist: Warfarin + NSAID painkillers (like ibuprofen) can raise bleeding risk." |

It's rule-based on purpose: it works offline, gives the same answer every time, and never guesses about medicine.
The rules live in [`assistant.py`](assistant.py) and each one has a test in
[`tests/test_assistant.py`](tests/test_assistant.py).

**Ask about the care plan (Google Gemini).** For open questions like *"Which dose is being missed most, and what
routine might help?"*, the Insights tab asks Gemini. It only sends what's needed (medicines, times and adherence
numbers), strips out names, phone numbers and ID numbers first, refuses emergencies ("call 112"), flags any answer
that talks about changing a dose, and falls back to another model when one is busy. It's on for the sample family
and opt-in for real ones.

**Medicine safety prompts.** When someone adds a medicine, CareCircle checks it against the rest of the list. It
knows common Indian and international brand names (Ecosprin is aspirin, Combiflam is ibuprofen plus paracetamol),
spots hidden double-dosing, and says plainly when it doesn't recognise a name, so a missing warning is never
mistaken for "safe".

**Insights.** Adherence over the week, streaks, which medicine or time of day is hardest, and the mood trend.

## Keeping the elder at the centre

- Asha owns her circle. Family suggestions wait for her (or a caretaker) to approve them.
- When anyone else changes her medicines, she's told and can say **OK** or **I don't agree**. Only she can switch on
  *"approve changes first"*.
- Large text is on by default for her. Her screens are available in Hindi, can read her medicines aloud, and can
  print a large-print list for the fridge.
- An **I need help** button emails everyone at once (and always shows the emergency number).
- Every tap can be undone, and every change is in the *who did what* log.

## Try it in two minutes

```bash
pip install -r requirements.txt
streamlit run app.py
```

1. On the sign-in screen, open **Try the sample family** and click **Load sample family**.
2. Copy the three access codes it shows (they're shown once).
3. Sign in as **Asha** (elder), then as **Ravi** (caretaker), and compare what each sees.
4. On Ravi's **Emails** tab, use *"Simulate a time of day"* to watch reminders being generated.

Without email set up, emails are simulated and appear on the Emails tab. To send real ones, copy `.env.example` to
`.env` and add a Gmail address with an
[App Password](https://myaccount.google.com/apppasswords). Add a `GEMINI_API_KEY` for the AI assistant.

## Security and privacy, briefly

Health information deserves care, so CareCircle treats it that way:

- Access codes are 12 characters and stored only as salted hashes. Wrong guesses are rate-limited, and five in a
  row pauses that code and tells its owner.
- Caretakers can be required to enter an emailed code too (on by default in production). Sessions time out.
- Names, emails, medicine details and notes are encrypted in the database. Backups are encrypted too.
- Every action is permission-checked on the server, not just hidden in the UI. Nobody can reach another family's data.
- Emails say *"it's time for your medicine"* by default, not which medicine.

More detail in [docs/SECURITY.md](docs/SECURITY.md) and [docs/PRIVACY.md](docs/PRIVACY.md).

## Assumptions and honest limits

- **CareCircle records what people tap.** It can't see a pill being swallowed, so it says "confirmed taken by
  Asha" rather than pretending to know.
- **The medicine check is a prompt, not a database.** It covers a short list of well-known combinations and hasn't
  been reviewed by a pharmacist. No warning doesn't mean safe.
- **Email is the reminder channel.** It's reliable and free, but it isn't a phone call. For emergencies, call 112.
- **The AI can be wrong.** Every answer is labelled, and nothing it says changes the care plan.
- **The Hindi text** was written for this project and should be checked by a native speaker.
- **Hosting:** the database is SQLite, which needs a persistent disk. Streamlit Community Cloud wipes files on
  restart, so it's fine for the demo but not for real families. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## For developers

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest                     # 177 tests, plus 7 that drive the app in real Streamlit
python manage.py --help    # backups, health check, key rotation, admin tasks
```

| File | What it does |
|---|---|
| `app.py` | The Streamlit screens. Renders only; every action goes through `services.py`. |
| `assistant.py` | The "what needs attention now" rules. |
| `services.py` | Every user action, with its permission check and input validation. |
| `auth.py`, `authz.py` | Sign-in, sessions, and who may do what. |
| `notifier.py`, `worker.py` | Email reminders, retries, and the background worker. |
| `insights.py` | Daily summaries, weekly insights and the Gemini assistant. |
| `interactions.py` | Medicine safety prompts (rules in `data/interactions.json`). |
| `db.py`, `crypto.py` | SQLite with migrations, and field encryption. |
| `maintenance.py`, `manage.py`, `health.py` | Backups, retention, admin commands, health checks. |
| `i18n.py` | English and Hindi text for the elder's screens. |

Settings live in `.env` locally or Streamlit secrets in the cloud (`APP_ENV` = development, demo or production).
[REMEDIATION.md](REMEDIATION.md) lists the security review this project went through and what changed for each point.


## Real-World Functionality

CareCircle is not limited to predefined sample data. Users can create and manage their own care circles and use the application with real information.

### What users can actually do

- **Create their own care circle:** Register a new family profile and generate individual access codes for elderly members, caretakers and family members.
- **Enter personal care information:** Add, edit and manage medicines, dosages, schedules, stock levels and doctor appointments.
- **Receive real emails:** Connect a Gmail account through SMTP to receive test emails, sign-in codes, medication reminders, missed-dose alerts and appointment notifications.
- **Collaborate with family:** Allow multiple authorised members to access the same care profile with role-based permissions.
- **Track medication adherence:** Record doses as taken, monitor unconfirmed doses and view adherence history.
- **Manage appointments:** Schedule doctor visits and share questions with other authorised members.
- **Protect personal information:** Store application data in SQLite, encrypt sensitive fields and restrict access through server-side permissions.
- **Manage data:** Back up, export and delete care-circle information using the application's management features.

### Sample mode and real usage

CareCircle includes a sample family for quick demonstrations. Users can also create their own care circles and enter their own information.

Email delivery requires SMTP configuration. When SMTP is not configured, the application simulates emails for demonstration purposes.

The application uses a background reminder worker for scheduled notifications. A deployment intended for continuous use must run this worker and provide persistent database storage.

**CareCircle is designed to support real care coordination, while remaining a support tool rather than a replacement for professional medical advice.**