"""Plain-language summaries and the optional Gemini assistant.

AI safeguards
- Off unless the family turns it on (Settings) and the server allows it. In production it also needs
  GEMINI_PAID_TIER=true, because Google's terms for the unpaid tier allow human review and product
  improvement and ask developers not to send personal data.
- Only what the answer needs is sent: medicine names, doses, times and adherence numbers. No names of people,
  doctors, places, emails or notes. Phone numbers, emails, ID/card numbers and known names typed into a
  question are removed before sending, and the user is told what was removed.
- Questions that sound like an emergency are not sent to the AI; the user is told to call for help.
- Answers are checked for dose-change instructions and doses that are not in the care plan, and flagged.
- Every answer is labelled as AI-generated and not medical advice.
- Short timeouts and a circuit breaker: after repeated failures the assistant pauses for a few minutes
  instead of hanging the page.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

import clock
import config
import db
import interactions

log = logging.getLogger("carecircle.ai")
MOODS = {1: "Not good", 2: "Low", 3: "Okay", 4: "Good", 5: "Great"}
AI_LABEL = "AI-generated. It can be wrong and is not medical advice. Check anything important with the doctor or pharmacist."


def daily_summary(family_id, day=None, full=True):
    day = day or clock.today()
    fam = db.get_family(family_id)
    elder = fam["elder_name"]
    doses = db.doses_for_day(family_id, day.isoformat())
    taken = [d for d in doses if d["status"] == "taken"]
    unconfirmed = [d for d in doses if d["status"] == "unconfirmed"]
    missed = [d for d in doses if d["status"] in ("missed", "skipped")]
    pending = [d for d in doses if d["status"] == "pending"]
    who = elder if full else (elder.split()[0] if elder else "")
    lines = [f"Care summary for {who} - {day.strftime('%A %d %B')}", ""]
    lines.append(f"Medicines: {len(taken)} of {len(doses)} doses confirmed taken.")
    if full:
        for d in unconfirmed:
            lines.append(f"  - NOT CONFIRMED: {d['name']} at {d['scheduled_time']}")
        for d in missed:
            lines.append(f"  - {d['status'].upper()}: {d['name']} at {d['scheduled_time']}")
        for d in pending:
            lines.append(f"  - Still to come: {d['name']} at {d['scheduled_time']}")
    else:
        if unconfirmed:
            lines.append(f"  - {len(unconfirmed)} not confirmed")
        if missed:
            lines.append(f"  - {len(missed)} missed or skipped")
        if pending:
            lines.append(f"  - {len(pending)} still to come")
    ck = [c for c in db.checkins(family_id, 1, today=day) if c["day"] == day.isoformat()]
    if ck:
        lines.append(f"Mood check-in: {MOODS.get(ck[0]['mood'], '?')}" + (f" - \"{ck[0]['note']}\"" if full and ck[0]["note"] else ""))
    else:
        lines.append("Mood check-in: not done yet.")
    nxt = db.appts(family_id)[:1]
    if nxt:
        lines.append(f"Next appointment: {nxt[0]['doctor'] + ' on ' if full else ''}{nxt[0]['when_at'].replace('T', ' ')}")
    low = [m for m in db.meds(family_id) if m["stock"] is not None and m["stock"] <= (m["refill_at"] or 5)]
    if full:
        for m in low:
            lines.append(f"Refill soon: {m['name']} ({m['stock']} left)")
    elif low:
        lines.append(f"Refill soon: {len(low)} medicine(s)")
    if not full:
        lines += ["", "Open CareCircle for details."]
    return "\n".join(lines)


def weekly_insights(family_id):
    """Short observations a caregiver would want to know."""
    a = db.adherence(family_id, 7)
    out = []
    if a["pct"] is None:
        out.append("Not enough data yet - insights appear after a few days of use.")
    else:
        out.append(f"{a['pct']}% of doses in the last 7 days were confirmed taken.")
        if a["unconfirmed"]:
            out.append(f"{a['unconfirmed']} dose(s) were never confirmed. They may have been taken without anyone tapping 'Taken'.")
        if a["streak"] >= 2:
            out.append(f"{a['streak']}-day streak of confirming every dose.")
        for name, (t, n) in a["per_med"].items():
            if n >= 3 and t / n < 0.7:
                out.append(f"{name} is often not confirmed ({t}/{n}). Consider moving it to a time that fits the daily routine.")
        rows = db.q("SELECT scheduled_time, status FROM doses WHERE family_id=? AND status IN ('taken','missed','unconfirmed')",
                    (family_id,))
        buckets = {}
        for r in rows:
            h = int(r["scheduled_time"][:2])
            b = "morning" if h < 12 else "afternoon" if h < 17 else "evening"
            t = buckets.setdefault(b, [0, 0])
            t[1] += 1
            t[0] += r["status"] == "taken"
        weak = [(b, t[0] / t[1]) for b, t in buckets.items() if t[1] >= 3]
        if len(weak) > 1:
            b, r = min(weak, key=lambda w: w[1])
            out.append(f"{b.capitalize()} doses are the hardest to keep up with ({round(r * 100)}% confirmed).")
    cks = db.checkins(family_id, 7)
    if len(cks) >= 3 and sum(c["mood"] for c in cks[-3:]) / 3 <= 2.3:
        out.append("Mood has been low for the last few check-ins - worth a call or a mention to the doctor.")
    res = interactions.check_all([m["name"] for m in db.meds(family_id)])
    for w in res.warnings:
        out.append("Safety check: " + w)
    if res.unrecognised:
        out.append("Safety check could not recognise: " + ", ".join(res.unrecognised) + ". Ask the pharmacist about these.")
    return out


# ---------------------------------------------------------------- AI availability
_model_cache = {}
_breaker = {"failures": 0, "open_until": 0.0, "last_error": ""}
_breaker_lock = threading.Lock()


def gemini_configured():
    return bool(config.get("GEMINI_API_KEY"))


def ai_status(family_id=None):
    """(available, reason). Reason is shown to the user when unavailable."""
    if not config.get_bool("AI_ENABLED", True):
        return False, "The AI assistant is switched off on this server."
    if not gemini_configured():
        return False, "No Gemini API key is configured."
    if config.is_production() and not config.get_bool("GEMINI_PAID_TIER"):
        return False, ("The AI assistant is off: the server is not on a paid Gemini plan, and Google asks that "
                       "personal or health data is not sent to the free tier.")
    if family_id is not None and not db.family_settings(family_id)["ai_enabled"]:
        return False, "The AI assistant is off for this care circle. The elder or a caretaker can turn it on in Settings."
    if time.time() < _breaker["open_until"]:
        return False, ("The AI service failed several times, so it is paused for a few minutes. Last error: "
                       + (_breaker["last_error"] or "unknown"))
    return True, ""


def _base():
    return config.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")


def _call(path, payload=None, timeout=20):
    req = urllib.request.Request(  # nosec B310 - fixed https base URL
        _base() + path, data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": config.get("GEMINI_API_KEY")})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - fixed https base URL
        return json.loads(r.read())


DEFAULT_MODEL = "gemini-flash-latest"   # Google's alias for the current Flash model
_FLASH = re.compile(r"^gemini-(\d+)\.(\d+)-flash(-\d{3})?$")
_FLASH_LITE = re.compile(r"^gemini-(\d+)\.(\d+)-flash-lite(-\d{3})?$")
RETRYABLE = {500, 502, 503, 504}   # Google-side overload or outage: worth retrying or trying another model
BUSY_SECONDS = 300


def _ranked(names):
    """Models worth trying, best first: stable numbered Flash models newest first, then the 'latest' alias,
    then the newest stable Flash-Lite (lighter, often less busy)."""
    def ver(n, pat):
        m = pat.match(n)
        return (int(m.group(1)), int(m.group(2))) if m else None
    stable = sorted((n for n in names if ver(n, _FLASH)), key=lambda n: ver(n, _FLASH), reverse=True)
    lite = sorted((n for n in names if ver(n, _FLASH_LITE)), key=lambda n: ver(n, _FLASH_LITE), reverse=True)
    out = stable + ([DEFAULT_MODEL] if DEFAULT_MODEL in names else []) + lite[:1]
    return out or [DEFAULT_MODEL]


def _best_flash(names, avoid=()):
    """Newest stable numbered Flash model (e.g. gemini-3.6-flash), else the 'latest' alias."""
    for n in _ranked([n for n in names if n not in avoid]):
        if not _FLASH_LITE.match(n):
            return n
    return DEFAULT_MODEL if DEFAULT_MODEL not in avoid else None


def _discover():
    """Ask Google which models this key can use (once per process)."""
    if "candidates" not in _model_cache:
        try:
            models = _call("/v1beta/models?pageSize=200", timeout=10).get("models", [])
            names = [m["name"].split("/")[-1] for m in models if "generateContent" in m.get("supportedGenerationMethods", [])]
            _model_cache["candidates"] = _ranked(names)
        except Exception:
            _model_cache["candidates"] = [DEFAULT_MODEL]
    return _model_cache["candidates"]


def _pick_model():
    """GEMINI_MODEL if set and still available, otherwise the newest Flash model this key can use.
    Google shuts old models down (gemini-2.0-flash stopped on 1 June 2026), so a pinned name can go stale."""
    if "m" in _model_cache:
        return _model_cache["m"]
    unavailable = _model_cache.get("unavailable", set())
    pinned = config.get("GEMINI_MODEL")
    if pinned and pinned not in unavailable:
        _model_cache["m"] = pinned
        return pinned
    name = next((n for n in _discover() if n not in unavailable and not _FLASH_LITE.match(n)), DEFAULT_MODEL)
    _model_cache["m"] = name
    return name


def _models_to_try(limit=3):
    """The usual model first, then fallbacks; models that were overloaded in the last few minutes go last."""
    primary = _pick_model()
    unavailable = _model_cache.get("unavailable", set())
    order = [primary] + [n for n in _discover() if n != primary]
    order = [n for n in dict.fromkeys(order) if n not in unavailable]
    busy = _model_cache.setdefault("busy_until", {})
    now = time.time()
    order = [n for n in order if busy.get(n, 0) <= now] + [n for n in order if busy.get(n, 0) > now]
    return order[:limit]


def _google_error(e):
    """Google's own error message and reason from an HTTPError, with the key scrubbed."""
    msg, reason = "", ""
    try:
        err = json.loads(e.read().decode("utf-8", "replace")).get("error", {})
        msg = str(err.get("message", ""))
        reason = next((d.get("reason") for d in err.get("details", []) if isinstance(d, dict) and d.get("reason")), "") \
            or str(err.get("status", ""))
    except Exception:
        pass
    key = config.get("GEMINI_API_KEY")
    if key and len(key) >= 8:   # never echo the key back, but don't mangle text with a too-short "key"
        msg = msg.replace(key, "[key]")
    return msg[:300], reason


