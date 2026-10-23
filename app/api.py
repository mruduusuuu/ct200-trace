from __future__ import annotations

import os
import tempfile
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import repo
from .analysis import diff_summary
from .generation import SelectionTooLarge, generate, selection_items
from .models import DocVersion, Document, LogicalNode, NodeVersion, Selection, SelectionItem
from .schemas import SelectionIn
from .staleness import evaluate_generation
from .textnorm import normalize_text
from .versioning import IngestRejected, ingest_pdf

router = APIRouter()


def get_session(request: Request):
    with request.app.state.session_factory() as s:
        yield s


# ------------------------------------------------------------------ serializers
def node_summary(nv: NodeVersion) -> dict:
    return {
        "node_id": nv.logical_node_id, "node_version_id": nv.id, "version": nv.version.number,
        "section_number": nv.section_number, "title": nv.title, "level": nv.level,
        "parent_node_id": nv.parent.logical_node_id if nv.parent else None,
        "content_hash": nv.content_hash, "change_status": nv.change_status, "flags": nv.flags,
    }


def node_detail(s: Session, nv: NodeVersion) -> dict:
    return {
        **node_summary(nv), "body": nv.body, "blocks": nv.blocks, "references": nv.refs,
        "pages": [nv.page_start, nv.page_end], "matched_by": nv.matched_by, "match_score": nv.match_score,
        "breadcrumbs": [{"node_id": b.logical_node_id, "section_number": b.section_number, "title": b.title}
                        for b in repo.breadcrumbs(nv)],
        "children": [node_summary(c) for c in repo.children(s, nv)],
    }


def _doc_version_or_404(s: Session, doc_id: int, version: int | None) -> DocVersion:
    if s.get(Document, doc_id) is None:
        raise HTTPException(404, f"document {doc_id} not found")
    v = repo.get_version(s, doc_id, version) if version else repo.latest_version(s, doc_id)
    if v is None:
        raise HTTPException(404, "version not found")
    return v


# ------------------------------------------------------------------ ingestion + browse
@router.get("/health")
def health():
    return {"ok": True}


@router.post("/documents/ingest", status_code=201)
async def ingest(request: Request, file: UploadFile = File(...), document_id: int | None = Form(None),
                 allow_errors: bool = Form(False), s: Session = Depends(get_session)):
    """No document_id => new document, version 1. With document_id => new version of that document."""
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(422, "uploaded file is not a PDF")
    fd, tmp = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            r = ingest_pdf(s, tmp, file.filename or "upload.pdf", document_id, allow_errors,
                           request.app.state.settings.match_fuzzy_threshold)
        except IngestRejected as e:
            raise HTTPException(422, {"message": str(e), "issues": e.issues})
        except KeyError as e:
            raise HTTPException(404, str(e))
    finally:
        os.unlink(tmp)
    return JSONResponse(status_code=201 if r.created else 200, content={
        "document_id": r.document_id, "version": r.version.number, "created": r.created,
        "title": r.version.title, "summary": r.summary,
        "parser_issues": [i for i in r.version.report.get("issues", []) if i["severity"] != "info"] if r.created else [],
    })


@router.get("/documents")
def list_documents(s: Session = Depends(get_session)):
    out = []
    for d in s.scalars(select(Document)):
        lv = repo.latest_version(s, d.id)
        out.append({"document_id": d.id, "name": d.name, "latest_version": lv.number if lv else None})
    return out


@router.get("/documents/{doc_id}/versions")
def list_versions(doc_id: int, s: Session = Depends(get_session)):
    _doc_version_or_404(s, doc_id, None)
    vs = s.scalars(select(DocVersion).where(DocVersion.document_id == doc_id).order_by(DocVersion.number))
    return [{"version": v.number, "file_name": v.file_name, "file_sha256": v.file_sha256, "title": v.title,
             "created_at": v.created_at, "ingest_summary": {k: v.report.get("ingest", {}).get(k)
                                                            for k in ("nodes", "unchanged", "modified", "added", "removed")},
             "parser_issue_codes": sorted({i["code"] for i in v.report.get("issues", [])})} for v in vs]


@router.get("/documents/{doc_id}/versions/{number}/report")
def version_report(doc_id: int, number: int, s: Session = Depends(get_session)):
    v = _doc_version_or_404(s, doc_id, number)
    return v.report


