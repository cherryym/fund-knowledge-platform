"""Semantic Wiki security regressions: real HTTP/SQLite, synthetic completion only.

All records live in test_wiki.env's tmp_path database. No application services,
real credentials, model calls, or existing platform data are used. These checks
establish behavior/security boundaries, not professional semantic accuracy.
"""
from __future__ import annotations

import copy
import os
import socket
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import execute, page
from test_wiki import provider as provider  # noqa: PLC0414
from test_wiki_unverified import draft_source

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb import wiki
from fund_kb.ingestion import block_text, text_sha256


@pytest.fixture(autouse=True)
def isolated_configuration_and_network(monkeypatch):
    # Only inspect variable names, never read a deployed credential's value.
    for name in tuple(os.environ):
        if name.startswith("FKB_"):
            monkeypatch.delenv(name)
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    attempts = []

    def guarded(original):
        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                attempts.append("blocked")
                raise AssertionError("Semantic tests must not open network connections")
            return original(sock, address)
        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(real_connect_ex))
    yield
    assert not attempts, "A network attempt was made even though completion must be synthetic"


def build_input(env, sources, *, mode="knowledge_points", references=(), **overrides):
    data = {
        "space_id": env.space,
        "source_resource_ids": [source[0] for source in sources],
        "source_mode": "unverified_draft",
        "granularity": mode,
        "reference_resource_ids": [reference[0] for reference in references],
        "model_selection": {"connection_id": env.connection, "model_id": "synthetic-test-model"},
        "max_pages": 3,
        "consent": True,
    }
    data.update(overrides)
    return data


def queue(env, data):
    response = env.call("POST", "/wiki/builds", data)
    assert response.status_code == 202, response.text
    jid = response.json()["id"]
    with env.db.begin() as db:
        job = db.get(m.Job, jid)
        job.state, job.attempts = "RUNNING", 1
        job.lease_until = svc.now() + timedelta(minutes=2)
    return jid


def finish(env, jid):
    result = execute(env, jid)
    # The shared execute helper deliberately bypasses the background dispatcher.
    with env.db.begin() as db:
        job = db.get(m.Job, jid)
        job.state, job.result = "SUCCEEDED", copy.deepcopy(result)
    return result


def node(title, role, evidence_ids):
    return {
        "title": title, "node_role": role, "category": "估值与核算/参数",
        "knowledge_type": "term", "aliases": [], "links": [],
        "blocks": [{"markdown": f"{title}：按来源核对适用条件，保留复核记录。",
                    "evidence_ids": list(evidence_ids)}],
    }


def relation(left, right, evidence_ids, kind="REQUIRES"):
    return {"source_title": left, "target_title": right, "relation_type": kind,
            "evidence_ids": list(evidence_ids), "explanation": "来源要求核对输入参数及适用条件。"}


def semantic_output(data, *, kind="REQUIRES", relation_only=False, cycle=False):
    ids = [item["id"] for item in data["sources"]]
    pages = [] if relation_only else [node("估值输入参数", "parameter", ids),
                                    node("估值适用条件", "condition", ids)]
    titles = [item["title"] for item in data["reference_nodes"]] if relation_only else [
        item["title"] for item in pages]
    edges = [relation(titles[0], titles[1], ids, kind)]
    if cycle:
        edges.append(relation(titles[1], titles[0], ids, "DEPENDS_ON"))
    return {"pages": pages, "relations": edges, "gaps": [],
            "source_dispositions": [{"evidence_id": eid, "disposition": "EXTRACTED",
                                     "reason": "已引用原始来源。"} for eid in ids]}


def use_output(provider, **options):
    provider.output_transform = lambda _old, data: semantic_output(data, **options)


def graph(env, suffix=""):
    response = env.call("GET", f"/wiki/graph?space_id={env.space}{suffix}")
    assert response.status_code == 200, response.text
    return response.json()


def semantic_edges(env):
    return [edge for edge in graph(env)["edges"] if edge["origin"] == "semantic"]


def policy(env, jid):
    with env.db() as db:
        row = db.scalar(select(m.RuntimePolicy).where(
            m.RuntimePolicy.name == f"wiki-semantic:{env.space}:{jid}"))
        assert row is not None
        return copy.deepcopy(row.config)


def content_snapshot(env):
    """Compare every column, including hashes, releases, metadata and citations."""
    tables = (m.Resource, m.ResourceVersion, m.ContentBlock, m.EvidenceLink, m.RelationEdge, m.Release)
    with env.db() as db:
        return {model.__tablename__: sorted(
            [copy.deepcopy(dict(row)) for row in db.execute(select(model.__table__)).mappings()],
            key=lambda row: repr(sorted(row.items())),
        ) for model in tables}


