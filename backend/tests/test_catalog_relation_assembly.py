"""Differential pure graph assembly: all anchors/order/aliasing stay identical."""
import copy
import random

from fund_kb import services as svc
from fund_kb.wiki_catalog import _add_relation, _RelationAssembly


def catalog():
    return {f"W{i}": {"id":f"W{i}","version_id":f"v{i}","relations":[],"links":[]} for i in range(6)}


def apply(pages, operations, assembly):
    version_map = {p["version_id"]:p for p in pages.values()}
    for a,b,kind,origin,conditions,anchors,explanation in operations:
        _add_relation(pages,pages[a],pages[b],kind,origin,conditions,anchors,explanation,
            version_map=version_map,assembly=assembly)
    return pages


def test_randomized_all_fields_duplicates_and_numeric_condition_equality_are_identical():
    rng = random.Random(417)
    conditions = [{}, {"x":1}, {"x":1.0}, {"x":True}, {"a":1,"b":[2,3]}, {"b":[2,3],"a":1}]
    operations = []
    for _ in range(1200):
        a,b = (f"W{rng.randrange(6)}" for _ in range(2))
        anchor = {"version_id":f"v{rng.randrange(6)}","block_id":f"b{rng.randrange(20)}","ignored":"x"}
        anchors = [anchor,copy.deepcopy(anchor)] if rng.randrange(3)==0 else [anchor]
        operations.append((a,b,rng.choice(["CITES","DEPENDS_ON"]),rng.choice(["registered","proposed"]),
            rng.choice(conditions),anchors,rng.choice(["", "explanation"])))
    before = copy.deepcopy(operations)
    legacy = apply(catalog(), operations, None)
    optimized = apply(catalog(), operations, _RelationAssembly())
    assert optimized == legacy and operations == before
    for pages in (legacy, optimized):
        for page in pages.values():
            for row in page["relations"]:
                other = pages[row["target"] if row["source"]==page["id"] else row["source"]]
                assert any(candidate is row for candidate in other["relations"])


def test_anchor_hash_work_is_linear_without_removing_duplicate_batch_anchors(monkeypatch):
    calls = [0]
    original = svc.digest
    def counted(value):
        calls[0] += 1
        return original(value)
    monkeypatch.setattr(svc,"digest",counted)
    operations = [("W1","W2","CITES","registered",{},[{"version_id":"v2","block_id":f"b{i}"}],"") for i in range(2000)]
    pages = apply(catalog(),operations,_RelationAssembly())
    assert len(pages["W1"]["relations"][0]["anchors"]) == 2000
    assert calls[0] < 2000 * 5
    duplicated = [("W1","W2","CITES","registered",{},[{"version_id":"v2","block_id":"new"}]*2,"")]
    # A fresh per-build assembly also correctly indexes already-built rows.
    assert apply(copy.deepcopy(pages),duplicated,None) == apply(copy.deepcopy(pages),duplicated,_RelationAssembly())


def test_assembly_never_leaves_private_construction_state_on_pages():
    pages = apply(catalog(),[("W0","W1","CITES","registered",{},[{"version_id":"v1"}],"")],_RelationAssembly())
    assert all(set(p)=={"id","version_id","relations","links"} for p in pages.values())
    assert all(set(row)=={"source","target","type","verification_status","origin","conditions","explanation","anchors","source_pages"}
        for p in pages.values() for row in p["relations"])
