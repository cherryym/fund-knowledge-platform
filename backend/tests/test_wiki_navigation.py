"""Inline navigation: synthetic in-memory DB only, no service/model/file writes."""
import copy
import json
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import event, select
from test_reference_review import env as review_env  # noqa: F401
from test_reference_review import make_version

from fund_kb import ai_transport, providers
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb import wiki_navigation as nav
from fund_kb.db import make_session_factory
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.wiki_catalog import build_catalog
from fund_kb.wiki_maintenance import ENTRY_PREFIX
from fund_kb.wiki_section_reader import read_scoped_pages


@pytest.fixture
def env(review_env, monkeypatch):  # noqa: F811 - imported fixture dependency
    def forbidden(*args, **kwargs):
        pytest.fail("Inline navigation must not access providers, credentials or models")

    for module, name in ((providers, "complete"), (providers, "_network"),
                         (providers, "_master_key"), (ai_transport, "post_json")):
        monkeypatch.setattr(module, name, forbidden)
    assert str(review_env.db.kw["bind"].url) == "sqlite:///:memory:"
    nav.clear_navigation_cache()
    with review_env.db.begin() as db:
        for vid in (review_env.source, review_env.wiki):
            version = db.get(m.ResourceVersion, vid)
            version.content_sha256 = svc.check_frozen_hash(db, version)
    yield review_env
    nav.clear_navigation_cache()


def add(env, title, text="合成导航正文。", *, aliases=(), kind="knowledge", frozen=True):
    vid = make_version(env, kind)
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, vid)
        version.title = title
        resource = db.get(m.Resource, version.resource_id)
        resource.name, resource.tags = title, ["alias:" + alias for alias in aliases]
        block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == vid))
        block.data, block.search_text, block.content_sha256 = {"text": text}, text, text_sha256(text)
        if frozen:
            db.flush()
            version.content_sha256 = svc.check_frozen_hash(db, version)
    return vid


def catalog(env, *, user=None):
    with env.db() as db:
        return build_catalog(db, user or env.owner, env.space)


def ids(pages):
    return {page["version_id"]: pid for pid, page in pages.items()}


@contextmanager
def sql(env):
    statements = []
    engine = env.db.kw["bind"]

    def capture(_conn, _cursor, statement, *_):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def load(env, pages, *, user=None, space=None):
    # Use the real read-only Session guard, not a mock database interface.
    factory = make_session_factory(env.db.kw["bind"], read_only=True)
    with factory() as db, sql(env) as statements:
        result = nav.load_inline_navigation(db, user or env.owner, space or env.space, pages)
    assert all(statement.lstrip().startswith("select ") or statement == "begin deferred"
               for statement in statements), statements
    return result, [statement for statement in statements if statement != "begin deferred"]


def test_exact_alias_display_alias_unicode_and_document_targets(env):
    target = add(env, "公开主条目", aliases=["ＡBC"])
    document = add(env, "原文标题", "DOCUMENT_BODY_MUST_NOT_LOAD" * 800, kind="document")
    source = add(env, "入口", "只参见[[公开主条目]]、[[ abc |显示标题不是目标]]、[[原文标题]]。")
    pages = catalog(env)
    pids, before = ids(pages), copy.deepcopy(pages)
    result, statements = load(env, pages)
    assert result["links"][pids[source]] == sorted([pids[target], pids[document]])
    assert result["links"][pids[target]] == []  # Directed, no invented reciprocal claim.
    assert result["ambiguous"] == {} and result["invalid_pages"] == []
    assert result["warnings"] == [] and not result["cache_hit"]
    assert len([s for s in statements if "content_blocks.data" in s]) == 1
    assert pages == before and all(not page["body_loaded"] and page["records"] == [] for page in pages.values())
    assert set(result) == {"links", "ambiguous", "invalid_pages", "warnings", "cache_hit",
        "body_blocks_loaded", "body_characters_loaded", "knowledge_pages", "cache_signature"}


