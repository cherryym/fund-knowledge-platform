"""Real SQLite/local objects/QdrantLocal tests. No external service pass claims."""
from __future__ import annotations

import io
import subprocess
import sys
import threading
import time
import zipfile
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.db import Base, build_engine, make_session_factory
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.jobs import JobDispatcher, JobError, LeaseLost, now, scan_file, sha, stable_id
from fund_kb.retrieval import VectorIndex
from fund_kb.settings import Settings
from fund_kb.storage import Storage, StorageError


def uid():
    return str(uuid4())


@pytest.fixture
def env(tmp_path):
    settings = Settings(app_env="test", storage_dir=tmp_path / "objects", qdrant_path=tmp_path / "vectors",
        database_url=f"sqlite:///{tmp_path / 'jobs.sqlite'}", job_recovery_interval_seconds=.1)
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    index = VectorIndex(settings)
    dispatcher = JobDispatcher(settings, factory, index)
    user, reviewer, reader, space = uid(), uid(), uid(), uid()
    with factory.begin() as db:
        db.add_all([m.User(id=user, external_subject="jobs:editor", display_name="任务编辑者"),
                    m.User(id=reviewer, external_subject="jobs:reviewer", display_name="独立复核者"),
                    m.User(id=reader, external_subject="jobs:reader", display_name="读者"),
                    m.Space(id=space, name="合成测试空间")])
        db.flush()
        for actor, roles in ((user, ["reader", "editor", "publisher", "admin"]),
                             (reviewer, ["reader", "reviewer"]), (reader, ["reader"])):
            for role in roles:
                db.add(m.SpaceMember(space_id=space, user_id=actor, role=role))
    result = SimpleNamespace(settings=settings, engine=engine, db=factory, index=index, d=dispatcher,
                             user=user, reviewer=reviewer, reader=reader, space=space)
    yield result
    result.d.close()
    index.close()
    engine.dispose()


def resource(env, *, text="基金估值应核对托管数据。", restricted=False, document=False,
             base=None, resource_id=None, approved=False):
    rid, vid, bid = resource_id or uid(), uid(), uid()
    with env.db.begin() as db:
        if resource_id is None:
            db.add(m.Resource(id=rid, space_id=env.space, kind="document" if document else "knowledge",
                              name="合成估值流程", owner_id=env.user, restricted=restricted))
            db.flush()
            if restricted:
                for permission in ("read", "download", "edit", "publish", "manage"):
                    db.add(m.ResourceGrant(resource_id=rid, user_id=env.user, permission=permission))
        version = m.ResourceVersion(id=vid, resource_id=rid, version_no=2 if base else 1, base_version_id=base,
            author_id=env.user, title="合成估值流程", state="DRAFT", origin="HUMAN", valid_from=date(2020, 1, 1),
            knowledge_type="source" if document else "sop", legal_status="NOT_APPLICABLE")
        db.add(version)
        db.flush()
        if text is not None:
            block = {"block_id": bid, "ordinal": 0, "block_type": "paragraph", "data": {"text": text},
                     "locator": {"label": "合成测试段落1"}, "citations": []}
            env.d._write_blocks(db, version, [block])
        if approved:
            version.content_sha256 = svc.check_frozen_hash(db, version)
            version.state = "APPROVED"
            db.add(m.ReviewDecision(id=uid(), version_id=vid, reviewer_id=env.reviewer, decision="APPROVE",
                                   reviewed_sha256=version.content_sha256, comment="合成测试独立复核记录"))
    return rid, vid, bid


def job(env, kind, payload, owner=None, **kw):
    jid = uid()
    with env.db.begin() as db:
        db.add(m.Job(id=jid, kind=kind, owner_id=owner or env.user, state="QUEUED", dedupe_key=jid,
                     payload=payload, **kw))
        db.flush()
        db.add(m.Outbox(id=uid(), event_type="JOB_CREATED", aggregate_id=jid, payload={"job_id": jid}))
    return jid


def get_job(env, jid):
    with env.db() as db:
        return db.get(m.Job, jid)


def upload(env, vid, data=b"Synthetic fund valuation evidence", filename="evidence.txt"):
    upload_id = uid()
    with env.db.begin() as db:
        db.add(m.Upload(id=upload_id, version_id=vid, user_id=env.user, filename=filename,
            declared_size=len(data), part_count=1, expected_sha256=sha(data), state="SEALED",
            expires_at=now() + timedelta(hours=24)))
        db.flush()
        key = f"uploads/{upload_id}/1"
        env.d.storage.write_bytes(key, data)
        db.add(m.UploadPart(upload_id=upload_id, part_no=1, size_bytes=len(data), sha256=sha(data), object_key=key))
    return upload_id


