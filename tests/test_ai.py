"""Issues 46-51: AI consent, data minimisation, redaction, output checks, failure handling."""
import json
from datetime import timedelta

import pytest

import clock
import db
import insights
import services


@pytest.fixture
def ai(circle, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    services.update_settings(circle["caretaker"], ai_enabled=True)
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "with food", ["08:00"], clock.today())
    services.add_appointment(circle["caretaker"], "Dr Iyer", "Cardio", clock.now() + timedelta(days=2), "Apollo Clinic", "")
    sent = []

    def fake_call(path, payload=None, timeout=20):
        sent.append(payload)
        return {"candidates": [{"content": {"parts": [{"text": fake_call.reply}]}}]}
    fake_call.reply = "Try linking the dose to breakfast."
    monkeypatch.setattr(insights, "_call", fake_call)
    return circle, sent, fake_call


def test_off_until_the_family_opts_in(circle, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ok, why = insights.ai_status(circle["fid"])
    assert not ok and "off for this care circle" in why


def test_production_needs_a_paid_tier(ai, monkeypatch):
    circle, sent, _ = ai
    monkeypatch.setenv("APP_ENV", "production")
    assert not insights.ai_status(circle["fid"])[0]
    monkeypatch.setenv("GEMINI_PAID_TIER", "true")
    assert insights.ai_status(circle["fid"])[0]


def test_prompt_contains_no_names_places_or_contacts(ai):
    circle, sent, _ = ai
    q = "Asha forgets her pills. Call Ravi on +91 98765 43210 or ravi@mail.test, Aadhaar 1234 5678 9012. Dr Iyer said?"
    res = insights.ask(q, circle["fid"])
    assert res["text"] and set(res["removed"]) >= {"name", "phone number", "email address", "ID or card number"}
    prompt = json.dumps(sent[-1])
    for leaked in ("Asha", "Sharma", "Ravi", "Meera", "Iyer", "Apollo", "98765", "ravi@", "1234 5678", "Test Family"):
        assert leaked not in prompt, leaked
    assert "Metformin" in prompt


def test_urgent_questions_are_not_sent(ai):
    circle, sent, _ = ai
    res = insights.ask("She has chest pain and fainted", circle["fid"])
    assert res["urgent"] and "112" in res["error"] and not sent


def test_dose_change_advice_is_flagged(ai):
    circle, _, fake = ai
    fake.reply = "You could double the dose of Metformin to 1000 mg."
    flags = insights.ask("What should we do?", circle["fid"])["flags"]
    assert any("changing a dose" in f for f in flags) and any("1000 mg" in f for f in flags)


def test_amounts_in_the_plan_are_not_flagged(ai):
    circle, _, fake = ai
    fake.reply = "Metformin 500 mg is usually taken with food."
    assert insights.ask("Tell me about it", circle["fid"])["flags"] == []


def test_failures_trip_the_circuit_breaker(ai, monkeypatch):
    circle, _, _ = ai

    def down(*a, **k):
        raise TimeoutError()
    monkeypatch.setattr(insights, "_call", down)
    for _ in range(3):
        assert insights.ask("hello", circle["fid"])["error"]
    ok, why = insights.ai_status(circle["fid"])
    assert not ok and "paused" in why and "busy or slow" in why


def test_explain_falls_back_to_plain_schedule(circle):
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "", ["08:00"], clock.today())
    res = insights.explain_medicines(circle["fid"])
    assert not res["ai"] and "Metformin 500 mg" in res["text"] and res["error"]


def test_summary_for_email_has_no_medicine_names(circle):
    services.add_medicine(circle["caretaker"], "Metformin", "500 mg", "", ["08:00"], clock.today())
    assert "Metformin" not in insights.daily_summary(circle["fid"], full=False)
    assert "Metformin" in insights.daily_summary(circle["fid"], full=True)


