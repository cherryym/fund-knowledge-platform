"""Synthetic, network-forbidden governance tests; no financial answer shortcuts."""
import copy
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_reference_review import make_version
from test_wiki_reader_job import base_env, execute, prepare  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb import source_authority as sa
from fund_kb.jobs import JobError
from fund_kb.source_reading_policy import policy_stamp
from fund_kb.wiki_catalog import build_catalog, catalog_signature
from fund_kb.wiki_section_reader import read_scoped_pages


def setup_sources(env):
    old, new, proof = [make_version(env, "document") for _ in range(3)]
    with env.db.begin() as db:
        if "admin" not in svc.roles(db, db.get(m.User, env.owner), env.space):
            db.add(m.SpaceMember(space_id=env.space, user_id=env.owner, role="admin"))
        for vid, title in [(old, "早期规则"), (new, "后续规则"), (proof, "替代公告")]:
            db.get(m.ResourceVersion, vid).title = title
    return {"space_id": env.space, "predecessor_version_id": old, "successor_version_id": new,
        "evidence_version_id": proof, "scope": "full", "effective_from": "2023-05-01", "reason": "合成替代证据核对"}


def context(env, db, data, record=None, actor=None, etag=None):
    request = SimpleNamespace(path_params={"id": record["id"]} if record else {},
        headers={"if-match": etag or f'"{record["revision"]}"'} if record else {},
        state=SimpleNamespace(trace_id=svc.uid()))
    return svc.Context(request, db, db.get(m.User, actor or env.owner), data, {}, "synthetic")


def create(env, data, **kwargs):
    with env.db.begin() as db:
        return sa.create_record(context(env, db, data, **kwargs))


def catalog(env, day="2026-09-12", **kwargs):
    with env.db() as db:
        return build_catalog(db, db.get(m.User, env.owner), env.space, {"business_date": day, **kwargs}, scope="reference")


def by_version(pages, vid):
    return next(page for page in pages.values() if page["version_id"] == vid)


def test_create_audits_exact_snapshots_without_mutating_sources_or_vector_stamp(env):
    data = setup_sources(env)
    before = catalog(env)
    with env.db() as db:
        source_hashes = {data[key]: svc.check_frozen_hash(db, db.get(m.ResourceVersion, data[key])) for key in sa.VERSION_KEYS}
    record = create(env, data)
    after = catalog(env)
    assert record["validation_state"] == "VALID" and record["state"] == "ACTIVE" and record["revision"] == 1
    assert "snapshots" not in record
    assert catalog_signature(before) != catalog_signature(after)
    assert {p["version_id"]: p["_metadata_signature"] for p in before.values()} == {
        p["version_id"]: p["_metadata_signature"] for p in after.values()}
    with env.db() as db:
        for vid, sha in source_hashes.items():
            version = db.get(m.ResourceVersion, vid)
            assert svc.check_frozen_hash(db, version) == sha
            assert version.state == "DRAFT" and version.legal_status == "UNKNOWN"
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "source_authority.confirmed"))
        assert db.scalar(select(m.RelationEdge)) is None and db.scalar(select(m.Release)) is None


@pytest.mark.parametrize("day,active", [("2023-04-30", False), ("2023-05-01", True), ("2026-09-12", True)])
def test_effective_date_inclusive_not_upload_time_or_title(env, day, active):
    data = setup_sources(env); create(env, data)
    pages = catalog(env, day)
    old = by_version(pages, data["predecessor_version_id"])
    route = sa.extend_reads(pages, [old["id"]])
    assert bool(old.get("source_authority")) is active
    assert (by_version(pages, data["successor_version_id"])["id"] in route["requested"]) is active
    with env.db() as db:
        assert sa.formal_excluded(db, db.get(m.ResourceVersion, data["predecessor_version_id"]), {"business_date": day}) is active


def test_knowledge_cutoff_prevents_future_recorded_fact_from_rewriting_past_view(env):
    data = setup_sources(env); create(env, data)
    pages = catalog(env, knowledge_cutoff="2020-01-01T00:00:00Z")
    assert not by_version(pages, data["predecessor_version_id"]).get("source_authority")


@pytest.mark.parametrize("scope", ["full", "partial"])
def test_wiki_citation_to_old_source_forces_real_successor_and_proof_read(env, scope):
    data = {**setup_sources(env), "scope": scope}; create(env, data)
    with env.db.begin() as db:
        old = db.get(m.ResourceVersion, data["predecessor_version_id"])
        block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == old.id))
        wiki_block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == env.wiki))
        db.add(m.EvidenceLink(id=svc.uid(), from_version_id=env.wiki, from_block_id=wiki_block.block_id,
            to_version_id=old.id, to_block_id=block.block_id, purpose="RULE"))
    pages = catalog(env)
    route = sa.extend_reads(pages, [by_version(pages, env.wiki)["id"]])
    assert {by_version(pages, data[key])["id"] for key in sa.VERSION_KEYS[1:]} <= set(route["requested"])
    with env.db() as db:
        records, missing, _, outlines = read_scoped_pages(db, env.owner, env.space, pages,
            route["requested"], full_pages=route["full_pages"])
        assert not missing and not outlines
        assert {data[key] for key in sa.VERSION_KEYS[1:]} <= {row["version_id"] for row in records}
    old = by_version(pages, data["predecessor_version_id"])
    assert ("整体替代" if scope == "full" else "部分替代") in sa.describe(old)


