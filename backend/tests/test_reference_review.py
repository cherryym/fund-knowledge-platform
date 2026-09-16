"""Synthetic handler tests: in-memory SQLite only, no app services or model calls.

Exercise the real ACL, frozen provenance, dependency, ETag and review logic.
The optional HTTP body/schema is owned and integrated by the main agent.
"""
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from fund_kb import api_content, wiki
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.db import Base, build_engine, make_session_factory
from fund_kb.ingestion import text_sha256


@pytest.fixture
def env(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Reference review tests must not call a network or model provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(wiki, "_provider_module", forbidden)
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    actors = {name: svc.uid() for name in ("owner", "reviewer", "reader", "editor")}
    space = svc.uid()
    with factory.begin() as db:
        db.add(m.Space(id=space, name="合成参考复核空间"))
        db.add_all(m.User(id=actor, external_subject=f"synthetic:{actor}", display_name=name)
                   for name, actor in actors.items())
        db.flush()
        for name, roles in {"owner": ["reader", "editor", "reviewer", "publisher"],
                            "reviewer": ["reader", "reviewer"], "reader": ["reader"],
                            "editor": ["reader", "editor"]}.items():
            db.add_all(m.SpaceMember(space_id=space, user_id=actors[name], role=role) for role in roles)
    result = SimpleNamespace(db=factory, space=space, **actors)
    result.source = make_version(result, "document")
    result.wiki = make_version(result, "knowledge")
    with factory.begin() as db:
        source = db.get(m.ResourceVersion, result.source)
        resource = db.get(m.Resource, source.resource_id)
        version = db.get(m.ResourceVersion, result.wiki)
        blob = db.get(m.Blob, source.source_blob_id)
        db.add(m.RuntimePolicy(id=svc.uid(), name=f"wiki-provenance:{version.resource_id}",
            updated_by=result.owner, config={"resource_id": version.resource_id,
                "source_mode": "unverified_draft", "source_version_ids": [source.id],
                "reference_snapshot": [], "source_snapshot": [{"resource_id": resource.id,
                    "version_id": source.id, "content_sha256": svc.check_frozen_hash(db, source),
                    "access_epoch": resource.access_epoch, "source_blob_sha256": blob.sha256}]}))
    try:
        yield result
    finally:
        engine.dispose()


def make_version(env, kind):
    rid, vid, bid = svc.uid(), svc.uid(), svc.uid()
    text = "合成资料：核对价格来源与业务日期，内容尚未核验。"
    with env.db.begin() as db:
        db.add(m.Resource(id=rid, space_id=env.space, kind=kind, name="合成复核资料", owner_id=env.owner))
        blob_id = svc.uid() if kind == "document" else None
        if blob_id:
            db.add(m.Blob(id=blob_id, space_id=env.space, object_key=f"synthetic/{blob_id}.txt",
                sha256=text_sha256(text), size_bytes=len(text.encode()), mime_type="text/plain", scan_state="CLEAN"))
        db.flush()
        db.add(m.ResourceVersion(id=vid, resource_id=rid, version_no=1, author_id=env.owner,
            title="合成待复核版本", knowledge_type="source" if kind == "document" else "rule",
            origin="UPLOAD" if kind == "document" else "AI_DRAFT", source_blob_id=blob_id,
            source_verified=False, legal_status="UNKNOWN"))
        db.flush()
        db.add(m.ContentBlock(version_id=vid, block_id=bid, ordinal=0, block_type="paragraph",
            data={"text": text}, locator={}, search_text=text, content_sha256=text_sha256(text)))
    return vid


def invoke(env, handler, operation, *, version_id=None, actor=None, data=None, etag="current"):
    # Each call has a fresh request transaction, including rollback on failures.
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, version_id or env.wiki)
        headers = {} if etag is None else {"if-match": f'"{version.revision}"' if etag == "current" else etag}
        request = SimpleNamespace(path_params={"id": version.id}, headers=headers,
                                  state=SimpleNamespace(trace_id=svc.uid()))
        ctx = svc.Context(request, db, db.get(m.User, actor or env.owner), data, {}, operation)
        return handler(ctx)


def submit(env, body=None, **kwargs):
    return invoke(env, api_content.submit, "submitVersion", data=body, **kwargs)


def reference_submit(env, **kwargs):
    return submit(env, {"review_scope": "reference"}, **kwargs)