def assert_no_build_artifacts(env, jid, before):
    assert content_snapshot(env) == before
    with env.db() as db:
        assert db.scalar(select(m.RuntimePolicy).where(
            m.RuntimePolicy.name == f"wiki-semantic:{env.space}:{jid}")) is None
        assert db.scalar(select(m.RuntimePolicy).where(
            m.RuntimePolicy.name == f"wiki-build-receipt:{jid}")) is None
        assert db.scalar(select(m.RuntimePolicy).where(
            m.RuntimePolicy.name.like("wiki-provenance:%"))) is None


def append_block(env, source, text, *, ordinal=1, block_type="paragraph"):
    bid = svc.uid()
    data = {"text": text, "text_format": "markdown"}
    canonical = block_text({"block_type": block_type, "data": data})
    with env.db.begin() as db:
        db.add(m.ContentBlock(version_id=source[1], block_id=bid, ordinal=ordinal,
                             block_type=block_type, data=data, locator={"label": "合成范围段落"},
                             search_text=canonical, content_sha256=text_sha256(canonical)))
        db.flush()
        version = db.get(m.ResourceVersion, source[1])
        version.revision += 1
        version.content_sha256 = None
    return bid


def change_record(env, record, change):
    """Simulate a concurrent, internally coherent edit in the temporary DB only."""
    with env.db.begin() as db:
        resource = db.get(m.Resource, record[0])
        version = db.get(m.ResourceVersion, record[1])
        if change == "body":
            block = db.scalar(select(m.ContentBlock).where(
                m.ContentBlock.version_id == version.id, m.ContentBlock.block_id == record[2]))
            block.data = {"text": "来源或节点正文已发生实质变化。", "text_format": "markdown"}
            block.search_text = block_text({"block_type": block.block_type, "data": block.data})
            block.content_sha256 = text_sha256(block.search_text)
            version.revision += 1
            db.flush()
            version.content_sha256 = svc.check_frozen_hash(db, version)
        elif change == "acl":
            resource.restricted = True
            for grant in db.scalars(select(m.ResourceGrant).where(m.ResourceGrant.resource_id == resource.id)):
                db.delete(grant)
        elif change == "epoch":
            resource.access_epoch += 1
        elif change == "delete":
            resource.deleted_at = svc.now()
        elif change == "suspend":
            resource.suspended = True
        elif change == "blob":
            db.get(m.Blob, version.source_blob_id).scan_state = "QUARANTINED"
        else:
            raise AssertionError(change)


def relation_build(env, provider):
    source = draft_source(env)
    references = [page(env, "独立估值参数", state="DRAFT"), page(env, "独立适用条件", state="DRAFT")]
    use_output(provider, relation_only=True)
    jid = queue(env, build_input(env, [source], mode="relations", references=references))
    result = finish(env, jid)
    assert result["semantic_relations_created"] == 1
    assert len(semantic_edges(env)) == 1
    return source, references, jid, result


