"""End-to-end demo of the versioning + staleness flow against a RUNNING server.

    LLM_PROVIDER=mock uvicorn app.main:create_app --factory      # offline, or set LLM_API_KEY for a real model
    python scripts/demo.py [http://localhost:8000]
"""
import json
import sys
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
DATA = Path(__file__).resolve().parent.parent / "data"
c = httpx.Client(base_url=BASE, timeout=120)


def show(title, obj):
    print(f"\n=== {title}")
    print(json.dumps(obj, indent=2, ensure_ascii=False)[:1800])


def ingest(path, **form):
    with open(path, "rb") as f:
        r = c.post("/documents/ingest", files={"file": (path.name, f, "application/pdf")}, data=form)
    r.raise_for_status()
    return r.json()


def sections(doc_id, version=None):
    out = {}
    def walk(ns):
        for n in ns:
            out[n["section_number"]] = n["node_id"]
            walk(n["children"])
    walk(c.get(f"/documents/{doc_id}/tree", params={"version": version} if version else {}).json()["tree"])
    return out


# 1. v1 ingestion (parser warnings are exposed, not hidden)
v1 = ingest(DATA / "ct200_manual.pdf")
doc = v1["document_id"]
show("1. v1 ingested", {"version": v1["version"], "nodes": v1["summary"]["nodes"],
                        "parser_issues": [f'{i["code"]}: {i["message"]}' for i in v1["parser_issues"]]})
ids = sections(doc)

# 2. a selection pinned to v1: inflation (3.2), error codes (4.2), whole "Data Management" section (5 + children)
sel = c.post("/selections", json={"name": "demo: inflation + errors + data",
                                  "node_ids": [ids["3.2"], ids["4.2"], ids["5"]]}).json()
show("2. selection (pinned to v1)", {"selection_id": sel["selection_id"], "version": sel["version"],
                                     "items": [(i["section_number"], i["title"]) for i in sel["items"]]})

# 3. generate; second submit is idempotent; force makes a new one
g = c.post(f"/selections/{sel['selection_id']}/generate")
g.raise_for_status()
g = g.json()
show("3. generation (fresh against v1)", {"id": g["generation_id"], "model": g["model"], "staleness": g["staleness"]["status"],
                                           "test_cases": [(t["id"], t["title"], t["source_node_ids"]) for t in g["test_cases"]]})
again = c.post(f"/selections/{sel['selection_id']}/generate").json()
print("\n   resubmit -> reused:", again.get("reused"), "| same id:", again["generation_id"] == g["generation_id"])

# 4. re-ingest v2 as a NEW VERSION of the same document
v2 = ingest(DATA / "ct200_manual_v2.pdf", document_id=doc)
s = v2["summary"]
show("4. v2 ingested as version 2", {"modified": [m["section"] for m in s["modified_nodes"]],
                                     "added": [a["section"] for a in s["added_nodes"]], "removed": s["removed_nodes"],
                                     "unchanged": s["unchanged"], "non_exact_matches_for_review": s["non_exact_matches"]})

# 5. node-level change + diff
ch = c.get(f"/nodes/{ids['3.2']}/changes").json()
show("5. /nodes/{3.2}/changes", {k: ch[k] for k in ("status", "from_version", "to_version", "diff")})
ch4 = c.get(f"/nodes/{ids['4']}/changes").json()
show("   /nodes/{4}/changes (H1 has no own body; children changed)",
     {"own_changed": ch4["own_changed"], "subtree_changed": ch4["subtree_changed"], "subtree": ch4["subtree"]})

# 6. staleness at retrieval time, by selection id and by node id
r = c.get(f"/selections/{sel['selection_id']}/generations").json()[0]
st = r["staleness"]
show("6. retrieval by selection: staleness", {"status": st["status"], "severity": st["severity"],
                                              "generated_from": st["generated_from_version"], "checked_against": st["checked_against_version"],
                                              "sources": [(x["section_number_at_generation"], x["status"], x["severity"], x["reasons"]) for x in st["sources"]],
                                              "added_under_selection": [(a["section_number"], a["title"]) for a in st["added_under_selection"]],
                                              "caveats": st["caveats"][:2]})
show("   per test case", [(t["id"], t["title"][:50], t["staleness"]["status"], t["staleness"]["severity"]) for t in r["test_cases"]])
n = c.get(f"/nodes/{ids['3.2']}/generations").json()[0]
show("   retrieval by node id (3.2)", {"focus": n["focus"], "overall": n["staleness"]["status"]})

# 7. old selection still resolves to the exact v1 text
pinned = c.get(f"/selections/{sel['selection_id']}").json()
t32 = next(i for i in pinned["items"] if i["section_number"] == "3.2")["body"]
print("\n7. pinned selection still says:", "'40 mmHg increments'" if "40 mmHg increments" in t32 else "!! changed")
print("   v2 node says              :", "'30 mmHg increments'" if "30 mmHg increments" in c.get(f"/nodes/{ids['3.2']}", params={"version": 2}).json()["body"] else "?")