def _explain(code, msg, reason, model):
    if code == 404:
        return f"Google says the model '{model}' isn't available ({msg or 'not found'}). Remove GEMINI_MODEL or set a current model."
    if code in (400, 401, 403) and ("API_KEY" in reason or "TOKEN" in reason or "key" in msg.lower()
                                    or "credential" in msg.lower() or code == 401):
        hint = (" Some new keys starting with 'AQ.' are rejected this way: try a key from a Google Cloud project with "
                "billing enabled, or create the key again in Google AI Studio." if "TOKEN_TYPE" in reason or "OAuth" in msg else "")
        return f"Google rejected the API key (HTTP {code}{', ' + reason if reason else ''}): {msg}.{hint}"
    if code in RETRYABLE:
        return f"Google's Gemini service is busy or unavailable (HTTP {code}): {msg} This is usually temporary."
    if code == 429:
        return f"Gemini quota or rate limit reached (HTTP 429): {msg}. Wait a while, or check the plan's limits."
    return f"The AI service returned an error (HTTP {code}{', ' + reason if reason else ''}): {msg or 'no details'}."


def _record(ok):
    with _breaker_lock:
        if ok:
            _breaker["failures"] = 0
        else:
            _breaker["failures"] += 1
            if _breaker["failures"] >= 3:
                _breaker["open_until"] = time.time() + 300
                _breaker["failures"] = 0


