"""CareCircle - Elderly Care & Medication Management (Streamlit).

This file only renders. Every read and change goes through auth (who is signed in) and services (what they
may do), which check permissions independently of what is shown here. All user-entered text is escaped
before it is rendered as Markdown or HTML.

Accessibility: status is always given in words as well as colour or icons, icons in HTML blocks are hidden from
screen readers, charts have a table alternative, keyboard focus is clearly outlined, the page language follows
the reader's choice, and a large-text mode and read-aloud button are on the Today screen.
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Sequence
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, TypeVar

import pandas as pd
import streamlit as st

import assistant
import auth
import authz
import clock
import config
import crypto
import db
import health
import i18n
import insights
import interactions
import logutil
import maintenance
import notifier
import seed
import services
from authz import Forbidden
from services import Invalid
from typedefs import Actor, Notice, Row

T = TypeVar("T")

st.set_page_config(page_title="CareCircle", page_icon="💊", layout="wide")


# ------------------------------------------------------------------ start-up
@st.cache_resource
def boot() -> list[str]:
    """Once per server process: logging, configuration check, database upgrade and the reminder thread.
    Returns configuration errors that must stop a production server from starting."""
    logutil.setup()
    errors, _ = config.validate()
    if errors and config.is_production():
        return errors
    db.init_db()
    if config.scheduler_mode() == "thread":
        notifier.start_scheduler()
    return []


KEY_HELP = """**How to fix it**
- **You added or changed `DATA_KEY`:** set the old value as `DATA_KEY_PREVIOUS` (comma-separated if there are several),
  restart, then run `python manage.py rotate-key` to move everything to the new key. In development, a database first
  created without `DATA_KEY` is read with its `.devkey` file automatically, as long as that file is still next to it.
- **You moved or copied the database:** copy its `.devkey` file with it, or set `DATA_KEY` to the key it was made with.
- **It's only sample or test data:** stop the app, delete `carecircle.db`, `carecircle.db-wal`, `carecircle.db-shm`
  and `carecircle.db.devkey`, then start again.

`python manage.py key-info` shows which key the database expects and which keys are available. Without the original
key the encrypted data can't be recovered (that is the point of encrypting it). A copy of the database from before
the upgrade can be upgraded again."""

try:
    BOOT_ERRORS = boot()
except crypto.DecryptionError as key_error:
    st.error("CareCircle can't read its database with the encryption key it was given.")
    st.write(str(key_error))
    st.markdown(KEY_HELP)
    st.stop()
if BOOT_ERRORS:
    st.error("CareCircle can't start because the server configuration is unsafe for real data:")
    for problem in BOOT_ERRORS:
        st.write("- " + problem)
    st.caption("Fix these in your environment variables or Streamlit secrets, then restart. "
               "`python manage.py check-config` lists them too.")
    st.stop()

ICON = {"taken": "✅", "unconfirmed": "❔", "missed": "❌", "pending": "⏰", "skipped": "⏭️"}
LEVEL_ICON = {"urgent": "🔴", "todo": "🟠", "info": "🔵", "good": "🟢"}
MOOD_FACES = ("😞", "🙁", "😐", "🙂", "😄")
STOCK_BAR_FULL = 30   # doses in stock shown as a full progress bar
MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$])")
_MD_ENTITIES = {ord(c): f"&#{ord(c)};" for c in "[]()*_`~$\\"}


# ------------------------------------------------------------------ small helpers
def esc(text: object) -> str:
    """Make user text safe inside st.markdown/st.write: no links, formatting, HTML or LaTeX from user input."""
    return MD_SPECIAL.sub(r"\\\1", str(text or ""))


def h(text: object) -> str:
    """Escape user text for HTML blocks, including Markdown characters so nothing becomes a link or formatting."""
    return html.escape(str(text or "")).translate(_MD_ENTITIES)


def fmt_t(t: str) -> str:
    """'20:00' -> '8:00 PM'."""
    return datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")


def fmt_times(times: str | Sequence[str]) -> str:
    """'08:00,20:00' (or a list of times) -> '8:00 AM, 8:00 PM'."""
    items = times.split(",") if isinstance(times, str) else times
    return ", ".join(fmt_t(t) for t in items)


def time_options() -> list[str]:
    """Every half hour from 05:00 to 23:30, for time pickers."""
    return [f"{hh:02d}:{mm:02d}" for hh in range(5, 24) for mm in (0, 30)]


def mask_email(e: str | None) -> str:
    """'asha@mail.com' -> 'a•••@mail.com', so an email can be recognised without being shown."""
    if not e or "@" not in e:
        return ""
    user, dom = e.split("@", 1)
    return f"{user[:1]}{'•' * max(1, len(user) - 1)}@{dom}"


def elder_first(a: Actor) -> str:
    """The elder's first name, escaped for Markdown."""
    return esc((a["elder_name"] or "the elder").split()[0])