@pytest.mark.parametrize("change", ["revision", "access_epoch", "state", "scan_state", "suspend", "delete", "restricted"])
def test_changed_or_hidden_proof_never_restores_old_current_status_or_leaks_identity(env, change):
    data = setup_sources(env); create(env, data)
    with env.db() as db:
        stamp = policy_stamp(db, env.space)
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, data["evidence_version_id"])
        resource = db.get(m.Resource, version.resource_id)
        if change == "revision": version.revision += 1
        elif change == "state":
            version.content_sha256 = svc.check_frozen_hash(db, version)
            version.state = "IN_REVIEW"
        elif change == "access_epoch": resource.access_epoch += 1
        elif change == "scan_state": db.get(m.Blob, version.source_blob_id).scan_state = "REJECTED"
        elif change == "suspend": resource.suspended = True
        elif change == "delete": resource.deleted_at = svc.now()
        else:
            resource.restricted = True
            resource.access_epoch += 1
    pages = catalog(env)
    old = by_version(pages, data["predecessor_version_id"])
    assert old["source_authority"][0]["validation_state"] != "VALID"
    text = str(old["source_authority"]) + sa.describe(old)
    assert data["evidence_version_id"] not in text and "替代公告" not in text
    assert "SOURCE_AUTHORITY_REVIEW_REQUIRED" in sa.extend_reads(pages, [old["id"]])["warnings"]
    with env.db() as db:
        assert policy_stamp(db, env.space) != stamp
        assert sa.formal_excluded(db, db.get(m.ResourceVersion, data["predecessor_version_id"]), {})


def test_revoke_keeps_audit_and_sources_requires_correct_revision(env):
    data = setup_sources(env); record = create(env, data)
    with pytest.raises(svc.APIError), env.db.begin() as db:
        sa.revoke_record(context(env, db, {"reason": "误录修正"}, record, etag='"0"'))
    with env.db.begin() as db:
        revoked = sa.revoke_record(context(env, db, {"reason": "误录修正"}, record))
    assert revoked["state"] == "REVOKED" and revoked["revision"] == 2
    assert not by_version(catalog(env), data["predecessor_version_id"]).get("source_authority")
    with env.db() as db:
        assert db.get(m.RuntimePolicy, record["id"]) and db.get(m.ResourceVersion, data["predecessor_version_id"])
        assert len(list(db.scalars(select(m.AuditEvent)))) == 2


@pytest.mark.parametrize("actor", ["reader", "editor", "reviewer"])
def test_only_current_space_admin_can_confirm_or_revoke(env, actor):
    data = setup_sources(env)
    with pytest.raises(svc.APIError) as err: create(env, data, actor=getattr(env, actor))
    assert err.value.status == 403
    record = create(env, data)
    with pytest.raises(svc.APIError), env.db.begin() as db:
        sa.revoke_record(context(env, db, {"reason": "不可越权"}, record, actor=getattr(env, actor)))


def test_duplicate_conflicting_and_cyclic_facts_are_rejected(env):
    data = setup_sources(env); create(env, data)
    for body, code in [
        (data, "SOURCE_AUTHORITY_DUPLICATE"),
        ({**data, "successor_version_id": data["evidence_version_id"]}, "SOURCE_AUTHORITY_CONFLICT"),
        ({**data, "predecessor_version_id": data["successor_version_id"], "successor_version_id": data["predecessor_version_id"]}, "SOURCE_AUTHORITY_CYCLE"),
        ({**data, "successor_version_id": data["predecessor_version_id"]}, "SOURCE_AUTHORITY_SELF_REPLACEMENT")]:
        with pytest.raises(svc.APIError) as err: create(env, body)
        assert err.value.code == code


def test_transitive_replacement_reads_all_proofs_but_cites_current_leaf(env):
    data = setup_sources(env); create(env, data)
    newest = make_version(env, "document")
    create(env, {**data, "predecessor_version_id": data["successor_version_id"], "successor_version_id": newest})
    pages = catalog(env)
    route = sa.extend_reads(pages, [by_version(pages, data["predecessor_version_id"])["id"]])
    assert by_version(pages, newest)["id"] in route["requested"]
    records = [{"version_id": pages[pid]["version_id"]} for pid in route["requested"]]
    answer = {"citations": [{"version_id": newest}], "quality_warnings": []}
    assert sa.check_coverage(pages, route["requirements"], records, answer)["covered"]
    assert answer["quality_warnings"] == []


