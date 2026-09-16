"""Metadata-only catalog, whole-page reads and typed navigation; no network."""
import copy

import pytest
from sqlalchemy import event, select

from test_reference_review import env, make_version
from fund_kb import models as m, services as svc
from fund_kb.ingestion import text_sha256
from fund_kb.wiki_catalog import build_catalog, read_whole_pages, related_reads, catalog_signature
from fund_kb.wiki_reader import index_lines, page_text


def catalog(env, actor=None):
    with env.db() as db:
        return build_catalog(db, actor or env.owner, env.space)


def test_catalog_reads_zero_paragraph_or_table_bodies(env):
    statements = []
    engine = env.db.kw["bind"]
    def capture(_conn, _cursor, statement, *_):
        statements.append(statement.lower())
    event.listen(engine, "before_cursor_execute", capture)
    try:
        pages = catalog(env)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(pages) == 2 and all(p["records"] == [] and not p["body_loaded"] for p in pages.values())
    assert not any("search_text" in s for s in statements)
    for statement in statements:
        if "content_blocks.data" in statement:
            assert "content_blocks.block_type in" in statement  # attachment IDs only
    assert "全文未读" in "\n".join(index_lines(pages))


def test_whole_page_read_allocates_frozen_ids_only_for_read_blocks(env):
    with env.db.begin() as db:
        for n in range(1, 151):
            text = f"全文连续第{n}段，完整条件和例外。"
            db.add(m.ContentBlock(version_id=env.source, block_id=svc.uid(), ordinal=n, block_type="paragraph",
                data={"text": text}, search_text=text, content_sha256=text_sha256(text)))
        policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like("wiki-provenance:%")))
        cfg = copy.deepcopy(policy.config)
        cfg["source_snapshot"][0]["content_sha256"] = svc.check_frozen_hash(db, db.get(m.ResourceVersion, env.source))
        policy.config = cfg
    pages = catalog(env)
    selected = next(p["id"] for p in pages.values() if p["version_id"] == env.source)
    with env.db() as db:
        rows, unavailable, next_id = read_whole_pages(db, env.owner, env.space, pages, [selected], next_evidence=73)
    assert not unavailable and len(rows) == 151 and next_id == 224
    assert rows[0]["evidence_id"] == "E73" and rows[-1]["evidence_id"] == "E223"
    assert "全文连续第150段" in page_text(pages[selected])
    assert all(not p["body_loaded"] for p in pages.values() if p["id"] != selected)


@pytest.mark.parametrize("change", ["suspended", "restricted", "scan", "deactivated", "source_snapshot", "deleted"])
def test_catalog_fails_closed_on_metadata_permission_and_provenance_change(env, change):
    with env.db.begin() as db:
        v = db.get(m.ResourceVersion, env.source)
        r = db.get(m.Resource, v.resource_id)
        if change == "suspended": r.suspended = True
        elif change == "restricted": r.restricted = True
        elif change == "scan": db.get(m.Blob, v.source_blob_id).scan_state = "REJECTED"
        elif change == "deactivated": db.get(m.User, env.owner).active = False
        elif change == "deleted": r.deleted_at = svc.now()
        elif change == "source_snapshot": r.access_epoch += 1
    if change == "deactivated":
        with pytest.raises(svc.APIError): catalog(env)
    elif change == "source_snapshot":
        assert {p["version_id"] for p in catalog(env).values()} == {env.source}
    else:
        assert catalog(env) == {}


def test_reader_cannot_see_owner_unpublished_title_or_alias(env):
    assert catalog(env, env.reader) == {}


def test_corrupt_body_is_not_admitted_by_metadata_permission(env):
    pages = catalog(env)
    pid = next(p["id"] for p in pages.values() if p["version_id"] == env.source)
    with env.db.begin() as db:
        block = db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == env.source)).first()
        block.search_text = "未通过Hash的内容不能发给模型"
    with env.db() as db:
        rows, unavailable, n = read_whole_pages(db, env.owner, env.space, pages, [pid])
    assert rows == [] and unavailable == [pid] and n == 1


def test_typed_relations_read_prerequisites_and_incoming_exceptions(env):
    exception = make_version(env, "knowledge")
    condition = make_version(env, "knowledge")
    with env.db.begin() as db:
        primary = db.get(m.ResourceVersion, env.wiki)
        db.add(m.RelationEdge(id=svc.uid(), source_version_id=exception, target_resource_id=primary.resource_id,
            relation_type="EXCEPTION_OF", conditions={"note": "仅特定产品情形"}))
        db.add(m.RelationEdge(id=svc.uid(), source_version_id=primary.id,
            target_resource_id=db.get(m.ResourceVersion, condition).resource_id, relation_type="REQUIRES", conditions={}))
    pages = catalog(env)
    ids = {p["version_id"]: p["id"] for p in pages.values()}
    expanded = related_reads(pages, [ids[env.wiki]])
    assert set(expanded) == set(ids.values())
    assert "EXCEPTION_OF" in "\n".join(index_lines(pages))
    assert all(e["verification_status"] != "VERIFIED" for p in pages.values() for e in p["relations"])
    with env.db() as db:
        read_whole_pages(db, env.owner, env.space, pages, expanded)
    assert "仅特定产品情形" in page_text(pages[ids[exception]])


def test_existing_body_links_are_discovered_after_whole_page_read(env):
    other = make_version(env, "knowledge")
    with env.db.begin() as db:
        v = db.get(m.ResourceVersion, other)
        v.title = "规范主知识条目"
        db.get(m.Resource, v.resource_id).tags = ["alias:简称别名"]
        b = db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == env.wiki)).first()
        b.data = {"text": "请参阅[[简称别名]]，不因链接就确认业务效力。"}
        b.search_text = b.data["text"]
        b.content_sha256 = text_sha256(b.search_text)
    pages = catalog(env)
    ids = {p["version_id"]: p["id"] for p in pages.values()}
    with env.db() as db:
        rows, bad, _ = read_whole_pages(db, env.owner, env.space, pages, [ids[env.wiki]])
    assert rows and not bad and ids[other] in pages[ids[env.wiki]]["links"]


def test_metadata_signature_detects_edit_while_body_remains_unread(env):
    before = catalog_signature(catalog(env))
    with env.db.begin() as db:
        v = db.get(m.ResourceVersion, env.source)
        v.title = "新修订资料标题"
        v.revision += 1
    assert catalog_signature(catalog(env)) != before


def test_evidence_links_are_grouped_without_losing_anchors(env):
    with env.db.begin() as db:
        left = db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == env.wiki)).first()
        for n in range(1, 11):
            text = f"来源完整第{n}段"
            block = m.ContentBlock(version_id=env.source, block_id=svc.uid(), ordinal=n, block_type="paragraph",
                data={"text": text}, search_text=text, content_sha256=text_sha256(text))
            db.add(block); db.flush()
            db.add(m.EvidenceLink(id=svc.uid(), from_version_id=env.wiki, from_block_id=left.block_id,
                to_version_id=env.source, to_block_id=block.block_id, purpose="FACT"))
        policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like("wiki-provenance:%")))
        config = copy.deepcopy(policy.config)
        config["source_snapshot"][0]["content_sha256"] = svc.check_frozen_hash(db, db.get(m.ResourceVersion, env.source))
        policy.config = config
    pages = catalog(env)
    wiki = next(p for p in pages.values() if p["version_id"] == env.wiki)
    registered = [r for r in wiki["relations"] if r["type"] == "CITES" and r["origin"] == "registered"]
    assert len(registered) == 1 and len(registered[0]["anchors"]) == 10