def publish(env, vid):
    jid = job(env, "PUBLISH", {"version_id": vid}, version_id=vid)
    assert env.d.run(jid), get_job(env, jid).error_code
    return get_job(env, jid).result["release_id"]


@pytest.mark.parametrize("key", ["../escape", "/tmp/escape", "a/../../escape", "a//b", "a/./b",
                                 "C:/escape", "a\\b", "a/..", "a\x00b", ""])
def test_storage_rejects_unsafe_keys(tmp_path, key):
    with Storage(SimpleNamespace(storage_dir=tmp_path, storage_backend="local")) as storage:
        for method in (storage.read_bytes, storage.exists, storage.delete, storage.local_path):
            with pytest.raises(ValueError):
                method(key)
        with pytest.raises(ValueError):
            storage.write_bytes(key, b"x")


def test_storage_atomic_immutable_and_symlink(tmp_path):
    with Storage(SimpleNamespace(storage_dir=tmp_path / "objects")) as storage:
        key = "blobs/immutable/source.txt"
        storage.write_bytes(key, b"first")
        storage.write_bytes(key, b"first")
        with pytest.raises(StorageError, match="IMMUTABLE"):
            storage.write_bytes(key, b"replacement")
        assert storage.read_bytes(key) == b"first"
        (storage.root / "escape").symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(ValueError):
            storage.write_bytes("escape/outside", b"x")
        storage.write_bytes("previews/a.html", b"one")
        storage.write_bytes("previews/a.html", b"two")
        assert storage.local_path("previews/a.html").read_bytes() == b"two"
        storage.delete("previews/a.html")
        assert not storage.exists("previews/a.html")


def test_s3_does_not_discover_shared_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "unrelated-environment-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unrelated-environment-secret")
    with pytest.raises(StorageError, match="S3_EXPLICIT_CREDENTIALS_REQUIRED"):
        Storage(SimpleNamespace(storage_dir=tmp_path, storage_backend="s3", s3_bucket="test"))


def test_scan_parse_real_storage_and_immutable_original(env):
    rid, vid, _ = resource(env, document=True, text=None)
    upload_id = upload(env, vid, "合成资料：估值核对。".encode())
    jid = job(env, "SCAN_PARSE", {"upload_id": upload_id}, resource_id=rid, version_id=vid)
    assert env.d.run(jid)
    row = get_job(env, jid)
    assert row.state == "SUCCEEDED" and row.result["block_count"] > 0
    assert row.result["scan"]["level"] == "basic"
    assert "DEVELOPMENT_BASIC_SCAN_ONLY" in row.result["warnings"][0]
    assert env.d.storage.exists(f"previews/{vid}.html")
    with env.db() as db:
        version = db.get(m.ResourceVersion, vid)
        original = db.get(m.Blob, version.source_blob_id)
        original_bytes = env.d.storage.read_bytes(original.object_key)
        assert original.scan_state == "CLEAN" and version.source_verified is False
        for block in svc.version_blocks(db, vid):
            persisted = db.get(m.ContentBlock, (vid, block["block_id"]))
            assert persisted.content_sha256 == text_sha256(block_text(block))
    second = upload(env, vid, b"replacement")
    bad = job(env, "SCAN_PARSE", {"upload_id": second})
    assert not env.d.run(bad)
    assert get_job(env, bad).error_code == "ORIGINAL_IMMUTABLE"
    assert env.d.storage.read_bytes(original.object_key) == original_bytes


def test_rejected_scan_leaves_quarantine_and_no_preview(env):
    _, vid, _ = resource(env, document=True, text=None)
    upload_id = upload(env, vid, b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE")
    jid = job(env, "SCAN_PARSE", {"upload_id": upload_id})
    assert not env.d.run(jid)
    assert get_job(env, jid).error_code == "FILE_REJECTED"
    assert env.d.storage.exists(f"quarantine/{jid}/evidence.txt")
    assert not env.d.storage.exists(f"previews/{vid}.html")
    with env.db() as db:
        assert db.get(m.ResourceVersion, vid).source_blob_id is None
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.object_id == jid)) >= 3


