"""Retrieval-time staleness: does a stored generation still reflect the CURRENT document?

Mechanism: every generation stores a snapshot (logical node id, content_hash, title, body, version)
of exactly the text the LLM saw. At retrieval we look up the same logical node in the target version:
  hash equal                       -> fresh
  hash differs                     -> modified  (+ diff_summary: critical | review)
  node missing in target version   -> removed   (generation is orphaned)
  new node under a selected root   -> added     (generation never saw it; severity review)
Per test case we evaluate only the sources it cites; the generation-level verdict is the worst
over all sources. Cross-references (e.g. 3.3 -> 4.2) produce informational warnings only.
Honest limits: see CAVEATS and APPROACH.md.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import repo
from .analysis import diff_summary
from .models import NodeVersion

CAVEATS = [
    "Staleness is hash-based per cited source; numeric/error-code/normative-word changes are 'critical', other edits 'review'.",
    "A test case is judged only on the sources it cites; if the model mis-cited, fresh may be wrong (see unsupported_numbers).",
    "Implicit dependencies between sections without a textual cross-reference are not detected.",
    "Synonym polarity flips and reworded-but-equivalent text are not distinguished from real changes beyond the tiers above.",
]
_SEV = {"none": 0, "review": 1, "critical": 2}
_ST = {"fresh": 0, "added": 1, "modified": 1, "removed": 2}


def _max_sev(a: str, b: str) -> str:
    return a if _SEV[a] >= _SEV[b] else b


def evaluate_generation(s: Session, gen: dict, target_version: int | None = None) -> dict:
    doc_id = gen["document_id"]
    tv = repo.get_version(s, doc_id, target_version) if target_version else repo.latest_version(s, doc_id)
    if tv is None:
        raise KeyError("target version not found")
    per_node: dict[str, dict] = {}
    snap_ids = {x["logical_node_id"] for x in gen["source_snapshot"]}
    warnings: list[dict] = []

    for snap in gen["source_snapshot"]:
        cur = repo.node_in_version(s, snap["logical_node_id"], tv.id)
        base = {"node_id": snap["logical_node_id"], "section_number_at_generation": snap["section_number"],
                "title_at_generation": snap["title"], "generated_from_version": snap["version_number"],
                "content_hash_at_generation": snap["content_hash"]}
        if cur is None:
            per_node[snap["logical_node_id"]] = {**base, "status": "removed", "severity": "critical",
                                                  "reasons": ["node_missing_in_target_version"]}
            continue
        base.update({"current_section_number": cur.section_number, "current_content_hash": cur.content_hash})
        if cur.content_hash == snap["content_hash"]:
            per_node[snap["logical_node_id"]] = {**base, "status": "fresh", "severity": "none", "reasons": []}
        else:
            d = diff_summary(snap["title"], snap["body"], cur.title, cur.body)
            per_node[snap["logical_node_id"]] = {**base, "status": "modified", "severity": d["severity"],
                                                  "reasons": d["reasons"], "diff": d}
        # informational: referenced sections that changed since generation
        for ref in cur.refs or []:
            r_nv = next((n for n in repo.version_nodes(s, tv.id) if n.section_number == ref), None)
            if r_nv is None:
                continue
            was = repo.node_in_version(s, r_nv.logical_node_id, gen["source_version_id"])
            if was is not None and was.content_hash != r_nv.content_hash:
                warnings.append({"type": "referenced_section_changed", "node_id": snap["logical_node_id"],
                                 "references_section": ref, "referenced_node_id": r_nv.logical_node_id})

    added = []
    if gen.get("include_descendants"):
        for snap in gen["source_snapshot"]:
            if not snap["is_root"]:
                continue
            cur = repo.node_in_version(s, snap["logical_node_id"], tv.id)
            if cur is None:
                continue
            for d in repo.subtree(s, cur, include_self=False):
                if d.logical_node_id not in snap_ids and d.logical_node_id not in {a["node_id"] for a in added}:
                    added.append({"node_id": d.logical_node_id, "section_number": d.section_number, "title": d.title,
                                  "status": "added", "severity": "review", "reasons": ["new_subsection_under_selected_node"],
                                  "under_node_id": snap["logical_node_id"]})

    def verdict(items: list[dict]) -> dict:
        if not items:
            return {"status": "fresh", "severity": "none"}
        worst = max(items, key=lambda i: _ST[i["status"]])["status"]
        sev = "none"
        for i in items:
            sev = _max_sev(sev, i["severity"])
        status = {"fresh": "fresh", "removed": "orphaned"}.get(worst, "stale")
        return {"status": status, "severity": sev}

    nodes = list(per_node.values())
    test_cases = []
    for tc in gen["test_cases"]:
        srcs = [per_node[n] for n in tc["source_node_ids"] if n in per_node]
        v = verdict(srcs)
        test_cases.append({**tc, "staleness": {**v, "stale_sources": [x["node_id"] for x in srcs if x["status"] != "fresh"]}})
    overall = verdict(nodes + added)
    return {
        "checked_against_version": tv.number, "generated_from_version": gen["source_version_number"],
        **overall,
        "sources": nodes, "added_under_selection": added, "warnings": warnings, "caveats": CAVEATS,
        "test_cases": test_cases,
    }