def review(env, decision="APPROVE", *, actor=None, reviewed_sha256=None, **kwargs):
    with env.db() as db:
        sha = db.get(m.ResourceVersion, kwargs.get("version_id", env.wiki)).content_sha256
    return invoke(env, api_content.reviews, "reviewVersion", actor=actor or env.reviewer,
        data={"decision": decision, "reviewed_sha256": reviewed_sha256 or sha,
              "comment": "合成独立复核", **kwargs.pop("fields", {})}, **kwargs)


def assert_error(code, status, action):
    with pytest.raises(svc.APIError) as caught:
        action()
    assert (caught.value.code, caught.value.status) == (code, status)


def assert_pending(env, state="DRAFT", revision=None):
    with env.db() as db:
        version = db.get(m.ResourceVersion, env.wiki)
        assert version.state == state
        assert version.source_verified is False and version.legal_status == "UNKNOWN"
        assert version.revision == (revision if revision is not None else (1 if state == "DRAFT" else 2))
        assert db.get(m.Resource, version.resource_id).active_release_id is None
        assert db.scalar(select(m.ReviewDecision.id)) is None
        assert db.scalar(select(m.Release.id)) is None
        assert db.scalar(select(m.Job.id)) is None
        if state == "DRAFT":
            assert version.content_sha256 is None
            assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == "version.submitted")) is None


def test_reference_submission_freezes_only_pending_review(env):
    with env.db() as db:
        before = svc.check_frozen_hash(db, db.get(m.ResourceVersion, env.wiki))
    result = reference_submit(env)
    assert result.status == 200 and result.body["state"] == "IN_REVIEW"
    assert result.body["content_sha256"] == before and result.headers["ETag"] == '"2"'
    assert result.body["source_verified"] is False and result.body["legal_status"] == "UNKNOWN"
    assert_pending(env, "IN_REVIEW")
    with env.db() as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.object_id == env.wiki))
        assert event.action == "version.submitted"
        assert event.details == {"sha256": before, "review_scope": "reference"}
        assert db.get(m.ResourceVersion, env.source).state == "DRAFT"
        assert not svc.evidence_version_eligible(db, db.get(m.User, env.owner),
                                               db.get(m.ResourceVersion, env.wiki))


@pytest.mark.parametrize("body", [None, {}, {"review_scope": "formal"}])
def test_default_submit_rejects_unverified_wiki(env, body):
    assert_error("WIKI_UNVERIFIED_SOURCES", 409, lambda: submit(env, body))
    assert_pending(env)


@pytest.mark.parametrize("decision", ["APPROVE", "REJECT"])
def test_author_cannot_review_reference_snapshot(env, decision):
    reference_submit(env)
    assert_error("SELF_REVIEW_FORBIDDEN", 403, lambda: review(env, decision, actor=env.owner))
    assert_pending(env, "IN_REVIEW")


def test_contributor_cannot_review_reference_snapshot(env):
    reference_submit(env)
    with env.db.begin() as db:
        db.add(m.AuditEvent(id=svc.uid(), actor_id=env.reviewer, action="version.edited",
            object_type="ResourceVersion", object_id=env.wiki, outcome="SUCCESS", trace_id=svc.uid(), details={}))
    assert_error("SELF_REVIEW_FORBIDDEN", 403, lambda: review(env))
    assert_pending(env, "IN_REVIEW")


def test_formal_approval_rejects_unverified_wiki(env):
    reference_submit(env)
    assert_error("WIKI_UNVERIFIED_SOURCES", 409, lambda: review(env))
    assert_pending(env, "IN_REVIEW")


def test_independent_rejection_of_reference_snapshot_is_allowed(env):
    submitted = reference_submit(env)
    result = review(env, "REJECT")
    assert result.status == 201 and result.body["decision"] == "REJECT"
    assert result.body["reviewed_sha256"] == submitted.body["content_sha256"]
    with env.db() as db:
        version = db.get(m.ResourceVersion, env.wiki)
        assert version.state == "REJECTED" and version.revision == 3
        assert version.source_verified is False and version.legal_status == "UNKNOWN"
        assert db.scalar(select(m.ReviewDecision).where(m.ReviewDecision.decision == "APPROVE")) is None
        assert db.scalar(select(m.Release.id)) is None


def test_reader_cannot_submit_even_with_reference_scope(env):
    assert_error("FORBIDDEN", 403, lambda: reference_submit(env, actor=env.reader))
    assert_pending(env)


def test_other_editor_cannot_submit_authors_private_draft(env):
    # Grant source-reading authority so this reaches the separate author guard.
    with env.db.begin() as db:
        db.add(m.SpaceMember(space_id=env.space, user_id=env.editor, role="reviewer"))
    assert_error("DRAFT_OWNER_REQUIRED", 403, lambda: reference_submit(env, actor=env.editor))
    assert_pending(env)