TOTAL_SECONDS = 60      # the most a person waits for an answer, across retries and fallbacks
PER_MODEL_SECONDS = 25  # a model slower than this is probably overloaded: try the next one
_GEN = re.compile(r"^gemini-(\d+)\.")


def _thinking(model):
    """Quick answers: newer models 'think' at full depth unless told otherwise, which is slow.
    Gemini 3+ takes thinkingLevel, Gemini 2.5 takes thinkingBudget; sending the wrong one is an error."""
    m = _GEN.match(model)
    if m and int(m.group(1)) >= 3:
        return {"thinkingLevel": "low"}
    if m and int(m.group(1)) == 2 and "flash" in model:
        return {"thinkingBudget": 0}
    return None   # unknown (e.g. an alias): leave the model's default


def _payload(prompt, system, thinking):
    cfg = {"temperature": 0.2, "maxOutputTokens": 1024}   # thinking tokens count towards this
    if thinking:
        cfg["thinkingConfig"] = thinking
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": cfg}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    return payload


def _answer_text(data):
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise KeyError("empty answer")
    return text


def _is_timeout(e):
    return isinstance(e, TimeoutError) or (isinstance(e, urllib.error.URLError) and isinstance(e.reason, TimeoutError))


def generate(prompt, system=None):
    """Low-level call. Returns (text, error)."""
    if not gemini_configured():
        return None, "No Gemini API key is configured."
    if time.time() < _breaker["open_until"]:
        return None, "The AI service is paused for a few minutes after repeated failures."
    deadline = time.time() + TOTAL_SECONDS
    tried, last = [], ""
    for model in _models_to_try():
        thinking = _thinking(model)
        for attempt in (1, 2):
            remaining = deadline - time.time()
            if remaining < 5:
                break
            try:
                data = _call(f"/v1beta/models/{model}:generateContent", _payload(prompt, system, thinking),
                             timeout=min(PER_MODEL_SECONDS, remaining))
                text = _answer_text(data)
            except urllib.error.HTTPError as e:
                msg, reason = _google_error(e)
                last = _explain(e.code, msg, reason, model)
                if e.code == 400 and thinking and "think" in msg.lower() and attempt == 1:
                    thinking = None            # this model doesn't take that setting: ask again without it
                    continue
                if e.code == 404:              # retired or renamed: never use it again in this process
                    _model_cache.setdefault("unavailable", set()).add(model)
                    if _model_cache.get("m") == model:
                        _model_cache.pop("m")
                    log.warning("gemini model unavailable", extra={"model": model})
                    break
                if e.code in RETRYABLE:        # busy: one quick retry, then move to the next model
                    if attempt == 1 and not tried:
                        time.sleep(1.5)
                        continue
                    _model_cache.setdefault("busy_until", {})[model] = time.time() + BUSY_SECONDS
                    log.warning("gemini model busy", extra={"status": e.code, "model": model})
                    break
                _record(False)                 # key, quota or request problem: another model won't help
                log.warning("gemini http error", extra={"status": e.code, "reason": reason, "model": model})
                _breaker["last_error"] = last
                return None, last
            except (KeyError, IndexError, TypeError):
                _record(False)
                return None, "The AI service sent back an empty answer (it may have been blocked by Google's safety filters)."
            except Exception as e:
                if _is_timeout(e):             # too slow right now: treat like busy and try the next model
                    _model_cache.setdefault("busy_until", {})[model] = time.time() + BUSY_SECONDS
                    last = f"Gemini ({model}) took too long to answer."
                    log.warning("gemini timeout", extra={"model": model})
                    break
                _record(False)
                log.warning("gemini call failed", extra={"error": type(e).__name__})
                _breaker["last_error"] = f"The AI service could not be reached ({type(e).__name__})."
                return None, _breaker["last_error"]
            else:
                if model != _model_cache.get("m") and _model_cache.get("m") in _model_cache.get("unavailable", set()) | {None}:
                    _model_cache["m"] = model   # the usual model is gone: make this one the default
                _record(True)
                return text, ""
        tried.append(model)
        if deadline - time.time() < 5:
            break
    _record(False)
    if tried and all(_model_cache.get("busy_until", {}).get(m, 0) > time.time() for m in tried):
        last = (f"Google's Gemini service is busy or slow right now (tried {', '.join(tried)}). "
                "This is usually temporary: please try again in a minute or two.")
    _breaker["last_error"] = last or "No usable Gemini model was found for this key."
    return None, _breaker["last_error"]