def test_future_successor_validity_conflict_does_not_promote_or_resurrect(env):
    data = setup_sources(env)
    with env.db.begin() as db:
        from datetime import date
        db.get(m.ResourceVersion, data["successor_version_id"]).valid_from = date(2099, 1, 1)
    create(env, data)
    old = by_version(catalog(env), data["predecessor_version_id"])
    assert old["source_authority"][0]["validation_state"] == "CONFLICT"


def test_citation_gap_warns_without_fabricating_citations_or_swallowing_answer(env):
    data = setup_sources(env); create(env, data)
    pages = catalog(env); old = by_version(pages, data["predecessor_version_id"])
    route = sa.extend_reads(pages, [old["id"]])
    answer = {"citations": [{"version_id": data["predecessor_version_id"]}], "narrative_markdown": "原公开回答"}
    citations = copy.deepcopy(answer["citations"])
    assert not sa.check_coverage(pages, route["requirements"], [], answer)["covered"]
    assert answer["citations"] == citations and answer["narrative_markdown"] == "原公开回答"
    assert answer["quality_warnings"][0]["code"] == "SOURCE_AUTHORITY_COVERAGE_GAP"


def test_real_worker_shape_reads_successor_without_another_selection_call(env, monkeypatch):
    data = setup_sources(env); create(env, data)
    def respond(calls, _):
        if len(calls) == 1: return "需核对相关规则及日期。"
        if len(calls) == 2: return "READ " + re.search(r"(W\d+) \| 来源文档 \| 早期规则", calls[-1])[1]
        assert "后续规则" in calls[-1] and "替代公告" in calls[-1]
        assert "不能作为当日现行主规则" in calls[-1]
        return "已比对后续规则，仍需业务复核。" + "".join(f"[{eid}]" for eid in re.findall(r"\[(E\d+)\]", calls[-1]))
    rid, jid, calls = prepare(env, monkeypatch, respond)
    execute(env, jid)
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "COMPLETED" and len(calls) == 3
        assert run.model_snapshot["source_authority_coverage"]["covered"]
        assert {data[key] for key in sa.VERSION_KEYS[1:]} <= {row["version_id"] for row in run.evidence_snapshot}


def test_governance_change_during_generation_invalidates_result(env, monkeypatch):
    data = setup_sources(env); record = create(env, data)
    def respond(calls, _):
        if len(calls) == 1: return "需要查阅相关制度。"
        if len(calls) == 2: return "READ " + re.search(r"(W\d+) \| 来源文档 \| 早期规则", calls[-1])[1]
        with env.db.begin() as db:
            sa.revoke_record(context(env, db, {"reason": "测试并发撤销"}, record))
        return "已基于变化前来源形成的公开正文。"
    _, jid, _ = prepare(env, monkeypatch, respond)
    with pytest.raises(JobError, match="SOURCE_(READING_POLICY|AUTHORITY)_CHANGED"): execute(env, jid)


def test_historical_read_warning_is_not_a_persisted_answer_rewrite(env):
    data = setup_sources(env); create(env, data)
    with env.db() as db:
        citations = [{"version_id": data["predecessor_version_id"]}]
        assert sa.historical_warning(db, db.get(m.User, env.owner), env.space, {}, citations, None)["code"] == "SOURCE_AUTHORITY_UPDATED"
        assert sa.historical_warning(db, db.get(m.User, env.owner), env.space, {"business_date": "2020-01-01"}, citations, None) is None
        assert sa.historical_warning(db, db.get(m.User, env.owner), env.space, {}, citations, sa.authority_stamp(db, env.space)) is None


def test_bound_draft_body_tampering_without_revision_cannot_validate_replacement(env):
    data = setup_sources(env); create(env, data)
    pages = catalog(env)
    route = sa.extend_reads(pages, [by_version(pages, data["predecessor_version_id"])["id"]])
    with env.db.begin() as db:
        from fund_kb.ingestion import text_sha256
        block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == data["evidence_version_id"]))
        block.data = {"text": "已被篡改的替代证据"}
        block.search_text = block.data["text"]
        block.content_sha256 = text_sha256(block.search_text)
    with env.db() as db, pytest.raises(svc.APIError, match="正文已变化"):
        sa.verify_bindings(db, db.get(m.User, env.owner), route["requirements"])


def test_explicit_cycle_is_a_gap_even_if_storage_was_modified_outside_api():
    pages = {pid: {"source_authority": [{"scope": "full", "validation_state": "VALID", "successor_page_id": target}]}
        for pid, target in [("W1", "W2"), ("W2", "W1")]}
    leaves, invalid = sa.successor_leaves(pages, "W1")
    assert not leaves and invalid
