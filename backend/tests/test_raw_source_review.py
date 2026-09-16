"""A clean scan-only original may be queued for human review, never fabricated text."""
from sqlalchemy import delete, select
from test_reference_review import assert_error, reference_submit, submit
from test_reference_review import env as env  # noqa: PLC0414

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.reference_evidence import reference_evidence


def remove_text(env, version_id):
    with env.db.begin() as db:
        db.execute(delete(m.ContentBlock).where(m.ContentBlock.version_id == version_id))
        db.get(m.ResourceVersion, version_id).revision += 1


def test_clean_raw_original_enters_pending_review_without_inventing_evidence(env):
    remove_text(env, env.source)
    assert_error("EMPTY_CONTENT", 409, lambda: submit(env, version_id=env.source))
    result = reference_submit(env, version_id=env.source)
    assert result.body["state"] == "IN_REVIEW"
    with env.db() as db:
        version = db.get(m.ResourceVersion, env.source)
        assert version.source_verified is False and version.legal_status == "UNKNOWN"
        assert svc.content_blocks(db, version.id) == []
        assert version.content_sha256 == svc.check_frozen_hash(db, version)
        assert not any(e["version_id"] == version.id for e in reference_evidence(db, env.owner, env.space))
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.object_id == version.id, m.AuditEvent.action == "version.submitted"))
        assert audit.details["raw_source_review"] is True and audit.details["parsed_content_available"] is False
        assert db.scalar(select(m.ReviewDecision.id)) is None
        assert db.scalar(select(m.Release.id)) is None


def test_empty_knowledge_or_quarantined_original_cannot_use_raw_review_exception(env):
    remove_text(env, env.wiki)
    assert_error("EMPTY_CONTENT", 409, lambda: reference_submit(env))
    remove_text(env, env.source)
    with env.db.begin() as db:
        source = db.get(m.ResourceVersion, env.source)
        db.get(m.Blob, source.source_blob_id).scan_state = "QUARANTINED"
    assert_error("EMPTY_CONTENT", 409, lambda: reference_submit(env, version_id=env.source))


def test_zero_byte_original_cannot_use_raw_review_exception(env):
    remove_text(env, env.source)
    with env.db.begin() as db:
        source = db.get(m.ResourceVersion, env.source)
        db.get(m.Blob, source.source_blob_id).size_bytes = 0
    assert_error("EMPTY_CONTENT", 409, lambda: reference_submit(env, version_id=env.source))
