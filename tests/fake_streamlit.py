"""A small stand-in for Streamlit so app.py can be executed in tests without a browser or the real package.

It renders nothing; it records what the app showed (errors, warnings, text) and lets a test decide what each
widget returns and which buttons are clicked. It mimics the parts of Streamlit's behaviour the app relies on:
session_state persists between runs, st.rerun() restarts the script, clicks only count on the run they happen.
Use the real Streamlit AppTest tests (tests/test_app_streamlit.py) for full fidelity.
"""
import datetime as _dt
import sys
import types


class RerunException(BaseException):
    pass


class StopException(BaseException):
    pass


class SessionState(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k) from None

    def __setattr__(self, k, v):
        self[k] = v


class _Ctx:
    def __init__(self, st):
        self._st = st

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):  # columns/expanders/sidebar expose the same API as st
        return getattr(self._st, name)


class FakeStreamlit(types.ModuleType):
    def __init__(self):
        super().__init__("streamlit")
        self.session_state = SessionState()
        self.inputs, self.clicks, self.log = {}, set(), []
        self.secrets = {}
        self.context = types.SimpleNamespace(ip_address=None, headers={})
        self.sidebar = _Ctx(self)

    # ---- output
    def _out(self, kind, *args, **kw):
        self.log.append((kind, " ".join(str(a) for a in args)))

    def __getattr__(self, name):
        if name in ("title", "header", "subheader", "markdown", "write", "caption", "text", "code", "error", "warning",
                    "info", "success", "metric", "progress", "bar_chart", "line_chart", "divider", "set_page_config",
                    "json", "dataframe", "table"):
            return lambda *a, **k: self._out(name, *a, **k)
        raise AttributeError(name)

    def cache_resource(self, fn):
        cache = {}

        def wrapper(*a):
            if "v" not in cache:
                cache["v"] = fn(*a)
            return cache["v"]
        return wrapper

    def rerun(self):
        raise RerunException()

    def stop(self):
        raise StopException()

    # ---- layout
    def tabs(self, names):
        return [_Ctx(self) for _ in names]

    def columns(self, spec, **kw):
        n = spec if isinstance(spec, int) else len(spec)
        return [_Ctx(self) for _ in range(n)]

    def expander(self, *a, **k):
        return _Ctx(self)

    def form(self, *a, **k):
        return _Ctx(self)

    def spinner(self, *a, **k):
        return _Ctx(self)

    def container(self, *a, **k):
        return _Ctx(self)

    # ---- widgets
    def _value(self, label, key, default):
        k = key or label
        if k in self.inputs:
            v = self.inputs[k]
        elif key and key in self.session_state:
            v = self.session_state[key]
        else:
            v = default
        if key:
            self.session_state[key] = v
        return v

    def _clicked(self, label, key):
        hit = (key in self.clicks) if key else (label in self.clicks)
        self.log.append(("button", f"{key or label}{' (clicked)' if hit else ''}"))
        self.log.append(("label", label))
        return hit

    def button(self, label, key=None, **k):
        return self._clicked(label, key) and not k.get("disabled")

    def form_submit_button(self, label="Submit", key=None, **k):
        return self._clicked(label, key)

    def download_button(self, label, data, key=None, **k):
        self.log.append(("download", f"{key or label}:{len(data)}"))
        return False

    def text_input(self, label, value="", key=None, **k):
        return self._value(label, key, value or "")

    def text_area(self, label, value="", key=None, **k):
        return self._value(label, key, value or "")

    def number_input(self, label, min_value=None, max_value=None, value=None, key=None, **k):
        return self._value(label, key, value if value is not None else min_value)

    def date_input(self, label, value=_dt.date.today(), key=None, **k):
        return self._value(label, key, value)

    def time_input(self, label, value=None, key=None, **k):
        return self._value(label, key, value)

    def selectbox(self, label, options, index=0, key=None, format_func=str, **k):
        options = list(options)
        return self._value(label, key, options[index] if options else None)

    def radio(self, label, options, index=0, key=None, format_func=str, **k):
        options = list(options)
        return self._value(label, key, options[index] if options else None)

    def multiselect(self, label, options, default=None, key=None, format_func=str, **k):
        return self._value(label, key, list(default or []))

    def checkbox(self, label, value=False, key=None, **k):
        return self._value(label, key, value)

    def toggle(self, label, value=False, key=None, **k):
        return self._value(label, key, value)


def install():
    st = FakeStreamlit()
    comps = types.ModuleType("streamlit.components")
    v1 = types.ModuleType("streamlit.components.v1")
    v1.html = lambda *a, **k: st.log.append(("html", a[0] if a else ""))
    comps.v1 = v1
    st.components = comps
    sys.modules["streamlit"] = st
    sys.modules["streamlit.components"] = comps
    sys.modules["streamlit.components.v1"] = v1
    return st


def run(st, path, inputs=None, clicks=(), max_reruns=5):
    """Execute the app like one user interaction: clicks apply to the first run only, then reruns are followed."""
    st.inputs = dict(inputs or {})
    st.clicks = set(clicks)
    st.log = []
    with open(path, encoding="utf-8") as f:
        code = compile(f.read(), path, "exec")
    for _ in range(max_reruns):
        try:
            exec(code, {"__name__": "__main__", "__file__": path})
            return st.log
        except RerunException:
            st.clicks = set()
            st.inputs = {}
            continue
        except StopException:
            return st.log
    raise AssertionError("too many reruns")
