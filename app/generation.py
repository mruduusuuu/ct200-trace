"""Selection -> LLM -> stored generation (with idempotency policy)."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import repo
from .llm import PROMPT_VERSION, LLMClient, run_generation
from .models import Selection, SelectionItem
from .store import GenerationStore

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class SelectionTooLarge(Exception):
    pass


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def selection_items(s: Session, sel: Selection) -> list[SelectionItem]:
    return sorted(s.query(SelectionItem).filter(SelectionItem.selection_id == sel.id).all(), key=lambda i: i.order_idx)


def idempotency_key(items: list[SelectionItem], prompt_version: str = PROMPT_VERSION) -> str:
    payload = json.dumps(sorted((i.node_version_id, i.node_version.content_hash) for i in items) + [prompt_version])
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def generate(s: Session, store: GenerationStore, llm: LLMClient, sel: Selection, force: bool,
             max_attempts: int, max_chars: int) -> tuple[dict, bool]:
    """Returns (generation_doc, reused)."""
    items = selection_items(s, sel)
    key = idempotency_key(items)
    with _lock_for(f"{sel.id}:{key}"):
        if not force:
            existing = store.find_ok_by_key(sel.id, key)
            if existing:
                return existing, True
        sources = []
        for n, it in enumerate(items, 1):
            nv = it.node_version
            sources.append({"label": f"S{n}", "logical_node_id": it.logical_node_id, "node_version_id": nv.id,
                            "version_number": nv.version.number, "section_number": nv.section_number, "title": nv.title,
                            "body": nv.body, "content_hash": nv.content_hash, "is_root": it.is_root})
        if sum(len(x["body"]) + len(x["title"]) for x in sources) > max_chars:
            raise SelectionTooLarge(f"selection text exceeds MAX_SOURCE_CHARS={max_chars}; select fewer/narrower sections")
        res = run_generation(llm, sources, max_attempts)
        label_to_node = {x["label"]: x["logical_node_id"] for x in sources}
        doc = {
            "id": "g_" + uuid.uuid4().hex[:12], "selection_id": sel.id, "document_id": sel.document_id,
            "source_version_id": sel.version_id, "source_version_number": sel.version.number,
            "include_descendants": sel.include_descendants,
            "idempotency_key": key, "prompt_version": PROMPT_VERSION, "model": getattr(llm, "name", "unknown"),
            "created_at": datetime.now(timezone.utc).isoformat(), "forced": force,
            "status": "ok" if res.ok else "failed", "failure_reason": res.failure_reason,
            "warnings": res.warnings, "attempts": res.attempts,
            # exact text the model saw -> survives re-versioning and even deletion of old nodes
            "source_snapshot": [{k: v for k, v in x.items()} for x in sources],
            "test_cases": [
                {"id": f"tc_{i}", **{k: v for k, v in tc.items() if k != "source_labels"},
                 "source_labels": tc["source_labels"], "source_node_ids": [label_to_node[l] for l in tc["source_labels"]]}
                for i, tc in enumerate(res.test_cases, 1)
            ],
        }
        store.insert(doc)
        return doc, False