@router.get("/documents/{doc_id}/sections")
def top_level_sections(doc_id: int, version: int | None = None, s: Session = Depends(get_session)):
    v = _doc_version_or_404(s, doc_id, version)
    nodes = s.scalars(select(NodeVersion).where(NodeVersion.version_id == v.id, NodeVersion.parent_id.is_(None))
                      .order_by(NodeVersion.order_idx))
    return {"document_id": doc_id, "version": v.number, "title": v.title, "sections": [node_summary(n) for n in nodes]}


@router.get("/documents/{doc_id}/tree")
def tree(doc_id: int, version: int | None = None, s: Session = Depends(get_session)):
    v = _doc_version_or_404(s, doc_id, version)
    nodes = repo.version_nodes(s, v.id)
    by_id = {n.id: {**node_summary(n), "children": []} for n in nodes}
    roots = []
    for n in nodes:
        (by_id[n.parent_id]["children"] if n.parent_id else roots).append(by_id[n.id])
    return {"document_id": doc_id, "version": v.number, "title": v.title, "tree": roots}


@router.get("/documents/{doc_id}/search")
def search(doc_id: int, q: str = Query(min_length=2), scope: str = Query("both", pattern="^(heading|body|both)$"),
           version: int | None = None, limit: int = Query(50, le=200), s: Session = Depends(get_session)):
    v = _doc_version_or_404(s, doc_id, version)
    qn = normalize_text(q).strip()
    like = "%" + qn.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    conds = []
    if scope in ("heading", "both"):
        conds += [NodeVersion.title.ilike(like, escape="\\"), NodeVersion.section_number == qn]
    if scope in ("body", "both"):
        conds.append(NodeVersion.body.ilike(like, escape="\\"))
    rows = s.scalars(select(NodeVersion).where(NodeVersion.version_id == v.id, or_(*conds))
                     .order_by(NodeVersion.order_idx).limit(limit)).all()
    out = []
    for n in rows:
        i = n.body.lower().find(qn.lower())
        snippet = n.body[max(0, i - 60): i + 100] if i >= 0 else None
        out.append({**node_summary(n), "snippet": snippet})
    return {"query": q, "version": v.number, "count": len(out), "results": out}


@router.get("/nodes/{node_id}")
def get_node(node_id: str, version: int | None = None, s: Session = Depends(get_session)):
    ln = repo.logical(s, node_id)
    if ln is None:
        raise HTTPException(404, "node not found")
    hist = repo.node_history(s, node_id)
    if version:
        nv = next((h for h in hist if h.version.number == version), None)
        if nv is None:
            raise HTTPException(404, f"node {node_id} does not exist in version {version}")
    else:
        nv = hist[-1]
    latest = repo.latest_version(s, ln.document_id)
    return {**node_detail(s, nv), "document_id": ln.document_id,
            "exists_in_latest": any(h.version_id == latest.id for h in hist), "latest_version": latest.number}


@router.get("/nodes/{node_id}/changes")
def node_changes(node_id: str, from_version: int | None = None, to_version: int | None = None,
                 s: Session = Depends(get_session)):
    ln = repo.logical(s, node_id)
    if ln is None:
        raise HTTPException(404, "node not found")
    hist = repo.node_history(s, node_id)
    by_ver = {h.version.number: h for h in hist}
    latest = repo.latest_version(s, ln.document_id)
    to_v = to_version or latest.number
    from_v = from_version or hist[0].version.number
    if from_v > to_v:
        raise HTTPException(422, "from_version must be <= to_version")
    a, b = by_ver.get(from_v), by_ver.get(to_v)
    out = {"node_id": node_id, "document_id": ln.document_id, "latest_version": latest.number,
           "from_version": from_v, "to_version": to_v,
           "history": [{"version": h.version.number, "node_version_id": h.id, "section_number": h.section_number,
                        "title": h.title, "content_hash": h.content_hash, "change_status": h.change_status}
                       for h in hist]}
    if b is None:
        out.update(status="removed", own_changed=True, diff=None, subtree_changed=None,
                   last_seen_version=hist[-1].version.number)
        return out
    if a is None:
        out.update(status="added", own_changed=False, diff=None, subtree_changed=None, first_seen_version=hist[0].version.number)
        return out
    own = a.content_hash != b.content_hash
    sa = {n.logical_node_id: n for n in repo.subtree(s, a, include_self=False)}
    sb = {n.logical_node_id: n for n in repo.subtree(s, b, include_self=False)}
    fmt = lambda n: {"node_id": n.logical_node_id, "section_number": n.section_number, "title": n.title}
    sub = {"added": [fmt(sb[k]) for k in sb if k not in sa], "removed": [fmt(sa[k]) for k in sa if k not in sb],
           "modified": [fmt(sb[k]) for k in sb if k in sa and sa[k].content_hash != sb[k].content_hash]}
    out.update(status="modified" if own else "unchanged", own_changed=own,
               diff=diff_summary(a.title, a.body, b.title, b.body) if own else None,
               subtree=sub, subtree_changed=any(sub.values()),
               renumbered=a.section_number != b.section_number)
    return out


