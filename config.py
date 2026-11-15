"""Settings and environment profiles.

Precedence for every key: process environment > .env file > Streamlit secrets (.streamlit/secrets.toml).

APP_ENV decides how strict the app is:
  development (default)  local machine. Sample family allowed. The Settings tab may write SMTP/Gemini keys to .env.
  demo                   public demo with sample data only. Sample family allowed. Secrets are read-only.
                         The default on Streamlit Community Cloud, where every app is public.
  production             real people. Demo features off, secrets read-only, startup refuses unsafe configuration.
An unrecognised APP_ENV value is treated as production (fail closed).
"""
from __future__ import annotations

import contextlib
import os
from typing import Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
DEFAULT_DB = os.path.join(BASE_DIR, "carecircle.db")
ENVIRONMENTS = ("development", "demo", "production")

# Keys the development-only Settings tab is allowed to write.
UI_MANAGED = ["SMTP_USER", "SMTP_PASS", "GEMINI_API_KEY"]

SECRETS_PATH = os.path.join(BASE_DIR, ".streamlit", "secrets.toml")
_loaded = False
_from_env_file: set[str] = set()
_secrets_cache: dict[str, Any] = {"stamp": None, "values": {}}
use_secrets_files = True  # tests switch this off so a developer's real secrets are never read


def _parse(path: str) -> dict[str, str]:
    """Read KEY=value lines from a .env file (missing file: empty)."""
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.split(" #", 1)[0] if not v.strip().startswith(("'", '"')) else v
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _load() -> None:
    """Load .env into the process environment once, without overriding real environment variables."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for k, v in _parse(ENV_PATH).items():
        if k not in os.environ:
            os.environ[k] = v
            _from_env_file.add(k)


def _load_toml(path: str) -> dict[str, Any]:
    """Parse a TOML file with tomllib, toml, or a minimal flat parser as a last resort."""
    with contextlib.suppress(ImportError):   # Python 3.11+
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    with contextlib.suppress(ImportError):   # installed with Streamlit
        import toml
        return dict(toml.load(path))
    # Last resort for a bare Python < 3.11: flat KEY = "value" / KEY = 123 lines, which is all CareCircle uses.
    out, section = {}, None
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                section = line
                continue
            if section or "=" not in line:
                continue
            k, v = (p.strip() for p in line.split("=", 1))
            if v[:1] in "\"'":
                q = v[0]
                end = v.find(q, 1)
                if end < 0:
                    raise ValueError(f"unterminated string on line {n}")
                out[k] = v[1:end]
            else:
                out[k] = v.split("#", 1)[0].strip()
    return out


def _project_secrets() -> dict[str, str]:
    """This app's own .streamlit/secrets.toml, read by absolute path.

    Streamlit only finds secrets.toml in the folder `streamlit run` is started from, so launching from another
    folder would silently lose DATA_KEY and the app would fall back to a different key. Reading the file
    directly makes the app, manage.py and worker.py see the same settings wherever they start."""
    try:
        mtime = os.path.getmtime(SECRETS_PATH)
    except OSError:
        return {}
    if _secrets_cache["stamp"] != (SECRETS_PATH, mtime):
        try:
            data = _load_toml(SECRETS_PATH)
        except Exception as e:
            raise RuntimeError(f"Can't read {SECRETS_PATH}: {e}") from None
        _secrets_cache.update(stamp=(SECRETS_PATH, mtime),
                              values={k: str(v) for k, v in data.items() if not isinstance(v, dict)})
    return _secrets_cache["values"]


def get(key: str, default: Any = "") -> Any:
    """A setting: environment, then .env, then this app's secrets.toml, then Streamlit secrets, then `default`."""
    _load()
    v = os.environ.get(key)
    if v:
        return v
    if not use_secrets_files:
        return default
    v = _project_secrets().get(key)
    if v:
        return v
    # Streamlit secrets (global ~/.streamlit or the launch folder); not available outside Streamlit
    with contextlib.suppress(Exception):
        import streamlit as st
        v = st.secrets.get(key)
        if v:
            return str(v)
    return default


def source(key: str) -> str:
    """Where a setting comes from, for diagnostics. Never returns the value."""
    _load()
    if os.environ.get(key):
        return ENV_PATH if key in _from_env_file else "an environment variable"
    if use_secrets_files and _project_secrets().get(key):
        return SECRETS_PATH
    return "Streamlit secrets" if get(key) else "not set"