def test_ambiguous_names_do_not_pick_titles_over_aliases_or_guess_fuzzy_matches(env):
    left = add(env, "同名页面")
    right = add(env, "不同标题", aliases=["同名页面"])
    fuzzy = add(env, "估值完整规则")
    source = add(env, "入口", "[[同名页面]] [[估值完整]] [[ ]] [[入口]]")
    pages = catalog(env)
    pids = ids(pages)
    result, _ = load(env, pages)
    assert result["links"][pids[source]] == []
    assert result["ambiguous"] == {pids[source]: [
        {"candidate_page_ids": sorted([pids[left], pids[right]])}]}
    assert pids[fuzzy] not in result["ambiguous"][pids[source]][0]["candidate_page_ids"]
    assert {"INLINE_NAVIGATION_AMBIGUOUS", "INLINE_NAVIGATION_TARGET_UNAVAILABLE"} <= set(result["warnings"])


def test_parser_excludes_code_escapes_embeds_and_display_aliases(env):
    target = add(env, "可见目标")
    source = add(env, "入口", "`[[可见目标]]`\n\n```\n[[可见目标]]\n```\n\n"
        r"\[[可见目标]] ![[可见目标]] [[不存在|可见目标]]")
    pages = catalog(env)
    result, _ = load(env, pages)
    assert result["links"][ids(pages)[source]] == []
    assert ids(pages)[target] not in result["ambiguous"]


def test_unavailable_target_leaks_neither_label_nor_resource_id(env):
    hidden = add(env, "HIDDEN_TITLE_CANARY", "HIDDEN_BODY_CANARY")
    source = add(env, "入口", "[[HIDDEN_TITLE_CANARY]]")
    pages = catalog(env)
    hidden_pid = ids(pages)[hidden]
    hidden_rid = pages[hidden_pid]["resource_id"]
    del pages[hidden_pid]  # This call's authorized scope excludes this object.
    result, _ = load(env, pages)
    assert result["links"][ids(pages)[source]] == [] and result["ambiguous"] == {}
    serialized = json.dumps(result) + repr(nav._CACHE)
    assert all(value not in serialized for value in (hidden_pid, hidden_rid, hidden, "HIDDEN_TITLE_CANARY"))
    assert result["warnings"] == ["INLINE_NAVIGATION_TARGET_UNAVAILABLE"]


def test_current_sidecar_aliases_and_canonical_metadata_override_old_tags(env):
    target = add(env, "主条目", aliases=["删掉的别名"])
    old = add(env, "旧条目")
    source = add(env, "入口", "[[旧条目]] [[当前别名]] [[主键]] [[删掉的别名]]")
    with env.db.begin() as db:
        target_rid = db.get(m.ResourceVersion, target).resource_id
        for vid, aliases, key in ((target, ["当前别名"], "主键"), (old, ["当前别名"], None)):
            rid = db.get(m.ResourceVersion, vid).resource_id
            db.add(m.RuntimePolicy(id=svc.uid(), name=ENTRY_PREFIX + rid, updated_by=env.owner,
                config={"schema_version": 1, "space_id": env.space, "resource_id": rid,
                        "canonical_resource_id": target_rid, "canonical_key": key, "aliases": aliases}))
    pages = catalog(env)
    pids = ids(pages)
    assert pages[pids[old]]["canonical_page_id"] == pids[target]
    assert "删掉的别名" not in pages[pids[target]]["aliases"]
    result, _ = load(env, pages)
    assert result["links"][pids[source]] == [pids[target]]
    assert result["ambiguous"] == {}
    assert result["warnings"] == ["INLINE_NAVIGATION_TARGET_UNAVAILABLE"]


@pytest.mark.parametrize("change", ["cycle", "missing_page", "missing_resource", "unavailable"])
def test_canonical_chains_cannot_route_through_missing_or_invalid_nodes(env, change):
    left = add(env, "旧条目")
    right = add(env, "主条目")
    source = add(env, "入口", "[[旧条目]]")
    pages = catalog(env)
    pids = ids(pages)
    pages[pids[left]]["canonical_page_id"] = pids[right]
    if change == "cycle":
        pages[pids[right]]["canonical_page_id"] = pids[left]
    elif change == "missing_page":
        pages[pids[right]]["canonical_page_id"] = "HIDDEN_PAGE_CANARY"
    elif change == "missing_resource":
        pages[pids[right]]["canonical_resource_id"] = svc.uid()
    else:
        pages[pids[right]]["canonical_available"] = False
    result, _ = load(env, pages)
    assert result["links"][pids[source]] == []
    assert "HIDDEN_PAGE_CANARY" not in json.dumps(result)
    assert "INLINE_NAVIGATION_TARGET_UNAVAILABLE" in result["warnings"]