# ------------------------------------------------------------------ selections
def selection_out(s: Session, sel: Selection, include_text: bool = False) -> dict:
    items = []
    for it in selection_items(s, sel):
        nv = it.node_version
        d = {"node_id": it.logical_node_id, "node_version_id": nv.id, "version": nv.version.number,
             "section_number": nv.section_number, "title": nv.title, "content_hash": nv.content_hash, "is_root": it.is_root}
        if include_text:
            d["body"] = nv.body
        items.append(d)
    return {"selection_id": sel.id, "name": sel.name, "document_id": sel.document_id, "version": sel.version.number,
            "include_descendants": sel.include_descendants, "created_at": sel.created_at, "items": items}


@router.post("/selections", status_code=201)
def create_selection(body: SelectionIn, s: Session = Depends(get_session)):
    ids = list(dict.fromkeys(body.node_ids))
    lns = {ln.id: ln for ln in s.scalars(select(LogicalNode).where(LogicalNode.id.in_(ids)))}
    unknown = [i for i in ids if i not in lns]
    if unknown:
        raise HTTPException(422, {"message": "unknown node ids", "unknown_node_ids": unknown})
    docs = {ln.document_id for ln in lns.values()}
    if len(docs) != 1:
        raise HTTPException(422, "all nodes in a selection must belong to the same document")
    doc_id = docs.pop()
    v = _doc_version_or_404(s, doc_id, body.version)
    roots, missing = [], []
    for i in ids:
        nv = repo.node_in_version(s, i, v.id)
        (roots if nv else missing).append(nv or i)
    if missing:
        raise HTTPException(422, {"message": f"nodes not present in version {v.number}", "not_in_version": missing})
    chosen: dict[int, NodeVersion] = {}
    for r in roots:
        for n in (repo.subtree(s, r) if body.include_descendants else [r]):
            chosen[n.id] = n
    root_ids = {r.id for r in roots}
    sel = Selection(id="s_" + uuid.uuid4().hex[:10], name=body.name, document_id=doc_id, version_id=v.id,
                    include_descendants=body.include_descendants)
    s.add(sel)
    s.flush()
    for n in sorted(chosen.values(), key=lambda x: x.order_idx):
        s.add(SelectionItem(selection_id=sel.id, node_version_id=n.id, logical_node_id=n.logical_node_id,
                            is_root=n.id in root_ids, order_idx=n.order_idx))
    s.commit()
    s.refresh(sel)
    return selection_out(s, sel)


@router.get("/selections")
def list_selections(s: Session = Depends(get_session)):
    return [{"selection_id": x.id, "name": x.name, "document_id": x.document_id, "version": x.version.number}
            for x in s.scalars(select(Selection).order_by(Selection.created_at))]


@router.get("/selections/{selection_id}")
def get_selection(selection_id: str, include_text: bool = True, s: Session = Depends(get_session)):
    sel = s.get(Selection, selection_id)
    if sel is None:
        raise HTTPException(404, "selection not found")
    return selection_out(s, sel, include_text)


