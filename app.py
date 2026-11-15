"""CareCircle - Elderly Care & Medication Management (Streamlit).

This file only renders. Every read and change goes through auth (who is signed in) and services (what they
may do), which check permissions independently of what is shown here. All user-entered text is escaped
before it is rendered as Markdown or HTML.
"""
import html
import json
import re
from datetime import datetime, time as dtime, timedelta

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

st.set_page_config(page_title="CareCircle", page_icon="💊", layout="wide")


# ------------------------------------------------------------------ start-up
@st.cache_resource
def boot():
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
except crypto.DecryptionError as e:
    st.error("CareCircle can't read its database with the encryption key it was given.")
    st.write(str(e))
    st.markdown(KEY_HELP)
    st.stop()
if BOOT_ERRORS:
    st.error("CareCircle can't start because the server configuration is unsafe for real data:")
    for e in BOOT_ERRORS:
        st.write("- " + e)
    st.caption("Fix these in your environment variables or Streamlit secrets, then restart. `python manage.py check-config` lists them too.")
    st.stop()

ICON = {"taken": "✅", "unconfirmed": "❔", "missed": "❌", "pending": "⏰", "skipped": "⏭️"}
MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$])")


def esc(text):
    """Make user text safe inside st.markdown/st.write: no links, formatting, HTML or LaTeX from user input."""
    return MD_SPECIAL.sub(r"\\\1", str(text or ""))


_MD_ENTITIES = {ord(c): f"&#{ord(c)};" for c in "[]()*_`~$\\"}


def h(text):
    """Escape user text for HTML blocks, including Markdown characters so nothing becomes a link or formatting."""
    return html.escape(str(text or "")).translate(_MD_ENTITIES)


def fmt_t(t):
    return datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")


def time_options():
    return [f"{hh:02d}:{mm:02d}" for hh in range(5, 24) for mm in (0, 30)]


def mask_email(e):
    if not e or "@" not in e:
        return ""
    user, dom = e.split("@", 1)
    return f"{user[:1]}{'•' * max(1, len(user) - 1)}@{dom}"


def clients():
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


def styles(big):
    st.markdown(f"""
<style>
html, body, [class*="css"] {{ font-size: {'22px' if big else '16px'}; }}
.stButton > button {{ min-height: {'3.6rem' if big else '2.4rem'}; font-size: {'1.3rem' if big else '1rem'}; border-radius: 12px; }}
.dose {{ padding: .8rem 1rem; border-radius: 14px; margin-bottom: .4rem; border: 1px solid rgba(128,128,128,.3); }}
.dose.taken {{ background: rgba(46,160,67,.15); }}
.dose.missed, .dose.unconfirmed {{ background: rgba(218,54,51,.12); }}
.dose.pending {{ background: rgba(128,128,128,.08); }}
.dose.skipped {{ background: rgba(210,153,34,.15); }}
.emergency {{ font-size: 1.4em; font-weight: bold; padding: .6rem 1rem; border-radius: 12px; border: 2px solid #da3633; }}
</style>""", unsafe_allow_html=True)


def show_error(e):
    if type(e).__name__ in ("RerunException", "StopException"):  # Streamlit control flow, never swallow
        raise e
    if isinstance(e, (Invalid, Forbidden, db.NotFound)):
        msg = str(e) if not isinstance(e, db.NotFound) else "That item no longer exists. Refresh the page."
        st.error(msg)
    else:
        st.error(f"Something went wrong. Please try again. (Reference {logutil.capture(e, 'ui')})")


def act(fn, *args, success=None, rerun=True, **kw):
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


def flash():
    msg = st.session_state.pop("flash", None)
    if msg:
        st.success(msg)


# ------------------------------------------------------------------ sign-in
def start_session(r):
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


def show_codes(codes, title):
    st.success(title)
    st.code("\n".join(f"{k}: {v}" for k, v in codes.items()), language=None)
    st.warning("Write these down or share each one privately now (for example, read it out on a call). "
               "CareCircle doesn't keep a readable copy, so it can't show them again. The elder or a caretaker can "
               "give anyone a new code from the Circle tab.")