def test_hit_reuses_only_navigation_and_never_retains_body_or_returned_mutations(env):
    target = add(env, "条目")
    source = add(env, "入口", "PRIVATE_BODY_CANARY [[条目]]")
    pages = catalog(env)
    first, _ = load(env, pages)
    pids = ids(pages)
    first["links"][pids[source]].clear()
    pages[pids[source]]["records"] = [{"text": "ALREADY_READ_ANSWER_CANARY"}]
    pages[pids[source]]["body_loaded"] = True
    pages[pids[source]]["characters"] = 999
    pages[pids[source]]["links"] = ["OTHER_SCOPE_CANARY"]
    second, statements = load(env, dict(reversed(list(pages.items()))))
    assert second["cache_hit"] and statements == []
    assert second["links"][pids[source]] == [pids[target]]
    assert second["body_blocks_loaded"] == second["body_characters_loaded"] == 0
    assert first["cache_signature"] == second["cache_signature"]
    assert all(canary not in repr(nav._CACHE) for canary in
        ("PRIVATE_BODY_CANARY", "ALREADY_READ_ANSWER_CANARY", "OTHER_SCOPE_CANARY", "条目"))


@pytest.mark.parametrize("field,value", [
    ("_metadata_signature", "new-authority-stamp"), ("title", "新标题"),
    ("aliases", ["新别名"]), ("canonical_page_id", "not-in-scope"),
    ("canonical_resource_id", "not-in-scope"), ("canonical_key", "新主键"),
    ("version_id", None), ("resource_id", None), ("access_epoch", 2), ("block_count", 3),
])
def test_every_relevant_catalog_change_invalidates_navigation(env, field, value):
    target = add(env, "目标")
    add(env, "入口", "[[目标]]")
    pages = catalog(env)
    before, _ = load(env, pages)
    changed = copy.deepcopy(pages)
    changed[ids(pages)[target]][field] = svc.uid() if value is None else value
    after, _ = load(env, changed)
    assert not after["cache_hit"] and after["cache_signature"] != before["cache_signature"]


def test_user_space_and_current_catalog_membership_isolate_cache(env):
    target = add(env, "私有目标")
    source = add(env, "入口", "[[私有目标]]")
    pages = catalog(env)
    first, _ = load(env, pages)
    assert load(env, pages)[0]["cache_hit"]
    # Even identical IDs/metadata cannot cross the actor namespace.
    other_user, _ = load(env, pages, user=env.reviewer)
    assert not other_user["cache_hit"] and other_user["cache_signature"] != first["cache_signature"]
    other_space, statements = load(env, pages, space=svc.uid())
    assert not other_space["cache_hit"] and other_space["body_blocks_loaded"] == 0
    assert not any("content_blocks.data" in statement for statement in statements)
    assert all(not links for links in other_space["links"].values())
    subset = copy.deepcopy(pages)
    del subset[ids(pages)[target]]
    restricted, _ = load(env, subset)
    assert not restricted["cache_hit"] and restricted["links"][ids(subset)[source]] == []
    assert ids(pages)[target] not in json.dumps(restricted["links"])
    # Rebuilding the real catalog as an unprivileged actor yields no private drafts.
    reader_pages = catalog(env, user=env.reader)
    assert reader_pages == {}
    assert load(env, reader_pages, user=env.reader)[0]["links"] == {}