def test_production_scanner_absence_is_failure(env):
    _, vid, _ = resource(env, document=True, text=None)
    upload_id = upload(env, vid)
    env.d.settings = env.settings.model_copy(update={"app_env": "production", "scan_backend": "basic"})
    jid = job(env, "SCAN_PARSE", {"upload_id": upload_id})
    assert not env.d.run(jid)
    assert get_job(env, jid).state == "FAILED"
    assert get_job(env, jid).error_code == "SCANNER_UNAVAILABLE"
    env.d.settings = env.settings.model_copy(update={"scan_backend": "clamav", "clamav_command": "missing-fkb-scanner"})
    with pytest.raises(JobError, match="SCANNER_UNAVAILABLE"):
        scan_file(env.d.settings, env.d.storage.local_path(f"uploads/{upload_id}/1"), "evidence.txt")


def test_real_export_and_duplicate_delivery(env):
    _, vid, _ = resource(env)
    jid = job(env, "EXPORT", {"version_ids": [vid], "format": "markdown"})
    assert env.d.run(jid)
    assert not env.d.run(jid)
    assert get_job(env, jid).attempts == 1
    with zipfile.ZipFile(io.BytesIO(env.d.storage.read_bytes(f"exports/{jid}.zip"))) as archive:
        assert "托管" in archive.read(f"{vid}.md").decode()


def test_export_final_permission_recheck(env, monkeypatch):
    from fund_kb import ingestion
    rid, vid, _ = resource(env, restricted=True)
    real = ingestion.render_blocks
    def revoke(*args, **kw):
        output = real(*args, **kw)
        with env.db.begin() as db:
            db.execute(delete(m.ResourceGrant).where(m.ResourceGrant.resource_id == rid,
                                                     m.ResourceGrant.permission == "download"))
        return output
    monkeypatch.setattr(ingestion, "render_blocks", revoke)
    jid = job(env, "EXPORT", {"version_ids": [vid], "format": "markdown"})
    assert not env.d.run(jid)
    assert get_job(env, jid).state == "FAILED"


def test_publish_artifacts_then_switch_and_failed_new_version_preserves_old(env, monkeypatch):
    rid, vid, _ = resource(env, approved=True)
    old_release = publish(env, vid)
    with env.db() as db:
        release = db.get(m.Release, old_release)
        for artifact in release.manifest["artifacts"].values():
            assert sha(env.d.storage.read_bytes(artifact["object_key"])) == artifact["sha256"]
        assert release.manifest["embedding"]["development_only"] is True
    _, next_vid, _ = resource(env, base=vid, resource_id=rid, approved=True, text="合成替换版本")
    def broken(_):
        raise RuntimeError("simulated external index failure")
    monkeypatch.setattr(env.index, "upsert", broken)
    jid = job(env, "PUBLISH", {"version_id": next_vid})
    assert not env.d.run(jid)
    with env.db() as db:
        assert db.get(m.Resource, rid).active_release_id == old_release
        assert db.get(m.Release, old_release).state == "ACTIVE"
        assert db.get(m.Release, stable_id(jid, "release")).state == "FAILED"


def test_publish_final_delete_check(env, monkeypatch):
    rid, vid, _ = resource(env, approved=True)
    real = env.index.upsert
    def remove(records):
        real(records)
        with env.db.begin() as db:
            db.execute(update(m.Resource).where(m.Resource.id == rid).values(deleted_at=now()))
    monkeypatch.setattr(env.index, "upsert", remove)
    jid = job(env, "PUBLISH", {"version_id": vid})
    assert not env.d.run(jid)
    with env.db() as db:
        assert db.get(m.Resource, rid).active_release_id is None


def test_publish_cannot_use_self_review_or_tampered_snapshot(env):
    _, vid, _ = resource(env, approved=True)
    with env.db.begin() as db:
        db.execute(update(m.ReviewDecision).where(m.ReviewDecision.version_id == vid).values(reviewer_id=env.user))
    jid = job(env, "PUBLISH", {"version_id": vid})
    assert not env.d.run(jid)
    assert get_job(env, jid).error_code == "INDEPENDENT_REVIEW_REQUIRED"


