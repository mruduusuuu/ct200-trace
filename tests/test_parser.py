"""Each test targets one irregularity actually present in data/ct200_manual.pdf."""
from app.parser import parse_pdf, Line, _strip_running_lines  # noqa: F401


def by_num(doc, num):
    return next(n for n in doc.nodes if n.section_number == num)


def test_duplicate_heading_text_gives_distinct_nodes_with_correct_parents(doc1):
    a, b = by_num(doc1, "4.2"), by_num(doc1, "7.1")
    assert a.title == b.title == "Error Codes"
    assert a.idx != b.idx
    assert doc1.nodes[a.parent].section_number == "4"
    assert doc1.nodes[b.parent].section_number == "7"
    assert "Auto-deflates within 2 seconds" in a.body          # table content, not 7.1's prose
    assert "persists after following" in b.body and "persists after" not in a.body


def test_skipped_level_is_parented_to_nearest_ancestor_and_flagged_not_invented(doc1):
    n = by_num(doc1, "2.1.1.1")
    assert n.level == 4 and doc1.nodes[n.parent].section_number == "2.1"
    assert "level_gap" in n.flags
    assert not any(x.section_number in ("2.1.1",) for x in doc1.nodes)       # no synthetic node
    assert any(i.code == "LEVEL_GAP" for i in doc1.issues)


def test_out_of_order_siblings_keep_document_order_and_their_own_bodies(doc1):
    n34, n33 = by_num(doc1, "3.4"), by_num(doc1, "3.3")
    assert n34.idx < n33.idx                                   # 3.4 precedes 3.3 in the PDF
    assert doc1.nodes[n34.parent].section_number == doc1.nodes[n33.parent].section_number == "3"
    assert "automatically powers off" in n34.body and "Hypertension" not in n34.body
    assert "Hypertension Stage 1" in n33.body
    assert any(i.code == "OUT_OF_ORDER" for i in doc1.issues)


def test_numbered_list_is_not_promoted_to_headings(doc1):
    assert [n.section_number for n in doc1.nodes if n.level == 1] == [str(i) for i in range(1, 9)]
    lst = [b for b in by_num(doc1, "3.3").blocks if b["type"] == "list"][0]
    assert [i["marker"] for i in lst["items"]] == ["1", "2", "3", "4", "5"]
    assert lst["items"][4]["text"].endswith("seeking immediate medical attention")  # wrapped line merged


def test_bold_headings_at_body_font_size_are_still_headings(doc1):
    # 3.2 and 2.1.1.1 are 11pt bold (same size as body) -> a font-size threshold would drop them
    assert by_num(doc1, "3.2").title == "Cuff Inflation Sequence"
    assert by_num(doc1, "2.1.1.1").title == "Battery Life Under Typical Use"


def test_tables_rebuilt_from_spans_not_find_tables_text(doc1):
    t = [b for b in by_num(doc1, "4.2").blocks if b["type"] == "table"][0]
    assert t["rows"][0] == ["Code", "Meaning", "Device Behavior"] and len(t["rows"]) == 6
    row = {r[0]: r for r in t["rows"]}
    assert row["E3"][2] == "Auto-deflates within 2 seconds, displays E3"
    assert row["E2"][2] == "Aborts measurement, displays E2, prompts retry"   # comma kept in place
    spec = [b for b in by_num(doc1, "2.1").blocks if b["type"] == "table"][0]
    assert len(spec["rows"]) == 8 and spec["rows"][6] == ["Power source", "4x AA batteries or 6V DC adapter"]


def test_ligatures_and_nonbreaking_hyphens_are_normalised(doc1):
    assert by_num(doc1, "2.1").title == "General Specifications"           # was 'Speciﬁcations'
    assert "CT-200" in by_num(doc1, "2.2").body and "\u2011" not in by_num(doc1, "2.2").body
    assert "fits" in by_num(doc1, "2.2").body


def test_section_split_across_page_break_is_one_node(doc1):
    n = by_num(doc1, "3.1")
    assert (n.page_start, n.page_end) == (2, 3)
    assert "power on the device. Use the profile button" in n.body


def test_cross_references_are_extracted_not_mistaken_for_headings(doc1):
    assert by_num(doc1, "3.3").refs == ["2.1", "4.3"]
    assert by_num(doc1, "7.1").refs == ["4.2"]


def test_no_content_lost_or_duplicated_and_no_errors(doc1, doc2):
    for d in (doc1, doc2):
        assert d.errors == [], [i.message for i in d.errors]          # includes COVERAGE_MISMATCH
        assert d.unparsed == []


def test_title_is_multiline_and_not_a_node(doc1):
    assert doc1.title.startswith("CardioTrack CT-200 Home Blood Pressure Monitor")
    assert doc1.title.endswith("User Manual")
    assert all(n.title != doc1.title for n in doc1.nodes)


def test_running_header_footer_removal_is_conservative():
    L = lambda p, y, t: Line(p, 33, y, 200, y + 10, 9, False, t)
    pages = [[L(p, 5, "ACME CONFIDENTIAL"), L(p, 300, f"body {p}"), L(p, 580, f"Page {p}")] for p in (1, 2, 3)]
    kept, removed = _strip_running_lines(pages, 595)
    assert len(removed) == 6 and all(len(k) == 1 for k in kept)