def clients() -> list[str]:
    """Rate-limit buckets for this browser session (and IP address, when Streamlit knows it)."""
    bs = st.session_state.setdefault("browser_session", crypto.token())
    ip = None
    try:
        ip = st.context.ip_address  # Streamlit >= 1.45; can be spoofed or shared, so only a soft signal
    except Exception:
        try:
            ip = (st.context.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or None
        except Exception:
            ip = None
    return auth.client_buckets(ip, bs)


def styles(big: bool) -> None:
    """Page styles: large text mode, dose colours, and a clear outline on whatever has keyboard focus."""
    st.markdown(f"""
<style>
html, body, [class*="css"] {{ font-size: {'22px' if big else '16px'}; }}
.stButton > button {{ min-height: {'3.6rem' if big else '2.4rem'}; font-size: {'1.3rem' if big else '1rem'};
  border-radius: 12px; }}
button:focus-visible, a:focus-visible, input:focus-visible, [role="tab"]:focus-visible, [tabindex]:focus-visible {{
  outline: 3px solid #1a73e8 !important; outline-offset: 2px; }}
.dose {{ padding: .8rem 1rem; border-radius: 14px; margin-bottom: .4rem; border: 1px solid rgba(128,128,128,.3); }}
.dose .status {{ display: block; margin-top: .2rem; }}
.dose.taken {{ background: rgba(46,160,67,.15); }}
.dose.missed, .dose.unconfirmed {{ background: rgba(218,54,51,.12); }}
.dose.pending {{ background: rgba(128,128,128,.08); }}
.dose.skipped {{ background: rgba(210,153,34,.15); }}
.emergency {{ font-size: 1.4em; font-weight: bold; padding: .6rem 1rem; border-radius: 12px; border: 2px solid #da3633; }}
</style>""", unsafe_allow_html=True)


def set_page_language(lang: str) -> None:
    """Tell browsers and screen readers which language the page is in, so Hindi is read with a Hindi voice."""
    import streamlit.components.v1 as components
    code = json.dumps("hi" if lang == "hi" else "en")
    components.html(f"<script>try {{ window.parent.document.documentElement.lang = {code}; }} catch (e) {{}}</script>",
                    height=0)


def show_error(e: BaseException) -> None:
    """Show a friendly message: the reason for expected problems, a reference number for unexpected ones."""
    if type(e).__name__ in ("RerunException", "StopException"):  # Streamlit control flow, never swallow
        raise e
    if isinstance(e, (Invalid, Forbidden, db.NotFound)):
        msg = str(e) if not isinstance(e, db.NotFound) else "That item no longer exists. Refresh the page."
        st.error(msg)
    else:
        st.error(f"Something went wrong. Please try again. (Reference {logutil.capture(e, 'ui')})")


def act(fn: Callable[..., T], *args: Any, success: str | None = None, rerun: bool = True, **kw: Any) -> T | None:
    """Run a service call and show friendly errors. Returns the result, or None if it failed."""
    try:
        res = fn(*args, **kw)
    except Exception as e:  # st.rerun/st.stop are not Exceptions, so they pass through
        show_error(e)
        return None
    if success:
        st.session_state["flash"] = success
    if rerun:
        st.rerun()
    return res


def flash() -> None:
    """Show (once) the success message left by the previous action."""
    msg = st.session_state.pop("flash", None)
    if msg:
        st.success(msg)


def as_table(data: dict[str, dict[str, Any]], label: str = "Show these numbers as a table") -> None:
    """A table version of a chart, for screen readers and anyone who prefers numbers."""
    with st.expander(label):
        st.table(pd.DataFrame(data))


# ------------------------------------------------------------------ sign-in
def start_session(r: auth.Result) -> None:
    """Act on a sign-in result: open the session, ask which circle, or show why it failed."""
    if r.status in ("ok", "rotate", "mfa"):
        st.session_state["token"] = r.token
        st.session_state["dev_code"] = r.dev_code
        for k in ("email_challenge", "pick"):
            st.session_state.pop(k, None)
        st.rerun()
    elif r.status == "choose":
        st.session_state["pick"] = {"token": r.pick_token, "choices": r.choices}
        st.rerun()
    else:
        st.error(r.message)


def show_codes(codes: dict[str, str], title: str) -> None:
    """Show newly issued access codes, once, with a reminder that they can't be shown again."""
    st.success(title)
    st.code("\n".join(f"{k}: {v}" for k, v in codes.items()), language=None)
    st.warning("Write these down or share each one privately now (for example, read it out on a call). "
               "CareCircle doesn't keep a readable copy, so it can't show them again. The elder or a caretaker can "
               "give anyone a new code from the Circle tab.")


def _pick_circle(pick: dict[str, Any]) -> None:
    """An email that belongs to several circles: ask which one to open."""
    st.subheader("Which care circle?")
    choice = st.radio("Your email belongs to more than one circle", pick["choices"],
                      format_func=lambda c: f"{c['family_name']} ({c['role']})", key="pick_choice")
    c1, c2 = st.columns(2)
    if c1.button("Open", type="primary", key="pick_open"):
        start_session(auth.email_signin_pick(pick["token"], choice["id"]))
    if c2.button("Start again", key="pick_cancel"):
        st.session_state.pop("pick", None)
        st.rerun()


def _code_tab() -> None:
    """Sign in with an access code."""
    with st.form("login"):
        code = st.text_input("Your access code", type="password", max_chars=20, placeholder="XXXX-XXXX-XXXX",
                             key="login_code", help="Capitals and dashes don't matter. Tap the eye icon to check what you typed.")
        ok = st.form_submit_button("Open", type="primary")
    st.caption("Forgot it? Use **Email me a code**, or ask the elder or a caretaker for a new one.")
    if ok:
        start_session(auth.login_with_code(code, clients()))


def _email_tab() -> None:
    """Sign in with a one-time code sent to an email on file."""
    st.write("If your email is on file in a care circle, we can email you a one-time code to sign in.")
    challenge = st.session_state.get("email_challenge")
    if not challenge:
        with st.form("email_start"):
            em = st.text_input("Your email address", key="email_addr")
            go = st.form_submit_button("Email me a code", type="primary")
        if go:
            r = auth.email_signin_start(em, clients())
            if r.status == "throttled":
                st.error(r.message)
            else:
                st.session_state["email_challenge"] = {"token": r.token, "dev": r.dev_code}
                st.rerun()
    else:
        st.info(auth.EMAIL_SENT)
        if challenge.get("dev"):
            st.warning(f"Development mode (email isn't set up): your code is **{challenge['dev']}**")
        with st.form("email_verify"):
            otp = st.text_input("6-digit code", max_chars=6, key="email_otp")
            go = st.form_submit_button("Sign in", type="primary")
        if go:
            start_session(auth.email_signin_verify(challenge["token"], otp, clients()))
        if st.button("Use a different email", key="email_restart"):
            st.session_state.pop("email_challenge", None)
            st.rerun()
    st.caption("Caretakers sign in with their access code plus an emailed code, so this option isn't available to them.")


def _create_tab() -> None:
    """Start a new care circle and show everyone's first access code."""
    with st.form("create", clear_on_submit=True):
        fam = st.text_input("Family / circle name")
        elder = st.text_input("Elderly person's name")
        elder_email = st.text_input("Their email (for reminders)")
        st.markdown("**Optionally invite two people now** (you can add more later)")
        c1, c2 = st.columns(2)
        cn, ce = c1.text_input("Caretaker name"), c2.text_input("Caretaker email")
        fn, fe = c1.text_input("Family member name"), c2.text_input("Family member email")
        ok = st.form_submit_button("Create circle", type="primary")
    if ok:
        try:
            _, codes = services.create_circle(fam, elder, elder_email, [(cn, ce, "caretaker"), (fn, fe, "family")],
                                              clients=clients())
            show_codes(codes, "Circle created. Each person's access code:")
        except Exception as e:
            show_error(e)


def _demo_tab() -> None:
    """Create the made-up sample family (development and demo only)."""
    st.write("Creates a made-up family with a week of history so you can explore. Not for real people.")
    if st.button("✨ Load sample family", key="demo_btn"):
        try:
            show_codes(seed.seed_demo(clients=clients()), "Sample family created. Access codes:")
        except Exception as e:
            show_error(e)


def login_screen() -> None:
    """The signed-out screen: sign in with a code or by email, start a circle, or try the sample family."""
    st.title("💊 CareCircle")
    st.caption("One shared, private care profile for an elderly person, their caretakers and their family.")
    notice = st.session_state.pop("notice", None)
    if notice:
        st.info(notice)
    if st.session_state.get("pick"):
        _pick_circle(st.session_state["pick"])
        return
    sections = [("🔑 Sign in", _code_tab), ("✉️ Email me a code", _email_tab), ("➕ Create a care circle", _create_tab)]
    if config.demo_features():
        sections.append(("✨ Try the sample family", _demo_tab))
    for tab, (_, render) in zip(st.tabs([name for name, _ in sections]), sections):
        with tab:
            render()


def mfa_screen(a: Actor) -> None:
    """Second sign-in step: the 6-digit code that was emailed."""
    st.title("💊 CareCircle")
    st.subheader("Two-step sign-in")
    st.write(f"Hello {esc(a['name'])}. We emailed a 6-digit code to **{esc(mask_email(a['email']))}**. It expires in 10 minutes.")
    dev = st.session_state.get("dev_code")
    if dev:
        st.warning(f"Development mode (email isn't set up): your code is **{dev}**")
    with st.form("mfa"):
        code = st.text_input("6-digit code", max_chars=6, key="mfa_code")
        ok = st.form_submit_button("Continue", type="primary")
    if ok:
        r = auth.verify_mfa(st.session_state["token"], code)
        if r.status in ("ok", "rotate"):
            st.session_state.pop("dev_code", None)
            st.rerun()
        st.error(r.message)
        if "Start again" in r.message:
            st.session_state.pop("token", None)
    c1, c2 = st.columns(2)
    if c1.button("Send a new code", key="mfa_resend"):
        r = auth.resend_mfa(st.session_state["token"])
        st.session_state["dev_code"] = r.dev_code
        (st.success if r.status == "sent" else st.error)(r.message)
    if c2.button("Cancel", key="mfa_cancel"):
        auth.sign_out(st.session_state.pop("token", None))
        st.rerun()


def new_code_screen(a: Actor, first_time: bool) -> None:
    """Take a new access code: required after an old 6-character code, optional from Account security."""
    st.title("💊 CareCircle")
    if "new_code" not in st.session_state:
        if first_time:
            st.subheader("Time for a new, stronger access code")
            st.write("CareCircle now uses longer codes that are much harder to guess. "
                     "Your old 6-character code will stop working.")
        if st.button("Show my new code", type="primary", key="rotate_go"):
            code = act(auth.rotate_own_code, st.session_state["token"], rerun=False)
            if code:
                st.session_state["new_code"] = code
                st.rerun()
        if not first_time and st.button("Cancel", key="rotate_cancel"):
            st.session_state.pop("rotating", None)
            st.rerun()
        return
    st.subheader("Your new access code")
    st.code(st.session_state["new_code"], language=None)
    st.warning("Write it down now. CareCircle can't show it again. Your old code and any other signed-in devices no longer work.")
    if st.checkbox("I've written it down", key="rotate_ack") and st.button("Continue", type="primary", key="rotate_done"):
        st.session_state.pop("new_code", None)
        st.session_state.pop("rotating", None)
        st.rerun()


# ------------------------------------------------------------------ Today
def read_aloud_button(text: str, lang: str) -> None:
    """A button that reads today's medicines aloud with the browser's own speech (nothing is sent anywhere)."""
    import streamlit.components.v1 as components
    payload = json.dumps(text).replace("</", "<\\/")
    label = h(i18n.t("read_aloud", lang))
    unsupported = json.dumps(i18n.t("read_aloud_unsupported", lang)).replace("</", "<\\/")
    components.html(f"""
<button id="speak" type="button" style="font-size:20px;padding:10px 18px;border-radius:12px;cursor:pointer">
<span aria-hidden="true">🔊</span> {label}</button>
<script>
var b = document.getElementById("speak");
b.onclick = function () {{
  if (!window.speechSynthesis) {{ b.textContent = {unsupported}; b.disabled = true; return; }}
  var u = new SpeechSynthesisUtterance({payload});
  u.lang = "{'hi-IN' if lang == 'hi' else 'en-IN'}"; u.rate = 0.9;
  window.speechSynthesis.cancel(); window.speechSynthesis.speak(u);
}};
</script>""", height=70)


def printable_schedule(a: Actor, meds: list[Row]) -> str:
    """A large-print HTML medicine list to print and keep by the bed or take to the doctor."""
    rows = "".join(
        f"<tr><td>{h(fmt_times(m['times']))}</td><td><b>{h(m['name'])}</b> {h(m['dosage'])}</td>"
        f"<td>{h(m['instructions'])}</td></tr>" for m in meds)
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Medicines</title>
<style>body{{font-family:Arial,sans-serif;font-size:22px;margin:24px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:2px solid #333;padding:12px;text-align:left}}caption{{text-align:left;font-weight:bold;padding:8px 0}}</style>
</head><body>
<h1>Medicines for {h(a['elder_name'])}</h1>
<p>Printed {h(clock.now().strftime('%d %B %Y'))}. Check with the doctor before changing anything.</p>
<table><caption>Daily medicines</caption>
<tr><th scope="col">When</th><th scope="col">Medicine</th><th scope="col">How</th></tr>{rows}</table>
<p>Emergency: call {h(config.get('EMERGENCY_NUMBER', '112'))}</p></body></html>"""


def help_box(a: Actor, lang: str) -> None:
    """The elder's 'I need help' button, with the emergency number and what happened after pressing it."""
    num = config.get("EMERGENCY_NUMBER", "112")
    st.markdown(f"<div class='emergency' role='note'><span aria-hidden='true'>📞</span> "
                f"{h(i18n.t('emergency', lang, num=num))}</div>", unsafe_allow_html=True)
    c1, c2 = st.columns([2, 3])
    if c1.button(i18n.t("help_btn", lang), type="primary", key="help_btn"):
        st.session_state["help_result"] = act(services.request_help, a, rerun=False)
    c2.caption(i18n.t("help_explain", lang, num=num))
    res = st.session_state.get("help_result")
    if not res:
        return
    if not res["recipients"]:
        st.error(i18n.t("help_none", lang, num=num))
    elif res["simulated"]:
        st.warning(i18n.t("help_sim", lang, num=num))
    else:
        st.success(i18n.t("help_sent", lang, n=res["delivered"], total=res["recipients"], num=num))
    if st.button(i18n.t("help_mistake", lang), key="help_cancel"):
        act(services.cancel_help, a, rerun=False)
        st.session_state.pop("help_result", None)
        st.session_state["flash"] = i18n.t("help_cancelled", lang)
        st.rerun()


def assistant_card(a: Actor, lang: str) -> None:
    """What needs attention now, for this person, at this time. Each line says how urgent it is, and why it's there."""
    items = assistant.suggestions(a)
    with st.container(border=True):
        st.markdown(f"#### {i18n.t('ast_title', lang)}")
        if not items:
            st.write(i18n.t("ast_none", lang))
        for it in items:
            st.markdown(f"{LEVEL_ICON[it['level']]} **{i18n.t('lvl_' + it['level'], lang)}:** {esc(it['text'])}")
            st.caption(f"{i18n.t('ast_why', lang)}: {esc(it['why'])}")


def review_box(a: Actor, lang: str) -> None:
    """Changes other people made (or want to make) to the elder's medicines, for the elder to answer."""
    items = services.changes_for_elder(a)
    if not items:
        return
    st.markdown(f"### {i18n.t('changes_title', lang)}")
    for c in items:
        when = clock.from_utc(c["created_at"])
        text = f"**{esc(c['by_name'])}** {esc(services.describe_change(c))}  \n{when}"
        if c["status"] == "pending":
            st.warning(text + "  \n" + i18n.t("change_pending_note", lang))
            ok_label, no_label, ok, no = i18n.t("change_approve", lang), i18n.t("change_decline", lang), "approve", "decline"
        else:
            (st.error if c["status"] == "disputed" else st.info)(
                text + ("  \n" + i18n.t("change_disputed", lang) if c["status"] == "disputed" else ""))
            ok_label, no_label, ok, no = i18n.t("change_ok", lang), i18n.t("change_disagree", lang), "ok", "disagree"
        c1, c2, c3 = st.columns([1.4, 1.6, 4])
        note = c3.text_input(i18n.t("change_note", lang), key=f"chn{c['id']}", label_visibility="collapsed",
                             placeholder=i18n.t("change_note", lang))
        if c1.button(ok_label, key=f"chok{c['id']}", type="primary"):
            act(services.review_change, a, c["id"], ok, note)
        if c["status"] != "disputed" and c2.button(no_label, key=f"chno{c['id']}"):
            act(services.review_change, a, c["id"], no, note)


def _mood_checkin(a: Actor, lang: str, today: date) -> None:
    """The elder taps a face; everyone else sees today's answer."""
    ck = {c["day"]: c for c in db.checkins(a["family_id"], 1)}.get(today.isoformat())
    if authz.can(a, "checkin.set"):
        st.markdown(f"**{i18n.t('how_feeling', lang)}**")
        cols = st.columns(len(MOOD_FACES))
        for i, face in enumerate(MOOD_FACES, start=1):
            if cols[i - 1].button(f"{face}\n{i18n.t(f'mood_{i}', lang)}", key=f"mood{i}",
                                  type="primary" if ck and ck["mood"] == i else "secondary"):
                act(services.check_in, a, i)
    elif ck:
        st.info(i18n.t("mood_today", lang, mood=i18n.t(f"mood_{ck['mood']}", lang)))
    else:
        st.caption(i18n.t("no_mood", lang))


def _dose_row(a: Actor, lang: str, d: Row, now_t: str) -> None:
    """One dose: what, when, its status in words, and the buttons to mark it."""
    c1, c2, c3 = st.columns([5, 1.6, 1.6])
    late = d["status"] == "pending" and d["scheduled_time"] < now_t
    status_text = i18n.t(f"st_{d['status']}", lang, who=d["marked_by"] or "?")
    c1.markdown(
        f"<div class='dose {h(d['status'])}'><span aria-hidden='true'>{ICON[d['status']]}</span> "
        f"<b>{h(fmt_t(d['scheduled_time']))}</b> - {h(d['name'])} {h(d['dosage'])}"
        f"{' <i>(' + h(d['instructions']) + ')</i>' if d['instructions'] else ''}"
        f"{' <b>- ' + h(i18n.t('due_now', lang)) + '</b>' if late else ''}"
        f"<span class='status'>{h(status_text)}</span></div>",
        unsafe_allow_html=True)
    if d["status"] in ("pending", "unconfirmed"):
        if c2.button(i18n.t("taken", lang), key=f"t{d['id']}", type="primary"):
            act(services.mark_dose, a, d["id"], "taken")
        label = i18n.t("not_taken" if d["status"] == "unconfirmed" else "skip", lang)
        if c3.button(label, key=f"s{d['id']}"):
            act(services.mark_dose, a, d["id"], "missed" if d["status"] == "unconfirmed" else "skipped")
    elif c2.button(i18n.t("undo", lang), key=f"u{d['id']}"):
        act(services.mark_dose, a, d["id"], "pending")


def _today_tools(a: Actor, lang: str, doses: list[Row]) -> None:
    """Read today's medicines aloud, or print the full list."""
    c1, c2 = st.columns(2)
    with c1:
        spoken = " ".join(i18n.t("aloud_dose", lang, time=fmt_t(d["scheduled_time"]), name=d["name"], dosage=d["dosage"],
                                 instr=d["instructions"] or "") for d in doses) or i18n.t("aloud_none", lang)
        read_aloud_button(spoken, lang)
    with c2:
        st.download_button(i18n.t("print", lang), printable_schedule(a, db.meds(a["family_id"])), file_name="medicines.html",
                           mime="text/html", help=i18n.t("print_help", lang), key="print_btn")


def _next_appointment(a: Actor, lang: str) -> None:
    """The next doctor's appointment, if there is one."""
    nxt = db.appts(a["family_id"])[:1]
    if not nxt:
        return
    ap = nxt[0]
    st.markdown(f"### {i18n.t('next_appt', lang)}")
    st.info(f"**{esc(ap['doctor'])}** {('(' + esc(ap['specialty']) + ')') if ap['specialty'] else ''}  \n"
            f"{datetime.fromisoformat(ap['when_at']).strftime('%A %d %B, %I:%M %p')} "
            f"{('· ' + esc(ap['location'])) if ap['location'] else ''}")


def tab_today(a: Actor) -> None:
    """The home screen: help, what needs attention, changes to answer, mood, today's doses and the next appointment."""
    lang = a["lang"]
    elder_view = a["role"] == "elder"
    st.subheader(i18n.t("hello", lang, name=esc(a["name"].split()[0])) if elder_view
                 else i18n.t("today_for", lang, name=esc(a["elder_name"])))
    if authz.can(a, "help.request"):
        help_box(a, lang)
    assistant_card(a, lang)
    today = clock.today()
    now_t = clock.now().strftime("%H:%M")
    if authz.can(a, "med.review"):
        review_box(a, lang)
    _mood_checkin(a, lang, today)

    st.markdown(f"### {i18n.t('medicines', lang)}")
    doses = db.doses_for_day(a["family_id"], today.isoformat())
    if not doses:
        st.info(i18n.t("none_today", lang))
    for d in doses:
        _dose_row(a, lang, d, now_t)
    if doses:
        st.caption(i18n.t("honesty", lang))
    _today_tools(a, lang, doses)
    _next_appointment(a, lang)


# ------------------------------------------------------------------ Medications
def safety_note() -> None:
    """What the medicine check covers, and that it isn't a full interaction check."""
    cov = interactions.coverage()
    st.caption(f"⚠️ {interactions.DISCLAIMER} The list covers {cov['ingredients']} ingredients"
               f"{'' if cov['reviewed_by'] else ' and has not yet been reviewed by a pharmacist'}.")


def show_check(check: interactions.CheckResult) -> None:
    """Warnings from the medicine check, and anything it couldn't recognise."""
    for w in check.warnings:
        st.warning("⚠️ " + esc(w) + " Please check with a doctor or pharmacist.")
    for n in check.unrecognised:
        s = check.suggestions.get(n)
        st.info(f"CareCircle's basic check doesn't recognise **{esc(n)}**, so it could not check it against the other "
                f"medicines.{' Did you mean ' + esc(s.title()) + '?' if s else ''} Ask the pharmacist.")


def _proposed_meds(a: Actor, who: str, elder_decides: bool) -> None:
    """Medicines suggested by family, waiting for approval."""
    pend = db.meds(a["family_id"], ("proposed",))
    if not pend:
        return
    st.warning(f"{len(pend)} suggested medicine(s) waiting for approval")
    for p in pend:
        c1, c2, c3 = st.columns([6, 1.3, 1.3])
        c1.write(f"**{esc(p['name'])} {esc(p['dosage'])}** at {fmt_times(p['times'])} "
                 f"- suggested by {esc(p['added_by'])}")
        if authz.can(a, "med.approve") and (a["role"] == "elder" or not elder_decides):
            if c2.button("Approve", key=f"ap{p['id']}", type="primary"):
                act(services.approve_medicine, a, p["id"], success=f"Approved {p['name']}.")
            if c3.button("Decline", key=f"de{p['id']}"):
                act(services.decline_medicine, a, p["id"])
        elif elder_decides:
            c2.caption(f"Waiting for {who}")
    st.divider()


def _change_banners(a: Actor, who: str) -> None:
    """Changes waiting for the elder's OK, and changes the elder disagreed with."""
    fid = a["family_id"]
    waiting = [c for c in db.changes(fid, ("pending",)) if c["action"] != "add"]
    disputed = db.changes(fid, ("disputed",))
    if not waiting and not disputed:
        return
    for c in waiting:
        st.warning(f"⏳ Waiting for {who}'s OK: **{esc(c['by_name'])}** {esc(services.describe_change(c))}")
    for c in disputed:
        st.error(f"✋ {who} doesn't agree: **{esc(c['by_name'])}** {esc(services.describe_change(c))}"
                 + (f' - "{esc(c["note"])}"' if c["note"] else "") + ". Please talk it through.")
    if a["role"] == "elder":
        st.caption("Answer these on the Today tab.")
    st.divider()


def _med_edit_form(a: Actor, med: Row, who: str, elder_decides: bool) -> None:
    """Change a medicine's dosage, times, instructions or prescriber."""
    with st.form(f"edit{med['id']}"):
        c1, c2 = st.columns(2)
        dosage = c1.text_input("Dosage", med["dosage"])
        prescriber = c2.text_input("Prescribed by", med.get("prescriber") or "")
        times = st.multiselect("Times", sorted(set(time_options()) | set(med["times"].split(","))),
                               default=med["times"].split(","), format_func=fmt_t)
        instr = st.text_input("Instructions", med["instructions"])
        st.caption("Changing the dosage or times clears the 'checked' mark, so someone checks again.")
        if elder_decides and a["role"] != "elder":
            st.caption(f"{who} approves changes first: yours will be sent to them.")
        if st.form_submit_button("Save changes", type="primary"):
            res = act(services.edit_medicine, a, med["id"], dosage, times, instr, prescriber, rerun=False)
            if res:
                st.session_state["flash"] = (f"Sent to {who} for approval." if res == "pending" else
                                             "Saved." + ("" if a["role"] == "elder" else f" {who} has been told."))
                st.rerun()


def _med_stock_form(a: Actor, med: Row) -> None:
    """Correct the number of doses in stock, and show recent stock changes."""
    with st.form(f"stock{med['id']}"):
        c1, c2 = st.columns(2)
        ns = c1.number_input("Doses in stock now", 0, 9999, med["stock"] or 0)
        why = c2.text_input("Why (e.g. refilled, recount)")
        if st.form_submit_button("Update stock"):
            act(services.adjust_stock, a, med["id"], ns, why, success="Stock updated.")
    hist = db.stock_events(a["family_id"], med["id"], 5)
    if hist:
        st.caption("Recent stock changes: " + "; ".join(
            f"{e['delta']:+d} → {e['new_stock']} ({esc(e['reason'])}, {esc(e['who'])})" for e in hist))


def _med_card(a: Actor, med: Row, who: str, elder_decides: bool) -> None:
    """One active medicine: details, prescription check, and (for editors) edit, stock and stop."""
    verified = med.get("verified_by")
    badge = "✔ checked" if verified else "⚠ not checked against prescription"
    with st.expander(f"💊 {esc(med['name'])} {esc(med['dosage'])} - {fmt_times(med['times'])} · {badge}"):
        st.write(esc(med["instructions"]) or "_No special instructions_")
        if verified:
            st.caption(f"Checked against the prescription by {esc(verified)} on {esc((med.get('verified_at') or '')[:10])}"
                       f"{' (prescribed by ' + esc(med['prescriber']) + ')' if med.get('prescriber') else ''}.")
        else:
            st.caption("Nobody has confirmed this entry against the prescription or the pharmacy label yet.")
        if med["stock"] is not None:
            st.progress(min(1.0, med["stock"] / STOCK_BAR_FULL),
                        text=f"{med['stock']} doses left (refill alert at {med['refill_at']})")
        if not authz.can(a, "med.edit"):
            return
        if not verified and st.button("I've checked this against the prescription", key=f"vf{med['id']}"):
            act(services.verify_medicine, a, med["id"], success="Marked as checked.")
        _med_edit_form(a, med, who, elder_decides)
        _med_stock_form(a, med)
        confirm = st.checkbox(f"Yes, stop {esc(med['name'])}", key=f"stopok{med['id']}")
        if st.button("Stop this medicine", key=f"stop{med['id']}", disabled=not confirm):
            res = act(services.stop_medicine, a, med["id"], rerun=False)
            if res:
                st.session_state["flash"] = (f"Sent to {who} for approval." if res == "pending"
                                             else f"Stopped {med['name']}.")
                st.rerun()


def _new_med_form(a: Actor) -> None:
    """Step 1 of adding a medicine: fill in the details, then review them."""
    with st.form("addmed"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Medicine name (as on the label)")
        dosage = c2.text_input("Dosage (e.g. 500 mg, 1 tablet)")
        times = st.multiselect("Times each day", time_options(), default=["08:00"], format_func=fmt_t)
        instr = st.text_input("Instructions (e.g. after food)")
        c3, c4, c5, c6 = st.columns(4)
        start = c3.date_input("Start date", clock.today())
        end = c4.date_input("End date (optional)", value=None)
        stock = c5.number_input("Doses in stock (0 = don't track)", 0, 9999, 0)
        refill = c6.number_input("Refill alert at", 0, 999, 5)
        prescriber = st.text_input("Prescribed by (optional)")
        ok = st.form_submit_button("Review", type="primary")
    if ok:
        try:
            st.session_state["med_preview"] = services.preview_medicine(a, name, dosage, instr, times, start, end,
                                                                       stock, refill, prescriber)
            st.rerun()
        except Exception as e:
            show_error(e)


def _confirm_new_med(a: Actor, pending: dict[str, Any], can_add: bool) -> None:
    """Step 2 of adding a medicine: read it back with the safety check, then confirm or go back."""
    st.info(f"Please check: **{esc(pending['name'])} {esc(pending['dosage'])}** at "
            f"{fmt_times(pending['times'])} every day from {pending['start']}"
            f"{' to ' + pending['end'] if pending['end'] else ''}"
            f"{'. ' + esc(pending['instructions']) if pending['instructions'] else ''}.")
    show_check(pending["check"])
    c1, c2 = st.columns(2)
    if c1.button("Confirm and " + ("add" if can_add else "send for approval"), type="primary", key="med_confirm"):
        v = st.session_state.pop("med_preview")
        res = act(services.add_medicine, a, v["name"], v["dosage"], v["instructions"], v["times"], v["start"], v["end"],
                  v["stock"], v["refill_at"], v["prescriber"], rerun=False)
        if res:
            st.session_state["flash"] = ("Added." if res["status"] == "active" else "Sent for approval.") + (
                "" if a["role"] == "elder" else f" {esc((a['elder_name'] or '').split()[0])} has been told.")
            st.rerun()
    if c2.button("Go back and change it", key="med_back"):
        st.session_state.pop("med_preview", None)
        st.rerun()


def tab_meds(a: Actor) -> None:
    """The medicine list: suggestions to approve, changes waiting for the elder, each medicine, and adding one."""
    fid = a["family_id"]
    st.subheader("Medication schedule")
    who = elder_first(a)
    elder_decides = db.family_settings(fid)["elder_approval"]
    if elder_decides:
        st.info(f"{who} has asked to approve medicine changes made by others before they start.")
    _proposed_meds(a, who, elder_decides)
    _change_banners(a, who)

    active = db.meds(fid)
    for med in active:
        _med_card(a, med, who, elder_decides)
    if not active:
        st.info("No medicines yet. Add the first one below.")

    can_add = authz.can(a, "med.add")
    st.markdown("### ➕ " + ("Add a medicine" if can_add else "Suggest a medicine (the elder or a caretaker approves it)"))
    pending = st.session_state.get("med_preview")
    if pending:
        _confirm_new_med(a, pending, can_add)
    else:
        _new_med_form(a)
    safety_note()


# ------------------------------------------------------------------ Appointments
def email_note(res: Notice) -> str:
    """One sentence on who was emailed about an appointment change."""
    if not res["recipients"]:
        return " Nobody in the circle has an email address yet, so no email was sent."
    if not res.get("queued", res["recipients"]):
        return " (Already emailed about this appointment.)"
    out = f" Emailed {res['sent']} of {res['recipients']} people." if res.get("sent") else ""
    if res.get("simulated_n"):
        out += f" {res['simulated_n']} email(s) were only simulated (test addresses, or email isn't set up)."
    waiting = res.get("queued", 0) - res.get("delivered", 0)
    if waiting:
        out += f" {waiting} couldn't be sent yet; CareCircle will retry."
    return out


def _appt_card(a: Actor, ap: Row) -> None:
    """One upcoming appointment: editors can change notes or cancel; everyone can add questions."""
    when = datetime.fromisoformat(ap["when_at"])
    with st.expander(f"🩺 {when.strftime('%a %d %b, %I:%M %p')} - {esc(ap['doctor'])} "
                     f"{('(' + esc(ap['specialty']) + ')') if ap['specialty'] else ''}"):
        st.write(f"📍 {esc(ap['location']) or '-'}")
        if not authz.can(a, "appt.edit"):
            st.text(ap["notes"] or "")
            st.markdown("**Questions to ask:**")
            st.text(ap["questions"] or "none yet")
            with st.form(f"q{ap['id']}", clear_on_submit=True):
                nq = st.text_input("Add a question for the doctor")
                if st.form_submit_button("Add question") and nq:
                    act(services.add_question, a, ap["id"], nq)
            return
        with st.form(f"ea{ap['id']}"):
            notes = st.text_area("Notes", ap["notes"])
            qs = st.text_area("Questions to ask the doctor", ap["questions"], help="Anyone in the circle can add these.")
            if st.form_submit_button("Save", type="primary"):
                act(services.update_appointment, a, ap["id"], notes, qs, success="Saved.")
        ok = st.checkbox("Yes, cancel this appointment", key=f"cxok{ap['id']}")
        if st.button("Cancel appointment", key=f"cx{ap['id']}", disabled=not ok):
            res = act(services.cancel_appointment, a, ap["id"], rerun=False)
            if res is not None:
                st.session_state["flash"] = "Appointment cancelled." + email_note(res)
                st.rerun()


def _add_appt_form(a: Actor) -> None:
    """Add an appointment; everyone with an email is told straight away."""
    st.markdown("### ➕ Add appointment")
    with st.form("addappt", clear_on_submit=True):
        c1, c2 = st.columns(2)
        doc = c1.text_input("Doctor")
        spec = c2.text_input("Specialty")
        d = c1.date_input("Date", clock.today() + timedelta(days=1))
        t = c2.time_input("Time", dtime(10, 0))
        loc = st.text_input("Location")
        qs = st.text_area("Questions to ask the doctor")
        if st.form_submit_button("Add", type="primary"):
            res = act(services.add_appointment, a, doc, spec, datetime.combine(d, t), loc, qs, rerun=False)
            if res is not None:
                st.session_state["flash"] = "Appointment added." + email_note(res)
                st.rerun()
    st.caption("Everyone in the circle with an email is told straight away, then reminded 24 hours and 2 hours before.")


def tab_appts(a: Actor) -> None:
    """Upcoming doctor appointments."""
    st.subheader("Doctor appointments")
    ups = db.appts(a["family_id"])
    if not ups:
        st.info("No upcoming appointments.")
    for ap in ups:
        _appt_card(a, ap)
    if authz.can(a, "appt.add"):
        _add_appt_form(a)


# ------------------------------------------------------------------ Insights & AI
def safe_ai_markdown(text: str) -> str:
    """AI output is untrusted: keep simple formatting, drop links, images, HTML and LaTeX."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?i)\b(?:https?|ftp|javascript|data):\S+", "[link removed]", text)
    text = re.sub(r"<[^>]*>", "", text)
    return text.replace("$", "\\$")


def show_ai(res: dict[str, Any]) -> None:
    """Show an assistant answer with its warnings, what was removed before sending, and the AI label."""
    if res.get("urgent"):
        st.error(res["error"])
        return
    if res.get("removed"):
        st.caption("Before sending, CareCircle removed: " + ", ".join(res["removed"]) + ".")
    if res.get("text"):
        for f in res.get("flags", []):
            st.warning("⚠️ " + f)
        if res.get("ai", True):
            st.markdown(safe_ai_markdown(res["text"]))
            st.caption("🤖 " + insights.AI_LABEL)
        else:
            st.text(res["text"])
    if res.get("error"):
        st.caption(res["error"])


def _adherence_charts(fid: int, s: dict[str, Any]) -> None:
    """Confirmed-taken rate by day and by medicine, and recent moods, each with a table alternative."""
    if s["per_day"]:
        st.markdown("**Daily: % confirmed taken**")
        daily = {"Confirmed %": {d: round(100 * t / n) for d, (t, n) in sorted(s["per_day"].items())}}
        st.bar_chart(pd.DataFrame(daily))
        as_table(daily)
    if s["per_med"]:
        st.markdown("**By medicine**")
        by_med = {"Confirmed %": {k: round(100 * t / n) for k, (t, n) in s["per_med"].items()}}
        st.bar_chart(pd.DataFrame(by_med))
        as_table(by_med)
    ck = db.checkins(fid, 14)
    if ck:
        st.markdown("**Mood (1-5)**")
        moods = {"Mood": {c["day"]: c["mood"] for c in ck}}
        st.line_chart(pd.DataFrame(moods))
        as_table(moods)


def _ask_ai(fid: int) -> None:
    """The optional Gemini assistant, or the plain medicine list when it's off."""
    st.markdown("### 🤖 Ask about this care plan")
    ok, why = insights.ai_status(fid)
    if not ok:
        st.caption(why)
        if st.button("Show the medicine list in plain words", key="gplain"):
            show_ai(insights.explain_medicines(fid))
        return
    st.caption("Your question and the medicine list (medicine names, doses, times and how often doses were confirmed) are "
               "sent to Google Gemini. Names, doctors, places and notes are not. Don't type personal details.")
    qn = st.text_input("Your question", key="gq", max_chars=500,
                       placeholder="e.g. Which dose is being missed most, and what routine could help?")
    c1, c2 = st.columns(2)
    if c1.button("Ask", key="gask") and qn:
        with st.spinner("Thinking..."):
            show_ai(insights.ask(qn, fid))
    if c2.button("Explain my medicines simply", key="gexplain"):
        with st.spinner("Writing..."):
            show_ai(insights.explain_medicines(fid))


def tab_insights(a: Actor) -> None:
    """Adherence numbers, patterns worth knowing, today's summary and the AI assistant."""
    fid = a["family_id"]
    st.subheader("Insights")
    s = db.adherence(fid, 7)
    c1, c2, c3 = st.columns(3)
    c1.metric("Doses confirmed taken (7 days)", f"{s['pct']}%" if s["pct"] is not None else "-")
    c2.metric("Days with every dose confirmed", f"{s['streak']}")
    c3.metric("Never confirmed (7 days)", s["unconfirmed"])
    st.caption("'Never confirmed' doses may have been taken without anyone tapping 'Taken'.")
    for line in insights.weekly_insights(fid):
        st.write("• " + esc(line))
    _adherence_charts(fid, s)
    st.markdown("**Today's summary**")
    st.code(insights.daily_summary(fid), language=None)
    _ask_ai(fid)


# ------------------------------------------------------------------ Circle
def _manage_access(a: Actor, people: list[Row]) -> None:
    """Change someone's email, give them a new code, or remove them."""
    with st.expander("Manage someone's access"):
        who = st.selectbox("Person", people, format_func=lambda p: f"{p['name']} ({p['role']})", key="mgsel")
        ne = st.text_input("Email", who["email"] if who else "", key=f"emval{who['id'] if who else 0}")
        if st.button("Save email", key="emsave"):
            act(services.update_email, a, who["id"], ne, success="Email updated.")
        ok = st.checkbox(f"Give {esc(who['name'])} a new access code (their current code stops working)", key=f"rcok{who['id']}")
        if st.button("Give a new code", key="rcbtn", disabled=not ok):
            code = act(services.reset_code, a, who["id"], rerun=False)
            if code:
                st.session_state["issued_code"] = (f"{who['name']} ({who['role']})", code)
                st.rerun()
        if who["role"] != "elder" and who["id"] != a["id"]:
            ok = st.checkbox(f"Remove {esc(who['name'])} from the circle", key=f"rmok{who['id']}")
            if st.button("Remove access", key="rmbtn", disabled=not ok):
                act(services.remove_member, a, who["id"], success=f"Removed {who['name']}.")


def _invite_form(a: Actor) -> None:
    """Invite a caretaker or family member and show their access code once."""
    with st.form("addmember", clear_on_submit=True):
        st.markdown("**Invite someone**")
        c1, c2, c3 = st.columns(3)
        n, e = c1.text_input("Name"), c2.text_input("Email")
        r = c3.selectbox("Role", ["family", "caretaker"],
                         help="Caretakers can change everything. Family can view, confirm doses, suggest medicines "
                              "and add questions.")
        if st.form_submit_button("Create access code", type="primary"):
            code = act(services.invite, a, n, e, r, rerun=False)
            if code:
                st.session_state["issued_code"] = (f"{n} ({r})", code)
                st.rerun()


def tab_circle(a: Actor) -> None:
    """Who is in the circle, managing their access, and who did what."""
    fid = a["family_id"]
    st.subheader("Care circle")
    if st.session_state.get("issued_code"):
        who, code = st.session_state["issued_code"]
        show_codes({who: code}, "New access code:")
        if st.button("Done, I've shared it", key="issued_done"):
            st.session_state.pop("issued_code", None)
            st.rerun()
    contact = authz.can(a, "member.view_contact")
    people = db.members(fid)
    for p in people:
        c1, c2, c3 = st.columns([3, 3, 3])
        c1.write(f"**{esc(p['name'])}** · {p['role']}" + (" · you" if p["id"] == a["id"] else ""))
        if contact:
            c2.write(esc(p["email"]) if p["email"] else "⚠️ no email")
        if p["legacy_lookup"]:
            c3.caption("still on an old 6-character code")
    st.caption("Access codes are never shown here: CareCircle stores only a scrambled form of them. "
               "Give someone a new code if they've lost theirs.")
    if authz.can(a, "member.edit"):
        _manage_access(a, people)
    if authz.can(a, "member.invite"):
        _invite_form(a)
    st.markdown("### Who did what")
    for row in db.activity(fid, 25):
        st.caption(f"{clock.from_utc(row['at'])} · **{esc(row['who'])}** {esc(row['what'])}")


# ------------------------------------------------------------------ Emails
def tab_outbox(a: Actor) -> None:
    """Every reminder email for this circle, whether it was sent, and the reminder engine's health."""
    st.subheader("Reminder emails")
    if notifier.smtp_configured():
        st.success("Email is configured: reminders go out as real emails.")
    else:
        st.info("Email is not configured on this server, so emails are simulated and shown here.")
    ok, msg = health.reminder_engine_status()
    (st.caption if ok else st.warning)(msg)
    st.caption("Checks run every minute: upcoming dose (15 min before), dose not confirmed (30 min after), refill "
               "needed, appointment 24h / 2h before, and an 8 PM family summary.")
    if config.demo_features() and authz.can(a, "demo.simulate"):
        with st.expander("🕒 Demo: simulate a time of day (this circle only)"):
            d = st.date_input("Date", clock.today(), key="simd")
            t = st.time_input("Time", dtime(8, 5), key="simt")
            if st.button("Run reminder check at this time", key="simrun"):
                n = act(services.run_reminders_now, a, datetime.combine(d, t), rerun=False)
                st.session_state["flash"] = f"Processed {n or 0} email(s)."
                st.rerun()
    if st.button("Run this circle's reminder check now", key="runnow"):
        act(services.run_reminders_now, a)
    for r in services.outbox(a):
        with st.expander(f"{clock.from_utc(r['created_at'])} · {r['kind']} → {esc(r['to_email'])} [{r['status']}]"):
            st.markdown(f"**{esc(r['subject'])}**")
            st.text(r["body"])
            if r["error"]:
                st.error(esc(r["error"]))
            if r["status"] == "pending" and r["attempts"]:
                st.caption(f"Attempt {r['attempts']} failed; it will be retried automatically.")


# ------------------------------------------------------------------ Settings
def _circle_settings(a: Actor) -> None:
    """Email detail, the AI assistant, and the elder's say over medicine changes."""
    fs = db.family_settings(a["family_id"])
    detail = st.radio("What reminder emails include", ["minimal", "full"], index=0 if fs["email_detail"] == "minimal" else 1,
                      horizontal=True, key="set_detail",
                      format_func=lambda v: {"minimal": "Minimal: just that something is due (recommended)",
                                             "full": "Full: medicine names, doses, doctors and questions"}[v])
    st.caption("Emails can be read by anyone with access to the inbox, and on lock screens. "
               "Minimal keeps health details inside CareCircle.")
    st.markdown("**AI assistant (Google Gemini)**")
    ai_on = st.toggle("Allow the AI assistant for this care circle", value=fs["ai_enabled"], key="set_ai")
    st.caption("When on, questions and the medicine list (no names, doctors, places or notes) are sent to Google. "
               "AI answers can be wrong and are never medical advice.")
    st.markdown("**Medicine changes made by others**")
    who = elder_first(a)
    if authz.can(a, "family.elder_settings"):
        approve_first = st.toggle("Changes to my medicines by others need my approval before they start",
                                  value=fs["elder_approval"], key="set_elder")
        st.caption("Off: changes start straight away and you're told, so you can say OK or 'I don't agree'. "
                   "On: nothing changes until you approve it. Only you can change this.")
    else:
        approve_first = fs["elder_approval"]
        st.write(("✅ On" if approve_first else "Off") + f": {who} "
                 + ("approves changes made by others before they start." if approve_first
                    else "is told about every change made by others and can say OK or disagree."))
        st.caption(f"Only {who} can change this.")
    if st.button("Save settings", type="primary", key="set_save"):
        act(services.update_settings, a, email_detail=detail, ai_enabled=ai_on, elder_approval=approve_first,
            success="Settings saved.")


def _local_keys_form() -> None:
    """Development only: save email and Gemini keys to the local .env file."""
    st.warning("Development mode: keys saved here go into a local .env file on this computer and apply to every "
               "care circle on this server. On a real deployment, use environment variables or Streamlit secrets; "
               "this form is turned off outside development.")
    with st.form("smtp"):
        user = st.text_input("Gmail address", config.get("SMTP_USER"))
        pw = st.text_input("App password", type="password",
                           placeholder="saved" if config.get("SMTP_PASS") else "xxxx xxxx xxxx xxxx")
        gem = st.text_input("Gemini API key", type="password",
                            placeholder="saved" if config.get("GEMINI_API_KEY") else "AIza...")
        if st.form_submit_button("Save keys"):
            vals = {"SMTP_USER": user.strip()}
            if pw.strip():
                vals["SMTP_PASS"] = pw
            if gem.strip():
                vals["GEMINI_API_KEY"] = gem
            insights.reset_model_cache()
            act(config.save, vals, success="Saved to .env.")
    if insights.gemini_configured() and st.button("Test Gemini", key="testgem"):
        txt, err = insights.generate("Reply with the single word: ready")   # no personal data
        if txt:
            st.success(f"Gemini replied: {txt.strip()}")
        else:
            st.error(err)


def _server_keys(a: Actor) -> None:
    """Whether email and the AI are set up on this server, and a test email."""
    st.markdown("**Server email and AI keys**")
    st.write("Email: " + ("✅ configured" if notifier.smtp_configured() else "❌ not configured (emails are simulated)"))
    st.write("Gemini: " + ("✅ key set" if insights.gemini_configured() else "not set (optional)"))
    if config.ui_can_save_secrets():
        _local_keys_form()
    else:
        st.caption("These are set by the server administrator (environment variables or Streamlit secrets) "
                   "and can't be changed here.")
    if notifier.smtp_configured() and a.get("email"):
        if st.button(f"Send a test email to me ({esc(mask_email(a['email']))})", key="testsend"):
            ok, err = notifier.send_test(a["email"])
            if ok:
                st.success("Sent. Check the inbox (and spam).")
            else:
                st.error(err)


def tab_settings(a: Actor) -> None:
    """Circle settings, then the server's email and AI set-up."""
    st.subheader("Settings")
    _circle_settings(a)
    st.divider()
    _server_keys(a)


# ------------------------------------------------------------------ Privacy & data
def tab_privacy(a: Actor) -> None:
    """What is stored and for how long, a copy of the data, the security log, and deleting the circle."""
    fid = a["family_id"]
    st.subheader("Privacy and data")
    r = maintenance.retention()
    st.markdown(
        "CareCircle stores names, email addresses, medicines, doses, appointments and mood check-ins for this circle. "
        "Names, emails, medicine details, notes and emails sent are encrypted in the database. Access codes are stored "
        "only in a scrambled (hashed) form.")
    st.markdown(f"""**How long things are kept**
- Dose history and mood check-ins: {r['RETENTION_DOSE_DAYS']} days
- Who-did-what log: {r['RETENTION_ACTIVITY_DAYS']} days
- Copies of sent emails: {r['RETENTION_OUTBOX_DAYS']} days
- Security log: {r['RETENTION_SECURITY_DAYS']} days
- Stopped medicines and past appointments: {r['RETENTION_RECORD_DAYS']} days""")
    if authz.can(a, "family.export"):
        if st.button("Prepare a copy of all our data", key="export_prep"):
            st.session_state["export"] = act(services.export_data, a, rerun=False)
        if st.session_state.get("export"):
            st.download_button("⬇️ Download (JSON)", st.session_state["export"], file_name="carecircle-data.json",
                               mime="application/json", key="export_dl")
            st.caption("This file contains health information. Store it somewhere private.")
    if authz.can(a, "audit.view"):
        with st.expander("Security log"):
            names = {m["id"]: m["name"] for m in db.members(fid)}
            for ev in services.security_log(a):
                when = datetime.fromtimestamp(ev["at"]).strftime("%Y-%m-%d %H:%M")
                st.caption(f"{when} · {ev['event']} · {esc(names.get(ev['member_id'], 'someone'))} {esc(ev['detail'])}")
    if authz.can(a, "family.delete"):
        with st.expander("Delete this care circle"):
            st.error("This permanently deletes the circle and all of its data for everyone. It can't be undone.")
            confirm = st.text_input(f"Type the circle name ({a['family_name']}) to confirm", key="del_confirm")
            if st.button("Delete everything", key="del_btn"):
                if act(services.delete_circle, a, confirm, rerun=False) is not None:
                    st.session_state.clear()
                    st.session_state["notice"] = "The care circle and its data were deleted."
                    st.rerun()


# ------------------------------------------------------------------ main
def _account_security(a: Actor) -> None:
    """Change my code, two-step sign-in, and signing out everywhere."""
    with st.expander("Account security"):
        st.caption(f"You'll be signed out after {config.get_int('SESSION_IDLE_MIN', 30)} minutes without activity.")
        if st.button("Change my access code", key="rotate_open"):
            st.session_state["rotating"] = True
            st.rerun()
        if a["role"] not in config.mfa_roles():
            mfa = st.toggle("Also ask for an emailed code when I sign in", value=a["mfa"], key="mfa_opt",
                            disabled=not a.get("email"))
            if mfa != a["mfa"]:
                act(services.set_my_preferences, a, mfa=mfa)
        else:
            st.caption("Two-step sign-in is on for your role.")
        if st.button("Sign out on all devices", key="signout_all"):
            auth.revoke_member_sessions(a["id"])
            st.session_state.clear()
            st.session_state["notice"] = "Signed out everywhere."
            st.rerun()


def sidebar(a: Actor) -> bool:
    """Who is signed in, large text, language, sign out and account security. Returns the large-text choice."""
    lang = a["lang"]
    with st.sidebar:
        st.markdown(f"### 💊 {esc(a['family_name'])}")
        st.write(f"Signed in as **{esc(a['name'])}** ({a['role']})")
        big = st.toggle(i18n.t("large_text", lang), value=a["large_text"], key="big")
        if big != a["large_text"]:
            act(services.set_my_preferences, a, large_text=big)
        new_lang = st.selectbox(i18n.t("language", lang), list(i18n.LANGS), index=list(i18n.LANGS).index(lang),
                                format_func=lambda k: i18n.LANGS[k], key="lang")
        if new_lang != lang:
            act(services.set_my_preferences, a, lang=new_lang)
        if st.button(i18n.t("sign_out", lang), key="signout"):
            auth.sign_out(st.session_state.get("token"))
            st.session_state.clear()
            st.rerun()
        _account_security(a)
    return bool(big)


def banners(a: Actor) -> None:
    """Problems caretakers should know about: email not set up, reminders stopped, undeliverable emails."""
    if not authz.can(a, "outbox.view"):
        return
    fid = a["family_id"]
    if not notifier.smtp_configured():
        st.warning("Emails are only simulated on this server, so reminders are not really being sent.")
    ok, msg = health.reminder_engine_status()
    if not ok:
        st.warning("⏰ " + msg)
    dead = notifier.delivery_problems(fid)
    if dead:
        st.error(f"{dead} reminder email(s) couldn't be delivered in the last 24 hours. See the Emails tab.")
    no_mail = [p["name"] for p in db.members(fid) if not p["email"]]
    if no_mail:
        st.warning("No email on file for: " + ", ".join(esc(n) for n in no_mail) + ". They won't get reminders.")
    if a["is_demo"]:
        st.info("This is a sample family with made-up data.")


TABS: list[tuple[str, Callable[[Actor], None], str]] = [
    ("Today", tab_today, "view"), ("Medications", tab_meds, "view"), ("Appointments", tab_appts, "view"),
    ("Insights", tab_insights, "view"), ("Circle", tab_circle, "view"), ("Emails", tab_outbox, "outbox.view"),
    ("Settings", tab_settings, "family.settings"), ("Privacy & data", tab_privacy, "family.export"),
]


def main() -> None:
    """Route to the right screen for this browser session: signed out, second step, new code, or the app."""
    tok = st.session_state.get("token")
    a = auth.current(tok) if tok else None
    if tok and not a:
        st.session_state.clear()
        st.session_state["notice"] = "You were signed out: you were inactive for a while, or your access changed."
    if not a:
        styles(False)
        login_screen()
        return
    if a["stage"] == "mfa":
        styles(False)
        mfa_screen(a)
        return
    if a["stage"] == "rotate" or st.session_state.get("new_code") or st.session_state.get("rotating"):
        styles(a["large_text"])
        new_code_screen(a, first_time=a["stage"] == "rotate")
        return
    styles(sidebar(a))
    set_page_language(a["lang"])
    flash()
    banners(a)
    tabs = [t for t in TABS if authz.can(a, t[2])]
    for tab, (_, render, _) in zip(st.tabs([t[0] for t in tabs]), tabs):
        with tab:
            try:
                render(a)
            except Exception as e:
                show_error(e)


main()