def test_recovery_reopens_database_and_runs_expired_lease(env):
    _, vid, _ = resource(env)
    jid = job(env, "EXPORT", {"version_ids": [vid], "format": "json"})
    with env.db.begin() as db:
        row = db.get(m.Job, jid)
        row.state, row.attempts, row.lease_until = "RUNNING", 1, now() - timedelta(seconds=1)
        row.result = {"retained_stage_sha256": "a" * 64}
    env.d.close()
    env.d = JobDispatcher(env.settings, env.db, env.index)
    assert env.d.recover() >= 1
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and get_job(env, jid).state != "SUCCEEDED":
        time.sleep(.02)
    result = get_job(env, jid)
    assert result.state == "SUCCEEDED" and result.attempts == 2
    assert result.result["retained_stage_sha256"] == "a" * 64
    with env.db() as db:
        assert db.scalar(select(m.Outbox).where(m.Outbox.aggregate_id == jid)).dispatched_at


def test_unexpired_lease_and_fencing_and_cancel(env):
    _, vid, _ = resource(env)
    jid = job(env, "EXPORT", {"version_ids": [vid], "format": "json"})
    first = env.d._claim(jid)
    assert first == 1 and not env.d.run(jid)
    with env.db.begin() as db:
        db.get(m.Job, jid).lease_until = now() - timedelta(seconds=1)
    assert env.d._claim(jid) == 2
    with pytest.raises(LeaseLost), env.db.begin() as db:
        env.d._fence(db, jid, 1)
    env.d._fail(jid, 1, JobError("STALE_WORKER_ERROR"))
    assert get_job(env, jid).state == "RUNNING"
    cancelled = job(env, "EXPORT", {"version_ids": [vid], "format": "json"}, cancel_requested=True)
    assert not env.d.run(cancelled)
    assert get_job(env, cancelled).state == "CANCELLED"
    assert not env.d.storage.exists(f"exports/{cancelled}.zip")


def test_retry_is_durable_and_preserves_evidence(env, monkeypatch):
    _, vid, _ = resource(env)
    jid = job(env, "EXPORT", {"version_ids": [vid], "format": "json"})
    first = env.d._claim(jid)
    env.d._checkpoint(jid, first, "CHECKED", {"retained_sha256": "b" * 64})
    env.d._fail(jid, first, JobError("TEMPORARY_UNAVAILABLE", retryable=True))
    row = get_job(env, jid)
    assert row.state == "QUEUED" and row.lease_until > now()
    assert env.d._claim(jid) is None
    with env.db.begin() as db:
        db.get(m.Job, jid).lease_until = now() - timedelta(seconds=1)
    assert env.d.run(jid)
    assert get_job(env, jid).result["retained_sha256"] == "b" * 64
    with env.db() as db:
        failed_events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "job.failed")))
        assert failed_events[0].details["error_code"] == "TEMPORARY_UNAVAILABLE"


def test_explicit_manual_retry_policy_never_requeues_or_recovers_automatically(env):
    jid = job(env, "EXPORT", {"manual_retry_only": True})
    first = env.d._claim(jid)
    env.d._fail(jid, first, JobError("TEMPORARY_UNAVAILABLE", retryable=True))
    assert get_job(env, jid).state == "FAILED"
    assert env.d._claim(jid) is None
    other = job(env, "EXPORT", {"manual_retry_only": True})
    assert env.d._claim(other) == 1
    with env.db.begin() as db:
        db.get(m.Job, other).lease_until = now() - timedelta(seconds=1)
    assert env.d._claim(other) is None
    row = get_job(env, other)
    assert row.state == "FAILED" and row.error_code == "MANUAL_RETRY_REQUIRED" and row.attempts == 1


def test_two_dispatchers_claim_once(env, monkeypatch):
    from fund_kb import ingestion
    _, vid, _ = resource(env)
    jid = job(env, "EXPORT", {"version_ids": [vid], "format": "markdown"})
    started, proceed = threading.Event(), threading.Event()
    original = ingestion.render_blocks
    def slow(*a, **kw):
        started.set()
        assert proceed.wait(5)
        return original(*a, **kw)
    monkeypatch.setattr(ingestion, "render_blocks", slow)
    future = env.d(jid)
    assert started.wait(5)
    other = JobDispatcher(env.settings, env.db, env.index)
    try:
        assert not other.run(jid)
    finally:
        proceed.set()
        other.close()
    assert future.result(timeout=5)
    assert get_job(env, jid).attempts == 1


def test_invalidation_removes_index_and_suspends_dependencies(env):
    rid, vid, bid = resource(env, approved=True)
    publish(env, vid)
    child_rid, child_vid, child_bid = resource(env)
    with env.db.begin() as db:
        db.add(m.EvidenceLink(id=uid(), from_version_id=child_vid, from_block_id=child_bid,
            to_version_id=vid, to_block_id=bid, purpose="FACT"))
        db.get(m.Resource, rid).deleted_at = now()
    jid = job(env, "INVALIDATE", {"resource_id": rid})
    assert env.d.run(jid)
    assert env.index.search("估值", [vid]) == []
    with env.db() as db:
        assert db.get(m.Resource, child_rid).suspended is True