def login_screen():
    st.title("💊 CareCircle")
    st.caption("One shared, private care profile for an elderly person, their caretakers and their family.")
    notice = st.session_state.pop("notice", None)
    if notice:
        st.info(notice)
    if st.session_state.get("pick"):
        pick = st.session_state["pick"]
        st.subheader("Which care circle?")
        choice = st.radio("Your email belongs to more than one circle", pick["choices"],
                          format_func=lambda c: f"{c['family_name']} ({c['role']})", key="pick_choice")
        c1, c2 = st.columns(2)
        if c1.button("Open", type="primary", key="pick_open"):
            start_session(auth.email_signin_pick(pick["token"], choice["id"]))
        if c2.button("Start again", key="pick_cancel"):
            st.session_state.pop("pick", None)
            st.rerun()
        return

    names = ["🔑 Sign in", "✉️ Email me a code", "➕ Create a care circle"]
    if config.demo_features():
        names.append("✨ Try the sample family")
    tabs = st.tabs(names)
    with tabs[0]:
        with st.form("login"):
            code = st.text_input("Your access code", type="password", max_chars=20, placeholder="XXXX-XXXX-XXXX",
                                 key="login_code", help="Capitals and dashes don't matter. Tap the eye icon to check what you typed.")
            ok = st.form_submit_button("Open", type="primary")
        st.caption("Forgot it? Use **Email me a code**, or ask the elder or a caretaker for a new one.")
        if ok:
            start_session(auth.login_with_code(code, clients()))
    with tabs[1]:
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
    with tabs[2]:
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
    if config.demo_features():
        with tabs[3]:
            st.write("Creates a made-up family with a week of history so you can explore. Not for real people.")
            if st.button("✨ Load sample family", key="demo_btn"):
                try:
                    show_codes(seed.seed_demo(clients=clients()), "Sample family created. Access codes:")
                except Exception as e:
                    show_error(e)


def mfa_screen(a):
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


def new_code_screen(a, first_time):
    st.title("💊 CareCircle")
    if "new_code" not in st.session_state:
        if first_time:
            st.subheader("Time for a new, stronger access code")
            st.write("CareCircle now uses longer codes that are much harder to guess. Your old 6-character code will stop working.")
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
def read_aloud_button(text, lang):
    import streamlit.components.v1 as components
    payload = json.dumps(text).replace("</", "<\\/")
    label = h(i18n.t("read_aloud", lang))
    components.html(f"""
<button id="speak" style="font-size:20px;padding:10px 18px;border-radius:12px;cursor:pointer">🔊 {label}</button>
<script>
document.getElementById("speak").onclick = function () {{
  if (!window.speechSynthesis) {{ return; }}
  var u = new SpeechSynthesisUtterance({payload});
  u.lang = "{'hi-IN' if lang == 'hi' else 'en-IN'}"; u.rate = 0.9;
  window.speechSynthesis.cancel(); window.speechSynthesis.speak(u);
}};
</script>""", height=70)


def printable_schedule(a, meds):
    rows = "".join(
        f"<tr><td>{h(', '.join(fmt_t(t) for t in m['times'].split(',')))}</td><td><b>{h(m['name'])}</b> {h(m['dosage'])}</td>"
        f"<td>{h(m['instructions'])}</td></tr>" for m in meds)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Medicines</title>
