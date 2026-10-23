"""Version matcher: decides which node in version N+1 is 'the same logical node' as one in N.

Pure functions over light dataclasses so the strategy is unit-testable without a DB.

Passes, strongest evidence first (each only touches still-unmatched nodes):
  A exact            same section number AND same normalised title
  B title_under_parent  same title under a parent that was already matched (survives renumbering)
  C same_content     identical non-trivial body, unique on both sides (survives renaming)
  D fuzzy            0.6*title + 0.3*body + 0.1*parent-context similarity >= threshold (recorded)
Anything left is added (new side) / removed (old side).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from rapidfuzz import fuzz

from .textnorm import title_key


@dataclass(frozen=True)
class NodeRef:
    idx: int
    number: str
    title: str
    body: str
    body_hash: str
    parent: int | None  # idx inside the same list


@dataclass(frozen=True)
class Match:
    old_idx: int
    method: str
    score: float


def _sim(a: str, b: str) -> float:
    return fuzz.ratio(a, b) / 100.0


def match_nodes(old: list[NodeRef], new: list[NodeRef], fuzzy_threshold: float = 0.72) -> dict[int, Match]:
    res: dict[int, Match] = {}
    used: set[int] = set()

    def take(n: NodeRef, o: NodeRef, method: str, score: float):
        res[n.idx] = Match(o.idx, method, score)
        used.add(o.idx)

    # A -- exact number + title
    go, gn = defaultdict(list), defaultdict(list)
    for o in old:
        go[(o.number, title_key(o.title))].append(o)
    for n in new:
        gn[(n.number, title_key(n.title))].append(n)
    for key, ns in gn.items():
        os_ = go.get(key, [])
        unique = len(ns) == 1 and len(os_) == 1
        for n, o in zip(ns, os_):
            take(n, o, "exact" if unique else "exact_ordered", 1.0 if unique else 0.9)

    # B -- same title under an already-matched parent (document order => parents first)
    for n in new:
        if n.idx in res:
            continue
        if n.parent is None:
            old_parent = None
        elif n.parent in res:
            old_parent = res[n.parent].old_idx
        else:
            continue
        cands = [o for o in old if o.idx not in used and o.parent == old_parent and title_key(o.title) == title_key(n.title)]
        if len(cands) == 1:
            take(n, cands[0], "title_under_parent", 0.95)
        elif len(cands) > 1:
            take(n, cands[0], "title_under_parent_ambiguous", 0.8)

    # C -- identical, non-trivial body that is unique among the leftovers on both sides
    left_old = [o for o in old if o.idx not in used]
    left_new = [n for n in new if n.idx not in res]
    ho, hn = defaultdict(list), defaultdict(list)
    for o in left_old:
        if len(o.body) >= 40:
            ho[o.body_hash].append(o)
    for n in left_new:
        if len(n.body) >= 40:
            hn[n.body_hash].append(n)
    for h, ns in hn.items():
        if len(ns) == 1 and len(ho.get(h, [])) == 1:
            take(ns[0], ho[h][0], "same_content", 0.9)

    # D -- fuzzy, greedy by best score
    left_old = [o for o in old if o.idx not in used]
    left_new = [n for n in new if n.idx not in res]
    pairs = []
    for n in left_new:
        for o in left_old:
            tr = _sim(title_key(n.title), title_key(o.title))
            if tr < 0.5:
                continue
            br = 0.5 if (not n.body and not o.body) else _sim(n.body[:2000], o.body[:2000])
            if n.parent is None and o.parent is None:
                ctx = 1.0
            else:
                ctx = 1.0 if (n.parent in res and res[n.parent].old_idx == o.parent) else 0.0
            pairs.append((0.6 * tr + 0.3 * br + 0.1 * ctx, n, o))
    pairs.sort(key=lambda p: -p[0])
    for score, n, o in pairs:
        if score < fuzzy_threshold:
            break
        if n.idx in res or o.idx in used:
            continue
        take(n, o, "fuzzy", round(score, 3))
    return res