def approved_retention_fixture(env):
    """Explicit v6 approval in the synthetic DB, never implicit timestamp approval."""
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=uid(), name=f"retention-policy:{env.space}", updated_by=env.user,
            config={"version": 1, "space_id": env.space, "retention_days": 2, "approved": True,
                "approved_by": env.user, "approved_at": svc.primitive(now() - timedelta(days=4)),
                "purge_allowed_roles": ["admin"], "approval_expires_at": None,
                "reason": "合成测试获准保留策略"}))


def test_purge_checks_retention_then_erases_exact_objects(env):
    approved_retention_fixture(env)
    rid, vid, _ = resource(env, text=None, document=True)
    upload_id = upload(env, vid)
    scan = job(env, "SCAN_PARSE", {"upload_id": upload_id}, resource_id=rid, version_id=vid)
    assert env.d.run(scan)
    with env.db.begin() as db:
        r = db.get(m.Resource, rid)
        r.deleted_at, r.legal_hold, r.retain_until = now() - timedelta(days=3), True, now() - timedelta(days=1)
        original = db.get(m.Blob, db.get(m.ResourceVersion, vid).source_blob_id).object_key
    denied = job(env, "PURGE", {"resource_id": rid, "reason": "合成数据销毁测试"})
    assert not env.d.run(denied)
    assert get_job(env, denied).error_code == "LEGAL_HOLD"
    assert env.d.storage.exists(original)
    with env.db.begin() as db:
        db.get(m.Resource, rid).legal_hold = False
    purge = job(env, "PURGE", {"resource_id": rid, "reason": "合成数据销毁测试"})
    assert env.d.run(purge), get_job(env, purge).error_code
    assert not env.d.storage.exists(original)
    assert not env.d.storage.exists(f"uploads/{upload_id}/1")
    assert get_job(env, scan).state == "SUCCEEDED"  # Retain old job evidence.
    with env.db() as db:
        assert db.get(m.Resource, rid).name == "[已清除]"
        assert svc.version_blocks(db, vid) == []
        assert db.get(m.AuditEvent, get_job(env, purge).result["purge_receipt_id"])


def test_compile_creates_private_draft_with_exact_source_links(env):
    _, vid, bid = resource(env)
    jid = job(env, "COMPILE", {"version_id": vid, "target_space_id": env.space, "knowledge_type": "sop"})
    assert env.d.run(jid), get_job(env, jid).error_code
    result = get_job(env, jid).result
    assert not env.d.run(jid)
    with env.db() as db:
        compiled = db.get(m.Resource, result["resource_id"])
        version = db.get(m.ResourceVersion, result["draft_version_id"])
        assert compiled.restricted and version.state == "DRAFT" and not version.source_verified
        links = list(db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == version.id)))
        assert len(links) == 1 and links[0].to_version_id == vid and links[0].to_block_id == bid
        assert "EXTRACTIVE" in svc.version_blocks(db, version.id)[0]["data"]["text"]
        assert version.content_sha256 == svc.check_frozen_hash(db, version)


def consultation(env, owner=None):
    tid, run_id = uid(), uid()
    with env.db.begin() as db:
        db.add(m.ConsultationThread(id=tid, owner_id=owner or env.user, space_id=env.space, title="合成测试咨询"))
        db.flush()
        db.add(m.ConsultationRun(id=run_id, thread_id=tid, state="QUEUED", mode="answer", request={
            "question": "基金估值核对托管数据", "mode": "answer", "context": {"business_date": "2026-09-07"}}))
    return run_id


def test_answer_executes_real_retrieval_and_schema_checked_extraction(env):
    _, vid, _ = resource(env, approved=True)
    publish(env, vid)
    run_id = consultation(env)
    jid = job(env, "ANSWER", {"run_id": run_id}, run_id=run_id)
    assert env.d.run(jid), get_job(env, jid).error_code
    with env.db() as db:
        run = db.get(m.ConsultationRun, run_id)
        assert run.state == "COMPLETED"
        assert run.response["status"] == "ANSWERED"
        assert run.response["review_status"] == "REQUIRES_EXPERT"
        assert run.response["citations"][0]["version_id"] == vid
        assert run.model_snapshot["execution_mode"] == "extractive"
        assert run.evidence_snapshot
        assert len(get_job(env, jid).result) <= 3  # No answer body in job API.