<style>body{{font-family:Arial,sans-serif;font-size:22px;margin:24px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:2px solid #333;padding:12px;text-align:left}}</style></head><body>
<h1>Medicines for {h(a['elder_name'])}</h1><p>Printed {h(clock.now().strftime('%d %B %Y'))}. Check with the doctor before changing anything.</p>
<table><tr><th>When</th><th>Medicine</th><th>How</th></tr>{rows}</table>
<p>Emergency: call {h(config.get('EMERGENCY_NUMBER', '112'))}</p></body></html>"""


def help_box(a, lang):
    num = config.get("EMERGENCY_NUMBER", "112")
    st.markdown(f"<div class='emergency'>📞 {h(i18n.t('emergency', lang, num=num))}</div>", unsafe_allow_html=True)
    c1, c2 = st.columns([2, 3])
    if c1.button(i18n.t("help_btn", lang), type="primary", key="help_btn"):
        st.session_state["help_result"] = act(services.request_help, a, rerun=False)
    c2.caption(i18n.t("help_explain", lang, num=num))
    res = st.session_state.get("help_result")
    if res:
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


LEVEL_ICON = {"urgent": "🔴", "todo": "🟠", "info": "🔵", "good": "🟢"}


def assistant_card(a, lang):
    """What needs attention now, for this person, at this time. Each line says why it's there."""
    items = assistant.suggestions(a)
    with st.container(border=True):
        st.markdown(f"#### {i18n.t('ast_title', lang)}")
        if not items:
            st.write(i18n.t("ast_none", lang))
        for it in items:
            st.markdown(f"{LEVEL_ICON[it['level']]} {esc(it['text'])}")
            st.caption(f"{i18n.t('ast_why', lang)}: {esc(it['why'])}")


def review_box(a, lang):
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


def tab_today(a):
    fid, lang = a["family_id"], a["lang"]
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

    ck = {c["day"]: c for c in db.checkins(fid, 1)}.get(today.isoformat())
    if authz.can(a, "checkin.set"):
        st.markdown(f"**{i18n.t('how_feeling', lang)}**")
        cols = st.columns(5)
        for i, face in enumerate(["😞", "🙁", "😐", "🙂", "😄"], start=1):
            if cols[i - 1].button(f"{face}\n{i18n.t(f'mood_{i}', lang)}", key=f"mood{i}",
                                  type="primary" if ck and ck["mood"] == i else "secondary"):
                act(services.check_in, a, i)
    elif ck:
        st.info(i18n.t("mood_today", lang, mood=i18n.t(f"mood_{ck['mood']}", lang)))
    else:
        st.caption(i18n.t("no_mood", lang))

    st.markdown(f"### {i18n.t('medicines', lang)}")
    doses = db.doses_for_day(fid, today.isoformat())
    if not doses:
        st.info(i18n.t("none_today", lang))
    for d in doses:
        c1, c2, c3 = st.columns([5, 1.6, 1.6])
        late = d["status"] == "pending" and d["scheduled_time"] < now_t
        who = d["marked_by"] or "?"
        status_text = i18n.t(f"st_{d['status']}", lang, who=who)
        c1.markdown(
            f"<div class='dose {h(d['status'])}'>{ICON[d['status']]} <b>{h(fmt_t(d['scheduled_time']))}</b> - "
            f"{h(d['name'])} {h(d['dosage'])}"
            f"{' <i>(' + h(d['instructions']) + ')</i>' if d['instructions'] else ''}"
            f"{' <b>- ' + h(i18n.t('due_now', lang)) + '</b>' if late else ''}"
            f"{'<br><small>' + h(status_text) + '</small>' if status_text else ''}</div>",
            unsafe_allow_html=True)
        if d["status"] in ("pending", "unconfirmed"):
            if c2.button(i18n.t("taken", lang), key=f"t{d['id']}", type="primary"):
                act(services.mark_dose, a, d["id"], "taken")
            label = i18n.t("not_taken" if d["status"] == "unconfirmed" else "skip", lang)
            if c3.button(label, key=f"s{d['id']}"):
                act(services.mark_dose, a, d["id"], "missed" if d["status"] == "unconfirmed" else "skipped")
        elif c2.button(i18n.t("undo", lang), key=f"u{d['id']}"):
            act(services.mark_dose, a, d["id"], "pending")
    if doses:
        st.caption(i18n.t("honesty", lang))

    meds = db.meds(fid)
    c1, c2 = st.columns(2)
    with c1:
        spoken = " ".join(i18n.t("aloud_dose", lang, time=fmt_t(d["scheduled_time"]), name=d["name"], dosage=d["dosage"],
                                 instr=d["instructions"] or "") for d in doses) or i18n.t("aloud_none", lang)
        read_aloud_button(spoken, lang)
    with c2:
        st.download_button(i18n.t("print", lang), printable_schedule(a, meds), file_name="medicines.html",
                           mime="text/html", help=i18n.t("print_help", lang), key="print_btn")

    nxt = db.appts(fid)[:1]
    if nxt:
        ap = nxt[0]
        st.markdown(f"### {i18n.t('next_appt', lang)}")
        st.info(f"**{esc(ap['doctor'])}** {('(' + esc(ap['specialty']) + ')') if ap['specialty'] else ''}  \n"
                f"{datetime.fromisoformat(ap['when_at']).strftime('%A %d %B, %I:%M %p')} "
                f"{('· ' + esc(ap['location'])) if ap['location'] else ''}")


# ------------------------------------------------------------------ Medications
def safety_note():
    cov = interactions.coverage()
    st.caption(f"⚠️ {interactions.DISCLAIMER} The list covers {cov['ingredients']} ingredients"
               f"{'' if cov['reviewed_by'] else ' and has not yet been reviewed by a pharmacist'}.")


def show_check(check):
    for w in check.warnings:
        st.warning("⚠️ " + esc(w) + " Please check with a doctor or pharmacist.")
    for n in check.unrecognised:
        s = check.suggestions.get(n)
        st.info(f"CareCircle's basic check doesn't recognise **{esc(n)}**, so it could not check it against the other "
                f"medicines.{' Did you mean ' + esc(s.title()) + '?' if s else ''} Ask the pharmacist.")


def tab_meds(a):
    fid = a["family_id"]
    st.subheader("Medication schedule")
    elder_first = esc((a["elder_name"] or "the elder").split()[0])
    elder_decides = db.family_settings(fid)["elder_approval"]
    if elder_decides:
        st.info(f"{elder_first} has asked to approve medicine changes made by others before they start.")
    pend = db.meds(fid, ("proposed",))
    if pend:
        st.warning(f"{len(pend)} suggested medicine(s) waiting for approval")
        for p in pend:
            c1, c2, c3 = st.columns([6, 1.3, 1.3])
            c1.write(f"**{esc(p['name'])} {esc(p['dosage'])}** at {', '.join(fmt_t(t) for t in p['times'].split(','))} "
                     f"- suggested by {esc(p['added_by'])}")
            if authz.can(a, "med.approve") and (a["role"] == "elder" or not elder_decides):
                if c2.button("Approve", key=f"ap{p['id']}", type="primary"):
                    act(services.approve_medicine, a, p["id"], success=f"Approved {p['name']}.")
                if c3.button("Decline", key=f"de{p['id']}"):
                    act(services.decline_medicine, a, p["id"])
            elif elder_decides:
                c2.caption(f"Waiting for {elder_first}")
        st.divider()
    waiting = [c for c in db.changes(fid, ("pending",)) if c["action"] != "add"]
    disputed = db.changes(fid, ("disputed",))
    if waiting or disputed:
        for c in waiting:
            st.warning(f"⏳ Waiting for {elder_first}'s OK: **{esc(c['by_name'])}** {esc(services.describe_change(c))}")
        for c in disputed:
            st.error(f"✋ {elder_first} doesn't agree: **{esc(c['by_name'])}** {esc(services.describe_change(c))}"
                     + (f' - "{esc(c["note"])}"' if c["note"] else "") + ". Please talk it through.")
        if a["role"] == "elder":
            st.caption("Answer these on the Today tab.")
        st.divider()

    active = db.meds(fid)
    for med in active:
        verified = med.get("verified_by")
        badge = "✔ checked" if verified else "⚠ not checked against prescription"
        with st.expander(f"💊 {esc(med['name'])} {esc(med['dosage'])} - {', '.join(fmt_t(t) for t in med['times'].split(','))} · {badge}"):
            st.write(esc(med["instructions"]) or "_No special instructions_")
            if verified:
                st.caption(f"Checked against the prescription by {esc(verified)} on {esc((med.get('verified_at') or '')[:10])}"
                           f"{' (prescribed by ' + esc(med['prescriber']) + ')' if med.get('prescriber') else ''}.")
            else:
                st.caption("Nobody has confirmed this entry against the prescription or the pharmacy label yet.")
            if med["stock"] is not None:
                st.progress(min(1.0, med["stock"] / 30), text=f"{med['stock']} doses left (refill alert at {med['refill_at']})")
            if not authz.can(a, "med.edit"):
                continue
            if not verified and st.button("I've checked this against the prescription", key=f"vf{med['id']}"):
                act(services.verify_medicine, a, med["id"], success="Marked as checked.")
            with st.form(f"edit{med['id']}"):
                c1, c2 = st.columns(2)
                dosage = c1.text_input("Dosage", med["dosage"])
                prescriber = c2.text_input("Prescribed by", med.get("prescriber") or "")
                times = st.multiselect("Times", sorted(set(time_options()) | set(med["times"].split(","))),
                                       default=med["times"].split(","), format_func=fmt_t)
                instr = st.text_input("Instructions", med["instructions"])
                st.caption("Changing the dosage or times clears the 'checked' mark, so someone checks again.")
                if elder_decides and a["role"] != "elder":
                    st.caption(f"{elder_first} approves changes first: yours will be sent to them.")
                if st.form_submit_button("Save changes", type="primary"):
                    res = act(services.edit_medicine, a, med["id"], dosage, times, instr, prescriber, rerun=False)
                    if res:
                        st.session_state["flash"] = (f"Sent to {elder_first} for approval." if res == "pending" else
                                                     "Saved." + ("" if a["role"] == "elder" else f" {elder_first} has been told."))
                        st.rerun()
            with st.form(f"stock{med['id']}"):
                c1, c2 = st.columns(2)
                ns = c1.number_input("Doses in stock now", 0, 9999, med["stock"] or 0)
                why = c2.text_input("Why (e.g. refilled, recount)")
                if st.form_submit_button("Update stock"):
                    act(services.adjust_stock, a, med["id"], ns, why, success="Stock updated.")
            hist = db.stock_events(fid, med["id"], 5)
            if hist:
                st.caption("Recent stock changes: " + "; ".join(
                    f"{e['delta']:+d} → {e['new_stock']} ({esc(e['reason'])}, {esc(e['who'])})" for e in hist))
            confirm = st.checkbox(f"Yes, stop {esc(med['name'])}", key=f"stopok{med['id']}")
            if st.button("Stop this medicine", key=f"stop{med['id']}", disabled=not confirm):
                res = act(services.stop_medicine, a, med["id"], rerun=False)
                if res:
                    st.session_state["flash"] = (f"Sent to {elder_first} for approval." if res == "pending"
                                                 else f"Stopped {med['name']}.")
                    st.rerun()
    if not active:
        st.info("No medicines yet. Add the first one below.")

    can_add = authz.can(a, "med.add")
    st.markdown("### ➕ " + ("Add a medicine" if can_add else "Suggest a medicine (the elder or a caretaker approves it)"))
    pending = st.session_state.get("med_preview")
    if not pending:
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
    else:
        st.info(f"Please check: **{esc(pending['name'])} {esc(pending['dosage'])}** at "
                f"{', '.join(fmt_t(t) for t in pending['times'])} every day from {pending['start']}"
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
    safety_note()


# ------------------------------------------------------------------ Appointments
def email_note(res):
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


def tab_appts(a):
    fid = a["family_id"]
    st.subheader("Doctor appointments")
    ups = db.appts(fid)
    if not ups:
        st.info("No upcoming appointments.")
    for ap in ups:
        when = datetime.fromisoformat(ap["when_at"])
        with st.expander(f"🩺 {when.strftime('%a %d %b, %I:%M %p')} - {esc(ap['doctor'])} "
                         f"{('(' + esc(ap['specialty']) + ')') if ap['specialty'] else ''}"):
            st.write(f"📍 {esc(ap['location']) or '-'}")
            if authz.can(a, "appt.edit"):
                with st.form(f"ea{ap['id']}"):
                    notes = st.text_area("Notes", ap["notes"])
                    qs = st.text_area("Questions to ask the doctor", ap["questions"],
                                      help="Anyone in the circle can add these.")
                    if st.form_submit_button("Save", type="primary"):
                        act(services.update_appointment, a, ap["id"], notes, qs, success="Saved.")
                ok = st.checkbox("Yes, cancel this appointment", key=f"cxok{ap['id']}")
                if st.button("Cancel appointment", key=f"cx{ap['id']}", disabled=not ok):
                    res = act(services.cancel_appointment, a, ap["id"], rerun=False)
                    if res is not None:
                        st.session_state["flash"] = "Appointment cancelled." + email_note(res)
                        st.rerun()
            else:
                st.text(ap["notes"] or "")
                st.markdown("**Questions to ask:**")
                st.text(ap["questions"] or "none yet")
                with st.form(f"q{ap['id']}", clear_on_submit=True):
                    nq = st.text_input("Add a question for the doctor")
                    if st.form_submit_button("Add question") and nq:
                        act(services.add_question, a, ap["id"], nq)
    if authz.can(a, "appt.add"):
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


# ------------------------------------------------------------------ Insights & AI
def safe_ai_markdown(text):
    """AI output is untrusted: keep simple formatting, drop links, images, HTML and LaTeX."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?i)\b(?:https?|ftp|javascript|data):\S+", "[link removed]", text)
    text = re.sub(r"<[^>]*>", "", text)
    return text.replace("$", "\\$")


def show_ai(res):
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
        else:
            st.text(res["text"])
        if res.get("ai", True):
            st.caption("🤖 " + insights.AI_LABEL)
    if res.get("error"):
        st.caption(res["error"])


def tab_insights(a):
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
    if s["per_day"]:
        st.markdown("**Daily: % confirmed taken**")
        st.bar_chart(pd.DataFrame({"Confirmed %": {d: round(100 * t / n) for d, (t, n) in sorted(s["per_day"].items())}}))
    if s["per_med"]:
        st.markdown("**By medicine**")
        st.bar_chart(pd.DataFrame({"Confirmed %": {k: round(100 * t / n) for k, (t, n) in s["per_med"].items()}}))
    ck = db.checkins(fid, 14)
    if ck:
        st.markdown("**Mood (1-5)**")
        st.line_chart(pd.DataFrame({"Mood": {c["day"]: c["mood"] for c in ck}}))
    st.markdown("**Today's summary**")
    st.code(insights.daily_summary(fid), language=None)

    st.markdown("### 🤖 Ask about this care plan")
    ok, why = insights.ai_status(fid)
    if not ok:
        st.caption(why)
        if st.button("Show the medicine list in plain words", key="gplain"):
            show_ai(insights.explain_medicines(fid))
        return
    st.caption("Your question and the medicine list (medicine names, doses, times and how often doses were confirmed) are "
               "sent to Google Gemini. Names, doctors, places and notes are not. Don't type personal details.")
    qn = st.text_input("e.g. Which dose is being missed most, and what routine could help?", key="gq", max_chars=500)
    c1, c2 = st.columns(2)
    if c1.button("Ask", key="gask") and qn:
        with st.spinner("Thinking..."):
            show_ai(insights.ask(qn, fid))
    if c2.button("Explain my medicines simply", key="gexplain"):
        with st.spinner("Writing..."):
            show_ai(insights.explain_medicines(fid))


# ------------------------------------------------------------------ Circle
def tab_circle(a):
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
    if authz.can(a, "member.invite"):
        with st.form("addmember", clear_on_submit=True):
            st.markdown("**Invite someone**")
            c1, c2, c3 = st.columns(3)
            n, e = c1.text_input("Name"), c2.text_input("Email")
            r = c3.selectbox("Role", ["family", "caretaker"],
                             help="Caretakers can change everything. Family can view, confirm doses, suggest medicines and add questions.")
            if st.form_submit_button("Create access code", type="primary"):
                code = act(services.invite, a, n, e, r, rerun=False)
                if code:
                    st.session_state["issued_code"] = (f"{n} ({r})", code)
                    st.rerun()
    st.markdown("### Who did what")
    for row in db.activity(fid, 25):
        st.caption(f"{clock.from_utc(row['at'])} · **{esc(row['who'])}** {esc(row['what'])}")


# ------------------------------------------------------------------ Emails
def tab_outbox(a):
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
def tab_settings(a):
    fid = a["family_id"]
    st.subheader("Settings")
    fs = db.family_settings(fid)
    st.markdown("**What reminder emails include**")
    detail = st.radio("Email detail", ["minimal", "full"], index=0 if fs["email_detail"] == "minimal" else 1, horizontal=True,
                      format_func=lambda v: {"minimal": "Minimal: just that something is due (recommended)",
                                             "full": "Full: medicine names, doses, doctors and questions"}[v],
                      key="set_detail", label_visibility="collapsed")
    st.caption("Emails can be read by anyone with access to the inbox, and on lock screens. Minimal keeps health details inside CareCircle.")
    st.markdown("**AI assistant (Google Gemini)**")
    ai_on = st.toggle("Allow the AI assistant for this care circle", value=fs["ai_enabled"], key="set_ai")
    st.caption("When on, questions and the medicine list (no names, doctors, places or notes) are sent to Google. "
               "AI answers can be wrong and are never medical advice.")
    st.markdown("**Medicine changes made by others**")
    elder_first = esc((a["elder_name"] or "the elder").split()[0])
    if authz.can(a, "family.elder_settings"):
        approve_first = st.toggle("Changes to my medicines by others need my approval before they start",
                                  value=fs["elder_approval"], key="set_elder")
        st.caption("Off: changes start straight away and you're told, so you can say OK or 'I don't agree'. "
                   "On: nothing changes until you approve it. Only you can change this.")
    else:
        approve_first = fs["elder_approval"]
        st.write(("✅ On" if approve_first else "Off") + f": {elder_first} "
                 + ("approves changes made by others before they start." if approve_first
                    else "is told about every change made by others and can say OK or disagree."))
        st.caption(f"Only {elder_first} can change this.")
    if st.button("Save settings", type="primary", key="set_save"):
        act(services.update_settings, a, email_detail=detail, ai_enabled=ai_on, elder_approval=approve_first,
            success="Settings saved.")

    st.divider()
    st.markdown("**Server email and AI keys**")
    st.write("Email: " + ("✅ configured" if notifier.smtp_configured() else "❌ not configured (emails are simulated)"))
    st.write("Gemini: " + ("✅ key set" if insights.gemini_configured() else "not set (optional)"))
    if config.ui_can_save_secrets():
        st.warning("Development mode: keys saved here go into a local .env file on this computer and apply to every "
                   "care circle on this server. On a real deployment, use environment variables or Streamlit secrets; "
                   "this form is turned off outside development.")
        with st.form("smtp"):
            user = st.text_input("Gmail address", config.get("SMTP_USER"))
            pw = st.text_input("App password", type="password", placeholder="saved" if config.get("SMTP_PASS") else "xxxx xxxx xxxx xxxx")
            gem = st.text_input("Gemini API key", type="password", placeholder="saved" if config.get("GEMINI_API_KEY") else "AIza...")
            if st.form_submit_button("Save keys"):
                vals = {"SMTP_USER": user.strip()}
                if pw.strip():
                    vals["SMTP_PASS"] = pw
                if gem.strip():
                    vals["GEMINI_API_KEY"] = gem
                insights._model_cache.clear()
                act(config.save, vals, success="Saved to .env.")
        if insights.gemini_configured() and st.button("Test Gemini", key="testgem"):
            txt, err = insights.generate("Reply with the single word: ready")   # no personal data
            st.success(f"Gemini replied: {txt.strip()}") if txt else st.error(err)
    else:
        st.caption("These are set by the server administrator (environment variables or Streamlit secrets) and can't be changed here.")
    if notifier.smtp_configured() and a.get("email"):
        if st.button(f"Send a test email to me ({esc(mask_email(a['email']))})", key="testsend"):
            ok, err = notifier.send_test(a["email"])
            st.success("Sent. Check the inbox (and spam).") if ok else st.error(err)


# ------------------------------------------------------------------ Privacy & data
def tab_privacy(a):
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
def sidebar(a):
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
    return big


def banners(a):
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


def main():
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
    flash()
    banners(a)
    tabs = [("Today", tab_today, "view"), ("Medications", tab_meds, "view"), ("Appointments", tab_appts, "view"),
            ("Insights", tab_insights, "view"), ("Circle", tab_circle, "view"), ("Emails", tab_outbox, "outbox.view"),
            ("Settings", tab_settings, "family.settings"), ("Privacy & data", tab_privacy, "family.export")]
    tabs = [t for t in tabs if authz.can(a, t[2])]
    for tab, (_, fn, _) in zip(st.tabs([t[0] for t in tabs]), tabs):
        with tab:
            try:
                fn(a)
            except Exception as e:
                show_error(e)


main()