# ------------------------------------------------------------------ generation + retrieval
def generation_out(s: Session, gen: dict, against_version: int | None, focus_node_id: str | None = None,
                   superseded: bool = False) -> dict:
    out = {"generation_id": gen["id"], "selection_id": gen["selection_id"], "status": gen["status"],
           "created_at": gen["created_at"], "model": gen["model"], "prompt_version": gen["prompt_version"],
           "forced": gen["forced"], "superseded": superseded, "generated_from_version": gen["source_version_number"],
           "warnings": gen["warnings"],
           "sources": [{"label": x["label"], "node_id": x["logical_node_id"], "section_number": x["section_number"],
                        "title": x["title"], "version": x["version_number"], "content_hash": x["content_hash"]}
                       for x in gen["source_snapshot"]]}
    if gen["status"] != "ok":
        return {**out, "failure_reason": gen["failure_reason"], "attempts": gen["attempts"], "test_cases": []}
    st = evaluate_generation(s, gen, against_version)
    tcs = st.pop("test_cases")
    out["staleness"] = st
    out["test_cases"] = tcs
    if focus_node_id:
        out["focus"] = {"node_id": focus_node_id,
                        "test_case_ids": [t["id"] for t in tcs if focus_node_id in t["source_node_ids"]],
                        "node_status": next((x["status"] for x in st["sources"] if x["node_id"] == focus_node_id), "added")}
    return out


def _mark_superseded(gens: list[dict]) -> dict[str, bool]:
    seen, flag = set(), {}
    for g in gens:  # newest first
        k = g["idempotency_key"]
        flag[g["id"]] = g["status"] == "ok" and k in seen
        if g["status"] == "ok":
            seen.add(k)
    return flag


@router.post("/selections/{selection_id}/generate")
def generate_test_cases(selection_id: str, request: Request, force: bool = False, s: Session = Depends(get_session)):
    """Idempotent by default: same pinned selection + prompt version => the existing successful generation is
    returned (200, reused=true). force=true => new generation, older ones kept (marked superseded)."""
    sel = s.get(Selection, selection_id)
    if sel is None:
        raise HTTPException(404, "selection not found")
    llm, st = request.app.state.llm, request.app.state.settings
    if llm is None:
        raise HTTPException(503, "LLM not configured: set LLM_API_KEY (or LLM_PROVIDER=mock for an offline demo)")
    try:
        gen, reused = generate(s, request.app.state.store, llm, sel, force, st.llm_max_attempts, st.max_source_chars)
    except SelectionTooLarge as e:
        raise HTTPException(413, str(e))
    if gen["status"] != "ok":
        return JSONResponse(status_code=502, content={
            "error": "LLM did not produce valid structured output", "failure_reason": gen["failure_reason"],
            "generation_id": gen["id"], "attempts": [{"n": a["n"], "error": a["error"]} for a in gen["attempts"]]})
    body = generation_out(s, gen, None)
    body["reused"] = reused
    return JSONResponse(status_code=200 if reused else 201, content=_jsonable(body))


def _jsonable(x):
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder(x)


@router.get("/selections/{selection_id}/generations")
def selection_generations(selection_id: str, request: Request, against_version: int | None = None,
                          include_failed: bool = False, s: Session = Depends(get_session)):
    if s.get(Selection, selection_id) is None:
        raise HTTPException(404, "selection not found")
    gens = request.app.state.store.find_by_selection(selection_id)
    sup = _mark_superseded(gens)
    return [generation_out(s, g, against_version, superseded=sup[g["id"]]) for g in gens
            if include_failed or g["status"] == "ok"]


@router.get("/nodes/{node_id}/generations")
def node_generations(node_id: str, request: Request, against_version: int | None = None,
                     include_failed: bool = False, s: Session = Depends(get_session)):
    if repo.logical(s, node_id) is None:
        raise HTTPException(404, "node not found")
    gens = request.app.state.store.find_by_node(node_id)
    out = []
    for g in gens:
        if g["status"] != "ok" and not include_failed:
            continue
        sup = _mark_superseded(request.app.state.store.find_by_selection(g["selection_id"]))
        out.append(generation_out(s, g, against_version, focus_node_id=node_id, superseded=sup.get(g["id"], False)))
    return out


@router.get("/generations/{generation_id}")
def get_generation(generation_id: str, request: Request, against_version: int | None = None,
                   s: Session = Depends(get_session)):
    g = request.app.state.store.get(generation_id)
    if g is None:
        raise HTTPException(404, "generation not found")
    sup = _mark_superseded(request.app.state.store.find_by_selection(g["selection_id"]))
    return generation_out(s, g, against_version, superseded=sup.get(g["id"], False))