@pytest.mark.parametrize("kind", ["EXPLAINS", "APPLIES_TO", "REQUIRES", "EXCEPTION_OF", "DEPENDS_ON"])
def test_http_atomic_nodes_typed_proposals_and_role_filter(env, provider, kind):
    source = draft_source(env)
    use_output(provider, kind=kind)
    jid = queue(env, build_input(env, [source]))
    assert provider.calls == 0
    result = finish(env, jid)
    assert provider.calls == 1
    assert result["granularity"] == "knowledge_points"
    assert result["source_mode"] == "unverified_draft" and result["formal_evidence_allowed"] is False
    assert result["semantic_relations_created"] == 1 and result["semantic_relations_skipped"] == []
    assert len(result["created_resource_ids"]) == len(result["created_version_ids"]) == 2
    assert result["coverage"]["professional_accuracy"] == "NOT_EVALUATED"
    rid1, rid2 = result["created_resource_ids"]
    edge, = semantic_edges(env)
    assert (edge["source"], edge["target"], edge["type"]) == (rid1, rid2, kind)
    assert edge["state"] == "DRAFT" and edge["verification_status"] == "PROPOSED"
    assert edge["evidence_count"] == 1 and edge["explanation"]
    proposals = policy(env, jid)
    assert proposals["source_snapshot"][0]["resource_id"] == source[0]
    proposal, = proposals["proposals"]
    assert proposal["relation_type"] == kind and proposal["verification_status"] == "PROPOSED"
    anchor, = proposal["anchors"]
    assert (anchor["resource_id"], anchor["version_id"], anchor["block_id"]) == source
    excerpt = provider.requests[-1]["sources"][0]["excerpt"]
    assert (anchor["char_start"], anchor["char_end"], anchor["excerpt_sha256"]) == (
        0, len(excerpt), text_sha256(excerpt))
    with env.db() as db:
        assert list(db.scalars(select(m.RelationEdge))) == []
        for rid, vid, role in zip(result["created_resource_ids"], result["created_version_ids"],
                                  ["parameter", "condition"]):
            resource, version = db.get(m.Resource, rid), db.get(m.ResourceVersion, vid)
            assert f"node-role:{role}" in resource.tags
            assert resource.active_release_id is None
            assert (version.state, version.origin, version.source_verified) == ("DRAFT", "AI_DRAFT", False)
        assert db.get(m.ResourceVersion, source[1]).source_verified is False
    filtered = graph(env, "&node_role=parameter")
    assert {item["id"] for item in filtered["nodes"]} == {rid1}
    assert filtered["nodes"][0]["node_role"] == "parameter" and filtered["edges"] == []
    assert graph(env, "&node_role=exception")["nodes"] == []
    workspace = env.call("GET", f"/wiki/workspace?space_id={env.space}")
    assert workspace.status_code == 200, workspace.text
    assert {item["node_role"] for item in workspace.json()["pages"]} == {"parameter", "condition"}
    env.login(env.reader)
    assert semantic_edges(env) == []
    assert not {rid1, rid2} & {item["id"] for item in graph(env)["nodes"]}


def test_all_node_roles_round_trip_through_http(env, provider):
    roles = ["concept", "asset", "rule", "method", "parameter", "condition", "exception", "procedure"]

    def output(_old, data):
        result = semantic_output(data)
        result["pages"] = [node(f"合成知识角色{role}", role, [data["sources"][0]["id"]]) for role in roles]
        result["relations"] = []
        return result

    provider.output_transform = output
    finish(env, queue(env, build_input(env, [draft_source(env)], max_pages=8)))
    for role in roles:
        filtered = graph(env, f"&node_role={role}")
        assert [item["node_role"] for item in filtered["nodes"]] == [role]
    assert env.call("GET", f"/wiki/graph?space_id={env.space}&node_role=publisher").status_code == 422


@pytest.mark.parametrize("mode", ["knowledge_points", "relations"])
@pytest.mark.parametrize("fault,code", [
    ("no_anchor", "WIKI_OUTPUT_SCHEMA_INVALID"),
    ("unknown_anchor", "WIKI_SEMANTIC_ENDPOINT_OR_EVIDENCE_INVALID"),
    ("reference_as_anchor", "WIKI_SEMANTIC_ENDPOINT_OR_EVIDENCE_INVALID"),
    ("unknown_endpoint", "WIKI_SEMANTIC_ENDPOINT_OR_EVIDENCE_INVALID"),
    ("self_edge", "WIKI_SEMANTIC_ENDPOINT_OR_EVIDENCE_INVALID"),
    ("duplicate_disposition", "WIKI_SOURCE_DISPOSITION_INCOMPLETE"),
    ("missing_disposition", "WIKI_SOURCE_DISPOSITION_INCOMPLETE"),
    ("unknown_disposition", "WIKI_SOURCE_DISPOSITION_INCOMPLETE"),
    ("forged_extracted", "WIKI_EXTRACTED_WITHOUT_EVIDENCE"),
    ("formal_relation_type", "WIKI_OUTPUT_SCHEMA_INVALID"),
    ("unsafe_explanation", "WIKI_UNSAFE_MODEL_OUTPUT"),
])
def test_invalid_semantic_output_rolls_back_all_artifacts(env, provider, mode, fault, code):
    # Separate documents guarantee two S inputs even when short passages coalesce.
    sources = [draft_source(env), draft_source(env)]
    references = [page(env, "参考节点甲"), page(env, "参考节点乙")]

    def output(_old, data):
        result = semantic_output(data, relation_only=mode == "relations")
        edge = result["relations"][0]
        if fault == "no_anchor":
            edge["evidence_ids"] = []
        elif fault == "unknown_anchor":
            edge["evidence_ids"] = ["S999"]
        elif fault == "reference_as_anchor":
            edge["evidence_ids"] = [references[0][0]]
        elif fault == "unknown_endpoint":
            edge["target_title"] = "未提供给模型的不存在端点"
        elif fault == "self_edge":
            edge["target_title"] = edge["source_title"]
        elif fault == "duplicate_disposition":
            result["source_dispositions"].append(copy.deepcopy(result["source_dispositions"][0]))
        elif fault == "missing_disposition":
            result["source_dispositions"].pop()
        elif fault == "unknown_disposition":
            result["source_dispositions"][-1]["evidence_id"] = "S999"
        elif fault == "forged_extracted":
            omitted = data["sources"][-1]["id"]
            edge["evidence_ids"].remove(omitted)
            for item in result["pages"]:
                item["blocks"][0]["evidence_ids"].remove(omitted)
        elif fault == "formal_relation_type":
            edge["relation_type"] = "SUPERSEDES"
        elif fault == "unsafe_explanation":
            edge["explanation"] = '<script>alert("unsafe")</script>'
        return result

    provider.output_transform = output
    jid = queue(env, build_input(env, sources, mode=mode, references=references))
    before = content_snapshot(env)
    with pytest.raises(wiki.WikiBuildError) as error:
        execute(env, jid)
    assert error.value.code == code
    assert provider.calls == 1 and len(provider.requests[0]["sources"]) == 2
    assert_no_build_artifacts(env, jid, before)


