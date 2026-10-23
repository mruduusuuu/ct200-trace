# Approach

## 1. Parsing approach
`pdffonts` shows embedded fonts and the PDFs have a real text layer, so **OCR is not needed** and would only add errors.
I use PyMuPDF span-level extraction (text + font + size + bbox). OCR fallback is *not* implemented: a page without a text layer
raises `PAGE_WITHOUT_TEXT` (error) and ingestion is refused rather than returning an empty-looking tree.
I chose geometry over flat text because the flat text rendering of this very PDF glues content across page breaks
(`...Specifications2.1 General Specifications`, `...device.Use the profile button`).

## 2. Hierarchy reconstruction
- **Heading** = bold line + `^\d+(\.\d+)*\.?\s+` + left-aligned. **Depth comes from the number**, not font size.
- **Parent** = deepest open node whose number is a proper prefix of the new number (stack). Level jumps and out-of-order siblings can't mis-parent.
- Body is rebuilt into typed blocks (paragraph / list / table) and rendered to canonical text; the content hash is over `title + body` (not the number, so renumbering alone is not a "change").
- Nothing suspicious is repaired silently. Each case produces an `Issue` (error / warning / info) stored with the version (`GET /documents/{id}/versions/{n}/report`). Errors make ingestion fail with 422 (`allow_errors=true` overrides).
- **Independent validation**: the word multiset of PyMuPDF's plain-text output must equal the word multiset of the rebuilt tree (`COVERAGE_MISMATCH` otherwise). This checks the structure builder for lost or duplicated content.

## 3. Irregularities found in the PDF, and how each is handled
| Irregularity (found by dumping spans, fonts, bboxes) | Handling | Test |
|---|---|---|
| Duplicate heading text: "Error Codes" is 4.2 and 7.1 | Ids are number/order based, parents correct, `duplicate_title` flag + info issue | `test_duplicate_heading_...` |
| Skipped level: 2.1.1.1 directly under 2.1 (no 2.1.1) | Parent = 2.1, `level_gap` flag + warning; no synthetic node invented | `test_skipped_level_...` |
| Out-of-order siblings: 3.4 precedes 3.3 | Document order kept (`order_idx`), bodies not merged, `out_of_order` warning | `test_out_of_order_...` |
| Numbered list `1.`–`5.` in 3.3 looks like H1 headings | Not bold and indented ⇒ list block; wrapped item merged | `test_numbered_list_...` |
| Some headings are bold at *body* size (3.2, 2.1.1.1) | Detection by bold+number, not size | `test_bold_headings_...` |
| Tables (2.1, 4.2) with multi-line cells; `find_tables().extract()` moves a comma (`'Aborts measurement displays E1\n,'`) | `find_tables` for cell geometry only; text rebuilt from span lines in each cell | `test_tables_rebuilt_...` |
| Ligatures (ﬁ ﬂ ﬀ) and U+2011 hyphens as separate spans | NFKC + hyphen folding, so search and hashing work | `test_ligatures_...` |
| Headings stranded at page bottom, sections split across pages (3.1) | One global flow across pages; a page break is not a paragraph break | `test_section_split_...` |
| Cross-references ("see 2.1, 4.3", "Section 4.2") look like numbering | Not bold ⇒ never headings; extracted into `references` | `test_cross_references_...` |
| Multi-line title | Merged, not a node | `test_title_...` |
| v2 reflows pages (4.3, 6.1, 8.1 now straddle pages) | Hash over canonical text ⇒ no false "modified" | `test_v1_to_v2_flags_exactly_...` |

**What failed in naive approaches** (found *before* writing the parser, by inspecting the dumps): flat-text extraction, font-size thresholds, a `^\d+\.` regex without a bold check, and `find_tables().extract()`. *(Add your own account of what your first version missed and how you caught it.)*

## 4. Data model
`Document → DocVersion` (immutable, file sha256, parser report). `LogicalNode` = stable public node id across versions.
`NodeVersion` = a logical node as it is in one version (body, blocks, hash, parent, change_status, match method/score, flags).
`Selection → SelectionItem(node_version_id)` pins node+version. Generations live in the NoSQL store with a **snapshot of the exact title/body/hash the LLM saw**,
so they survive re-versioning and even deletion of old nodes. Test cases cite `source_node_ids` (logical ids).