@pytest.mark.parametrize("damage", ["search_text", "data", "block_hash", "source_hash", "count", "empty", "malformed"])
def test_bad_page_is_atomic_excluded_on_both_ends_and_never_cached(env, damage):
    target = add(env, "目标")
    broken = add(env, "损坏页", "[[目标]]")
    source = add(env, "入口", "[[损坏页]]")
    pages = catalog(env)
    with env.db.begin() as db:
        block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == broken))
        if damage == "search_text":
            block.search_text = "BAD_SEARCH_TEXT_CANARY [[目标]]"
        elif damage == "data":
            block.data = {"text": "BAD_DATA_CANARY [[目标]]"}
        elif damage == "block_hash":
            block.content_sha256 = "0" * 64
        elif damage == "source_hash":
            # Individually valid text hashes cannot excuse a stale frozen source.
            block.data, block.search_text = {"text": "新正文[[目标]]"}, "新正文[[目标]]"
            block.content_sha256 = text_sha256(block.search_text)
        elif damage == "count":
            pages[ids(pages)[broken]]["block_count"] += 1
        elif damage == "empty":
            db.delete(block)
        else:
            block.block_type, block.data = "table", {"rows": 5}
    for _ in range(2):
        result, _ = load(env, pages)
        assert result["invalid_pages"] == [ids(pages)[broken]]
        assert ids(pages)[broken] not in result["links"]
        assert result["links"][ids(pages)[source]] == []
        assert result["links"][ids(pages)[target]] == []
        assert result["warnings"] and not result["cache_hit"]
        assert not nav._CACHE
    # Repaired original content, with exactly the same catalog stamp, must reload.
    with env.db.begin() as db:
        block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == broken))
        if block is not None:
            block.block_type, block.data = "paragraph", {"text": "[[目标]]"}
            block.search_text, block.content_sha256 = "[[目标]]", text_sha256("[[目标]]")
    if damage not in {"empty", "count"}:
        repaired, _ = load(env, pages)
        assert not repaired["cache_hit"] and repaired["invalid_pages"] == []
        assert repaired["links"][ids(pages)[broken]] == [ids(pages)[target]]


def test_a_bad_second_block_discards_edges_from_the_first_good_block(env):
    target = add(env, "目标")
    source = add(env, "入口", "[[目标]]", frozen=False)
    with env.db.begin() as db:
        db.add(m.ContentBlock(version_id=source, block_id=svc.uid(), ordinal=1, block_type="paragraph",
            data={"text": "坏块"}, search_text="坏块", content_sha256="0" * 64))
    pages = catalog(env)
    result, _ = load(env, pages)
    assert result["invalid_pages"] == [ids(pages)[source]]
    assert ids(pages)[source] not in result["links"]
    assert result["links"][ids(pages)[target]] == []


@pytest.mark.parametrize("damage", [None, "locator", "citation", "relation", "version_metadata", "blob"])
def test_full_source_hash_matches_central_kernel_and_checks_nontext_content(env, damage):
    target = add(env, "目标")
    source = add(env, "入口", "待核验：这是正文保留的历史提示。请参见[[目标]]。", frozen=False)
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, source)
        version.valid_from, version.required_facts = date(2026, 1, 1), ["业务日期"]
        version.applicability, version.source_url = {"products": ["公募"]}, "https://example.invalid/source"
        version.source_blob_id = db.get(m.ResourceVersion, env.source).source_blob_id
        own = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == source))
        destination = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == target))
        own.locator = {"page": 2}
        db.add(m.EvidenceLink(id=svc.uid(), from_version_id=source, from_block_id=own.block_id,
            to_version_id=target, to_block_id=destination.block_id, purpose="FACT"))
        db.add(m.RelationEdge(id=svc.uid(), source_version_id=source,
            target_resource_id=db.get(m.ResourceVersion, target).resource_id, relation_type="EXPLAINS",
            conditions={"condition": "需核对"}, evidence_version_id=target, evidence_block_id=destination.block_id))
        db.flush()
        version.content_sha256 = svc.check_frozen_hash(db, version)
        version.state = "APPROVED"  # Body text is not a state instruction.
        assert svc.check_frozen_hash(db, version) == version.content_sha256
    pages = catalog(env)
    with env.db.begin() as db:
        if damage == "locator":
            db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == source)).locator = {"page": 9}
        elif damage == "citation":
            db.scalar(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == source)).purpose = "RULE"
        elif damage == "relation":
            db.scalar(select(m.RelationEdge).where(m.RelationEdge.source_version_id == source)).conditions = {}
        elif damage == "version_metadata":
            db.get(m.ResourceVersion, source).required_facts = ["新条件"]
        elif damage == "blob":
            blob = db.get(m.Blob, db.get(m.ResourceVersion, source).source_blob_id)
            blob.sha256 = "f" * 64
    result, _ = load(env, pages)
    if damage:
        assert ids(pages)[source] in result["invalid_pages"]
        assert "INLINE_NAVIGATION_SOURCE_HASH_MISMATCH" in result["warnings"]
    else:
        assert result["invalid_pages"] == []
        assert result["links"][ids(pages)[source]] == [ids(pages)[target]]
        assert pages[ids(pages)[source]]["state"] == "APPROVED"
        assert result["warnings"] == []


