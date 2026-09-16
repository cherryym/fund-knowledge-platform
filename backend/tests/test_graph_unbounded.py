"""Unbounded graph contract using temporary SQLite and in-process HTTP only."""
from __future__ import annotations

import json
import os
import socket
from datetime import date
from urllib.parse import urlencode

import pytest
from sqlalchemy import select
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import grant, page
from test_wiki import provider as provider  # noqa: PLC0414
from test_wiki_semantics import build_input, finish, queue, use_output
from test_wiki_unverified import draft_source

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb import wiki
from fund_kb.ingestion import block_text, text_sha256


@pytest.fixture(autouse=True)
def isolated_configuration_and_network(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("FKB_"):
            monkeypatch.delenv(name)
    attempts = []

    def guarded(original):
        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                attempts.append("blocked")
                raise AssertionError("Graph tests must not open network connections")
            return original(sock, address)
        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(socket.socket.connect_ex))
    yield
    assert not attempts


def graph(env, **params):
    response = env.call("GET", "/wiki/graph?" + urlencode({"space_id": env.space, **params}))
    assert response.status_code == 200, response.text
    return response.json()


def endpoints(result):
    return {(edge["source"], edge["target"]) for edge in result["edges"]}


def assert_counts(result, *, total_nodes, total_edges, matched_nodes, matched_edges):
    assert result["total_visible_nodes"] == total_nodes
    assert result["total_visible_edges"] == total_edges
    assert result["matched_visible_nodes"] == matched_nodes
    assert result["matched_visible_edges"] == matched_edges


def test_all_591_nodes_and_1182_edges_default_none_and_explicit_limits(env):
    count = 591
    records = [page(env, f"合成图节点{index:04d}", tags=["node-role:concept"],
                    text=" ".join(f"[[合成图节点{(index + step) % count:04d}]]" for step in (1, 2)))
               for index in range(count)]
    expected_nodes = [{"id": rid, "label": f"合成图节点{index:04d}", "kind": "knowledge",
                       "node_role": "concept", "source_mode": "published", "knowledge_type": "faq",
                       "category": "估值/费用", "state": "APPROVED", "version_id": vid}
                      for index, (rid, vid, _bid) in enumerate(records)]
    expected_edges = {(record[0], records[(index + step) % count][0])
                      for index, record in enumerate(records) for step in (1, 2)}
    complete = graph(env)
    assert complete["nodes"] == expected_nodes
    assert len(complete["edges"]) == len(expected_edges) == 1182
    assert endpoints(complete) == expected_edges
    assert len({edge["id"] for edge in complete["edges"]}) == 1182
    assert all((edge["type"], edge["origin"], edge["state"]) == ("WIKI_LINK", "wikilink", "APPROVED")
               for edge in complete["edges"])
    assert complete["truncated"] is False
    assert_counts(complete, total_nodes=591, total_edges=1182, matched_nodes=591, matched_edges=1182)
    assert complete["semantic_relation_count"] == 0
    assert complete["node_role_counts"] == {"concept": 591}
    with env.db() as db:
        assert wiki.graph(db, db.get(m.User, env.owner), env.space, limit=None) == complete
    # An explicit limit may exceed both the old cap and machine-sized integers.
    for limit in (201, 591, 10**30):
        limited = graph(env, limit=limit)
        expected = expected_nodes[:limit]
        included = {node["id"] for node in expected}
        assert limited["nodes"] == expected
        assert limited["edges"] == [edge for edge in complete["edges"]
                                    if edge["source"] in included and edge["target"] in included]
        assert limited["truncated"] is (limit < 591)
        assert_counts(limited, total_nodes=591, total_edges=1182, matched_nodes=591, matched_edges=1182)
        assert limited["node_role_counts"] == complete["node_role_counts"]
        assert limited["semantic_relation_count"] == complete["semantic_relation_count"]


@pytest.fixture
def scoped_graph(env):
    records = {
        "a": page(env, "A费用", text="查询标记 [[B口径]]", tags=["node-role:concept"]),
        "b": page(env, "B口径", text="查询标记 [[C价格]]", category="估值/费用/口径",
                  tags=["node-role:condition"]),
        "c": page(env, "C价格", text="[[Z交易]]", category="估值/价格", tags=["node-role:concept"]),
        "d": page(env, "Z交易", text="[[A费用]]", category="投资/交易", tags=["node-role:asset"]),
        "e": page(env, "Y孤立", category="其他"),
    }
    edges = {(records[a][0], records[b][0]) for a, b in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "a"))}
    return records, edges


