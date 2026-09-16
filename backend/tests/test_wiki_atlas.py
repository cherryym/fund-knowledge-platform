"""Complete Atlas readers, real temporary SQLite/API, no model or runtime IO."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import page
from test_wiki_read_concurrency import offline_identity_and_network as offline_identity_and_network  # noqa: PLC0414
from test_wiki_unverified import draft_source

from fund_kb import api_catalog, api_content, api_wiki, wiki
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.db import ReadOnlySession
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.projection_read import projection_read

DAY = "2026-09-08"


def hydrate(env, suffix=""):
    result = env.call("GET", f"/wiki/workspace?space_id={env.space}&business_date={DAY}&hydrate=true{suffix}")
    assert result.status_code == 200, result.text
    return result.json()


def assert_readers_equal_endpoints(env, atlas):
    """Use the actual independent handlers, sharing only a temporary read snapshot."""
    assert Path(wiki.__file__).resolve().parents[1] == Path(__file__).resolve().parents[1]
    assert set(atlas["readers"]) == {p["id"] for p in atlas["pages"]}
    with env.app.state.read_session_factory() as db, projection_read(db):
        assert isinstance(db, ReadOnlySession)
        user = db.get(m.User, env.owner)
        for item in atlas["pages"]:
            reader = atlas["readers"][item["id"]]
            assert set(reader) == {"resource", "version", "links"}
            assert reader["resource"]["id"] == reader["version"]["resource_id"] == item["id"]
            assert reader["resource"]["kind"] in {"knowledge", "template"}
            assert reader["version"]["id"] == item["version_id"]
            ctx = SimpleNamespace(db=db, user=user, id=item["id"], query={"business_date": DAY})
            assert reader["resource"] == api_catalog.get_resource(ctx).body
            assert reader["links"] == api_wiki.get_links(ctx).body
            ctx.id = item["version_id"]
            assert reader["version"] == api_content.get_version(ctx).body
            assert set(reader["links"]) == {"outgoing", "incoming", "sources", "unresolved", "truncated"}
            assert reader["links"]["truncated"] is False
        assert not db.new and not db.dirty and not db.deleted
    if atlas["pages"]:
        assert atlas["reader"] == atlas["readers"][atlas["pages"][0]["id"]]
    else:
        assert "reader" not in atlas


def other_space(env):
    sid = svc.uid()
    with env.db.begin() as db:
        db.add(m.Space(id=sid, name="合成Atlas外部空间"))
        db.flush()
        for role in ("reader", "editor", "reviewer", "admin"):
            db.add(m.SpaceMember(space_id=sid, user_id=env.owner, role=role))
    return SimpleNamespace(db=env.db, space=sid, owner=env.owner)


def freeze_sources(env, target, sources):
    """Synthetic, source/hash/epoch-bound provenance, without a generation job."""
    with env.db.begin() as db:
        snapshots = []
        for source in sources:
            resource, version = db.get(m.Resource, source[0]), db.get(m.ResourceVersion, source[1])
            blob = db.get(m.Blob, version.source_blob_id)
            snapshots.append({"resource_id": resource.id, "version_id": version.id,
                "content_sha256": svc.check_frozen_hash(db, version), "access_epoch": resource.access_epoch,
                "source_blob_sha256": blob.sha256})
        resource = db.get(m.Resource, target[0])
        db.add(m.RuntimePolicy(id=svc.uid(), name=f"wiki-provenance:{target[0]}", updated_by=env.owner,
            config={"space_id": resource.space_id, "resource_id": target[0], "created_version_id": target[1],
                "source_mode": wiki.DRAFT_SOURCE_MODE, "source_version_ids": [s[1] for s in sources],
                "source_snapshot": snapshots, "reference_snapshot": []}))


def test_more_than_200_complete_readers_match_independent_resource_version_and_links(env, monkeypatch):
    source = page(env, "Atlas原文", kind="document", text="ATLAS_DOCUMENT_BODY_NOT_PRELOADED：合成原始资料正文。")
    records = [page(env, f"Atlas知识{i:03d}", kind="template" if i % 7 == 0 else "knowledge", cites=[source])
               for i in range(205)]
    hidden = page(env, "Atlas不可见知识", restricted=True)
    counts = Counter()
    with monkeypatch.context() as patch:
        for name in ("visible_pages", "_linked_pages", "_atlas_link_views", "_atlas_links", "_edges"):
            original = getattr(wiki, name)

            def counted(*args, _name=name, _original=original, **kwargs):
                counts[_name] += 1
                return _original(*args, **kwargs)

            patch.setattr(wiki, name, counted)

        def forbidden(*_args, **_kwargs):
            pytest.fail("Hydration must not invoke page_links once per manuscript")

        patch.setattr(wiki, "page_links", forbidden)
        atlas = hydrate(env)
    assert counts == {"visible_pages": 1, "_linked_pages": 1, "_atlas_link_views": 1,
                      "_atlas_links": 1, "_edges": 2}
    assert len(atlas["readers"]) == atlas["stats"]["total_pages"] == 205
    assert set(atlas["readers"]) == {r[0] for r in records}
    assert atlas["truncated"] is False and not atlas["graph"]["truncated"]
    assert len(atlas["graph"]["nodes"]) == 206 and len(atlas["graph"]["edges"]) == 205
    assert source[0] not in atlas["readers"] and hidden[0] not in atlas["readers"]
    assert "ATLAS_DOCUMENT_BODY_NOT_PRELOADED" not in str(atlas)
    assert_readers_equal_endpoints(env, atlas)
    graph = env.call("GET", f"/wiki/graph?space_id={env.space}&business_date={DAY}")
    assert graph.status_code == 200 and atlas["graph"] == graph.json()


def test_complete_four_link_types_and_current_version_are_bound_to_each_reader(env):
    source = page(env, "Atlas证据", kind="document")
    old = page(env, "Atlas版本页", text="旧版本正文。")
    current = page(env, "Atlas版本页", resource_id=old[0], cites=[source],
                   text="当前版本正文。[[Atlas模板]] [[未创建的合成标题]]")
    template = page(env, "Atlas模板", kind="template", text="模板正文和操作步骤。")
    citing = page(env, "Atlas反链页", cites=[current])
    with env.db.begin() as db:
        db.add(m.RelationEdge(id=svc.uid(), source_version_id=current[1], target_resource_id=template[0],
                             relation_type="REQUIRES", conditions={}, evidence_version_id=source[1], evidence_block_id=source[2]))
        db.flush()
        version = db.get(m.ResourceVersion, current[1])
        version.content_sha256 = svc.check_frozen_hash(db, version)
    atlas = hydrate(env)
    assert_readers_equal_endpoints(env, atlas)
    reader = atlas["readers"][current[0]]
    assert reader["version"]["id"] == current[1] != old[1]
    assert {r["relation_type"] for r in reader["links"]["outgoing"]} == {"REQUIRES", "WIKI_LINK"}
    assert reader["links"]["sources"][0]["id"] == source[0]
    assert reader["links"]["sources"][0]["version_id"] == source[1]
    assert citing[0] in {r["id"] for r in reader["links"]["incoming"]}
    assert reader["links"]["unresolved"] == [{"title": "未创建的合成标题"}]


@pytest.mark.parametrize("revocation", ["grant", "space"])
def test_visible_cross_space_backlinks_are_complete_but_never_expand_global_graph(env, revocation):
    first, second = page(env, "Atlas本空间甲"), page(env, "Atlas本空间乙")
    foreign = other_space(env)
    incoming = page(foreign, "跨空间可见反链", cites=[first, second])
    hidden = page(foreign, "跨空间隐藏反链", cites=[first], restricted=True)
    unrelated = page(foreign, "跨空间无关知识")
    atlas = hydrate(env)
    assert_readers_equal_endpoints(env, atlas)
    for record in (first, second):
        assert incoming[0] in {r["id"] for r in atlas["readers"][record[0]]["links"]["incoming"]}
    assert set(atlas["readers"]) == {first[0], second[0]}
    assert {n["id"] for n in atlas["graph"]["nodes"]} == {first[0], second[0]}
    assert hidden[0] not in str(atlas) and unrelated[0] not in str(atlas)
    assert atlas["graph"] == env.call("GET", f"/wiki/graph?space_id={env.space}&business_date={DAY}").json()
    with env.db.begin() as db:
        if revocation == "grant":
            db.get(m.Resource, incoming[0]).restricted = True
        else:
            db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == foreign.space))
    revised = hydrate(env)
    assert incoming[0] not in str(revised)
    assert_readers_equal_endpoints(env, revised)


def test_readers_do_not_resolve_titles_from_another_readers_incoming_only_scope(env):
    source = draft_source(env)
    first = page(env, "独立范围甲", text="[[待核验估值源]]")
    second = page(env, "独立范围乙", text="[[待核验估值源]]")
    foreign = other_space(env)
    incoming = page(foreign, "仅乙页的外部反链", state="DRAFT", cites=[source, second])
    freeze_sources(env, incoming, [source])
    atlas = hydrate(env)
    assert_readers_equal_endpoints(env, atlas)
    first_links, second_links = (atlas["readers"][r[0]]["links"] for r in (first, second))
    assert first_links["unresolved"] == [{"title": "待核验估值源"}]
    assert first_links["outgoing"] == first_links["incoming"] == []
    assert second_links["unresolved"] == []
    assert source[0] in {r["id"] for r in second_links["outgoing"]}
    assert incoming[0] in {r["id"] for r in second_links["incoming"]}
    assert {n["id"] for n in atlas["graph"]["nodes"]} == {first[0], second[0]}


def test_different_readers_preserve_distinct_frozen_versions_of_same_source(env):
    old_source = draft_source(env)
    with env.db.begin() as db:
        # The real schema permits only one DRAFT per resource/author.
        version = db.get(m.ResourceVersion, old_source[1])
        version.content_sha256 = svc.check_frozen_hash(db, version)
        version.state = "IN_REVIEW"
    new_source = page(env, "待核验估值源", resource_id=old_source[0], kind="document", state="DRAFT",
                      text="另一版合成估值来源，仅用于测试冻结版本不能串用。")
    first = page(env, "冻结版本甲", text="[[待核验估值源]]")
    second = page(env, "冻结版本乙", text="[[待核验估值源]]")
    foreign = other_space(env)
    for index, (source, target) in enumerate(((old_source, first), (new_source, second))):
        incoming = page(foreign, f"冻结版本外部反链{index}", state="DRAFT", cites=[source, target])
        freeze_sources(env, incoming, [source])
    atlas = hydrate(env)
    assert_readers_equal_endpoints(env, atlas)
    for record, source in ((first, old_source), (second, new_source)):
        links = atlas["readers"][record[0]]["links"]
        assert [(r["id"], r["version_id"]) for r in links["outgoing"]] == [(source[0], source[1])]
    assert old_source[0] not in atlas["readers"]


def test_semantic_edge_metadata_and_proposed_cycle_match_each_independent_reader(env):
    source = draft_source(env)
    first = page(env, "Atlas语义甲", state="DRAFT", cites=[source])
    second = page(env, "Atlas语义乙", state="DRAFT", cites=[source])
    for target in (first, second):
        freeze_sources(env, target, [source])
    with env.db.begin() as db:
        provenance = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"wiki-provenance:{first[0]}"))
        endpoints = []
        for target in (first, second):
            version, resource = db.get(m.ResourceVersion, target[1]), db.get(m.Resource, target[0])
            endpoints.append({"resource_id": resource.id, "version_id": version.id,
                              "content_sha256": svc.check_frozen_hash(db, version), "access_epoch": resource.access_epoch})
        db.add(m.RuntimePolicy(id=svc.uid(), name=f"wiki-semantic:{env.space}:{svc.uid()}", updated_by=env.owner,
            config={"space_id": env.space, "source_snapshot": provenance.config["source_snapshot"], "reference_snapshot": [],
                "proposals": [{"endpoints": pair, "relation_type": "EXPLAINS", "explanation": "合成的待复核语义解释。",
                               "anchors": [{"version_id": source[1], "block_id": source[2]}]}
                              for pair in (endpoints, list(reversed(endpoints)))]}))
    atlas = hydrate(env)
    assert_readers_equal_endpoints(env, atlas)
    for target in (first, second):
        links = atlas["readers"][target[0]]["links"]
        semantic = [item for item in links["outgoing"] if item["relation_type"] == "EXPLAINS"]
        assert len(semantic) == 1
        assert semantic[0]["verification_status"] == "PROPOSED" and semantic[0]["evidence_count"] == 1
        assert semantic[0]["explanation"] == "合成的待复核语义解释。"
    with env.db() as db:
        assert list(db.scalars(select(m.RelationEdge))) == []
        assert list(db.scalars(select(m.Release))) == []


@pytest.mark.parametrize("change", ["acl", "delete", "epoch", "body", "blob"])
def test_new_hydrate_rechecks_derived_source_and_omits_hidden_manuscripts(env, change):
    source = draft_source(env)
    target = page(env, "Atlas待核验知识", state="DRAFT", cites=[source])
    freeze_sources(env, target, [source])
    safe = page(env, "Atlas独立知识")
    before = hydrate(env)
    assert set(before["readers"]) == {target[0], safe[0]}
    assert_readers_equal_endpoints(env, before)
    with env.db.begin() as db:
        resource, version = db.get(m.Resource, source[0]), db.get(m.ResourceVersion, source[1])
        if change == "acl":
            resource.restricted = True
        elif change == "delete":
            resource.deleted_at = svc.now()
        elif change == "epoch":
            resource.access_epoch += 1
        elif change == "blob":
            db.get(m.Blob, version.source_blob_id).scan_state = "QUARANTINED"
        else:
            block = db.get(m.ContentBlock, (source[1], source[2]))
            block.data = {"text": "合成来源正文已经变更，不得复用旧快照。"}
            block.search_text = block_text({"block_type": block.block_type, "data": block.data})
            block.content_sha256 = text_sha256(block.search_text)
            version.revision += 1
    after = hydrate(env)
    assert set(after["readers"]) == {safe[0]}
    assert target[0] not in str(after) and target[1] not in str(after)
    assert_readers_equal_endpoints(env, after)


@pytest.mark.parametrize("suffix", ["&q=筛选甲", "&category=筛选/甲", "&kind=template", "&status=DRAFT"])
def test_filtered_readers_only_cover_returned_pages_and_graph_keeps_own_filter_contract(env, suffix):
    page(env, "筛选甲", category="筛选/甲")
    page(env, "筛选乙", category="筛选/乙", kind="template")
    page(env, "筛选草稿", state="DRAFT")
    atlas = hydrate(env, suffix)
    assert_readers_equal_endpoints(env, atlas)
    assert len(atlas["readers"]) == 1
    graph_suffix = suffix if suffix.startswith(("&q=", "&category=")) else ""
    expected = env.call("GET", f"/wiki/graph?space_id={env.space}&business_date={DAY}{graph_suffix}")
    assert expected.status_code == 200 and atlas["graph"] == expected.json()


def test_empty_and_nonhydrated_workspaces_do_not_fabricate_readers(env):
    atlas = hydrate(env)
    assert atlas["readers"] == {} and "reader" not in atlas
    assert atlas["graph"]["nodes"] == atlas["graph"]["edges"] == []
    plain = env.call("GET", f"/wiki/workspace?space_id={env.space}").json()
    assert "reader" not in plain and "readers" not in plain and "graph" not in plain


def test_draft_status_body_uses_list_version_while_links_and_global_graph_keep_endpoint_contract(env):
    source = page(env, "状态筛选原件", kind="document")
    published = page(env, "有草稿的已发布知识", cites=[source])
    draft = page(env, "后续草稿标题", resource_id=published[0], state="DRAFT", text="后续草稿的独立正文。")
    atlas = hydrate(env, "&status=DRAFT")
    assert_readers_equal_endpoints(env, atlas)
    assert atlas["readers"][draft[0]]["version"]["id"] == draft[1]
    node = next(n for n in atlas["graph"]["nodes"] if n["id"] == draft[0])
    assert node["version_id"] == published[1]


def test_incoming_batch_queries_never_exceed_400_parameters(env, monkeypatch):
    # Exercise only batching with synthetic IDs; authorization is tested above.
    pages = {}
    for _ in range(401):
        rid, vid = svc.uid(), svc.uid()
        pages[rid] = {"resource": SimpleNamespace(id=rid), "version": SimpleNamespace(id=vid), "blocks": []}
    monkeypatch.setattr(wiki, "_linked_steps", lambda *_args: ())
    sizes = []

    def record(_connection, _cursor, statement, parameters, _context, _many):
        if " IN (" in statement:
            sizes.append(len(parameters))

    engine = env.app.state.read_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        with env.app.state.read_session_factory() as db, projection_read(db):
            views = wiki._atlas_link_views(db, db.get(m.User, env.owner), pages, list(pages), {})
            assert len(views) == 401 and all(set(view) == set(pages) for view in views.values())
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert sorted(sizes) == [1, 1, 400, 400]