@pytest.mark.parametrize("fault", ["missing_role", "unknown_role", "page_without_anchor"])
def test_atomic_node_schema_cannot_drop_role_or_source_evidence(env, provider, fault):
    def output(_old, data):
        result = semantic_output(data)
        if fault == "missing_role":
            result["pages"][0].pop("node_role")
        elif fault == "unknown_role":
            result["pages"][0]["node_role"] = "publisher"
        else:
            result["pages"][0]["blocks"][0]["evidence_ids"] = []
        return result

    provider.output_transform = output
    jid = queue(env, build_input(env, [draft_source(env)]))
    before = content_snapshot(env)
    with pytest.raises(wiki.WikiBuildError, match="^WIKI_OUTPUT_SCHEMA_INVALID$"):
        execute(env, jid)
    assert_no_build_artifacts(env, jid, before)


def test_relation_only_preserves_existing_content_and_replay_is_idempotent(env, provider):
    source = draft_source(env)
    references = [page(env, "原有参数知识"), page(env, "原有条件知识")]
    before = content_snapshot(env)
    use_output(provider, relation_only=True)
    jid = queue(env, build_input(env, [source], mode="relations", references=references))
    result = execute(env, jid)
    assert result["created_resource_ids"] == result["created_version_ids"] == []
    assert result["semantic_relations_created"] == 1
    assert result["coverage"]["cited_fragments"] == 1
    assert result["coverage"]["uncited_selected_blocks"] == 0
    assert content_snapshot(env) == before
    edge, = semantic_edges(env)
    assert (edge["source"], edge["target"], edge["evidence_count"]) == (references[0][0], references[1][0], 1)
    links = env.call("GET", f"/wiki/pages/{references[0][0]}/links")
    assert links.status_code == 200, links.text
    linked, = links.json()["outgoing"]
    assert linked["id"] == references[1][0] and linked["verification_status"] == "PROPOSED"
    assert linked["evidence_count"] == 1
    replayed = execute(env, jid)
    assert provider.calls == 1
    assert replayed["semantic_relations_created"] == 1
    assert len(policy(env, jid)["proposals"]) == len(semantic_edges(env)) == 1
    assert content_snapshot(env) == before


def test_relation_only_rejects_model_attempt_to_create_pages(env, provider):
    source = draft_source(env)
    refs = [page(env, "参考甲"), page(env, "参考乙")]
    use_output(provider)  # Deliberately returns pages to a relations-only request.
    jid = queue(env, build_input(env, [source], mode="relations", references=refs))
    before = content_snapshot(env)
    with pytest.raises(wiki.WikiBuildError, match="^WIKI_OUTPUT_SCHEMA_INVALID$"):
        execute(env, jid)
    assert_no_build_artifacts(env, jid, before)


