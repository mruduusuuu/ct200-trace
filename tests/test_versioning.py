import pytest
from sqlalchemy import select

from app import repo
from app.analysis import diff_summary
from app.db import Base, make_engine, make_session_factory
from app.match import NodeRef, match_nodes
from app.models import NodeVersion
from app.textnorm import body_hash
from app.versioning import ingest_pdf
from tests.conftest import V1, V2


@pytest.fixture()
def session():
    eng = make_engine("sqlite://")
    Base.metadata.create_all(eng)
    with make_session_factory(eng)() as s:
        yield s


def sec(s, version_no, doc_id, number):
    v = repo.get_version(s, doc_id, version_no)
    return s.scalars(select(NodeVersion).where(NodeVersion.version_id == v.id, NodeVersion.section_number == number)).one()


def test_v1_to_v2_flags_exactly_the_edited_sections(session):
    r1 = ingest_pdf(session, V1, "v1.pdf")
    r2 = ingest_pdf(session, V2, "v2.pdf", document_id=r1.document_id)
    assert r1.version.number == 1 and r2.version.number == 2
    assert {m["section"] for m in r2.summary["modified_nodes"]} == {"2.1.1.1", "3.2", "4.2", "4.3"}
    assert [a["section"] for a in r2.summary["added_nodes"]] == ["5.3"]
    assert r2.summary["removed"] == 0
    # page reflow in v2 (4.3, 6.1, 8.1 now straddle pages) must NOT cause false "modified"
    assert r2.summary["nodes"] == 28 and r2.summary["unchanged"] == 28 - 1 - 4
    assert r2.summary["non_exact_matches"] == []


def test_logical_ids_are_stable_and_v1_is_untouched(session):
    r1 = ingest_pdf(session, V1, "v1.pdf")
    r2 = ingest_pdf(session, V2, "v2.pdf", document_id=r1.document_id)
    a, b = sec(session, 1, r1.document_id, "3.2"), sec(session, 2, r1.document_id, "3.2")
    assert a.logical_node_id == b.logical_node_id and a.id != b.id
    assert "40 mmHg increments" in a.body and "30 mmHg increments" in b.body
    assert a.content_hash != b.content_hash
    e1, e2 = sec(session, 1, r1.document_id, "4.2"), sec(session, 2, r1.document_id, "4.2")
    assert "E6" not in e1.body and "E6" in e2.body
    assert sec(session, 1, r1.document_id, "7.1").logical_node_id != e1.logical_node_id  # duplicate title, distinct ids


def test_reingesting_same_file_is_idempotent(session):
    r1 = ingest_pdf(session, V1, "v1.pdf")
    r1b = ingest_pdf(session, V1, "again.pdf", document_id=r1.document_id)
    assert r1b.created is False and r1b.version.number == 1


def test_diff_classification_distinguishes_threshold_from_wording():
    thr = diff_summary("T", "increments of 40 mmHg up to 299 mmHg.", "T", "increments of 30 mmHg up to 299 mmHg.")
    assert thr["severity"] == "critical" and "numeric_value_changed" in thr["reasons"]
    assert thr["numbers"] == {"removed": ["40mmhg"], "added": ["30mmhg"]}
    modal = diff_summary("T", "The device shall beep.", "T", "The device should beep.")
    assert modal["severity"] == "critical" and "normative_wording_changed" in modal["reasons"]
    typo = diff_summary("T", "The device beeps loudly.", "T", "The device beeps loudy.")
    assert typo["severity"] == "review" and typo["reasons"] == ["text_changed"]


# ---- matcher on synthetic trees (strategies the real v2 never exercises)
def R(idx, num, title, body="", parent=None):
    return NodeRef(idx, num, title, body, body_hash(body), parent)


LONG = "This section describes a long enough body so that the content matcher is allowed to use it."


def test_renumbering_is_survived_via_title_under_parent():
    old = [R(0, "1", "Intro"), R(1, "1.1", "Scope", LONG, 0), R(2, "2", "Ops"), R(3, "2.1", "Run", "x", 2)]
    new = [R(0, "1", "Preface"), R(1, "2", "Intro"), R(2, "2.1", "Scope", LONG, 1), R(3, "3", "Ops"), R(4, "3.1", "Run", "x", 3)]
    m = match_nodes(old, new)
    assert m[1].old_idx == 0 and m[2].old_idx == 1 and m[3].old_idx == 2 and m[4].old_idx == 3
    assert 0 not in m  # "Preface" is genuinely new


def test_rename_with_identical_body_matches_by_content():
    old = [R(0, "1", "Top"), R(1, "1.1", "Battery Life", LONG, 0)]
    new = [R(0, "1", "Top"), R(1, "1.1", "Battery Runtime", LONG, 0)]
    assert match_nodes(old, new)[1].method == "same_content"


def test_fuzzy_match_is_recorded_and_unrelated_nodes_are_not_forced_together():
    old = [R(0, "1", "Top"), R(1, "1.1", "Overpressure Protection", "cuff above 299 mmHg vents", 0)]
    new = [R(0, "1", "Top"), R(1, "1.1", "Overpressure Protection System", "cuff above 299 mmHg vents quickly", 0)]
    assert match_nodes(old, new)[1].method == "fuzzy"
    new2 = [R(0, "1", "Top"), R(1, "1.1", "Data Export", "csv timestamp profile", 0)]
    assert 1 not in match_nodes(old, new2)


def test_duplicate_titles_under_one_parent_after_renumbering_are_paired_in_order_and_flagged():
    old = [R(0, "1", "P"), R(1, "1.1", "Notes", "alpha", 0), R(2, "1.2", "Notes", "beta", 0)]
    new = [R(0, "1", "P"), R(1, "1.8", "Notes", "alpha", 0), R(2, "1.9", "Notes", "beta", 0)]
    m = match_nodes(old, new)
    assert (m[1].old_idx, m[2].old_idx) == (1, 2)
    assert m[1].method == "title_under_parent_ambiguous"   # a human should look at this one
