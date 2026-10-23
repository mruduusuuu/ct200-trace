"""PDF -> hierarchical tree for the CT-200 manual.

Approach (see APPROACH.md): the PDFs have a real text layer, so OCR is unnecessary. We read
span-level geometry/fonts with PyMuPDF and rebuild structure from that, rather than from flat
text (flat text glued headings to the previous section across page breaks).

Heading = bold + numbering regex + left-aligned. Depth comes from the NUMBER, not font size
(3.2 and 2.1.1.1 are bold at *body* size). Parent = deepest open node whose number is a proper
prefix of the new number, so skipped levels / out-of-order siblings can't mis-parent.
Everything suspicious is reported as an Issue instead of being silently repaired.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from .textnorm import clean_line, normalize_text

HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
LIST_RE = re.compile(r"^(\d+)[.)]\s+(\S.*)$")
REF_RE = re.compile(
    r"(?:\b[Ss]ections?|\b[Ss]ee)\s+(\d+(?:\.\d+)*(?:\s*(?:,|and)\s*\d+(?:\.\d+)*)*)"
)


@dataclass
class Issue:
    code: str
    severity: str  # error | warning | info
    message: str
    section: str | None = None
    page: int | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool
    text: str


@dataclass
class TableBlock:
    page: int
    y0: float
    bbox: tuple
    rows: list[list[str]]
    has_header: bool


@dataclass
class ParsedNode:
    idx: int
    number: tuple[int, ...]
    title: str
    parent: int | None
    page_start: int
    page_end: int
    blocks: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)

    @property
    def level(self) -> int:
        return len(self.number)

    @property
    def section_number(self) -> str:
        return ".".join(map(str, self.number))

    @property
    def heading(self) -> str:
        return f"{self.section_number} {self.title}"

    @property
    def body(self) -> str:
        return render_blocks(self.blocks)


@dataclass
class ParsedDocument:
    title: str
    nodes: list[ParsedNode]
    issues: list[Issue]
    unparsed: list[dict]
    stats: dict

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]


# ----------------------------------------------------------------------------- rendering
def _md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render_blocks(blocks: list[dict]) -> str:
    out = []
    for b in blocks:
        if b["type"] == "paragraph":
            out.append(b["text"])
        elif b["type"] == "list":
            out.append("\n".join(f"{it['marker']}. {it['text']}" for it in b["items"]))
        elif b["type"] == "table":
            rows = b["rows"]
            lines = ["| " + " | ".join(_md_cell(c) for c in rows[0]) + " |"]
            lines.append("| " + " | ".join("---" for _ in rows[0]) + " |")
            for r in rows[1:]:
                lines.append("| " + " | ".join(_md_cell(c) for c in r) + " |")
            out.append("\n".join(lines))
    return "\n\n".join(out)


# ----------------------------------------------------------------------------- extraction
def _page_lines(page, page_no: int) -> list[Line]:
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type", 0) != 0:
            continue
        for ln in b["lines"]:
            spans = [s for s in ln["spans"] if s["text"] != ""]
            if not spans:
                continue
            text = clean_line("".join(s["text"] for s in spans))
            if not text:
                continue
            real = [s for s in spans if s["text"].strip()]
            bold = bool(real) and all(("Bold" in s["font"]) or (s["flags"] & 16) for s in real)
            x0, y0, x1, y1 = ln["bbox"]
            out.append(Line(page_no, x0, y0, x1, y1, max(s["size"] for s in spans), bold, text))
    return out


def _strip_running_lines(pages: list[list[Line]], page_h: float) -> tuple[list[list[Line]], list[Line]]:
    """Drop identical-looking lines repeated in the top/bottom margin of >= half the pages."""
    if len(pages) < 2:
        return pages, []
    band = 0.08 * page_h
    cnt: Counter = Counter()
    for lines in pages:
        seen = set()
        for l in lines:
            if l.y0 < band or l.y1 > page_h - band:
                seen.add((re.sub(r"\d+", "#", l.text), round(l.y0 / 4)))
        cnt.update(seen)
    thresh = max(2, len(pages) / 2)
    bad = {k for k, v in cnt.items() if v >= thresh}
    kept, removed = [], []
    for lines in pages:
        k_, r_ = [], []
        for l in lines:
            key = (re.sub(r"\d+", "#", l.text), round(l.y0 / 4))
            (r_ if key in bad and (l.y0 < band or l.y1 > page_h - band) else k_).append(l)
        kept.append(k_)
        removed += r_
    return kept, removed


def _page_tables(page, page_no: int, lines: list[Line], issues: list[Issue]):
    """Use find_tables() for cell GEOMETRY only; fill cell text from span-level lines.

    find_tables().extract() reorders punctuation ('Aborts measurement displays E1\\n,'), so
    cell text is rebuilt from the lines whose centre falls inside each cell.
    """
    tables, consumed = [], set()
    try:
        found = page.find_tables(strategy="lines").tables
    except Exception as e:  # pragma: no cover
        issues.append(Issue("TABLE_DETECT_FAILED", "error", str(e), page=page_no))
        return tables, consumed
    for t in found:
        rows, header_bold = [], False
        for r_i, row in enumerate(t.rows):
            cells = []
            for bbox in row.cells:
                if bbox is None:
                    cells.append("")
                    issues.append(Issue("TABLE_MERGED_CELL", "warning", "merged/empty cell", page=page_no))
                    continue
                inside = [
                    (i, l) for i, l in enumerate(lines)
                    if bbox[0] - 1 <= (l.x0 + l.x1) / 2 <= bbox[2] + 1
                    and bbox[1] - 1 <= (l.y0 + l.y1) / 2 <= bbox[3] + 1
                ]
                inside.sort(key=lambda p: (round(p[1].y0), p[1].x0))
                consumed.update(i for i, _ in inside)
                cells.append(" ".join(l.text for _, l in inside))
                if r_i == 0 and inside and all(l.bold for _, l in inside):
                    header_bold = True
            rows.append(cells)
        x0, y0, x1, y1 = t.bbox
        stray = [
            l for i, l in enumerate(lines)
            if i not in consumed and x0 <= (l.x0 + l.x1) / 2 <= x1 and y0 <= (l.y0 + l.y1) / 2 <= y1
        ]
        if stray:
            issues.append(Issue("TABLE_UNASSIGNED_LINES", "error",
                                f"{len(stray)} line(s) inside table bbox not in any cell: {stray[0].text!r}",
                                page=page_no))
        tables.append(TableBlock(page_no, y0, tuple(t.bbox), rows, header_bold))
    return tables, consumed


# ----------------------------------------------------------------------------- main parse
def parse_pdf(path: str) -> ParsedDocument:
    doc = pymupdf.open(path)
    issues: list[Issue] = []
    page_lines, page_tables = [], []
    for p_i, page in enumerate(doc, start=1):
        lines = _page_lines(page, p_i)
        if not lines:  # scanned/image-only page: no OCR fallback here, so say so loudly instead of returning nothing
            issues.append(Issue("PAGE_WITHOUT_TEXT", "error",
                                f"page {p_i} has no text layer (scanned or blank); OCR fallback is not implemented", page=p_i))
        page_lines.append(lines)
    page_h = doc[0].rect.height
    page_lines, removed = _strip_running_lines(page_lines, page_h)

    items: list[tuple[int, float, float, str, object]] = []
    n_tables = 0
    for p_i, page in enumerate(doc, start=1):
        lines = page_lines[p_i - 1]
        tables, consumed = _page_tables(page, p_i, lines, issues)
        n_tables += len(tables)
        for t in tables:
            items.append((p_i, t.y0, 0.0, "table", t))
        for i, l in enumerate(lines):
            if i not in consumed:
                items.append((p_i, l.y0, l.x0, "line", l))
    items.sort(key=lambda it: (it[0], round(it[1], 1), it[2]))

    flow_lines = [it[4] for it in items if it[3] == "line"]
    sizes = Counter(round(l.size, 1) for l in flow_lines if not l.bold)
    body_size = sizes.most_common(1)[0][0] if sizes else 11.0
    margin = Counter(round(l.x0) for l in flow_lines).most_common(1)[0][0] if flow_lines else 0

    title_parts: list[str] = []
    nodes: list[ParsedNode] = []
    unparsed: list[dict] = []
    stack: list[ParsedNode] = []
    prev_line: Line | None = None
    last_heading_line: Line | None = None

    def add_unparsed(l: Line, why: str):
        unparsed.append({"page": l.page, "text": l.text, "reason": why})

    for page_no, _, _, kind, obj in items:
        cur = nodes[-1] if nodes else None
        if kind == "table":
            if cur is None:
                issues.append(Issue("PREAMBLE_CONTENT", "error", "table before first heading", page=page_no))
                continue
            cur.blocks.append({"type": "table", "rows": obj.rows, "has_header": obj.has_header})
            cur.page_end = max(cur.page_end, page_no)
            prev_line = None
            last_heading_line = None
            continue

        l: Line = obj
        m = HEADING_RE.match(l.text) if (l.bold and l.x0 <= margin + 3) else None
        if m:
            number = tuple(int(x) for x in m.group(1).split("."))
            while stack and not (len(stack[-1].number) < len(number) and number[: len(stack[-1].number)] == stack[-1].number):
                stack.pop()
            parent = stack[-1] if stack else None
            node = ParsedNode(len(nodes), number, m.group(2).strip(), parent.idx if parent else None, l.page, l.page)
            if parent and len(number) - len(parent.number) > 1:
                node.flags.append("level_gap")
                issues.append(Issue("LEVEL_GAP", "warning",
                                    f"{node.section_number} is level {len(number)} but nearest ancestor {parent.section_number} "
                                    f"is level {len(parent.number)}; no synthetic nodes invented",
                                    node.section_number, l.page))
            if parent is None and len(number) > 1:
                node.flags.append("missing_parent")
                issues.append(Issue("MISSING_PARENT", "error",
                                    f"{node.section_number} has no ancestor heading", node.section_number, l.page))
            sibs = [n for n in nodes if n.parent == node.parent]
            if sibs:
                prev = sibs[-1]
                if number == prev.number or any(n.number == number for n in sibs):
                    node.flags.append("duplicate_number")
                    issues.append(Issue("DUPLICATE_NUMBER", "error",
                                        f"section number {node.section_number} appears twice", node.section_number, l.page))
                elif number < prev.number:
                    node.flags.append("out_of_order")
                    issues.append(Issue("OUT_OF_ORDER", "warning",
                                        f"{node.section_number} appears after {prev.section_number}; document order preserved",
                                        node.section_number, l.page))
                elif number[-1] > prev.number[-1] + 1:
                    node.flags.append("sequence_gap")
                    issues.append(Issue("SEQUENCE_GAP", "warning",
                                        f"{node.section_number} follows {prev.section_number}; something may be missing",
                                        node.section_number, l.page))
            nodes.append(node)
            stack.append(node)
            prev_line, last_heading_line = None, l
            continue

        # wrapped heading (bold, same size/x, directly under the heading line)
        if (last_heading_line and l.bold and cur and not cur.blocks and l.page == last_heading_line.page
                and abs(l.size - last_heading_line.size) < 0.3 and abs(l.x0 - last_heading_line.x0) < 2
                and l.y0 - last_heading_line.y0 <= 1.7 * l.size):
            cur.title += " " + l.text
            last_heading_line = l
            continue
        last_heading_line = None

        if cur is None:
            if l.bold and l.page == 1 and l.size >= 1.3 * body_size:
                title_parts.append(l.text)
            else:
                add_unparsed(l, "content before first heading")
                issues.append(Issue("PREAMBLE_CONTENT", "error", f"unclassified text before first heading: {l.text!r}", page=l.page))
            continue

        # body line
        cur.page_end = max(cur.page_end, l.page)
        if l.bold:
            issues.append(Issue("UNCLASSIFIED_BOLD", "warning",
                                f"bold line that is not a numbered heading kept as body text: {l.text!r}",
                                cur.section_number, l.page))
        lm = LIST_RE.match(l.text) if (not l.bold and l.x0 > margin + 8) else None
        last = cur.blocks[-1] if cur.blocks else None
        if lm:
            item = {"marker": lm.group(1), "text": lm.group(2)}
            if last and last["type"] == "list":
                last["items"].append(item)
            else:
                cur.blocks.append({"type": "list", "items": [item]})
        elif l.x0 > margin + 8 and last and last["type"] == "list":
            last["items"][-1]["text"] += " " + l.text
        elif last and last["type"] == "paragraph" and not (
            prev_line and prev_line.page == l.page and (l.y0 - prev_line.y0) > 1.7 * l.size
        ):
            last["text"] += " " + l.text  # page break is NOT a paragraph break
        else:
            cur.blocks.append({"type": "paragraph", "text": l.text})
        prev_line = l

    # post-checks
    children = Counter(n.parent for n in nodes)
    titles: dict[str, list[ParsedNode]] = {}
    for n in nodes:
        n.refs = sorted({x.strip() for m in REF_RE.finditer(n.body)
                         for x in re.split(r",|and", m.group(1)) if x.strip()})
        titles.setdefault(normalize_text(n.title).casefold(), []).append(n)
        if not n.blocks and children[n.idx] == 0:
            n.flags.append("empty_leaf")
            issues.append(Issue("EMPTY_LEAF", "warning", "heading with no body and no children", n.section_number, n.page_start))
        if n.page_end > n.page_start:
            n.flags.append("spans_pages")
    for group in titles.values():
        if len(group) > 1:
            for n in group:
                n.flags.append("duplicate_title")
            issues.append(Issue("DUPLICATE_TITLE", "info",
                                f"title {group[0].title!r} used by sections {[n.section_number for n in group]}; "
                                "node ids are number/order based so they stay distinct"))

    title = " ".join(title_parts).strip()
    if not title:
        issues.append(Issue("NO_TITLE", "warning", "document title not found on page 1"))
    parsed = ParsedDocument(
        title=title, nodes=nodes, issues=issues, unparsed=unparsed,
        stats={"pages": len(doc), "tables": n_tables, "removed_running_lines": len(removed),
               "body_font_size": body_size, "left_margin": margin},
    )
    issues.extend(validate_coverage(doc, parsed, removed))
    return parsed


# ----------------------------------------------------------------------------- validation
def _words(s: str) -> Counter:
    return Counter(re.findall(r"\w+", normalize_text(s).casefold()))


def validate_coverage(doc, parsed: ParsedDocument, removed: list[Line]) -> list[Issue]:
    """Independent check: word multiset of PyMuPDF's plain-text extraction must equal the
    word multiset of the rebuilt tree (title + headings + bodies). Catches silently lost or
    duplicated content regardless of how the structure builder behaves."""
    src = Counter()
    for page in doc:
        src.update(_words(page.get_text("text")))
    for l in removed:
        src.subtract(_words(l.text))
    out = _words(parsed.title)
    for n in parsed.nodes:
        out.update(_words(n.heading))
        out.update(_words(n.body))
    for u in parsed.unparsed:
        out.update(_words(u["text"]))
    missing = {w: c for w, c in (src - out).items()}
    extra = {w: c for w, c in (out - src).items()}
    if missing or extra:
        return [Issue("COVERAGE_MISMATCH", "error",
                      f"tree vs PDF word mismatch; missing={dict(list(missing.items())[:10])} extra={dict(list(extra.items())[:10])}")]
    return []