@pytest.mark.parametrize("change", ["body", "delete", "suspend", "epoch", "acl", "blob"])
def test_source_changes_hide_only_proposals_on_all_relation_read_surfaces(env, provider, change):
    source, refs, jid, _ = relation_build(env, provider)
    frozen = policy(env, jid)
    change_record(env, source, change)
    assert semantic_edges(env) == []
    assert {ref[0] for ref in refs}.issubset({item["id"] for item in graph(env)["nodes"]})
    for ref in refs:
        links = env.call("GET", f"/wiki/pages/{ref[0]}/links")
        assert links.status_code == 200, links.text
        assert links.json()["outgoing"] == links.json()["incoming"] == []
    for path in (f"/wiki/builds/{jid}", f"/jobs/{jid}"):
        response = env.call("GET", path)
        assert response.status_code in {403, 404, 409}, response.text
        assert "proposals" not in response.json()
    assert policy(env, jid) == frozen, "Invalidation must not erase or rewrite the frozen proposal"


@pytest.mark.parametrize("endpoint", [0, 1])
@pytest.mark.parametrize("change", ["body", "acl", "epoch", "delete", "suspend"])
def test_endpoint_changes_hide_semantic_edge(env, provider, endpoint, change):
    _source, refs, jid, _ = relation_build(env, provider)
    frozen = policy(env, jid)
    change_record(env, refs[endpoint], change)
    assert semantic_edges(env) == []
    other = refs[1 - endpoint]
    assert env.call("GET", f"/versions/{other[1]}").status_code == 200
    links = env.call("GET", f"/wiki/pages/{other[0]}/links")
    assert links.status_code == 200, links.text
    assert links.json()["outgoing"] == links.json()["incoming"] == []
    if change in {"body", "epoch"}:
        assert env.call("GET", f"/versions/{refs[endpoint][1]}").status_code == 200
    assert policy(env, jid) == frozen


@pytest.mark.parametrize("endpoint", [0, 1])
@pytest.mark.parametrize("change", ["body", "acl"])
def test_generated_endpoint_snapshot_is_checked_without_reference_context(env, provider, endpoint, change):
    source = draft_source(env)
    use_output(provider)
    jid = queue(env, build_input(env, [source]))
    result = finish(env, jid)
    assert len(semantic_edges(env)) == 1
    assert policy(env, jid)["reference_snapshot"] == []
    rid, vid = result["created_resource_ids"][endpoint], result["created_version_ids"][endpoint]
    with env.db() as db:
        block = db.scalar(select(m.ContentBlock).where(
            m.ContentBlock.version_id == vid, m.ContentBlock.block_type == "paragraph"))
        record = (rid, vid, block.block_id)
    change_record(env, record, change)
    assert semantic_edges(env) == []
    if change == "body":
        # A legitimate edit remains readable; only the old proposal has expired.
        assert env.call("GET", f"/versions/{vid}").status_code == 200
    else:
        assert env.call("GET", f"/versions/{vid}").status_code == 404
    other = result["created_resource_ids"][1 - endpoint]
    links = env.call("GET", f"/wiki/pages/{other}/links")
    assert links.status_code == 200, links.text
    assert links.json()["outgoing"] == links.json()["incoming"] == []


@pytest.mark.parametrize("mode", ["knowledge_points", "relations"])
@pytest.mark.parametrize("visibility", ["restricted", "foreign_space", "document"])
def test_references_must_be_visible_knowledge_in_the_same_space(env, provider, mode, visibility):
    source = draft_source(env)
    good = page(env, "当前空间参考")
    bad = page(env, "不允许外发的参考节点", restricted=visibility == "restricted",
               kind="document" if visibility == "document" else "knowledge")
    if visibility == "foreign_space":
        # Grant full access there to prove the rejection is a space boundary,
        # rather than accidentally passing because the node is unreadable.
        with env.db.begin() as db:
            sid = svc.uid()
            db.add(m.Space(id=sid, name="另一个合成空间"))
            db.flush()
            for role in ("reader", "editor", "reviewer", "publisher", "admin"):
                db.add(m.SpaceMember(space_id=sid, user_id=env.owner, role=role))
            db.get(m.Resource, bad[0]).space_id = sid
        assert env.call("GET", f"/resources/{bad[0]}").status_code == 200
    before = content_snapshot(env)
    response = env.call("POST", "/wiki/builds", build_input(env, [source], mode=mode, references=[good, bad]))
    assert response.status_code >= 400, response.text
    assert response.json()["code"] == ("NOT_FOUND" if visibility == "restricted" else "WIKI_REFERENCE_NOT_VISIBLE")
    assert "不允许外发的参考节点" not in response.text
    assert provider.calls == 0 and provider.requests == []
    assert content_snapshot(env) == before
    with env.db() as db:
        assert list(db.scalars(select(m.Job))) == []