def test_answer_rejects_other_users_run(env):
    run_id = consultation(env, env.reader)
    jid = job(env, "ANSWER", {"run_id": run_id}, run_id=run_id)
    assert not env.d.run(jid)
    assert get_job(env, jid).error_code == "RUN_NOT_ACCESSIBLE"
    with env.db() as db:
        assert db.get(m.ConsultationRun, run_id).response is None


def test_answer_permission_revoked_during_generation_retains_evidence_not_output(env, monkeypatch):
    from fund_kb import ai
    rid, vid, _ = resource(env, approved=True, restricted=True)
    publish(env, vid)
    run_id = consultation(env)
    original = ai.generate_answer
    def revoke(*args, **kwargs):
        answer = original(*args, **kwargs)
        with env.db.begin() as db:
            db.execute(delete(m.ResourceGrant).where(m.ResourceGrant.resource_id == rid,
                                                     m.ResourceGrant.permission == "read"))
        return answer
    monkeypatch.setattr(ai, "generate_answer", revoke)
    jid = job(env, "ANSWER", {"run_id": run_id}, run_id=run_id)
    assert not env.d.run(jid)
    with env.db() as db:
        run = db.get(m.ConsultationRun, run_id)
        assert run.state == "FAILED" and run.response is None
        assert run.evidence_snapshot
        assert db.scalar(select(m.RunEvidence).where(m.RunEvidence.run_id == run_id))


def test_answer_bad_schema_fails_and_retains_snapshot(env, monkeypatch):
    from fund_kb import ai
    _, vid, _ = resource(env, approved=True)
    publish(env, vid)
    run_id = consultation(env)
    monkeypatch.setattr(ai, "generate_answer", lambda *a, **kw: {"status": "ANSWERED"})
    jid = job(env, "ANSWER", {"run_id": run_id}, run_id=run_id)
    assert not env.d.run(jid)
    assert get_job(env, jid).error_code == "ANSWER_SCHEMA_INVALID"
    with env.db() as db:
        assert db.get(m.ConsultationRun, run_id).evidence_snapshot


def test_purge_inbound_consultation_retention_blocks_deletion(env):
    approved_retention_fixture(env)
    rid, vid, _ = resource(env, approved=True)
    publish(env, vid)
    run_id = consultation(env)
    answer = job(env, "ANSWER", {"run_id": run_id}, run_id=run_id)
    assert env.d.run(answer)
    with env.db.begin() as db:
        r = db.get(m.Resource, rid)
        r.deleted_at, r.retain_until = now() - timedelta(days=3), now() - timedelta(days=1)
    purge = job(env, "PURGE", {"resource_id": rid, "reason": "合成依赖保留测试"})
    assert not env.d.run(purge)
    assert get_job(env, purge).error_code == "INBOUND_DEPENDENCIES"


def test_celery_adapter_configuration_is_not_broker_verification(env):
    from fund_kb.celery_app import create_celery_app
    application = create_celery_app(env.settings)
    try:
        assert application.conf.task_acks_late
        assert application.conf.task_reject_on_worker_lost
        assert application.conf.broker_transport_options["confirm_publish"]
        assert "fund_kb.run_job" in application.tasks
        assert "fund_kb.recover_jobs" in application.tasks
    finally:
        application.close()


