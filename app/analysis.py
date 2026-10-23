"""Change classification. A hash says THAT text changed; this says WHAT kind of change.

Tiers (deliberately not all equal -- a changed threshold is not a typo fix):
  critical  numeric facts changed, error-code set changed, or normative/negation words changed
  review    text changed but none of the above (could still be semantically important)
Known blind spots are listed in APPROACH.md (synonym polarity flips, units spelled out, ...).
"""
from __future__ import annotations

import difflib
import re
from collections import Counter

from .textnorm import canonical

NUM_RE = re.compile(
    r"(?<![\w.])[<>≥≤±]?\d+(?:\.\d+)?(?:\s?(?:mmHg|bpm|cm|%|V|seconds?|s|minutes?|years?|hours?))?(?!\w)"
)
CODE_RE = re.compile(r"\bE\d{1,2}\b")
MODAL_RE = re.compile(
    r"\b(shall|must|should|may|can|cannot|not|never|only|always|except|no|without|unless)\b|n't", re.I
)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n")


def numeric_facts(text: str) -> Counter:
    return Counter(re.sub(r"\s+", "", m.group(0)).lower() for m in NUM_RE.finditer(text))


def error_codes(text: str) -> Counter:
    return Counter(CODE_RE.findall(text))


def modal_words(text: str) -> Counter:
    return Counter(m.group(0).lower() for m in MODAL_RE.finditer(text))


def _delta(a: Counter, b: Counter) -> tuple[list, list]:
    return sorted((a - b).elements()), sorted((b - a).elements())


def _sentences(s: str) -> list[str]:
    return [x.strip(" |-") for x in SENT_SPLIT.split(s) if x.strip(" |-")]


def diff_summary(old_title: str, old_body: str, new_title: str, new_body: str, max_items: int = 5) -> dict:
    ot, nt = canonical(old_title), canonical(new_title)
    ob, nb = canonical(old_body), canonical(new_body)
    o_all, n_all = ot + "\n" + ob, nt + "\n" + nb
    num_rm, num_add = _delta(numeric_facts(o_all), numeric_facts(n_all))
    code_rm, code_add = _delta(error_codes(o_all), error_codes(n_all))
    mod_rm, mod_add = _delta(modal_words(o_all), modal_words(n_all))
    reasons = []
    if num_rm or num_add:
        reasons.append("numeric_value_changed")
    if code_rm or code_add:
        reasons.append("error_code_set_changed")
    if mod_rm or mod_add:
        reasons.append("normative_wording_changed")
    if not reasons:
        reasons.append("text_changed")
    os_, ns_ = _sentences(ob), _sentences(nb)
    sm = difflib.SequenceMatcher(None, os_, ns_, autojunk=False)
    removed, added = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += os_[i1:i2]
        if tag in ("replace", "insert"):
            added += ns_[j1:j2]
    cut = lambda xs: [x[:220] for x in xs[:max_items]]
    return {
        "severity": "review" if reasons == ["text_changed"] else "critical",
        "reasons": reasons,
        "title_changed": ot != nt,
        "numbers": {"removed": num_rm, "added": num_add},
        "error_codes": {"removed": code_rm, "added": code_add},
        "normative_words": {"removed": mod_rm, "added": mod_add},
        "sentences_removed": cut(removed),
        "sentences_added": cut(added),
        "similarity": round(difflib.SequenceMatcher(None, ob, nb).ratio(), 3),
    }