def test_batches_cross_400_without_n_plus_one_or_document_body_loading(env):
    # Construct 405 current catalog entries without catalog SQL affecting counts.
    pages, texts = {}, []
    with env.db.begin() as db:
        for number in range(405):
            rid, vid, title = svc.uid(), svc.uid(), f"条目{number}"
            text = f"[[条目{number + 1}]]" if number < 404 else "末页全文"
            texts.append(text)
            db.add(m.Resource(id=rid, space_id=env.space, kind="knowledge", name=title, owner_id=env.owner))
            db.flush()
            db.add(m.ResourceVersion(id=vid, resource_id=rid, version_no=1, author_id=env.owner,
                title=title, knowledge_type="rule", origin="HUMAN"))
            db.flush()
            db.add(m.ContentBlock(version_id=vid, block_id=svc.uid(), ordinal=0, block_type="paragraph",
                data={"text": text}, search_text=text, content_sha256=text_sha256(text)))
            pid = f"W{number}"
            pages[pid] = {"id": pid, "kind": "knowledge", "version_id": vid, "resource_id": rid,
                "title": title, "block_count": 1, "aliases": [], "_metadata_signature": str(number)}
    result, statements = load(env, pages)
    assert len(statements) == 4  # 2 batches x (version metadata + bodies), no frozen drafts.
    assert len([s for s in statements if "content_blocks.data" in s]) == 2
    assert result["body_blocks_loaded"] == result["knowledge_pages"] == 405
    assert result["body_characters_loaded"] == sum(map(len, texts))
    assert all(result["links"][f"W{n}"] == [f"W{n + 1}"] for n in range(404))
    assert result["links"]["W404"] == [] and result["invalid_pages"] == []
    assert result["warnings"] == ["INLINE_NAVIGATION_SOURCE_HASH_UNFROZEN"]
    hit, statements = load(env, pages)
    assert statements == [] and hit["cache_hit"]
    assert hit["body_blocks_loaded"] == hit["body_characters_loaded"] == 0


def test_counts_all_block_types_and_bad_rows_without_changing_catalog_contract(env):
    target = add(env, "目标")
    source = add(env, "入口", "[[目标]]", frozen=False)
    extra = [("table", {"columns": ["引用"], "rows": [["[[目标]]"]]}),
             ("list", {"items": ["[[目标]]", "完整条件"]}),
             ("step", {"action": "核对[[目标]]", "owner_role": "运营"}),
             ("paragraph", {"text": "坏块也已加载"})]
    with env.db.begin() as db:
        for ordinal, (kind, data) in enumerate(extra, 1):
            text = block_text({"block_type": kind, "data": data})
            db.add(m.ContentBlock(version_id=source, block_id=svc.uid(), ordinal=ordinal, block_type=kind,
                data=data, search_text=text, content_sha256="0" * 64 if ordinal == 4 else text_sha256(text)))
    with sql(env) as statements:
        pages = catalog(env)
    assert not any("search_text" in statement for statement in statements)
    assert all("block_type in" in statement for statement in statements if "content_blocks.data" in statement)
    with env.db() as db:
        expected = list(db.scalars(select(m.ContentBlock.search_text).join(m.ResourceVersion)
            .join(m.Resource).where(m.Resource.kind == "knowledge")))
    result, statements = load(env, pages)
    assert result["body_blocks_loaded"] == len(expected)
    assert result["body_characters_loaded"] == sum(map(len, expected))
    assert result["knowledge_pages"] == sum(page["kind"] == "knowledge" for page in pages.values())
    assert result["invalid_pages"] == [ids(pages)[source]]
    assert result["links"][ids(pages)[target]] == []
    assert len(statements) == 4 and len([s for s in statements if "content_blocks.data" in s]) == 1


