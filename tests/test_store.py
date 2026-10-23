import pytest

from app.store import JsonFileStore, MongoStore


def doc(i, sel="s1", key="k1", status="ok", node="n_a"):
    return {"id": f"g{i}", "selection_id": sel, "idempotency_key": key, "status": status,
            "created_at": f"2026-01-0{i}T00:00:00", "source_snapshot": [{"logical_node_id": node}]}


@pytest.fixture(params=["json", "mongo"])
def store(request, tmp_path):
    if request.param == "json":
        return JsonFileStore(str(tmp_path))
    import mongomock
    return MongoStore("", "t", client=mongomock.MongoClient())


def test_store_contract(store):
    store.insert(doc(1)); store.insert(doc(2, key="k2")); store.insert(doc(3, status="failed", node="n_b"))
    assert store.get("g1")["id"] == "g1" and store.get("nope") is None
    assert [d["id"] for d in store.find_by_selection("s1")] == ["g3", "g2", "g1"]      # newest first
    assert [d["id"] for d in store.find_by_node("n_a")] == ["g2", "g1"]
    assert store.find_ok_by_key("s1", "k1")["id"] == "g1"
    assert store.find_ok_by_key("s1", "missing") is None
    store.insert(doc(4, key="k3", status="failed"))
    assert store.find_ok_by_key("s1", "k3") is None                                    # failed never satisfies idempotency