@pytest.mark.parametrize("mode", ["knowledge_points", "relations"])
@pytest.mark.parametrize("change", ["body", "epoch", "acl", "delete"])
def test_reference_change_during_completion_prevents_atomic_commit(env, provider, mode, change):
    source = draft_source(env)
    refs = [page(env, "并发参考甲", state="DRAFT"), page(env, "并发参考乙", state="DRAFT")]
    use_output(provider, relation_only=mode == "relations")
    jid = queue(env, build_input(env, [source], mode=mode, references=refs))
    after_change = {}

    def mutate():
        change_record(env, refs[0], change)
        after_change["content"] = content_snapshot(env)

    provider.after_call = mutate
    with pytest.raises((wiki.WikiBuildError, svc.APIError)) as error:
        execute(env, jid)
    assert error.value.code == ("WIKI_REFERENCE_CHANGED" if change in {"body", "epoch"} else "NOT_FOUND")
    assert provider.calls == 1
    assert_no_build_artifacts(env, jid, after_change["content"])


@pytest.mark.parametrize("mode", ["knowledge_points", "relations"])
def test_source_change_during_completion_prevents_commit(env, provider, mode):
    source = draft_source(env)
    refs = [page(env, "并发节点甲"), page(env, "并发节点乙")]
    use_output(provider, relation_only=mode == "relations")
    jid = queue(env, build_input(env, [source], mode=mode, references=refs))
    after_change = {}

    def mutate():
        change_record(env, source, "body")
        after_change["content"] = content_snapshot(env)

    provider.after_call = mutate
    with pytest.raises(wiki.WikiBuildError, match="^WIKI_SOURCE_CHANGED$"):
        execute(env, jid)
    assert provider.calls == 1
    assert_no_build_artifacts(env, jid, after_change["content"])


def test_atomic_references_stay_shallow_but_relation_only_accepts_atomic_nodes(env, provider):
    source = draft_source(env)
    entry = page(env, "原有估值专题入口")
    use_output(provider)
    atomic = finish(env, queue(env, build_input(env, [source], references=[entry])))
    assert provider.requests[0]["reference_nodes"][0]["title"] == "原有估值专题入口"
    refs = [(rid, vid) for rid, vid in zip(atomic["created_resource_ids"], atomic["created_version_ids"])]
    response = env.call("POST", "/wiki/builds", build_input(env, [source], references=refs))
    assert response.status_code >= 400 and response.json()["code"] == "WIKI_REFERENCE_USE_TOPIC_ENTRY"
    assert provider.calls == 1
    before = content_snapshot(env)
    use_output(provider, relation_only=True, kind="EXPLAINS")
    relation_result = finish(env, queue(env, build_input(env, [source], mode="relations", references=refs)))
    assert relation_result["created_resource_ids"] == [] and relation_result["semantic_relations_created"] == 1
    assert provider.calls == 2 and content_snapshot(env) == before


def test_proposed_cycle_does_not_become_formal_dependency_or_evidence(env, provider):
    source = draft_source(env)
    use_output(provider, cycle=True)
    result = finish(env, queue(env, build_input(env, [source])))
    rids, vids = result["created_resource_ids"], result["created_version_ids"]
    edges = semantic_edges(env)
    assert {(edge["source"], edge["target"]) for edge in edges} == {(rids[0], rids[1]), (rids[1], rids[0])}
    assert all(edge["verification_status"] == "PROPOSED" for edge in edges)
    with env.db() as db:
        assert list(db.scalars(select(m.RelationEdge))) == []
        assert list(db.scalars(select(m.Release))) == []
        actor = db.get(m.User, env.owner)
        for vid in vids:
            version = svc.version_access(db, actor, vid)
            assert svc.dependency_ids(db, version) == {source[1]}
        assert svc.eligible_evidence(db, actor, env.space) == []
        assert wiki.choose_wiki_evidence(db, actor, env.space, "估值输入参数") == []
    for vid in vids:
        current = env.call("GET", f"/versions/{vid}")
        assert current.status_code == 200, current.text
        assert env.call("GET", f"/versions/{vid}/relations").json() == []
        for action in ("submit", "publish"):
            response = env.call("POST", f"/versions/{vid}/{action}", etag=current.headers["etag"])
            assert response.status_code == 409 and response.json()["code"] == "WIKI_UNVERIFIED_SOURCES"
    # Removable presentation labels cannot turn the cyclic graph into published evidence.
    for rid in rids:
        current = env.call("GET", f"/resources/{rid}")
        response = env.call("PATCH", f"/resources/{rid}", {"tags": []}, etag=current.headers["etag"])
        assert response.status_code == 200, response.text
    current = env.call("GET", f"/versions/{vids[0]}")
    assert env.call("POST", f"/versions/{vids[0]}/publish", etag=current.headers["etag"]).json()["code"] == "WIKI_UNVERIFIED_SOURCES"
    with env.db() as db:
        assert list(db.scalars(select(m.RelationEdge))) == list(db.scalars(select(m.Release))) == []
        assert svc.eligible_evidence(db, db.get(m.User, env.owner), env.space) == []
        assert not list(db.scalars(select(m.Job).where(m.Job.kind == "PUBLISH")))


