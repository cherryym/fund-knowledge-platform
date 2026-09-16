"""Reference retrieval work elimination; synthetic DB, no model or network."""
from collections import Counter

import pytest
from sqlalchemy import delete, event, select

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.reference_evidence import _reference_lineage, reference_evidence
from test_reference_review import env as env, make_version  # noqa: F401


def read(env, version_ids=None, user=None):
    with env.db() as db:
        return reference_evidence(db, user or env.owner, env.space, version_ids=version_ids)


def cite(env, parent, child):
    with env.db.begin() as db:
        source = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == parent))
        target = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == child))
        db.add(m.EvidenceLink(id=svc.uid(), from_version_id=parent, from_block_id=source.block_id,
            to_version_id=child, to_block_id=target.block_id, purpose="RULE"))


def test_plain_source_resource_authority_is_not_walked_twice(env, monkeypatch):
    calls = []
    original = svc.resource_access

    def counted(db, user, resource, *args, **kwargs):
        calls.append(resource if isinstance(resource, str) else resource.id)
        return original(db, user, resource, *args, **kwargs)

    monkeypatch.setattr(svc, "resource_access", counted)
    rows = read(env, {env.source})
    assert rows and calls == [rows[0]["resource_id"]]


def test_full_source_dependency_walk_checks_each_child_once(env, monkeypatch):
    child = make_version(env, "document")
    cite(env, env.source, child)
    calls = Counter()
    original = svc.version_access

    def counted(db, user, version, *args, **kwargs):
        calls[version if isinstance(version, str) else version.id] += 1
        return original(db, user, version, *args, **kwargs)

    monkeypatch.setattr(svc, "version_access", counted)
    rows = read(env, {env.source})
    assert rows and calls == {env.source: 1, child: 1}


def test_latest_snapshot_selection_is_batched_not_per_resource(env):
    versions = {make_version(env, "document") for _ in range(24)}
    statements = []
    engine = env.db.kw["bind"]

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        rows = read(env, versions)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert {row["version_id"] for row in rows} == versions
    assert sum("max(resource_versions.version_no)" in sql for sql in statements) == 1
    assert not any("order by resource_versions.version_no desc" in sql for sql in statements)


@pytest.mark.parametrize("change", ["restricted", "suspended", "deleted", "scan", "body", "epoch"])
def test_each_request_rechecks_frozen_wiki_source_not_previous_decision(env, change):
    assert read(env, {env.wiki})
    with env.db.begin() as db:
        source = db.get(m.ResourceVersion, env.source)
        resource = db.get(m.Resource, source.resource_id)
        if change == "restricted":
            resource.restricted = True
        elif change == "suspended":
            resource.suspended = True
        elif change == "deleted":
            resource.deleted_at = svc.now()
        elif change == "scan":
            db.get(m.Blob, source.source_blob_id).scan_state = "QUARANTINED"
        elif change == "body":
            source.title = "已变更的来源标题"
        else:
            resource.access_epoch += 1
    assert read(env, {env.wiki}) == []


def test_revoked_grant_is_not_cached_across_reference_calls(env):
    with env.db.begin() as db:
        rid = db.get(m.ResourceVersion, env.source).resource_id
        db.get(m.Resource, rid).restricted = True
        db.add(m.ResourceGrant(resource_id=rid, user_id=env.owner, permission="read"))
    assert read(env, {env.source})
    with env.db.begin() as db:
        db.execute(delete(m.ResourceGrant).where(m.ResourceGrant.resource_id == rid,
            m.ResourceGrant.user_id == env.owner))
    assert read(env, {env.source}) == []


def test_source_child_acl_and_real_citations_are_preserved(env):
    child = make_version(env, "document")
    cite(env, env.source, child)
    rows = read(env, {env.source, child})
    primary = next(row for row in rows if row["version_id"] == env.source)
    assert len(primary["source_citations"]) == 1
    assert primary["source_citations"][0]["version_id"] == child
    with env.db.begin() as db:
        rid = db.get(m.ResourceVersion, child).resource_id
        db.get(m.Resource, rid).restricted = True
    assert read(env, {env.source}) == []


def test_same_session_next_transaction_selects_new_latest_snapshot(env):
    with env.db() as db:
        assert reference_evidence(db, env.owner, env.space, version_ids={env.source})
        old = db.get(m.ResourceVersion, env.source)
        rid, blob_id = old.resource_id, old.source_blob_id
        db.rollback()
        newer = svc.uid()
        with env.db.begin() as writer:
            previous = writer.get(m.ResourceVersion, env.source)
            previous.content_sha256 = svc.check_frozen_hash(writer, previous)
            previous.state = "IN_REVIEW"
            writer.flush()
            writer.add(m.ResourceVersion(id=newer, resource_id=rid, version_no=2, author_id=env.owner,
                title="更新的来源快照", knowledge_type="source", origin="UPLOAD", source_blob_id=blob_id))
            writer.flush()
            text = "新的来源正文"
            from fund_kb.ingestion import text_sha256
            writer.add(m.ContentBlock(version_id=newer, block_id=svc.uid(), ordinal=0,
                block_type="paragraph", data={"text": text}, locator={}, search_text=text,
                content_sha256=text_sha256(text)))
        assert reference_evidence(db, env.owner, env.space, version_ids={env.source}) == []
        assert {row["version_id"] for row in reference_evidence(
            db, env.owner, env.space, version_ids={newer})} == {newer}


@pytest.mark.parametrize("length,allowed", [(8, True), (9, False)])
def test_dependency_depth_guard_survives_single_walk_and_memo(env, length, allowed):
    chain = [make_version(env, "document") for _ in range(length)]
    for parent, child in zip(chain, chain[1:]):
        cite(env, parent, child)
    with env.db() as db:
        user = db.get(m.User, env.owner)
        memo = {}
        # Prime the shared subtree by visiting it with a shorter ancestry.
        _reference_lineage(db, user, db.get(m.ResourceVersion, chain[-3]), memo=memo)
        if allowed:
            assert len(_reference_lineage(db, user, db.get(m.ResourceVersion, chain[0]), memo=memo)) == length
        else:
            with pytest.raises(svc.APIError) as caught:
                _reference_lineage(db, user, db.get(m.ResourceVersion, chain[0]), memo=memo)
            assert caught.value.code == "DEPENDENCY_CYCLE"


def test_cycle_through_cached_subtree_cannot_bypass_ancestry_guard(env):
    child = make_version(env, "document")
    cite(env, env.source, child)
    with env.db() as db:
        user = db.get(m.User, env.owner)
        memo = {}
        _reference_lineage(db, user, db.get(m.ResourceVersion, env.source), memo=memo)
        with pytest.raises(svc.APIError) as caught:
            _reference_lineage(db, user, db.get(m.ResourceVersion, env.source), path=(child,), memo=memo)
        assert caught.value.code == "DEPENDENCY_CYCLE"