def test_process_crash_recovers_persisted_job_after_real_lease_expiry(env):
    _, vid, _ = resource(env)
    jid = job(env, "EXPORT", {"version_ids": [vid], "format": "json"})
    child = """
import os, sys
from fund_kb.db import build_engine, make_session_factory
from fund_kb.jobs import JobDispatcher
from fund_kb.settings import Settings
settings = Settings(app_env='test', database_url=sys.argv[1], storage_dir=sys.argv[2],
                    qdrant_path=sys.argv[3], job_lease_seconds=5)
engine = build_engine(settings.database_url)
worker = JobDispatcher(settings, make_session_factory(engine), None)
attempt = worker._claim(sys.argv[4])
worker._checkpoint(sys.argv[4], attempt, 'BEFORE_CRASH', {'crash_evidence_sha256': 'c' * 64})
os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", child, env.settings.database_url,
        str(env.settings.storage_dir), str(env.settings.qdrant_path), jid],
        capture_output=True, timeout=15, check=False)
    assert result.returncode == 17
    row = get_job(env, jid)
    assert row.state == "RUNNING" and row.attempts == 1 and row.lease_until > now()
    assert env.d.recover() == 0  # An unexpired lease is not stolen even after process death.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and get_job(env, jid).state != "SUCCEEDED":
        time.sleep(.05)
    row = get_job(env, jid)
    assert row.state == "SUCCEEDED" and row.attempts == 2
    assert row.result["crash_evidence_sha256"] == "c" * 64
    assert env.d.storage.exists(f"exports/{jid}.zip")


def test_cancel_during_work_preserves_job_and_no_success(env, monkeypatch):
    from fund_kb import ingestion
    _, vid, _ = resource(env)
    jid = job(env, "EXPORT", {"version_ids": [vid], "format": "markdown"})
    original = ingestion.render_blocks
    def cancel(*a, **kw):
        result = original(*a, **kw)
        with env.db.begin() as db:
            db.get(m.Job, jid).cancel_requested = True
        return result
    monkeypatch.setattr(ingestion, "render_blocks", cancel)
    assert not env.d.run(jid)
    assert get_job(env, jid).state == "CANCELLED"
    with env.db() as db:
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.object_id == jid,
                                                   m.AuditEvent.action == "job.failed"))


def test_restore_rebuilds_preview_and_unsuspend_rebuilds_index(env):
    rid, vid, _ = resource(env, approved=True)
    publish(env, vid)
    with env.db.begin() as db:
        resource_row = db.get(m.Resource, rid)
        resource_row.deleted_at, resource_row.suspended = now(), True
    remove = job(env, "INVALIDATE", {"resource_id": rid, "reason": "DELETED"})
    assert env.d.run(remove)
    assert env.index.search("估值", [vid]) == []
    with env.db.begin() as db:
        db.get(m.Resource, rid).deleted_at = None
    restore = job(env, "INVALIDATE", {"resource_id": rid, "reason": "RESTORED"})
    assert env.d.run(restore)
    assert env.d.storage.exists(f"previews/{vid}.html")
    assert env.index.search("估值", [vid]) == []
    with env.db.begin() as db:
        db.get(m.Resource, rid).suspended = False
    enable = job(env, "INVALIDATE", {"resource_id": rid, "reason": "SUSPENSION_CHANGED"})
    assert env.d.run(enable)
    assert env.index.search("估值", [vid])


def test_unknown_applicability_clarifies_without_calling_model(env, monkeypatch):
    from fund_kb import ai
    _, vid, _ = resource(env)
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, vid)
        version.applicability = {"all": [{"field": "product_type", "op": "eq", "values": ["基金"]}]}
        version.content_sha256 = svc.check_frozen_hash(db, version)
        version.state = "APPROVED"
        db.add(m.ReviewDecision(id=uid(), version_id=vid, reviewer_id=env.reviewer, decision="APPROVE",
                               reviewed_sha256=version.content_sha256, comment="合成测试"))
    publish(env, vid)
    def forbidden(*_, **__):
        raise AssertionError("unknown applicability must never reach generation")
    monkeypatch.setattr(ai, "generate_answer", forbidden)
    run_id = consultation(env)
    jid = job(env, "ANSWER", {"run_id": run_id}, run_id=run_id)
    assert env.d.run(jid), get_job(env, jid).error_code
    with env.db() as db:
        run = db.get(m.ConsultationRun, run_id)
        assert run.response["status"] == "NEEDS_CLARIFICATION"
        assert run.response["missing_facts"][0]["field"] == "product_type"
        assert run.response["claims"] == []
        assert run.model_snapshot["execution_mode"] == "deterministic_clarification"


def test_unknown_legal_source_is_not_a_clarification_candidate(env):
    _, vid, _ = resource(env)
    with env.db.begin() as db:
        v = db.get(m.ResourceVersion, vid)
        v.legal_status = "UNKNOWN"
        v.required_facts = ["product_type"]
        v.content_sha256 = svc.check_frozen_hash(db, v)
        v.state = "APPROVED"
        db.add(m.ReviewDecision(id=uid(), version_id=vid, reviewer_id=env.reviewer, decision="APPROVE",
                               reviewed_sha256=v.content_sha256, comment="合成测试"))
    publish(env, vid)
    with env.db() as db:
        assert svc.clarification_candidates(db, db.get(m.User, env.user), env.space, {}) == []
    run_id = consultation(env)
    jid = job(env, "ANSWER", {"run_id": run_id}, run_id=run_id)
    assert env.d.run(jid)
    with env.db() as db:
        run = db.get(m.ConsultationRun, run_id)
        assert run.response["status"] == "INSUFFICIENT_EVIDENCE"
        assert run.evidence_snapshot == []


def policy(env, **changes):
    config = {"provider_ref": "evidence", "generation_model": "evidence-only",
        "extraction_model": "deterministic-extraction", "enable_vector": False,
        "prompt_version": "v1", "evaluation_id": "synthetic-test-only", **changes}
    with env.db.begin() as db:
        row = m.RuntimePolicy(id=uid(), name="model-policy", revision=1, config=config, updated_by=env.user)
        db.add(row)
    return row.id


def test_runtime_evidence_policy_overrides_http_and_disables_vector(env, monkeypatch):
    from fund_kb import ai
    _, vid, _ = resource(env, approved=True)
    publish(env, vid)
    policy_id = policy(env)
    env.d.settings = env.settings.model_copy(update={"llm_provider": "http", "llm_base_url": "https://not-called.invalid",
                                                   "llm_model": "not-called"})
    calls = []
    original = ai.generate_answer
    def capture(question, mode, context, evidence, settings, run_id, **kwargs):
        calls.append(settings.llm_provider)
        return original(question, mode, context, evidence, settings, run_id, **kwargs)
    monkeypatch.setattr(ai, "generate_answer", capture)
    vector_calls = []
    monkeypatch.setattr(env.index, "search", lambda *a, **kw: vector_calls.append(a) or [])
    run_id = consultation(env)
    jid = job(env, "ANSWER", {"run_id": run_id}, run_id=run_id)
    assert env.d.run(jid), get_job(env, jid).error_code
    assert calls == ["evidence"] and vector_calls == []
    with env.db() as db:
        snapshot = db.get(m.ConsultationRun, run_id).policy_snapshot
        assert snapshot["runtime_policy_id"] == policy_id and snapshot["enable_vector"] is False
        assert snapshot["worker_frozen"] and snapshot["revision"] == 1


def test_runtime_http_model_is_selected_and_policy_freezes_across_change(env, monkeypatch):
    from fund_kb import ai
    _, vid, _ = resource(env, approved=True)
    publish(env, vid)
    pid = policy(env, provider_ref="configured-http", generation_model="approved-synthetic-model")
    env.d.settings = env.settings.model_copy(update={"llm_provider": "http", "llm_base_url": "https://not-called.invalid",
                                                   "llm_model": "environment-model"})
    original = ai.generate_answer
    seen = []
    def capture(question, mode, context, evidence, settings, run_id, **kwargs):
        seen.append(settings.llm_model)
        with env.db.begin() as db:
            current = db.get(m.RuntimePolicy, pid)
            current.config = {**current.config, "generation_model": "later-model"}
        # Test the worker's selection; deliberately do not exercise an external provider.
        return original(question, mode, context, evidence,
                        settings.model_copy(update={"llm_provider": "evidence"}), run_id)
    monkeypatch.setattr(ai, "generate_answer", capture)
    run_id = consultation(env)
    jid = job(env, "ANSWER", {"run_id": run_id}, run_id=run_id)
    assert env.d.run(jid)
    assert seen == ["approved-synthetic-model"]
    with env.db() as db:
        run = db.get(m.ConsultationRun, run_id)
        assert run.policy_snapshot["generation_model"] == "approved-synthetic-model"
        assert run.model_snapshot["execution_mode"] == "extractive"


def test_unsupported_runtime_prompt_fails_before_generation(env, monkeypatch):
    from fund_kb import ai
    policy(env, prompt_version="not-installed")
    called = []
    monkeypatch.setattr(ai, "generate_answer", lambda *a, **kw: called.append(True))
    run_id = consultation(env)
    jid = job(env, "ANSWER", {"run_id": run_id}, run_id=run_id)
    assert not env.d.run(jid)
    assert get_job(env, jid).error_code == "POLICY_TEMPLATE_UNAVAILABLE" and called == []


def test_sqlite_dispatcher_uses_immediate_before_any_read(env):
    statements = []
    from sqlalchemy import event
    @event.listens_for(env.engine, "before_cursor_execute")
    def collect(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement)
    try:
        with env.d.session_factory() as db:
            db.get(m.User, env.user)
        assert statements[0] == "BEGIN IMMEDIATE"
    finally:
        event.remove(env.engine, "before_cursor_execute", collect)