@pytest.mark.parametrize("params,keys", [
    ({"category": "估值"}, "abc"),
    ({"category": "估值/费用"}, "ab"),
    ({"q": "查询标记"}, "ab"),
    ({"node_role": "concept"}, "ac"),
    ({"category": "估值/费用", "q": "查询标记", "node_role": "concept"}, "a"),
    ({"q": "无匹配的查询"}, ""),
])
def test_filters_return_all_induced_edges_without_marking_scope_as_truncated(env, scoped_graph, params, keys):
    records, all_edges = scoped_graph
    selected = {records[key][0] for key in keys}
    expected_edges = {(a, b) for a, b in all_edges if a in selected and b in selected}
    for optional_limit in ({}, {"limit": 10**30}, {"limit": 1}):
        result = graph(env, **params, **optional_limit)
        included = {node["id"] for node in result["nodes"]}
        if optional_limit.get("limit") == 1:
            assert [node["id"] for node in result["nodes"]] == [records[key][0] for key in keys[:1]]
        else:
            assert included == selected
        assert endpoints(result) == {(a, b) for a, b in expected_edges if a in included and b in included}
        assert result["truncated"] is (optional_limit.get("limit") == 1 and len(selected) > 1)
        assert_counts(result, total_nodes=5, total_edges=4, matched_nodes=len(selected),
                      matched_edges=len(expected_edges))
        assert result["node_role_counts"] == {"concept": 2, "condition": 1, "asset": 1, "topic_or_source": 1}


@pytest.mark.parametrize("depth,keys", [(0, "d"), (1, "dac"), (2, "dabc"), (3, "dabc")])
def test_focus_depth_keeps_focus_first_and_preserves_filtered_scope(env, scoped_graph, depth, keys):
    records, all_edges = scoped_graph
    selected = {records[key][0] for key in keys}
    expected_edges = {(a, b) for a, b in all_edges if a in selected and b in selected}
    params = {"focus_id": records["d"][0], "depth": depth, "category": "估值"}
    result = graph(env, **params)
    assert [node["id"] for node in result["nodes"]] == [records[key][0] for key in keys]
    assert endpoints(result) == expected_edges
    assert result["truncated"] is False
    assert_counts(result, total_nodes=5, total_edges=4, matched_nodes=len(selected), matched_edges=len(expected_edges))
    limited = graph(env, **params, limit=1)
    assert limited["nodes"] == result["nodes"][:1] and limited["edges"] == []
    assert limited["truncated"] is (depth > 0)
    assert_counts(limited, total_nodes=5, total_edges=4, matched_nodes=len(selected), matched_edges=len(expected_edges))


def test_explicit_limit_does_not_grant_access_or_leak_hidden_nodes_relations_and_counts(env):
    left = page(env, "A公开", text="[[B公开]] [[隐藏唯一标题]] [[私有草稿]]")
    right = page(env, "B公开")
    hidden = page(env, "隐藏唯一标题", text="[[A公开]]", restricted=True, tags=["node-role:exception"])
    draft = page(env, "私有草稿", text="[[A公开]]", state="DRAFT")
    with env.db.begin() as db:
        db.add(m.RelationEdge(id=svc.uid(), source_version_id=hidden[1], target_resource_id=right[0],
                             relation_type="EXPLAINS", conditions={}))
        db.flush()
        version = db.get(m.ResourceVersion, hidden[1])
        version.content_sha256 = svc.check_frozen_hash(db, version)
    grant(env, hidden[0], env.owner)
    owner = graph(env, limit=10**30)
    assert {node["id"] for node in owner["nodes"]} == {left[0], right[0], hidden[0], draft[0]}
    assert len(owner["edges"]) == 6
    assert any(edge["origin"] == "relation" and edge["source"] == hidden[0] for edge in owner["edges"])
    env.login(env.reader)
    for params in ({}, {"limit": 1}, {"limit": 10**30}):
        result = graph(env, **params)
        assert [node["id"] for node in result["nodes"]] == [left[0], right[0]][:params.get("limit")]
        assert endpoints(result) == (set() if params.get("limit") == 1 else {(left[0], right[0])})
        assert result["truncated"] is (params.get("limit") == 1)
        assert_counts(result, total_nodes=2, total_edges=1, matched_nodes=2, matched_edges=1)
        assert result["node_role_counts"] == {"topic_or_source": 2}
        assert result["semantic_relation_count"] == 0
        serialized = json.dumps(result, ensure_ascii=False)
        assert all(value not in serialized for value in (*hidden, *draft, "隐藏唯一标题", "私有草稿", "EXPLAINS"))
        for inaccessible in (hidden, draft):
            response = env.call("GET", "/wiki/graph?" + urlencode({
                "space_id": env.space, "focus_id": inaccessible[0], **params}))
            assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
            assert inaccessible[0] not in response.text


