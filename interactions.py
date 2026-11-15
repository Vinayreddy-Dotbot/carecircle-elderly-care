"""Basic offline medicine-safety prompts. NOT medical advice and NOT a drug interaction database.

What it does
- Maps brand names and spellings to generic ingredients (data/interactions.json), including a few
  combination products, so "Ecosprin" is checked as aspirin and "Combiflam" as ibuprofen + paracetamol.
- Warns about a short list of well-known risky pairs, at the drug-class level where the message already was.
- Warns when two entries contain the same ingredient or two NSAIDs (possible double dosing).
- Reports every medicine it could NOT recognise, with a spelling suggestion where one is close, so a missing
  warning is never presented as "safe".

The list is small and has not been reviewed by a pharmacist (see meta.reviewed_by). A licensed source such as
DrugBank, First Databank or Medi-Span, or a pharmacist-curated import of the open DDInter dataset, is needed for
real coverage. NLM's free RxNav interaction API was discontinued in January 2024.
"""
import difflib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "interactions.json")
DISCLAIMER = ("CareCircle's check covers only a short list of well-known combinations. No warning does not mean "
              "a combination is safe. Ask the pharmacist or doctor to review the full medicine list.")


@dataclass
class CheckResult:
    warnings: list = field(default_factory=list)
    unrecognised: list = field(default_factory=list)
    suggestions: dict = field(default_factory=dict)

    def __iadd__(self, other):
        self.warnings += [w for w in other.warnings if w not in self.warnings]
        self.unrecognised += [u for u in other.unrecognised if u not in self.unrecognised]
        self.suggestions.update(other.suggestions)
        return self


@lru_cache(maxsize=1)
def _data():
    with open(DATA_PATH, encoding="utf-8") as f:
        d = json.load(f)
    alias = {}
    for ing, names in d["ingredients"].items():
        for n in [ing, *names]:
            alias[_norm(n)] = (ing,)
    for prod, ings in d["products"].items():
        alias[_norm(prod)] = tuple(ings)
    member_of = {}
    for cls, info in d["classes"].items():
        for ing in info["members"]:
            member_of.setdefault(ing, set()).add(cls)
    # longest names first so "isosorbide mononitrate" wins over shorter overlaps
    patterns = [(re.compile(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])"), v)
                for k, v in sorted(alias.items(), key=lambda kv: -len(kv[0]))]
    vocab = sorted({w for k in alias for w in k.split() if len(w) >= 4})
    return d, patterns, member_of, vocab


def _norm(text):
    t = (text or "").lower()
    t = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", t)   # "metformin500" -> "metformin 500"
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def ingredients(name):
    """Generic ingredients recognised in a medicine name (empty set if unknown)."""
    _, patterns, _, _ = _data()
    text, found = _norm(name), set()
    for pat, ings in patterns:
        if pat.search(text):
            found.update(ings)
            text = pat.sub(" ", text)
    return found


def _classes(ings):
    _, _, member_of, _ = _data()
    return {c for i in ings for c in member_of.get(i, ())}


def _matches(term, ings, classes):
    return term[6:] in classes if term.startswith("class:") else term in ings


def _suggest(name):
    _, _, _, vocab = _data()
    for word in _norm(name).split():
        if len(word) >= 5:
            hit = difflib.get_close_matches(word, vocab, n=1, cutoff=0.8)
            if hit and hit[0] != word:
                return hit[0]
    return None


def coverage():
    d = _data()[0]
    return {"ingredients": len(d["ingredients"]), "rules": len(d["rules"]), "reviewed_by": d["meta"]["reviewed_by"],
            "reviewed_on": d["meta"]["reviewed_on"]}


def _pair(a_name, b_name):
    d = _data()[0]
    ia, ib = ingredients(a_name), ingredients(b_name)
    ca, cb = _classes(ia), _classes(ib)
    out, groups = [], set()
    for r in d["rules"]:
        if r["group"] in groups:
            continue
        if (_matches(r["a"], ia, ca) and _matches(r["b"], ib, cb)) or (_matches(r["b"], ia, ca) and _matches(r["a"], ib, cb)):
            groups.add(r["group"])
            out.append(f"{a_name} and {b_name}: {r['message']}")
    shared = ia & ib
    if shared and _norm(a_name) != _norm(b_name):
        out.append(f"{a_name} and {b_name} both contain {', '.join(sorted(shared))} - check for double dosing.")
    elif not shared:
        for cls in d["duplicate_classes"]:
            if cls in ca and cls in cb:
                out.append(f"{a_name} and {b_name} are both {d['classes'][cls]['label']} - check for double dosing.")
    return out


def _unknown(names):
    res = CheckResult()
    for n in names:
        if not ingredients(n):
            res.unrecognised.append(n)
            s = _suggest(n)
            if s:
                res.suggestions[n] = s
    return res


def check(new_name, existing_names):
    """Warnings for adding `new_name` next to `existing_names`, plus anything that could not be checked."""
    res = CheckResult()
    for other in existing_names:
        res.warnings += [w for w in _pair(new_name, other) if w not in res.warnings]
    res += _unknown([new_name])
    return res


def check_all(names):
    res = CheckResult()
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            res.warnings += [w for w in _pair(a, b) if w not in res.warnings]
    res += _unknown(names)
    return res
