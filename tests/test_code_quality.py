"""Guards for the code's own conventions, so they don't drift: every module and function is documented and
fully type-annotated (mypy checks the annotations in CI), functions stay short, and there are no silent
`except: pass` blocks."""
import ast
import glob
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = sorted(glob.glob(os.path.join(ROOT, "*.py")))
MAX_FUNCTION_LINES = 60
MAX_LINE = 130


def functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


@pytest.fixture(params=MODULES, ids=os.path.basename)
def module(request):
    with open(request.param, encoding="utf-8") as f:
        src = f.read()
    return request.param, src, ast.parse(src)


def test_module_and_every_function_has_a_docstring(module):
    path, _, tree = module
    assert ast.get_docstring(tree), f"{path} has no module docstring"
    missing = [f"{n.name}:{n.lineno}" for n in functions(tree) if not ast.get_docstring(n)]
    assert not missing, f"{os.path.basename(path)}: functions without a docstring: {missing}"


def test_every_function_is_fully_annotated(module):
    path, _, tree = module
    missing = []
    for n in functions(tree):
        a = n.args
        params = [p for p in a.posonlyargs + a.args + a.kwonlyargs if p.arg not in ("self", "cls")]
        params += [p for p in (a.vararg, a.kwarg) if p]
        if n.returns is None or any(p.annotation is None for p in params):
            missing.append(f"{n.name}:{n.lineno}")
    assert not missing, f"{os.path.basename(path)}: functions without full type hints: {missing}"


def test_functions_stay_short(module):
    path, _, tree = module
    long = [f"{n.name} ({n.end_lineno - n.lineno + 1} lines)" for n in functions(tree)
            if n.end_lineno - n.lineno + 1 > MAX_FUNCTION_LINES]
    assert not long, f"{os.path.basename(path)}: split these up: {long}"


def test_lines_are_readable(module):
    path, src, _ = module
    long = [i for i, line in enumerate(src.splitlines(), 1) if len(line) > MAX_LINE]
    assert not long, f"{os.path.basename(path)}: lines over {MAX_LINE} characters: {long}"


def test_no_silently_swallowed_errors(module):
    path, _, tree = module
    silent = [h.lineno for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler)
              and (h.type is None or (len(h.body) == 1 and isinstance(h.body[0], ast.Pass)))]
    assert not silent, f"{os.path.basename(path)}: bare or silent except at lines {silent} (use contextlib.suppress and say why)"