def test_empty_and_document_only_catalogs_never_load_bodies(env):
    pages = catalog(env)
    for subset in ({}, {pid: page for pid, page in pages.items() if page["kind"] == "document"}):
        result, statements = load(env, subset)
        assert statements == [] and result["knowledge_pages"] == 0
        assert result["body_blocks_loaded"] == result["body_characters_loaded"] == 0
        assert result["invalid_pages"] == [] and not result["cache_hit"]
        assert set(result["links"]) == set(subset)


def test_cache_ttl_lru_scope_limit_and_byte_limit_never_truncate_output(env, monkeypatch):
    target = add(env, "目标")
    source = add(env, "入口", "[[目标]]")
    pages = catalog(env)
    clock = [100.0]
    monkeypatch.setattr(nav.time, "monotonic", lambda: clock[0])
    first, _ = load(env, pages)
    clock[0] = 159.0
    assert load(env, pages)[0]["cache_hit"]
    clock[0] = 160.0  # Hits do not extend the TTL.
    assert not load(env, pages)[0]["cache_hit"]
    actors = [svc.uid() for _ in range(7)]
    for actor in actors:
        load(env, pages, user=actor)
    assert len(nav._CACHE) == 8
    assert load(env, pages)[0]["cache_hit"]  # Owner is now most recently used.
    load(env, pages, user=svc.uid())
    assert len(nav._CACHE) == 8 and load(env, pages)[0]["cache_hit"]
    assert not load(env, pages, user=actors[0])[0]["cache_hit"]
    nav.clear_navigation_cache()
    assert not nav._CACHE and nav._CACHE_BYTES == 0
    monkeypatch.setattr(nav, "_CACHE_MAX_BYTES", 1)
    result, _ = load(env, pages)
    assert result["links"][ids(pages)[source]] == [ids(pages)[target]]
    assert result["links"] == first["links"] and not nav._CACHE
    assert not load(env, pages)[0]["cache_hit"]


def test_source_database_unchanged_and_pending_mutations_are_not_flushed(env):
    target = add(env, "目标")
    add(env, "入口", "[[目标]]")
    pages = catalog(env)
    engine = env.db.kw["bind"]
    with engine.connect() as connection:
        before = list(connection.connection.driver_connection.iterdump())
    load(env, pages)
    load(env, pages)
    nav.clear_navigation_cache()
    with env.db() as db:
        version = db.get(m.ResourceVersion, target)
        version.title = "尚未提交的业务修改"
        with sql(env) as statements:
            result = nav.load_inline_navigation(db, env.owner, env.space, pages)
        assert all(statement.startswith("select ") for statement in statements)
        assert not result["cache_hit"] and not nav._CACHE
        assert version in db.dirty
    with engine.connect() as connection:
        after = list(connection.connection.driver_connection.iterdump())
    assert after == before  # Includes source, graph, RuntimePolicy, audit and business rows.


def test_navigation_hit_does_not_admit_model_evidence_or_bypass_scoped_hash_acl(env):
    target = add(env, "目标")
    source = add(env, "入口", "[[目标]]")
    pages = catalog(env)
    load(env, pages)
    assert load(env, pages)[0]["cache_hit"]
    with env.db.begin() as db:
        block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == target))
        block.search_text = "被篡改的正文"
    with env.db() as db:
        rows, unavailable, _, _ = read_scoped_pages(db, env.owner, env.space, pages, [ids(pages)[target]])
    assert rows == [] and unavailable == [ids(pages)[target]]
    with env.db() as db:
        rows, unavailable, _, _ = read_scoped_pages(db, env.reader, env.space, pages, [ids(pages)[source]])
    assert rows == [] and unavailable == [ids(pages)[source]]
    assert all(not page["body_loaded"] for page in pages.values())