# ---------------------------------------------------------------- input safety
URGENT = re.compile(r"\b(chest pain|can'?t breathe|cannot breathe|short(ness)? of breath|stroke|unconscious|"
                    r"fainted|collapsed|seizure|fits?\b|overdose|took (a )?double|too many (pills|tablets)|"
                    r"bleeding heavily|suicid|kill (my|him|her)self|not waking|face drooping|slurred speech)", re.I)
REDACTIONS = [  # order matters: phone numbers before long digit runs (ID / card numbers)
    ("email address", re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", re.I)),
    ("phone number", re.compile(r"(?<![\d+])(?:\+?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?!\d)|\+\d{1,3}[ -]?\d(?:[ -]?\d){6,13}\b")),
    ("ID or card number", re.compile(r"\b(?:\d[ -]?){11,18}\d\b")),
    ("PAN number", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
]


def redact(text, family_id=None):
    """Remove personal details from a question before it leaves the server. Returns (clean_text, removed_kinds)."""
    removed = []
    for label, pat in REDACTIONS:
        text, n = pat.subn(f"[{label} removed]", text)
        if n:
            removed.append(label)
    if family_id is not None:
        names = set()
        fam = db.get_family(family_id)
        for full in [fam["elder_name"], *[m["name"] for m in db.members(family_id)],
                     *[a["doctor"] for a in db.appts(family_id, upcoming_only=False)]]:
            for part in re.split(r"[\s()]+", full or ""):
                if len(part) >= 3 and part.lower() not in ("son", "daughter", "dr.", "mrs", "mr.", "the"):
                    names.add(part)
        for n in sorted(names, key=len, reverse=True):
            text, k = re.subn(r"\b" + re.escape(n) + r"\b", "[name]", text, flags=re.I)
            if k and "name" not in removed:
                removed.append("name")
    return text, removed


def _context(family_id):
    """Only what the assistant needs: no people, doctors, places or notes."""
    a = db.adherence(family_id, 7)
    lines = ["Medicines:"]
    for m in db.meds(family_id):
        lines.append(f"- {m['name']} {m['dosage']} at {m['times'].replace(',', ', ')}"
                     f"{' (' + m['instructions'] + ')' if m['instructions'] else ''}")
    lines.append(f"Doses confirmed taken in the last 7 days: {a['pct'] if a['pct'] is not None else 'no data'}%")
    lines.append(f"Doses never confirmed in the last 7 days: {a['unconfirmed']}")
    for name, (t, n) in a["per_med"].items():
        lines.append(f"- {name}: {t} of {n} confirmed")
    return "\n".join(lines)


SYSTEM = ("You help a family care for an elderly person. Use plain, warm, short sentences. "
          "Never diagnose. Never tell anyone to start, stop, skip, double or change the dose or timing of a medicine: "
          "say that only their doctor or pharmacist can decide that. If something sounds urgent, tell them to call "
          "a doctor or emergency services. You only know the medicine list and adherence numbers you are given.")


# ---------------------------------------------------------------- output safety
DOSE_CHANGE = re.compile(r"\b(increase|decrease|reduce|lower|raise|double|halve|stop taking|skip|discontinue|"
                         r"cut (it|the dose) in half|switch to)\b[^.\n]{0,60}\b(dose|dosage|tablet|pill|mg|medicine|medication)",
                         re.I)
AMOUNT = re.compile(r"\b(\d+(?:\.\d+)?)\s*(mg|mcg|µg|g|ml|iu|units?)\b", re.I)


def check_answer(text, family_id):
    """Return a list of warnings about an AI answer."""
    flags = []
    if DOSE_CHANGE.search(text):
        flags.append("This answer talks about changing a dose. Do not change any medicine without asking the doctor or pharmacist.")
    plan = " ".join(f"{m['dosage']} {m['name']}" for m in db.meds(family_id)).lower()
    known = {(float(v), u.lower()) for v, u in AMOUNT.findall(plan)}
    for v, u in AMOUNT.findall(text):
        if (float(v), u.lower()) not in known:
            flags.append(f"This answer mentions an amount ({v} {u}) that is not in the care plan. Do not act on it.")
            break
    return flags


def ask(question, family_id):
    """Answer a caregiver's question about the care plan. Returns dict(text, error, flags, removed, urgent)."""
    out = {"text": None, "error": "", "flags": [], "removed": [], "urgent": False}
    if URGENT.search(question or ""):
        out["urgent"] = True
        out["error"] = (f"This sounds urgent. Call the doctor now, or the emergency number "
                        f"({config.get('EMERGENCY_NUMBER', '112')}) if it is an emergency. CareCircle's assistant can't help with emergencies.")
        return out
    ok, why = ai_status(family_id)
    if not ok:
        out["error"] = why
        return out
    clean, out["removed"] = redact(question[:1000], family_id)
    text, err = generate(f"Care plan data:\n{_context(family_id)}\n\nQuestion: {clean}", system=SYSTEM)
    if text:
        out["text"], out["flags"] = text, check_answer(text, family_id)
    out["error"] = err
    return out


def explain_medicines(family_id):
    """Simple explanation for the elder. Falls back to the plain schedule. Returns dict like ask()."""
    meds = db.meds(family_id)
    plain = "\n".join(f"- {m['name']} {m['dosage']}: {m['times'].replace(',', ', ')} {m['instructions']}".rstrip() for m in meds)
    out = {"text": plain or "No medicines yet.", "error": "", "flags": [], "removed": [], "urgent": False, "ai": False}
    if not meds:
        return out
    ok, why = ai_status(family_id)
    if not ok:
        out["error"] = why
        return out
    text, err = generate("Explain each medicine below in one or two simple sentences: what it is generally for and any "
                         "common tip (like taking it with food). End by reminding them to ask their doctor if unsure.\n\n"
                         + "\n".join(f"- {m['name']} {m['dosage']} {m['instructions']}" for m in meds), system=SYSTEM)
    if text:
        out.update(text=text, flags=check_answer(text, family_id), ai=True)
    out["error"] = err
    return out