def _http_error(code, message, reason="", status=""):
    import io
    import urllib.error
    body = {"error": {"code": code, "message": message, "status": status,
                      "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}] if reason else []}}
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(json.dumps(body).encode()))


def test_retired_pinned_model_falls_back_to_a_current_one(circle, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-flash")
    calls = []

    def fake(path, payload=None, timeout=20):
        calls.append(path)
        if "gemini-2.0-flash:" in path:
            raise _http_error(404, "models/gemini-2.0-flash is not found for API version v1beta", status="NOT_FOUND")
        if path.startswith("/v1beta/models?"):
            return {"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]}
                               for n in ("gemini-2.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.7-flash-lite",
                                         "gemini-3.8-flash-preview", "gemini-flash-latest")]}
        return {"candidates": [{"content": {"parts": [{"text": "ready"}]}}]}
    monkeypatch.setattr(insights, "_call", fake)
    assert insights.generate("hi") == ("ready", "")
    assert calls[-1] == "/v1beta/models/gemini-3.7-flash:generateContent"
    assert insights.generate("again")[0] == "ready" and calls[-1].endswith("gemini-3.7-flash:generateContent")
    assert insights._breaker["failures"] == 0


def test_rejected_aq_key_explains_what_google_said(circle, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.secret-value")

    def fake(path, payload=None, timeout=20):
        raise _http_error(401, "Request had invalid authentication credentials. Expected OAuth 2 access token "
                               "(key AQ.secret-value)", reason="ACCESS_TOKEN_TYPE_UNSUPPORTED", status="UNAUTHENTICATED")
    monkeypatch.setattr(insights, "_call", fake)
    text, err = insights.generate("hi")
    assert text is None and "ACCESS_TOKEN_TYPE_UNSUPPORTED" in err and "AQ." in err
    assert "secret-value" not in err


def test_quota_error_is_explained(circle, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(insights, "_call", lambda *a, **k: (_ for _ in ()).throw(
        _http_error(429, "Resource has been exhausted", status="RESOURCE_EXHAUSTED")))
    assert "quota" in insights.generate("hi")[1]


def test_best_flash_ignores_lite_preview_and_image_models():
    names = ["gemini-3.7-flash-lite", "gemini-3.8-flash-preview", "gemini-3.1-flash-image", "gemini-3.6-flash",
             "gemini-2.5-flash", "gemini-flash-latest"]
    assert insights._best_flash(names) == "gemini-3.6-flash"
    assert insights._best_flash(["gemini-flash-latest"]) == "gemini-flash-latest"


MODELS = {"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]}
                     for n in ("gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.7-flash-lite", "gemini-flash-latest")]}


def _busy_api(monkeypatch, busy):
    calls = []

    def fake(path, payload=None, timeout=20):
        if path.startswith("/v1beta/models?"):
            return MODELS
        model = path.split("/")[3].split(":")[0]
        calls.append(model)
        if model in busy:
            raise _http_error(503, "This model is currently experiencing high demand. Spikes in demand are usually "
                                   "temporary. Please try again later.", status="UNAVAILABLE")
        return {"candidates": [{"content": {"parts": [{"text": f"answer from {model}"}]}}]}
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(insights, "_call", fake)
    monkeypatch.setattr(insights.time, "sleep", lambda s: None)
    return calls


def test_busy_model_is_retried_then_another_model_answers(circle, monkeypatch):
    calls = _busy_api(monkeypatch, busy={"gemini-3.7-flash"})
    assert insights.generate("hi") == ("answer from gemini-3.6-flash", "")
    assert calls == ["gemini-3.7-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
    assert insights._breaker["failures"] == 0
    calls.clear()
    assert insights.generate("again")[0] == "answer from gemini-3.6-flash"
    assert calls == ["gemini-3.6-flash"]           # the busy model is skipped for a few minutes
    assert insights._pick_model() == "gemini-3.7-flash"   # but stays the usual choice


def test_busy_for_one_retry_only(circle, monkeypatch):
    calls = _busy_api(monkeypatch, busy=set())
    state = {"n": 0}
    real = insights._call

    def flaky(path, payload=None, timeout=20):
        if not path.startswith("/v1beta/models?") and state["n"] == 0:
            state["n"] += 1
            raise _http_error(503, "high demand", status="UNAVAILABLE")
        return real(path, payload, timeout)
    monkeypatch.setattr(insights, "_call", flaky)
    assert insights.generate("hi") == ("answer from gemini-3.7-flash", "")


def test_everything_busy_gives_a_clear_message(circle, monkeypatch):
    calls = _busy_api(monkeypatch, busy={"gemini-3.7-flash", "gemini-3.6-flash", "gemini-flash-latest", "gemini-3.7-flash-lite"})
    text, err = insights.generate("hi")
    assert text is None and "busy or slow right now" in err and "gemini-3.7-flash" in err and "try again" in err
    assert len(calls) == 4                         # one retry on the first model, then two fallbacks; bounded


def test_quick_thinking_settings_match_each_generation():
    assert insights._thinking("gemini-3.7-flash") == {"thinkingLevel": "low"}
    assert insights._thinking("gemini-3.7-flash-lite") == {"thinkingLevel": "low"}
    assert insights._thinking("gemini-2.5-flash") == {"thinkingBudget": 0}
    assert insights._thinking("gemini-flash-latest") is None


def test_slow_model_falls_back_and_answer_ignores_thoughts(circle, monkeypatch):
    payloads = {}

    def fake(path, payload=None, timeout=20):
        if path.startswith("/v1beta/models?"):
            return MODELS
        model = path.split("/")[3].split(":")[0]
        payloads[model] = (payload, timeout)
        if model == "gemini-3.7-flash":
            raise TimeoutError("read timed out")
        return {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True},
                                                      {"text": "Try the evening dose with dinner."}]}}]}
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(insights, "_call", fake)
    assert insights.generate("hi") == ("Try the evening dose with dinner.", "")
    assert payloads["gemini-3.7-flash"][0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert payloads["gemini-3.7-flash"][1] <= insights.PER_MODEL_SECONDS
    assert insights._model_cache["busy_until"]["gemini-3.7-flash"] > 0


def test_model_that_rejects_the_thinking_setting_is_asked_again_without_it(circle, monkeypatch):
    seen = []

    def fake(path, payload=None, timeout=20):
        if path.startswith("/v1beta/models?"):
            return MODELS
        seen.append("thinkingConfig" in payload["generationConfig"])
        if seen[-1]:
            raise _http_error(400, "Thinking level is not supported for this model.", status="INVALID_ARGUMENT")
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(insights, "_call", fake)
    assert insights.generate("hi") == ("ok", "")
    assert seen == [True, False]