@pytest.mark.parametrize("mode", ["knowledge_points", "relations"])
@pytest.mark.parametrize("explicit_scope", [True, False])
def test_short_sentences_and_headings_are_accounted_for_without_silent_scope_loss(env, provider, mode, explicit_scope):
    source = draft_source(env)
    short = append_block(env, source, "仅限当日。", ordinal=1)
    heading = append_block(env, source, "例外", ordinal=2, block_type="heading")
    tail = append_block(env, source, "须复核。", ordinal=3)
    refs = [page(env, "范围节点甲"), page(env, "范围节点乙")]
    overrides = {"source_block_ids": [short, heading, tail]} if explicit_scope else {}
    use_output(provider, relation_only=mode == "relations")
    result = finish(env, queue(env, build_input(env, [source], mode=mode, references=refs, **overrides)))
    sent = provider.requests[0]["sources"]
    text = "\n".join(item["excerpt"] for item in sent)
    assert all(value in text for value in ["仅限当日。", "例外", "须复核。"])
    if explicit_scope:
        assert "计量日期" not in text
    coverage = result["coverage"]
    expected_blocks = {short, heading, tail} | (set() if explicit_scope else {source[2]})
    assert coverage["corpus_source_blocks"] == 4
    assert coverage["explicit_block_scope"] is explicit_scope
    assert coverage["total_source_blocks"] == coverage["selected_blocks"] == coverage["cited_blocks"] == len(expected_blocks)
    assert coverage["omitted_blocks"] == coverage["uncited_selected_blocks"] == 0
    dispositions = coverage["source_dispositions"]
    assert len(dispositions) == len(sent)
    assert {item["evidence_id"] for item in dispositions} == {item["id"] for item in sent}
    assert all(item["disposition"] == "EXTRACTED" for item in dispositions)
    assert {block["block_id"] for item in dispositions for block in item["source_blocks"]} == expected_blocks
    edge, = semantic_edges(env)
    assert edge["evidence_count"] == len(expected_blocks)


def test_explicit_scope_rejects_blocks_from_unselected_source_before_model(env, provider):
    source, other = draft_source(env), draft_source(env)
    response = env.call("POST", "/wiki/builds", build_input(env, [source], source_block_ids=[other[2]]))
    assert response.status_code == 422 and response.json()["code"] == "WIKI_BLOCK_NOT_IN_SOURCES"
    assert provider.calls == 0


@pytest.mark.parametrize("disposition", ["SUPPORTING", "NO_REUSABLE_POINT", "NEEDS_REVIEW"])
def test_unextracted_input_has_explicit_disposition_and_remains_in_frozen_scope(env, provider, disposition):
    sources = [draft_source(env), draft_source(env)]

    def output(_old, data):
        result = semantic_output(data)
        unused = data["sources"][-1]["id"]
        result["relations"][0]["evidence_ids"].remove(unused)
        for item in result["pages"]:
            item["blocks"][0]["evidence_ids"].remove(unused)
        result["source_dispositions"][-1].update(disposition=disposition, reason="该输入尚需人工判断。")
        return result

    provider.output_transform = output
    jid = queue(env, build_input(env, sources))
    result = finish(env, jid)
    coverage = result["coverage"]
    assert coverage["selected_fragments"] == 2 and coverage["cited_fragments"] == 1
    assert coverage["omitted_blocks"] == 0 and coverage["uncited_selected_blocks"] == 1
    assert coverage["status"] == "PARTIAL" and coverage["truncated"] is True
    statuses = {item["evidence_id"]: item for item in coverage["source_dispositions"]}
    unused_id = provider.requests[0]["sources"][-1]["id"]
    assert statuses[unused_id]["disposition"] == disposition
    assert len(semantic_edges(env)) == 1
    # Even an uncited S input was sent to the model and must remain ACL-bound.
    unused_resource = statuses[unused_id]["source_blocks"][0]["resource_id"]
    source = next(item for item in sources if item[0] == unused_resource)
    change_record(env, source, "epoch")
    assert semantic_edges(env) == []
    assert not set(result["created_resource_ids"]) & {item["id"] for item in graph(env)["nodes"]}


