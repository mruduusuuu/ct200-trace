"""End-to-end: ingest v1 -> select -> generate -> re-ingest v2 -> staleness visible at retrieval."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import make_engine
from app.main import create_app
from app.store import JsonFileStore
from tests.conftest import V1, V2
from tests.helpers import ScriptedLLM, payload, tc


def make_client(tmp_path, llm, **kw):
    settings = Settings(database_url="sqlite://", json_store_dir=str(tmp_path), **kw)
    app = create_app(settings, llm=llm, store=JsonFileStore(str(tmp_path)), engine=make_engine("sqlite://"))
    return TestClient(app)


def upload(c, path, **form):
    with open(path, "rb") as f:
        return c.post("/documents/ingest", files={"file": (path.split("/")[-1], f, "application/pdf")}, data=form)


def sections(c, doc_id, version=None):
    t = c.get(f"/documents/{doc_id}/tree", params={"version": version} if version else {}).json()["tree"]
    out = {}
    def walk(ns):
        for n in ns:
            out[n["section_number"]] = n["node_id"]
            walk(n["children"])
    walk(t)
    return out


CASES = payload(
    tc("Inflation increments", ("S1",), "Device inflates in 40 mmHg increments up to 299 mmHg"),
    tc("E3 deflation timing", ("S2",), "Device auto-deflates within 2 seconds and shows E3"),
    tc("Storage capacity", ("S4",), "Device stores up to 100 readings per profile"),
    tc("Bluetooth no manual sync", ("S5",), "There is no manual sync trigger"),
)


@pytest.fixture()
def env(tmp_path):
    llm = ScriptedLLM([CASES, CASES])
    c = make_client(tmp_path, llm)
    r1 = upload(c, V1)
    assert r1.status_code == 201, r1.text
    doc_id = r1.json()["document_id"]
    return c, llm, doc_id


def test_full_versioning_and_staleness_flow(env):
    c, llm, doc = env
    ids = sections(c, doc)
    sel = c.post("/selections", json={"name": "alarms+ops", "node_ids": [ids["3.2"], ids["4.2"], ids["5"]]}).json()
    assert sel["version"] == 1 and [i["section_number"] for i in sel["items"]] == ["3.2", "4.2", "5", "5.1", "5.2"]
    assert [i["is_root"] for i in sel["items"]] == [True, True, True, False, False]

    g1 = c.post(f"/selections/{sel['selection_id']}/generate")
    assert g1.status_code == 201 and g1.json()["staleness"]["status"] == "fresh"

    # idempotency policy: same selection again -> same generation, no LLM call
    g1b = c.post(f"/selections/{sel['selection_id']}/generate")
    assert g1b.status_code == 200 and g1b.json()["reused"] and g1b.json()["generation_id"] == g1.json()["generation_id"]
    assert len(llm.calls) == 1
    # force -> new generation, old one marked superseded
    g2 = c.post(f"/selections/{sel['selection_id']}/generate", params={"force": True})
    assert g2.status_code == 201 and g2.json()["generation_id"] != g1.json()["generation_id"]
    listing = c.get(f"/selections/{sel['selection_id']}/generations").json()
    assert [g["superseded"] for g in listing] == [False, True]

    # ---- v1 -> v2
    r2 = upload(c, V2, document_id=doc)
    assert r2.status_code == 201 and r2.json()["version"] == 2
    s = r2.json()["summary"]
    assert {m["section"] for m in s["modified_nodes"]} == {"2.1.1.1", "3.2", "4.2", "4.3"}
    assert [a["section"] for a in s["added_nodes"]] == ["5.3"]
    assert upload(c, V2, document_id=doc).json()["created"] is False          # same file again: no version 3

    # staleness is visible via selection id ...
    gen = c.get(f"/selections/{sel['selection_id']}/generations").json()[0]
    st = gen["staleness"]
    assert st["status"] == "stale" and st["severity"] == "critical"
    assert (st["generated_from_version"], st["checked_against_version"]) == (1, 2)
    by = {x["section_number_at_generation"]: x for x in st["sources"]}
    assert by["3.2"]["status"] == "modified" and by["3.2"]["diff"]["numbers"] == {"removed": [], "added": ["30mmhg"]}
    assert by["4.2"]["status"] == "modified" and "error_code_set_changed" in by["4.2"]["reasons"]
    assert by["5.1"]["status"] == "fresh" and by["5.2"]["status"] == "fresh"
    assert [a["section_number"] for a in st["added_under_selection"]] == ["5.3"]
    # ... per test case: judged only on what it cites
    verdicts = {t["title"]: t["staleness"]["status"] for t in gen["test_cases"]}
    assert verdicts == {"Inflation increments": "stale", "E3 deflation timing": "stale",
                        "Storage capacity": "fresh", "Bluetooth no manual sync": "fresh"}
    # ... and via node id
    by_node = c.get(f"/nodes/{ids['3.2']}/generations").json()
    assert by_node and by_node[0]["focus"]["test_case_ids"] == ["tc_1"] and by_node[0]["focus"]["node_status"] == "modified"
    # checking against the ORIGINAL version stays fresh
    assert c.get(f"/selections/{sel['selection_id']}/generations", params={"against_version": 1}).json()[0]["staleness"]["status"] == "fresh"

    # selection is version-pinned: still resolves to the exact v1 text
    pinned = c.get(f"/selections/{sel['selection_id']}").json()
    assert "40 mmHg increments" in next(i for i in pinned["items"] if i["section_number"] == "3.2")["body"]
    assert c.get(f"/nodes/{ids['3.2']}", params={"version": 2}).json()["body"].count("30 mmHg increments") == 1

    # node change endpoints
    ch = c.get(f"/nodes/{ids['3.2']}/changes").json()
    assert ch["status"] == "modified" and ch["diff"]["severity"] == "critical" and len(ch["history"]) == 2
    parent = c.get(f"/nodes/{ids['4']}/changes").json()                     # H1 has no body of its own
    assert parent["own_changed"] is False and parent["subtree_changed"] is True
    assert {m["section_number"] for m in parent["subtree"]["modified"]} == {"4.2", "4.3"}
    assert c.get(f"/nodes/{ids['1.1']}/changes").json()["status"] == "unchanged"
    assert c.get(f"/nodes/{sections(c, doc, 2)['5.3']}/changes").json()["status"] == "added"


def test_browse_and_search(env):
    c, _, doc = env
    top = c.get(f"/documents/{doc}/sections").json()
    assert [s["section_number"] for s in top["sections"]] == [str(i) for i in range(1, 9)]
    ids = sections(c, doc)
    node = c.get(f"/nodes/{ids['2.1']}").json()
    assert node["children"][0]["section_number"] == "2.1.1.1" and node["content_hash"] and "Oscillometric" in node["body"]
    assert [b["section_number"] for b in node["breadcrumbs"]] == ["2", "2.1"]
    hits = c.get(f"/documents/{doc}/search", params={"q": "Error Codes", "scope": "heading"}).json()["results"]
    assert [h["section_number"] for h in hits] == ["4.2", "7.1"] and hits[0]["node_id"] != hits[1]["node_id"]
    assert any(h["section_number"] == "2.1" for h in c.get(f"/documents/{doc}/search", params={"q": "Specifications"}).json()["results"])
    assert c.get(f"/documents/{doc}/search", params={"q": "100%"}).json()["count"] == 0   # LIKE wildcards are escaped
    assert c.get("/nodes/n_doesnotexist").status_code == 404
    rep = c.get(f"/documents/{doc}/versions/1/report").json()
    assert {i["code"] for i in rep["issues"]} >= {"LEVEL_GAP", "OUT_OF_ORDER"}


def test_selection_validation(env):
    c, _, doc = env
    ids = sections(c, doc)
    assert c.post("/selections", json={"name": "x", "node_ids": ["n_nope"]}).status_code == 422
    assert c.post("/selections", json={"name": "x", "node_ids": [ids["4"]], "version": 9}).status_code == 404
    leaf = c.post("/selections", json={"name": "x", "node_ids": [ids["4"]], "include_descendants": False}).json()
    assert [i["section_number"] for i in leaf["items"]] == ["4"]


def test_llm_failure_is_502_stored_as_failed_and_does_not_block_retry(tmp_path):
    llm = ScriptedLLM(["garbage"] * 3 + [CASES])
    c = make_client(tmp_path, llm)
    doc = upload(c, V1).json()["document_id"]
    ids = sections(c, doc)
    sel = c.post("/selections", json={"name": "t", "node_ids": [ids["3.2"], ids["4.2"], ids["5.1"], ids["5.2"]]}).json()
    r = c.post(f"/selections/{sel['selection_id']}/generate")
    assert r.status_code == 502 and r.json()["failure_reason"] == "invalid_output_after_retries" and len(r.json()["attempts"]) == 3
    assert c.get(f"/selections/{sel['selection_id']}/generations").json() == []
    failed = c.get(f"/selections/{sel['selection_id']}/generations", params={"include_failed": True}).json()
    assert failed[0]["status"] == "failed" and failed[0]["test_cases"] == []
    assert c.get(f"/generations/{r.json()['generation_id']}").json()["attempts"][0]["raw"] == "garbage"


def test_no_llm_configured_is_503_and_oversized_selection_is_413(tmp_path):
    c = make_client(tmp_path, None, llm_provider="openai_compat", llm_api_key="")
    c.app.state.llm = None
    doc = upload(c, V1).json()["document_id"]
    ids = sections(c, doc)
    sel = c.post("/selections", json={"name": "t", "node_ids": [ids["1"]]}).json()
    assert c.post(f"/selections/{sel['selection_id']}/generate").status_code == 503
    c.app.state.llm = ScriptedLLM([])
    c.app.state.settings.max_source_chars = 100
    assert c.post(f"/selections/{sel['selection_id']}/generate").status_code == 413


def test_non_pdf_upload_rejected(tmp_path):
    c = make_client(tmp_path, None)
    r = c.post("/documents/ingest", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 422