@pytest.mark.parametrize("etag,code,status", [(None, "PRECONDITION_REQUIRED", 428),
                                             ('"0"', "REVISION_CONFLICT", 412)])
@pytest.mark.parametrize("stage", ["submit", "review"])
def test_reference_review_preserves_etag_guards(env, etag, code, status, stage):
    if stage == "review":
        reference_submit(env)
    action = (lambda: reference_submit(env, etag=etag)) if stage == "submit" else (
        lambda: review(env, "REJECT", etag=etag))
    assert_error(code, status, action)
    assert_pending(env, "DRAFT" if stage == "submit" else "IN_REVIEW")


@pytest.mark.parametrize("change", ["body", "epoch", "blob_hash", "quarantine", "suspend", "delete", "acl"])
@pytest.mark.parametrize("stage", ["submit", "review"])
def test_changed_source_rejects_reference_submission_and_review(env, change, stage):
    if stage == "review":
        reference_submit(env)
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, env.source)
        resource = db.get(m.Resource, version.resource_id)
        if change == "body":
            block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == version.id))
            block.data = {"text": "已经改变的合成来源"}
            block.search_text = block.data["text"]
            block.content_sha256 = text_sha256(block.search_text)
            version.revision += 1
        elif change == "epoch":
            resource.access_epoch += 1
        elif change == "blob_hash":
            db.get(m.Blob, version.source_blob_id).sha256 = "b" * 64
        elif change == "quarantine":
            db.get(m.Blob, version.source_blob_id).scan_state = "QUARANTINED"
        elif change == "suspend":
            resource.suspended = True
        elif change == "delete":
            resource.deleted_at = svc.now()
        elif change == "acl":
            resource.restricted = True
    code, status = ("NOT_FOUND", 404) if change in {"delete", "acl"} else ("WIKI_SOURCE_CHANGED", 409)
    action = (lambda: reference_submit(env)) if stage == "submit" else (lambda: review(env, "REJECT"))
    assert_error(code, status, action)
    assert_pending(env, "DRAFT" if stage == "submit" else "IN_REVIEW")


def test_reference_submission_still_checks_additional_dependency_acl(env):
    other = make_version(env, "document")
    with env.db.begin() as db:
        target = db.get(m.ResourceVersion, other)
        db.get(m.Resource, target.resource_id).restricted = True
        source_block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == env.wiki))
        target_block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == other))
        db.add(m.EvidenceLink(id=svc.uid(), from_version_id=env.wiki, from_block_id=source_block.block_id,
            to_version_id=other, to_block_id=target_block.block_id, purpose="FACT"))
    assert_error("NOT_FOUND", 404, lambda: reference_submit(env))
    assert_pending(env)


@pytest.mark.parametrize("tamper", [False, True])
def test_rejection_still_requires_the_frozen_snapshot_hash(env, tamper):
    reference_submit(env)
    if tamper:
        with env.db.begin() as db:
            db.get(m.ResourceVersion, env.wiki).title = "篡改后的合成标题"
    assert_error("REVIEW_HASH_MISMATCH", 409, lambda: review(env, "REJECT",
        reviewed_sha256=None if tamper else "0" * 64))
    assert_pending(env, "IN_REVIEW", revision=3 if tamper else 2)


@pytest.mark.parametrize("body", [None, {}, {"review_scope": "reference"}])
def test_ordinary_source_submission_is_unchanged_and_wiki_can_follow(env, body):
    result = submit(env, body, version_id=env.source)
    assert result.status == 200 and result.body["state"] == "IN_REVIEW"
    assert result.body["source_verified"] is False and result.body["legal_status"] == "UNKNOWN"
    assert reference_submit(env).body["state"] == "IN_REVIEW"
    assert_pending(env, "IN_REVIEW")


def test_ordinary_source_can_still_be_independently_verified(env):
    submit(env, version_id=env.source)
    result = review(env, version_id=env.source, fields={"source_verified": True})
    assert result.status == 201 and result.body["decision"] == "APPROVE"
    with env.db() as db:
        source = db.get(m.ResourceVersion, env.source)
        assert source.state == "APPROVED" and source.source_verified is True
        assert source.legal_status == "UNKNOWN"
        assert db.scalar(select(m.Release.id)) is None
    # A verified source does not upgrade an existing unverified Wiki's provenance.
    reference_submit(env)
    assert_error("WIKI_UNVERIFIED_SOURCES", 409, lambda: review(env))