@pytest.mark.parametrize("change", ["hash", "scan", "permissions", "suspend", "delete", "expired"])
def test_unbounded_and_explicit_limits_keep_source_validity_fail_closed(env, change):
    source = page(env, "合成有效来源", kind="document")
    derived = page(env, "来源派生知识", cites=[source])
    independent = page(env, "A独立知识", text="[[来源派生知识]]")
    before = graph(env, business_date="2026-09-08")
    assert {node["id"] for node in before["nodes"]} == {source[0], derived[0], independent[0]}
    assert endpoints(before) == {(derived[0], source[0]), (independent[0], derived[0])}
    with env.db.begin() as db:
        resource, version = db.get(m.Resource, source[0]), db.get(m.ResourceVersion, source[1])
        if change == "hash":
            block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == source[1]))
            block.data = {"text": "来源正文已变化，原冻结版本不可继续用作依据。"}
            block.search_text = block_text({"block_type": block.block_type, "data": block.data})
            block.content_sha256 = text_sha256(block.search_text)
        elif change == "scan":
            db.get(m.Blob, version.source_blob_id).scan_state = "QUARANTINED"
        elif change == "permissions":
            resource.restricted = True
        elif change == "suspend":
            resource.suspended = True
        elif change == "delete":
            resource.deleted_at = svc.now()
        elif change == "expired":
            version.valid_to = date(2026, 9, 8)
    for params in ({}, {"limit": 1}, {"limit": 10**30}):
        result = graph(env, business_date="2026-09-08", **params)
        assert [node["id"] for node in result["nodes"]] == [independent[0]]
        assert result["edges"] == [] and result["truncated"] is False
        assert_counts(result, total_nodes=1, total_edges=0, matched_nodes=1, matched_edges=0)
        assert all(value not in json.dumps(result) for value in (*source, *derived))


def test_semantic_metadata_and_frozen_draft_sources_survive_optional_limit_contract(env, provider):
    source = draft_source(env)
    use_output(provider)
    built = finish(env, queue(env, build_input(env, [source])))
    complete = graph(env)
    assert len(complete["nodes"]) == 3 and len(complete["edges"]) == 3
    assert complete["truncated"] is False
    assert_counts(complete, total_nodes=3, total_edges=3, matched_nodes=3, matched_edges=3)
    assert complete["node_role_counts"] == {"topic_or_source": 1, "parameter": 1, "condition": 1}
    assert all(node["source_mode"] == "unverified_draft" and node["state"] == "DRAFT"
               for node in complete["nodes"])
    edge, = [edge for edge in complete["edges"] if edge["origin"] == "semantic"]
    assert (edge["source"], edge["target"]) == tuple(built["created_resource_ids"])
    assert (edge["type"], edge["state"], edge["verification_status"], edge["evidence_count"]) == (
        "REQUIRES", "DRAFT", "PROPOSED", 1)
    assert edge["explanation"] == "来源要求核对输入参数及适用条件。"
    assert complete["semantic_relation_count"] == 1
    assert graph(env, limit=10**30) == complete
    filtered = graph(env, node_role="parameter")
    assert [node["id"] for node in filtered["nodes"]] == [built["created_resource_ids"][0]]
    assert filtered["edges"] == [] and filtered["truncated"] is False
    assert filtered["semantic_relation_count"] == 1 and filtered["node_role_counts"] == complete["node_role_counts"]
    limited = graph(env, focus_id=edge["source"], depth=1, limit=1)
    assert [node["id"] for node in limited["nodes"]] == [edge["source"]]
    assert limited["edges"] == [] and limited["truncated"] is True
    assert limited["semantic_relation_count"] == 1
    assert_counts(limited, total_nodes=3, total_edges=3, matched_nodes=3, matched_edges=3)
    with env.db.begin() as db:
        db.get(m.Resource, source[0]).access_epoch += 1
    for params in ({}, {"limit": 10**30}):
        invalidated = graph(env, **params)
        assert invalidated["nodes"] == invalidated["edges"] == []
        assert invalidated["truncated"] is False and invalidated["semantic_relation_count"] == 0
        assert invalidated["node_role_counts"] == {}
        assert_counts(invalidated, total_nodes=0, total_edges=0, matched_nodes=0, matched_edges=0)
    assert provider.calls == 1


def test_openapi_graph_has_no_array_caps_or_limit_default_or_maximum(env):
    spec = env.app.openapi()
    properties = spec["components"]["schemas"]["WikiGraph"]["properties"]
    for key in ("nodes", "edges"):
        assert properties[key]["type"] == "array"
        assert "maxItems" not in properties[key]
    operation = next(value["get"] for path, value in spec["paths"].items() if path.endswith("/wiki/graph"))
    parameter, = [item for item in operation["parameters"] if item.get("name") == "limit"]
    assert parameter["in"] == "query" and parameter["required"] is False
    assert parameter["schema"] == {"type": "integer", "minimum": 1}


@pytest.mark.parametrize("limit", ["0", "-1", "1.5", "not-an-integer", ""])
def test_explicit_limit_still_requires_a_positive_integer(env, limit):
    response = env.call("GET", "/wiki/graph?" + urlencode({"space_id": env.space, "limit": limit}))
    assert response.status_code == 422


def test_empty_graph_without_limit_is_complete(env):
    for params in ({}, {"limit": 1}, {"limit": 10**30}):
        result = graph(env, **params)
        assert result["nodes"] == result["edges"] == [] and result["truncated"] is False
        assert_counts(result, total_nodes=0, total_edges=0, matched_nodes=0, matched_edges=0)
        assert result["semantic_relation_count"] == 0 and result["node_role_counts"] == {}