@pytest.mark.parametrize("mode", ["knowledge_points", "relations"])
@pytest.mark.parametrize("budget", ["source_bytes", "fragment_count", "complete_request"])
def test_over_budget_scope_requires_explicit_split_without_call_or_partial_commit(env, provider, monkeypatch, mode, budget):
    source = draft_source(env)
    refs = [page(env, "预算节点甲"), page(env, "预算节点乙")]
    if budget == "source_bytes":
        append_block(env, source, "需要核对估值模型的输入参数和适用条件。" * 250)
    elif budget == "fragment_count":
        # Raise the byte bound only to isolate the independently enforced count bound.
        monkeypatch.setattr(wiki, "MAX_SOURCE_BYTES", 1_000_000)
        for index in range(wiki.MAX_SOURCE_BLOCKS):
            # Ordinal gaps prevent coherent passage joining from masking the count.
            append_block(env, source, f"第{index}项需复核。", ordinal=2 * index + 2)
    else:
        # Valid maximum reference count, each individually valid; their titles,
        # summaries and schema can still overflow the complete request envelope.
        refs = [page(env, f"参考{i:02d}" + "估值适用条件" * 25, text="需核对参考节点的条件与参数。" * 20)
                for i in range(24)]
    use_output(provider, relation_only=mode == "relations")
    jid = queue(env, build_input(env, [source], mode=mode, references=refs))
    before = content_snapshot(env)
    with pytest.raises(wiki.WikiBuildError) as error:
        execute(env, jid)
    assert provider.calls == 0 and provider.requests == []
    assert_no_build_artifacts(env, jid, before)
    assert error.value.code == "WIKI_SCOPE_REQUIRES_SPLIT"


@pytest.mark.parametrize("count", [0, 1])
def test_relation_only_requires_at_least_two_distinct_reference_nodes(env, provider, count):
    refs = [page(env, "单独参考")][:count]
    response = env.call("POST", "/wiki/builds", build_input(env, [draft_source(env)], mode="relations", references=refs))
    assert response.status_code == 422 and response.json()["code"] == "WIKI_RELATIONS_NEED_ENDPOINTS"
    assert provider.calls == 0


@pytest.mark.parametrize("fault", ["too_many", "duplicate", "published_semantics"])
def test_request_bounds_reject_before_model(env, provider, fault):
    source = draft_source(env)
    refs = [page(env, "合法参考")]
    data = build_input(env, [source], references=refs)
    if fault == "too_many":
        data["reference_resource_ids"] = [svc.uid() for _ in range(25)]
    elif fault == "duplicate":
        data["reference_resource_ids"] *= 2
    else:
        data["source_mode"] = "published"
    response = env.call("POST", "/wiki/builds", data)
    assert response.status_code == 422, response.text
    if fault == "published_semantics":
        assert response.json()["code"] == "WIKI_SEMANTIC_DRAFT_ONLY"
    assert provider.calls == 0
    with env.db() as db:
        assert list(db.scalars(select(m.Job))) == []


def test_omitted_granularity_preserves_original_topic_contract(env, provider):
    source = page(env, "已核验发布来源", kind="document")
    data = build_input(env, [source])
    for key in ("granularity", "source_mode", "reference_resource_ids"):
        data.pop(key)
    # Default FakeProvider emits legacy pages without roles or dispositions.
    result = finish(env, queue(env, data))
    assert provider.calls == 1 and result["source_mode"] == "published"
    assert result["granularity"] == "topic" and len(result["created_resource_ids"]) == 1
    assert result["semantic_relations_created"] == 0 and semantic_edges(env) == []
    assert "reference_nodes" not in provider.requests[0]
    assert "node_role" not in provider.requests[0]["schema"]["properties"]["pages"]["items"]["required"]
    with env.db() as db:
        resource = db.get(m.Resource, result["created_resource_ids"][0])
        assert not any(tag.startswith("node-role:") for tag in resource.tags)
    data["source_resource_ids"] = [draft_source(env)[0]]
    rejected = env.call("POST", "/wiki/builds", data)
    assert rejected.status_code == 409 and rejected.json()["code"] == "WIKI_SOURCE_NOT_READY"
    assert provider.calls == 1
