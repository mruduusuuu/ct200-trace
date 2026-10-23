"""Ingestion: PDF -> new DocVersion (never mutating older versions)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import repo
from .match import NodeRef, match_nodes
from .models import DocVersion, Document, LogicalNode, NodeVersion
from .parser import ParsedDocument, parse_pdf
from .textnorm import body_hash, content_hash, title_key


class IngestRejected(Exception):
    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__("parser reported errors; refusing to persist a possibly-wrong tree")


@dataclass
class IngestResult:
    document_id: int
    version: DocVersion
    created: bool            # False => identical file already ingested as latest version
    summary: dict


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _new_logical_id(document_id: int, version_no: int, idx: int, number: str) -> str:
    return "n_" + hashlib.sha1(f"{document_id}:{version_no}:{idx}:{number}".encode()).hexdigest()[:10]


def ingest_pdf(s: Session, path: str, file_name: str, document_id: int | None = None,
               allow_errors: bool = False, fuzzy_threshold: float = 0.72,
               parsed: ParsedDocument | None = None) -> IngestResult:
    sha = _sha256_file(path)
    parsed = parsed or parse_pdf(path)
    if parsed.errors and not allow_errors:
        raise IngestRejected([i.to_dict() for i in parsed.issues if i.severity == "error"])

    if document_id is None:
        doc = Document(name=parsed.title or file_name)
        s.add(doc)
        s.flush()
        prev_version = None
    else:
        doc = s.get(Document, document_id)
        if doc is None:
            raise KeyError(f"document {document_id} not found")
        prev_version = repo.latest_version(s, doc.id)
        if prev_version and prev_version.file_sha256 == sha:
            return IngestResult(doc.id, prev_version, False, {"note": "identical file already ingested as latest version"})

    version = DocVersion(document_id=doc.id, number=(prev_version.number + 1) if prev_version else 1,
                         file_name=file_name, file_sha256=sha, title=parsed.title, report={})
    s.add(version)
    s.flush()

    # ---- matching against the latest existing version
    old_nodes = repo.version_nodes(s, prev_version.id) if prev_version else []
    old_pos = {nv.id: i for i, nv in enumerate(old_nodes)}
    old_refs = [NodeRef(i, nv.section_number, nv.title, nv.body, body_hash(nv.body),
                        old_pos.get(nv.parent_id)) for i, nv in enumerate(old_nodes)]
    new_refs = [NodeRef(n.idx, n.section_number, n.title, n.body, body_hash(n.body), n.parent)
                for n in parsed.nodes]
    matches = match_nodes(old_refs, new_refs, fuzzy_threshold) if prev_version else {}

    # ---- persist nodes (parents precede children in document order)
    created: dict[int, NodeVersion] = {}
    counts = {"unchanged": 0, "modified": 0, "added": 0}
    modified, added, non_exact = [], [], []
    for n in parsed.nodes:
        m = matches.get(n.idx)
        h = content_hash(n.title, n.body)
        flags = list(n.flags)
        if prev_version is None:
            status, logical_id, method, score = "initial", _new_logical_id(doc.id, version.number, n.idx, n.section_number), None, None
        elif m is None:
            status, logical_id, method, score = "added", _new_logical_id(doc.id, version.number, n.idx, n.section_number), None, None
        else:
            old = old_nodes[m.old_idx]
            logical_id, method, score = old.logical_node_id, m.method, m.score
            status = "unchanged" if old.content_hash == h else "modified"
            if old.section_number != n.section_number:
                flags.append("renumbered")
            if m.method not in ("exact",):
                flags.append(f"matched_by_{m.method}")
                non_exact.append({"node_id": logical_id, "old_section": old.section_number,
                                  "new_section": n.section_number, "method": m.method, "score": m.score})
        if prev_version is None or m is None:
            s.add(LogicalNode(id=logical_id, document_id=doc.id, created_in_version=version.number))
            s.flush()
        nv = NodeVersion(
            logical_node_id=logical_id, version_id=version.id,
            parent_id=created[n.parent].id if n.parent is not None else None,
            section_number=n.section_number, title=n.title, level=n.level, order_idx=n.idx,
            body=n.body, blocks=n.blocks, content_hash=h, change_status=status,
            matched_by=method, match_score=score, refs=n.refs, flags=flags,
            page_start=n.page_start, page_end=n.page_end,
        )
        s.add(nv)
        s.flush()
        created[n.idx] = nv
        if status in counts:
            counts[status] += 1
        if status == "modified":
            modified.append({"node_id": logical_id, "section": n.section_number, "title": n.title})
        if status == "added":
            added.append({"node_id": logical_id, "section": n.section_number, "title": n.title})

    removed = []
    if prev_version:
        matched_old = {mm.old_idx for mm in matches.values()}
        removed = [{"node_id": o.logical_node_id, "section": o.section_number, "title": o.title}
                   for i, o in enumerate(old_nodes) if i not in matched_old]

    summary = {
        "nodes": len(parsed.nodes), **counts, "removed": len(removed),
        "modified_nodes": modified, "added_nodes": added, "removed_nodes": removed,
        # every non-exact match is a judgement call -> surfaced for human review
        "non_exact_matches": non_exact,
    }
    version.report = {"issues": [i.to_dict() for i in parsed.issues], "stats": parsed.stats,
                      "unparsed_blocks": parsed.unparsed, "ingest": summary}
    s.commit()
    return IngestResult(doc.id, version, True, summary)