def get_int(key: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """A whole-number setting, clamped to [lo, hi]; `default` if it isn't a number."""
    try:
        v = int(str(get(key, default)).strip())
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def get_bool(key: str, default: bool = False) -> bool:
    """A yes/no setting: 1, true, yes or on mean yes."""
    v = str(get(key, "")).strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def get_list(key: str, default: str = "") -> list[str]:
    """A comma-separated setting as a lower-case list."""
    return [p.strip().lower() for p in str(get(key, default)).split(",") if p.strip()]


# ---------------------------------------------------------------- profiles
def default_env() -> str:
    """APP_ENV when it isn't set: development on your own machine, but demo on Streamlit Community Cloud.

    A Community Cloud app is public, and development mode is only safe on a local machine: it shows sign-in codes on
    screen when email isn't set up, and lets the Settings tab write server keys."""
    return "demo" if on_community_cloud() else "development"


def env() -> str:
    """The environment profile: development, demo or production (anything unrecognised is production)."""
    v = str(get("APP_ENV", default_env())).strip().lower()
    return v if v in ENVIRONMENTS else "production"


def is_production() -> bool:
    """True when real people's data is at stake and every safety check applies."""
    return env() == "production"


def demo_features() -> bool:
    """Sample family, time simulation and other demo-only tools."""
    return env() in ("development", "demo")


def ui_can_save_secrets() -> bool:
    """Only a local development install may write credentials from the browser."""
    return env() == "development" and get_bool("ALLOW_UI_SECRETS", True)


def on_community_cloud() -> bool:
    """Streamlit Community Cloud runs apps from /mount/src/<repo>."""
    return BASE_DIR.startswith("/mount/src/")


def db_path() -> str:
    """Where the SQLite database lives (CARECIRCLE_DB, default next to the app)."""
    return get("CARECIRCLE_DB", DEFAULT_DB)


def mfa_roles() -> set[str]:
    """Roles that must use two-step sign-in (default: caretaker in production)."""
    default = "caretaker" if is_production() else ""
    return set(get_list("MFA_ROLES", default))


def scheduler_mode() -> str:
    """How reminders run: thread (inside the web app), external (worker.py) or off."""
    v = str(get("SCHEDULER_MODE", "thread")).strip().lower()
    return v if v in ("thread", "external", "off") else "thread"


def scheduler_interval() -> int:
    """Seconds between reminder passes (15 to 3600)."""
    return get_int("SCHEDULER_INTERVAL_SEC", 60, lo=15, hi=3600)


def smtp_configured() -> bool:
    """True when real emails can be sent."""
    sec = get("SMTP_SECURITY", "ssl").lower()
    if sec == "none":  # local test server
        return bool(get("SMTP_HOST"))
    return bool(get("SMTP_USER") and get("SMTP_PASS"))


# ---------------------------------------------------------------- validation
def validate() -> tuple[list[str], list[str]]:
    """Return (errors, warnings). In production any error stops the app from starting."""
    errors, warnings = [], []
    prod = is_production()
    raw_env = str(get("APP_ENV", default_env())).strip().lower()
    if raw_env not in ENVIRONMENTS:
        warnings.append(f"APP_ENV={raw_env!r} is not recognised, so production rules apply.")

    key = get("DATA_KEY")
    if key:
        try:
            import crypto
            crypto.parse_key(key)
        except Exception as e:
            errors.append(f"DATA_KEY is invalid: {e}")
    elif prod:
        errors.append("DATA_KEY is not set. Generate one with `python manage.py generate-key` and add it to your secrets.")
    else:
        warnings.append("DATA_KEY is not set, so a development key file next to the database is used.")
    if key and not prod and os.path.exists(db_path() + ".devkey"):
        warnings.append("DATA_KEY is set and a development key file also exists, so data created before DATA_KEY "
                        "was set is still read with the file. Run `python manage.py rotate-key`, then delete "
                        f"{db_path()}.devkey.")

    path = db_path()
    if prod and on_community_cloud() and not get_bool("ALLOW_EPHEMERAL_DB"):
        errors.append("This looks like Streamlit Community Cloud, which has no persistent disk: the SQLite database is "
                      "lost on every reboot or redeploy. Use APP_ENV=demo there, or deploy production on a host with a "
                      "persistent volume (see docs/DEPLOYMENT.md).")
    if prod:
        if not os.environ.get("CARECIRCLE_DB") and not _secret_set("CARECIRCLE_DB"):
            errors.append("CARECIRCLE_DB is not set. Point it at a persistent disk, not the app folder.")
        elif os.path.abspath(path).startswith(BASE_DIR + os.sep) and not get_bool("ALLOW_EPHEMERAL_DB"):
            errors.append("CARECIRCLE_DB is inside the app folder, which cloud hosts wipe on rebuild. "
                          "Use a persistent volume, or set ALLOW_EPHEMERAL_DB=true if you accept data loss.")
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(parent):
        errors.append(f"The database folder {parent} does not exist.")

    if prod and not smtp_configured():
        errors.append("SMTP is not configured. Production needs real email for reminders and sign-in codes.")
    elif not smtp_configured():
        warnings.append("SMTP is not configured, so emails are simulated.")

    if prod and get("GEMINI_API_KEY") and not get_bool("GEMINI_PAID_TIER"):
        warnings.append("GEMINI_API_KEY is set but GEMINI_PAID_TIER is not true. The AI assistant stays off in "
                        "production, because Google's terms say not to send personal data to the unpaid tier.")
    if prod and scheduler_mode() == "thread":
        warnings.append("SCHEDULER_MODE=thread runs reminders inside the web app. Run `python worker.py` as its own "
                        "process and set SCHEDULER_MODE=external for reliable reminders.")
    if prod and get("ALLOW_UI_SECRETS"):
        warnings.append("ALLOW_UI_SECRETS has no effect in production.")
    return errors, warnings


def _secret_set(key: str) -> bool:
    """True if `key` is set in a secrets file (not the environment)."""
    if not use_secrets_files:
        return False
    if _project_secrets().get(key):
        return True
    try:
        import streamlit as st
        return bool(st.secrets.get(key))
    except Exception:
        return False


# ---------------------------------------------------------------- development-only writes
def save(values: dict[str, str]) -> None:
    """Persist SMTP/Gemini settings to .env. Development only."""
    if not ui_can_save_secrets():
        raise PermissionError("Settings can only be saved from the browser in a local development install. "
                              "Use environment variables or Streamlit secrets instead.")
    _load()
    current = _parse(ENV_PATH)
    for k, v in values.items():
        if k not in UI_MANAGED:
            raise ValueError(f"{k} cannot be set from the Settings tab")
        v = (v or "").strip()
        if any(ch in v for ch in "\r\n"):
            raise ValueError(f"{k} must be a single line")
        if v:
            current[k] = v
            os.environ[k] = v
        else:
            current.pop(k, None)
            os.environ.pop(k, None)
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# CareCircle development settings - do not commit this file\n")
        for k, v in current.items():
            f.write(f"{k}={v}\n")
