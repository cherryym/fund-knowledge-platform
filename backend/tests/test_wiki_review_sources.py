"""IN_REVIEW originals can seed unverified Wiki without changing review state.

Real HTTP/file-SQLite lifecycle and source checks, synthetic provider only.
Local bibliographic imports remain separate from reference-answer evidence.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_local_wiki_import import note
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import execute, page
from test_wiki import provider as provider  # noqa: PLC0414
from test_wiki_unverified import draft_source

from fund_kb import codex_bridge, codex_text, wiki
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.reference_evidence import reference_evidence


@pytest.fixture(autouse=True)
def offline_configuration(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("FKB_"):
            monkeypatch.delenv(name)
    attempts = []
    original_connect = socket.socket.connect

    def forbidden(*args, **kwargs):
        attempts.append("blocked")
        raise AssertionError("Review-source tests must not use real auth, subprocesses or network")

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            return forbidden()
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(codex_bridge, "_read_private", forbidden)
    monkeypatch.setattr(codex_text, "_read_private", forbidden)
    yield
    assert not attempts


def review_source(env):
    source = draft_source(env)
    current = env.call("GET", f"/versions/{source[1]}")
    assert current.status_code == 200, current.text
    submitted = env.call("POST", f"/versions/{source[1]}/submit", {"review_scope": "reference"},
                         etag=current.headers["etag"])
    assert submitted.status_code == 200 and submitted.json()["state"] == "IN_REVIEW", submitted.text
    with env.db() as db:
        version = db.get(m.ResourceVersion, source[1])
        assert version.content_sha256 == svc.check_frozen_hash(db, version)
        assert version.source_verified is False
    return source


def newer_review_over_editable_draft(env):
    older = draft_source(env)
    # Distinct authors permit the real schema to retain an older editable DRAFT
    # while another author's newer IN_REVIEW exists for the same resource.
    owner = env.owner
    env.owner = env.reader
    try:
        latest = page(env, "最新版待复核来源", kind="document", state="IN_REVIEW", resource_id=older[0],
                      text="最新版标记：必须核对估值输入参数，并保留适用条件与复核记录。")
    finally:
        env.owner = owner
    with env.db.begin() as db:
        db.get(m.ResourceVersion, latest[1]).source_verified = False
    with env.db() as db:
        assert svc.can_edit_draft(db, db.get(m.User, env.owner), db.get(m.ResourceVersion, older[1]))
    return older, latest


def build_input(env, source, **overrides):
    return {"space_id": env.space, "source_resource_ids": [source[0]], "source_mode": "unverified_draft",
            "model_selection": {"connection_id": env.connection, "model_id": "synthetic-test-model"},
            "max_pages": 3, "consent": True, **overrides}


def queue_build(env, source, **overrides):
    response = env.call("POST", "/wiki/builds", build_input(env, source, **overrides))
    assert response.status_code == 202, response.text
    jid = response.json()["id"]
    with env.db.begin() as db:
        job = db.get(m.Job, jid)
        job.state, job.attempts = "RUNNING", 1
        job.lease_until = svc.now() + timedelta(minutes=2)
    return jid


def finish(env, jid):
    result = execute(env, jid)
    with env.db.begin() as db:
        job = db.get(m.Job, jid)
        job.state, job.result = "SUCCEEDED", copy.deepcopy(result)
    return result


def source_rows(env, source):
    """Capture source resource/all versions/blocks/blob, including frozen metadata."""
    with env.db() as db:
        versions = select(m.ResourceVersion.id).where(m.ResourceVersion.resource_id == source[0])
        blobs = select(m.ResourceVersion.source_blob_id).where(m.ResourceVersion.resource_id == source[0])
        queries = {
            "resource": select(m.Resource.__table__).where(m.Resource.id == source[0]),
            "versions": select(m.ResourceVersion.__table__).where(m.ResourceVersion.id.in_(versions)),
            "blocks": select(m.ContentBlock.__table__).where(m.ContentBlock.version_id.in_(versions)),
            "blobs": select(m.Blob.__table__).where(m.Blob.id.in_(blobs)),
            "reviews": select(m.ReviewDecision.__table__).where(m.ReviewDecision.version_id.in_(versions)),
        }
        return {name: sorted(repr(tuple(row)) for row in db.execute(query)) for name, query in queries.items()}


def assert_no_created_output(env, jid=None):
    with env.db() as db:
        assert not list(db.scalars(select(m.Resource).where(m.Resource.kind == "knowledge")))
        assert not list(db.scalars(select(m.Release)))
        assert not list(db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like("wiki-provenance:%"))))
        if jid:
            assert db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"wiki-build-receipt:{jid}")) is None


def mutate_source(env, source, change):
    with env.db.begin() as db:
        resource, version = db.get(m.Resource, source[0]), db.get(m.ResourceVersion, source[1])
        if change == "read_acl":
            resource.restricted = True
        elif change == "edit_acl":
            resource.restricted = True
            db.add(m.ResourceGrant(resource_id=resource.id, user_id=env.owner, permission="read"))
        elif change == "editor_role":
            db.delete(db.get(m.SpaceMember, (env.space, env.owner, "editor")))
        elif change == "delete":
            resource.deleted_at = svc.now()
        elif change == "suspend":
            resource.suspended = True
        elif change == "epoch":
            resource.access_epoch += 1
        elif change in {"QUARANTINED", "REJECTED"}:
            db.get(m.Blob, version.source_blob_id).scan_state = change
        elif change == "frozen_hash":
            version.content_sha256 = "a" * 64
        elif change == "body":
            block = db.get(m.ContentBlock, (source[1], source[2]))
            block.data = {"text": "修改后的来源正文，原构建快照必须失效。", "text_format": "markdown"}
            block.search_text = block_text({"block_type": block.block_type, "data": block.data})
            block.content_sha256 = text_sha256(block.search_text)
            version.revision += 1
            # Leave the review-time hash frozen: tampering is not a new approval.
        elif change == "refrozen_body":
            block = db.get(m.ContentBlock, (source[1], source[2]))
            block.data = {"text": "重新冻结的来源正文，仍不能提交之前的模型结果。", "text_format": "markdown"}
            block.search_text = block_text({"block_type": block.block_type, "data": block.data})
            block.content_sha256 = text_sha256(block.search_text)
            version.revision += 1
            db.flush()
            version.content_sha256 = svc.check_frozen_hash(db, version)
        else:
            raise AssertionError(change)


@pytest.mark.parametrize("granularity", ["topic", "knowledge_points"])
def test_review_source_builds_only_unverified_drafts_and_preserves_frozen_source(env, provider, granularity):
    source = review_source(env)
    before = source_rows(env, source)
    if granularity == "knowledge_points":
        def atomic_output(output, data):
            output["pages"][0]["node_role"] = "procedure"
            output["relations"] = []
            output["source_dispositions"] = [
                {"evidence_id": item["id"], "disposition": "EXTRACTED", "reason": "已引用来源。"}
                for item in data["sources"]]
            return output
        provider.output_transform = atomic_output
    jid = queue_build(env, source, granularity=granularity)
    result = finish(env, jid)
    assert provider.calls == 1
    assert result["source_mode"] == "unverified_draft" and result["formal_evidence_allowed"] is False
    assert result["source_version_ids"] == [source[1]]
    assert {item["version_id"] for item in provider.requests[0]["sources"]} == {source[1]}
    assert all(item["review_notice"]["source_state"] == "IN_REVIEW" for item in provider.requests[0]["sources"])
    assert len(result["created_resource_ids"]) == len(result["created_version_ids"]) == 1
    rid, vid = result["created_resource_ids"][0], result["created_version_ids"][0]
    with env.db() as db:
        resource, version = db.get(m.Resource, rid), db.get(m.ResourceVersion, vid)
        assert version.state == "DRAFT" and version.origin == "AI_DRAFT" and not version.source_verified
        assert resource.active_release_id is None and "unverified-sources" in resource.tags
        provenance = wiki._policy(db, f"wiki-provenance:{rid}").config
        assert provenance["source_mode"] == "unverified_draft"
        assert provenance["source_snapshot"][0]["version_id"] == source[1]
        assert {(link.to_version_id, link.to_block_id) for link in db.scalars(
            select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == vid))} == {(source[1], source[2])}
        assert not list(db.scalars(select(m.Release)))
        assert svc.eligible_evidence(db, db.get(m.User, env.owner), env.space) == []
    graph = env.call("GET", f"/wiki/graph?space_id={env.space}")
    assert graph.status_code == 200, graph.text
    assert any(item["id"] == rid and item["source_mode"] == "unverified_draft" for item in graph.json()["nodes"])
    current = env.call("GET", f"/versions/{vid}")
    for action in ("submit", "publish"):
        denied = env.call("POST", f"/versions/{vid}/{action}", {}, etag=current.headers["etag"])
        assert denied.status_code == 409 and denied.json()["code"] == "WIKI_UNVERIFIED_SOURCES"
    assert source_rows(env, source) == before


def test_latest_review_version_is_selected_instead_of_older_editable_draft(env, provider):
    old, latest = newer_review_over_editable_draft(env)
    before = source_rows(env, latest)
    result = finish(env, queue_build(env, latest))
    assert result["source_version_ids"] == [latest[1]]
    sent = provider.requests[0]["sources"]
    assert {item["version_id"] for item in sent} == {latest[1]}
    assert "最新版标记" in json.dumps(sent, ensure_ascii=False)
    assert old[1] not in json.dumps(sent)
    assert source_rows(env, latest) == before


@pytest.mark.parametrize("change,code", [
    ("read_acl", "NOT_FOUND"), ("edit_acl", "NOT_FOUND"), ("editor_role", "FORBIDDEN"),
    ("delete", "NOT_FOUND"), ("suspend", "WIKI_SOURCE_DRAFT_NOT_READY"),
    ("QUARANTINED", "WIKI_SOURCE_NOT_CLEAN"), ("REJECTED", "WIKI_SOURCE_NOT_CLEAN"),
    ("frozen_hash", "WIKI_SOURCE_HASH_INVALID"), ("body", "WIKI_SOURCE_HASH_INVALID"),
])
def test_invalid_review_source_is_rejected_before_model(env, provider, change, code):
    source = review_source(env)
    mutate_source(env, source, change)
    before = source_rows(env, source)
    response = env.call("POST", "/wiki/builds", build_input(env, source))
    assert response.status_code in {403, 404, 409} and response.json()["code"] == code, response.text
    assert provider.calls == 0 and provider.requests == []
    assert source_rows(env, source) == before
    assert_no_created_output(env)
    with env.db() as db:
        assert not list(db.scalars(select(m.Job)))


@pytest.mark.parametrize("change", ["QUARANTINED", "frozen_hash", "body"])
def test_invalid_latest_review_never_falls_back_to_older_valid_draft(env, provider, change):
    _old, latest = newer_review_over_editable_draft(env)
    mutate_source(env, latest, change)
    response = env.call("POST", "/wiki/builds", build_input(env, latest))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == ("WIKI_SOURCE_NOT_CLEAN" if change == "QUARANTINED" else "WIKI_SOURCE_HASH_INVALID")
    assert provider.calls == 0
    assert_no_created_output(env)


@pytest.mark.parametrize("state", ["REJECTED", "APPROVED"])
def test_latest_non_source_state_does_not_reactivate_older_draft(env, provider, state):
    _old, latest = newer_review_over_editable_draft(env)
    with env.db.begin() as db:
        db.get(m.ResourceVersion, latest[1]).state = state
    response = env.call("POST", "/wiki/builds", build_input(env, latest))
    assert response.status_code == 409 and response.json()["code"] == "WIKI_SOURCE_DRAFT_NOT_READY"
    assert provider.calls == 0


@pytest.mark.parametrize("state", ["DRAFT", "IN_REVIEW"])
def test_draft_author_gate_is_preserved_while_readable_review_can_be_reused(env, provider, state):
    source = page(env, "其他作者的来源", kind="document", state=state,
                  text="估值时需要核对价格来源、计量日期与输入参数，并保留完整的复核记录。")
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, source[1])
        version.author_id = env.reader
        version.source_verified = False
        db.flush()
        version.content_sha256 = svc.check_frozen_hash(db, version)
    with env.db() as db:
        actor, version = db.get(m.User, env.owner), db.get(m.ResourceVersion, source[1])
        svc.resource_access(db, actor, source[0], "edit")
        svc.version_access(db, actor, version)
        assert svc.can_edit_draft(db, actor, version) is False
    if state == "DRAFT":
        response = env.call("POST", "/wiki/builds", build_input(env, source))
        assert response.status_code == 409 and response.json()["code"] == "WIKI_SOURCE_DRAFT_NOT_READY"
        assert provider.calls == 0
    else:
        result = finish(env, queue_build(env, source))
        assert result["source_version_ids"] == [source[1]] and provider.calls == 1


def test_review_requires_version_read_authority_in_addition_to_resource_edit(env, provider):
    _old, latest = newer_review_over_editable_draft(env)
    with env.db.begin() as db:
        for role in ("reviewer", "publisher"):
            db.delete(db.get(m.SpaceMember, (env.space, env.owner, role)))
    with env.db() as db:
        svc.resource_access(db, db.get(m.User, env.owner), latest[0], "edit")
    response = env.call("POST", "/wiki/builds", build_input(env, latest))
    assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
    assert provider.calls == 0


@pytest.mark.parametrize("explicit_mode", [False, True])
def test_formal_build_still_rejects_unpublished_review_sources(env, provider, explicit_mode):
    source = review_source(env)
    body = build_input(env, source)
    if explicit_mode:
        body["source_mode"] = "published"
    else:
        body.pop("source_mode")
    response = env.call("POST", "/wiki/builds", body)
    assert response.status_code == 409 and response.json()["code"] == "WIKI_SOURCE_NOT_READY"
    assert provider.calls == 0


@pytest.mark.parametrize("when", ["before_execute", "during_model"])
@pytest.mark.parametrize("change,code", [
    ("epoch", "WIKI_SOURCE_CHANGED"), ("refrozen_body", "WIKI_SOURCE_CHANGED"),
    ("read_acl", "NOT_FOUND"), ("edit_acl", "NOT_FOUND"), ("delete", "NOT_FOUND"),
    ("QUARANTINED", "WIKI_SOURCE_NOT_CLEAN"),
])
def test_review_source_change_rejects_queued_or_inflight_build_without_partial_output(env, provider, when, change, code):
    source = review_source(env)
    jid = queue_build(env, source)
    after_change = {}

    def change_now():
        mutate_source(env, source, change)
        after_change["rows"] = source_rows(env, source)

    if when == "before_execute":
        change_now()
    else:
        provider.after_call = change_now
    with pytest.raises((svc.APIError, wiki.WikiBuildError)) as error:
        execute(env, jid)
    assert error.value.code == code
    assert provider.calls == (1 if when == "during_model" else 0)
    assert source_rows(env, source) == after_change["rows"]
    assert_no_created_output(env, jid)


@pytest.mark.parametrize("new_state", ["DRAFT", "IN_REVIEW"])
def test_new_latest_version_during_generation_invalidates_review_source_snapshot(env, provider, new_state):
    source = review_source(env)
    jid = queue_build(env, source)
    after_change = {}

    def add_version():
        page(env, "更新的来源版本", kind="document", state=new_state, resource_id=source[0],
             text="新版本要求重新核对估值计量日期及输入参数，不能沿用上一版本的生成结果。")
        after_change["rows"] = source_rows(env, source)

    provider.after_call = add_version
    with pytest.raises(wiki.WikiBuildError, match="^WIKI_SOURCE_CHANGED$"):
        execute(env, jid)
    assert provider.calls == 1
    assert source_rows(env, source) == after_change["rows"]
    assert_no_created_output(env, jid)


@pytest.mark.parametrize("change", ["epoch", "refrozen_body", "read_acl", "delete", "QUARANTINED"])
def test_review_source_revocation_invalidates_generated_page_and_job_readback(env, provider, change):
    source = review_source(env)
    jid = queue_build(env, source)
    result = finish(env, jid)
    rid, vid = result["created_resource_ids"][0], result["created_version_ids"][0]
    assert env.call("GET", f"/versions/{vid}").status_code == 200
    mutate_source(env, source, change)
    for path in (f"/resources/{rid}", f"/versions/{vid}", f"/wiki/pages/{rid}/links", f"/jobs/{jid}"):
        response = env.call("GET", path)
        assert response.status_code in {403, 404, 409}, (path, response.text)
    for path in ("/wiki/graph", "/wiki/workspace"):
        response = env.call("GET", f"{path}?space_id={env.space}")
        assert response.status_code == 200 and rid not in response.text


@pytest.mark.parametrize("source_state", ["DRAFT", "IN_REVIEW"])
def test_local_migration_reuses_source_selection_but_local_note_stays_out_of_reference_evidence(env, provider, source_state):
    source = draft_source(env) if source_state == "DRAFT" else review_source(env)
    before = source_rows(env, source)
    body = note(env, [source])
    response = env.call("POST", "/wiki/local-imports", body)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["state"] == "DRAFT" and result["source_mode"] == "unverified_draft"
    assert result["formal_evidence_allowed"] is False and result["citation_precision"] == "DOCUMENT"
    assert provider.calls == 0
    rid, vid = result["resource_id"], result["version_id"]
    with env.db() as db:
        version = db.get(m.ResourceVersion, vid)
        assert version.origin == "COPY" and not version.source_verified
        lineage = wiki._policy(db, f"wiki-provenance:{rid}").config
        assert lineage["source_version_ids"] == [source[1]]
        assert lineage["source_snapshot"][0]["version_id"] == source[1]
        assert lineage["imported_local_note"]["citation_precision"] == "DOCUMENT"
        assert not list(db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == vid)))
        records = reference_evidence(db, env.owner, env.space)
        assert {item["version_id"] for item in records} == {source[1]}
        assert reference_evidence(db, env.owner, env.space, version_ids=[vid]) == []
        assert svc.eligible_evidence(db, env.owner, env.space) == []
    # Removing presentation labels cannot turn a bibliographic import into evidence.
    resource = env.call("GET", f"/resources/{rid}")
    assert env.call("PATCH", f"/resources/{rid}", {"tags": []}, etag=resource.headers["etag"]).status_code == 200
    with env.db() as db:
        assert reference_evidence(db, env.owner, env.space, version_ids=[vid]) == []
    current = env.call("GET", f"/versions/{vid}")
    denied = env.call("POST", f"/versions/{vid}/submit", {"review_scope": "reference"}, etag=current.headers["etag"])
    assert denied.status_code == 409 and denied.json()["code"] == "WIKI_UNVERIFIED_SOURCES"
    assert source_rows(env, source) == before