## 5. Version matching and where it breaks
Matched against the latest version only, strongest evidence first: (A) same number + title; (B) same title under an already-matched parent (survives renumbering);
(C) identical non-trivial body, unique (survives renaming); (D) fuzzy `0.6·title + 0.3·body + 0.1·parent` ≥ 0.72 (recorded, listed as `non_exact_matches` for human review). Leftovers are added/removed.
The real v1→v2 pair matches 100% in pass A; B–D are covered by synthetic tests.
**Breaks:** split/merge of sections (one removed + one added); two sections with the same title under one parent after renumbering (ordered pairing, flagged `title_under_parent_ambiguous`);
a section whose title and body are both rewritten (new node); a removed node that reappears later (new id, matching only looks at the previous version); fuzzy false positives when a section is deleted and a similarly titled one added.

## 6. LLM design
Prompt `tc-v1`: sources labelled `[S1]…` (short labels, not hashes, so the model can cite them), "use only the sources, copy numbers exactly", JSON-only schema with `source_labels`.
**Validation**: strip fences → decode only the *outermost* JSON value (a truncated reply must fail rather than yield an inner fragment) → Pydantic (3 ≤ cases, steps ≥ 1, priority enum, labels must exist).
**Failure policy**: any violation rejects the *whole* response and retries with the error fed back (max 3). >5 valid cases are truncated with a warning (harmless); <3 retries. After the last attempt: stored as `failed` (raw attempts kept), API returns 502, partial output is never returned as valid.
Transport errors (429/5xx/timeouts) retry with backoff, then report `llm_unavailable`. Numbers in a test case that appear in none of its cited sources are flagged `unsupported_numbers` (warning, not rejection).
**Same selection twice**: selections are immutable, so same selection + prompt version ⇒ same inputs ⇒ return the existing successful generation (200, `reused`; no cost, auditable). `?force=true` creates a new one; old ones are kept and marked `superseded`. Failed generations never satisfy idempotency. A per-key lock stops concurrent duplicates (single process only).

## 7. Staleness
At retrieval, each snapshot source is compared with the same logical node in the target version (`against_version`, default latest): `fresh` / `modified` / `removed`, plus `added` for new subsections under a selected root.
A change is **critical** if numeric facts, the error-code set or normative/negation words (`shall/should/must/not/only…`) differ, otherwise **review**. So `40→30 mmHg` and `shall→should` are critical, a typo fix is not.
Per test case the verdict uses only the sources it cites; the generation verdict is the worst across all sources (`fresh | stale | orphaned`). Cross-reference changes are informational `warnings`.
**Limits (also returned in every response as `caveats`):** it is a heuristic, not semantics; synonym polarity flips and equivalent rewordings are not distinguished; an implicit dependency without a textual link is invisible.
Concrete example in this data: v2 changes E3 deflation to 1.5 s in 4.2 but leaves 4.1 saying "within 2 seconds". A test case generated only from 4.1 stays `fresh`, although the manual now contradicts itself.

## 8. Decision log (draft — rewrite in your own words)
1. **Most likely silent wrong result:** the staleness verdict for a test case that cites the wrong source (or too few). It would report `fresh` while depending on changed text. Mitigations: the `unsupported_numbers` check against cited sources, and the generation-level verdict is the worst over *all* selected sources. To catch it for real: periodically sample test cases and have a human check their citations against the text; add a check that every numeric fact in the test case appears in a cited source and reject rather than warn.
2. **Simplicity over correctness:** change severity is regex tiers (numbers, codes, modal words), and the matcher compares only with the previous version. In production the first thing to break is semantic changes with no numeric/modal signal (e.g. "wipe with a dry cloth" → "wipe with a damp cloth" is only `review`), and a node that disappears then reappears gets a new id.
3. **Not handled:** a scanned/image-only page (no OCR fallback) — the parser raises `PAGE_WITHOUT_TEXT` and ingestion is rejected with 422. Also not handled: a selection above `MAX_SOURCE_CHARS` — rejected with 413, never truncated, because truncation silently drops requirements.

## 9. With more time
OCR fallback (Tesseract) for image-only pages; compare the matcher against all prior versions; an LLM-assisted semantic diff for `review`-tier changes, clearly labelled as advisory;
section-level (not only citation-level) dependency edges, including entity links such as "E3" across 4.1 and 4.2; a Mongo integration test against a real server; async/worker queue for generation and a cross-process lock; golden-file tests for the PDF fixtures; pagination on list endpoints.

Stack: as specified (FastAPI, Pydantic, SQLAlchemy+SQLite). The NoSQL store defaults to a JSON file store (document-shaped, write-once, zero setup); MongoDB is supported behind the same interface.
