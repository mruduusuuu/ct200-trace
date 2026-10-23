"""NoSQL store for LLM output. Same interface for a JSON-file backend and MongoDB.

Why a JSON store is the default: generated output is a self-contained document (snapshot of
source text + test cases + raw attempts) that is written once and read by a few keys. That is
a document-store shape, it needs no joins, and a file-per-document store keeps local setup at
zero dependencies. MongoStore is a drop-in when STORE_BACKEND=mongo.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Protocol


class GenerationStore(Protocol):
    def insert(self, doc: dict) -> None: ...
    def get(self, gen_id: str) -> dict | None: ...
    def find_by_selection(self, selection_id: str) -> list[dict]: ...
    def find_by_node(self, logical_node_id: str) -> list[dict]: ...
    def find_ok_by_key(self, selection_id: str, key: str) -> dict | None: ...


def _newest_first(docs: list[dict]) -> list[dict]:
    return sorted(docs, key=lambda d: d["created_at"], reverse=True)


class JsonFileStore:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def insert(self, doc: dict) -> None:
        path = self.dir / f"{doc['id']}.json"
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)  # atomic: readers never see half-written documents

    def _all(self) -> list[dict]:
        out = []
        for p in self.dir.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def get(self, gen_id: str) -> dict | None:
        p = self.dir / f"{os.path.basename(gen_id)}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def find_by_selection(self, selection_id: str) -> list[dict]:
        return _newest_first([d for d in self._all() if d["selection_id"] == selection_id])

    def find_by_node(self, logical_node_id: str) -> list[dict]:
        return _newest_first([d for d in self._all()
                              if any(s["logical_node_id"] == logical_node_id for s in d["source_snapshot"])])

    def find_ok_by_key(self, selection_id: str, key: str) -> dict | None:
        c = [d for d in self.find_by_selection(selection_id) if d["status"] == "ok" and d["idempotency_key"] == key]
        return c[0] if c else None


class MongoStore:
    def __init__(self, uri: str, db: str, client=None):
        if client is None:
            from pymongo import MongoClient
            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        self.col = client[db]["generations"]
        self.col.create_index("selection_id")
        self.col.create_index("source_snapshot.logical_node_id")
        self.col.create_index([("selection_id", 1), ("idempotency_key", 1)])

    @staticmethod
    def _clean(d: dict | None) -> dict | None:
        if d:
            d.pop("_id", None)
        return d

    def insert(self, doc: dict) -> None:
        self.col.insert_one({**doc, "_id": doc["id"]})

    def get(self, gen_id: str) -> dict | None:
        return self._clean(self.col.find_one({"_id": gen_id}))

    def find_by_selection(self, selection_id: str) -> list[dict]:
        return [self._clean(d) for d in self.col.find({"selection_id": selection_id}).sort("created_at", -1)]

    def find_by_node(self, logical_node_id: str) -> list[dict]:
        return [self._clean(d) for d in self.col.find({"source_snapshot.logical_node_id": logical_node_id}).sort("created_at", -1)]

    def find_ok_by_key(self, selection_id: str, key: str) -> dict | None:
        return self._clean(self.col.find_one({"selection_id": selection_id, "idempotency_key": key, "status": "ok"},
                                             sort=[("created_at", -1)]))


def make_store(settings) -> GenerationStore:
    if settings.store_backend == "mongo":
        return MongoStore(settings.mongo_uri, settings.mongo_db)
    return JsonFileStore(settings.json_store_dir)
