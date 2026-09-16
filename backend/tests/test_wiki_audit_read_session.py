"""Offline adapter contract/security tests; all databases belong to tmp_path."""
from __future__ import annotations

import copy
import importlib.util
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import DatabaseError
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import page
from test_wiki import provider as provider  # noqa: PLC0414
from test_wiki_read_concurrency import (
    offline_identity_and_network as offline_identity_and_network,  # noqa: PLC0414
)
from test_wiki_review_sources import review_source
from test_wiki_unverified import build_draft

from fund_kb import admin_review
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.ingestion import block_text, text_sha256

SPEC = importlib.util.spec_from_file_location("wiki_audit_adapter", Path(__file__).resolve().parents[2]
                                            / "scripts/wiki-audit-read-session.py")
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)
Reader, AuditReadError = ADAPTER.WikiAuditReadSession, ADAPTER.AuditReadError
CANARY = "SYNTHETIC_CREDENTIAL_MUST_NOT_BE_READ"


class API:
    def __init__(self, env):
        self.env, self.calls, self.me_count = env, [], 0
        self.me_hook = self.graph_hook = None

    def request(self, method, path):
        assert method == "GET", "The adapter must never login, restore OAuth, refresh or mutate"
        self.calls.append((method, path))
        response = self.env.call(method, path)
        if response.status_code != 200:
            raise RuntimeError(CANARY)
        result = response.json()
        if path == "/me":
            self.me_count += 1
            if self.me_hook:
                result = self.me_hook(result, self.me_count)
        elif self.graph_hook:
            result = self.graph_hook(result)
        return result


@pytest.fixture
def scope(env, tmp_path):
    source = page(env, "审计原件", kind="document")
    knowledge = page(env, "审计知识", cites=[source])
    jid, secret_id = svc.uid(), svc.uid()
    with env.db.begin() as db:
        db.add(m.Job(id=jid, kind="COMPILE", owner_id=env.owner, state="SUCCEEDED", dedupe_key="audit:" + jid,
            payload={"task": "WIKI_BUILD", "space_id": env.space, "source_snapshot": [{"version_id": source[1]}]},
            result={"created_resource_ids": [knowledge[0]], "created_version_ids": [knowledge[1]],
                    "source_version_ids": [source[1]]}))
        db.add(m.RuntimePolicy(id=svc.uid(), name="wiki-provenance:" + knowledge[0], updated_by=env.owner,
            config={"resource_id": knowledge[0], "space_id": env.space, "owner_id": env.owner,
                    "source_mode": "published", "source_version_ids": [source[1]]}))
        db.add(m.RuntimePolicy(id=secret_id, name="model-connection:" + secret_id, updated_by=env.owner,
                               config={"credential_ciphertext": CANARY, "api_key": CANARY}))
    path = Path(env.app.state.engine.url.database)
    assert path.resolve().parent == tmp_path.resolve()
    with sqlite3.connect(path, isolation_level=None) as raw:
        assert raw.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    return env, API(env), source, knowledge, jid, secret_id, path


def reader_for(scope, **overrides):
    env, api, source, knowledge, jid, _secret_id, path = scope
    arguments = {"database_path": path, "space_id": env.space,
                 "resource_ids": [source[0], knowledge[0]], "version_ids": [source[1], knowledge[1]],
                 "job_ids": [jid]}
    arguments.update(overrides)
    return Reader(api, **arguments)


def test_body_etag_metadata_and_final_revalidation_match_real_handlers(scope):
    env, api, source, knowledge, jid, _secret_id, _path = scope
    with reader_for(scope) as reader:
        assert reader.status == "OPEN_UNVERIFIED" and api.calls == [("GET", "/me")]
        for kind, oid in (("resources", knowledge[0]), ("versions", knowledge[1]), ("jobs", jid)):
            path = f"/{kind}/{oid}"
            expected = env.call("GET", path)
            assert expected.status_code == 200
            actual = reader.get(path)
            assert actual == expected.json()
            actual.clear()
            assert reader.get(path) == expected.json(), "Caller mutation corrupted the audit snapshot"
            cached = reader._view.bodies[(kind, oid)]
            if expected.headers.get("etag"):
                assert cached["headers"]["ETag"] == expected.headers["etag"]
        assert reader._view.db.autoflush is False
        report = reader.close_and_revalidate()
    assert report["status"] == "PASS" and reader.status == "PASS"
    assert report["resources_checked"] == report["versions_checked"] == 2
    assert report["jobs_checked"] == 1 and report["formal_evidence_allowed"] is False
    assert api.me_count == 3 and {path for _method, path in api.calls} == {"/me"}
    stamps = [datetime.fromisoformat(report[key]) for key in (
        "authenticated_at_utc", "snapshot_started_at_utc", "snapshot_ended_at_utc",
        "revalidation_started_at_utc", "revalidation_snapshot_at_utc", "revalidation_finished_at_utc")]
    assert stamps == sorted(stamps)
    with pytest.raises(AuditReadError, match="AUDIT_CLOSED"):
        reader.get(f"/versions/{source[1]}")


@pytest.mark.parametrize("path", [
    "/resources", "/jobs", "/me", "/auth/demo", "/model-connections/anything",
    "/versions/{vid}/publish", "/versions/{vid}?extra=true", "https://invalid/versions/{vid}",
    "//invalid/versions/{vid}", "/versions/{vid}#fragment", "/versions/%2e%2e",
    "/wiki/graph?space_id={space}&space_id={space}", "/wiki/graph?space_id={space}&q=anything",
    "/wiki/graph?space_id={space}&depth=4",
])
def test_only_supported_paths_are_admitted_and_failure_cannot_be_certified(scope, path):
    env, api, _source, knowledge, _jid, _secret_id, _dbpath = scope
    reader = reader_for(scope)
    with pytest.raises(AuditReadError):
        reader.get(path.format(vid=knowledge[1], space=env.space))
    assert reader.status == "FAILED" and reader._view is None
    assert api.calls == [("GET", "/me")]
    with pytest.raises(AuditReadError):
        reader.close_and_revalidate()


@pytest.mark.parametrize("kind", ["resources", "versions", "jobs"])
def test_known_id_allowlist_is_required(scope, kind):
    with reader_for(scope) as reader, pytest.raises(AuditReadError, match="AUDIT_ID_NOT_ALLOWED"):
        reader.get(f"/{kind}/{svc.uid()}")


def test_missing_dependency_scope_fails_instead_of_skipping_acl(scope):
    _env, _api, _source, knowledge, _jid, _secret_id, _path = scope
    with reader_for(scope, resource_ids=[knowledge[0]], version_ids=[knowledge[1]]) as reader:
        with pytest.raises(AuditReadError, match="OUT_OF_SCOPE"):
            reader.get(f"/versions/{knowledge[1]}")
        assert reader.status == "FAILED"


def test_cross_space_ids_are_rejected_even_when_explicitly_allowlisted(scope):
    env, _api, source, knowledge, _jid, _secret_id, _path = scope
    foreign = page(env, "不在验收空间")
    with env.db.begin() as db:
        sid = svc.uid()
        db.add(m.Space(id=sid, name="另一个空间"))
        db.flush()
        db.add(m.SpaceMember(space_id=sid, user_id=env.owner, role="reader"))
        db.get(m.Resource, foreign[0]).space_id = sid
    with (reader_for(scope, resource_ids=[source[0], knowledge[0], foreign[0]],
                     version_ids=[source[1], knowledge[1], foreign[1]]) as reader,
          pytest.raises(AuditReadError, match="AUDIT_RESOURCE_OUT_OF_SCOPE")):
        reader.get(f"/resources/{foreign[0]}")


def test_model_jobs_are_rejected_before_loading_model_policies(scope):
    env, _api, _source, _knowledge, jid, secret_id, _path = scope
    with env.db.begin() as db:
        db.get(m.Job, jid).payload = {"task": "MODEL_TEST", "space_id": env.space, "connection_id": secret_id}
    with reader_for(scope) as reader, pytest.raises(AuditReadError, match="AUDIT_JOB_OUT_OF_SCOPE"):
        reader.get(f"/jobs/{jid}")


def test_sqlite_is_readonly_and_credential_tables_and_policies_are_unreadable(scope):
    env, _api, _source, knowledge, _jid, secret_id, _path = scope
    with reader_for(scope) as reader:
        queries = []

        def query(connection, cursor, statement, params, context, many):
            queries.append((statement.upper(), params))

        event.listen(reader._engine, "before_cursor_execute", query)
        reader.get(f"/versions/{knowledge[1]}")
        assert not any("LOGIN_SESSIONS" in sql for sql, _params in queries)
        assert CANARY not in str(queries)
        rows = reader._view.db.scalars(select(m.RuntimePolicy)).all()
        assert all(row.name.startswith(("wiki-", "space-governance:")) for row in rows)
        assert not any(row.id == secret_id for row in rows)
        with pytest.raises(AuditReadError, match="AUDIT_SECRET_POLICY_FORBIDDEN"):
            reader._view.db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "model-connection:" + secret_id))
        with pytest.raises(AuditReadError, match="AUDIT_SQL_NOT_ALLOWED"):
            reader._view.db.execute(update(m.User).where(m.User.id == env.owner).values(active=False))
        connection = reader._view.db.connection()
        assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
        with pytest.raises(DatabaseError):
            connection.exec_driver_sql("SELECT token_hash FROM login_sessions")
        with pytest.raises(DatabaseError):
            connection.exec_driver_sql("PRAGMA query_only=OFF")


@pytest.mark.parametrize("change", ["source_acl", "source_epoch", "source_body", "source_hash", "blob_hash",
                                    "blob_scan", "parent_tags", "job_result", "job_state", "provenance"])
def test_new_snapshot_rejects_changes_to_read_objects_or_transitive_source_closure(scope, change):
    env, _api, source, knowledge, jid, _secret_id, _path = scope
    reader = reader_for(scope)
    old = reader.get(f"/versions/{knowledge[1]}")
    reader.get(f"/jobs/{jid}")
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, source[1])
        if change == "source_acl":
            db.get(m.Resource, source[0]).restricted = True
        elif change == "source_epoch":
            db.get(m.Resource, source[0]).access_epoch += 1
        elif change == "source_body":
            block = db.get(m.ContentBlock, (source[1], source[2]))
            block.data = {"text": "来源内容已改变。"}
            block.search_text = block_text({"block_type": block.block_type, "data": block.data})
            block.content_sha256 = text_sha256(block.search_text)
        elif change == "source_hash":
            version.content_sha256 = "a" * 64
        elif change == "blob_hash":
            db.get(m.Blob, version.source_blob_id).sha256 = "b" * 64
        elif change == "blob_scan":
            db.get(m.Blob, version.source_blob_id).scan_state = "QUARANTINED"
        elif change == "parent_tags":
            db.get(m.Resource, knowledge[0]).tags = ["changed"]
        elif change == "job_result":
            job = db.get(m.Job, jid)
            job.result = {**job.result, "changed": True}
        elif change == "job_state":
            db.get(m.Job, jid).stage = "changed"
        else:
            policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "wiki-provenance:" + knowledge[0]))
            policy.config = {**policy.config, "changed": True}
    assert reader.get(f"/versions/{knowledge[1]}") == old, "The initial view must remain a single snapshot"
    with pytest.raises(AuditReadError):
        reader.close_and_revalidate()
    assert reader.status == "FAILED" and reader._view is None


@pytest.mark.parametrize("phase", [2, 3])
@pytest.mark.parametrize("change", ["identity", "expired", "write_during_validation"])
def test_final_api_reauthentication_and_concurrent_commit_fence(scope, phase, change):
    env, api, _source, knowledge, _jid, _secret_id, _path = scope
    reader = reader_for(scope)
    reader.get(f"/versions/{knowledge[1]}")

    def hook(me, count):
        if count == phase:
            if change == "identity":
                return {**me, "id": env.reader}
            if change == "expired":
                raise RuntimeError(CANARY)
            with env.db.begin() as db:
                db.get(m.User, env.reader).display_name = "unrelated concurrent commit"
        return me

    api.me_hook = hook
    with pytest.raises(AuditReadError) as error:
        reader.close_and_revalidate()
    assert CANARY not in str(error.value)
    assert reader.status == "FAILED" and reader._view is None


def test_initial_api_authentication_precedes_database_access_and_checks_expected_user(scope, tmp_path):
    _env, api, _source, _knowledge, _jid, _secret_id, _path = scope
    api.me_hook = lambda _me, _count: (_ for _ in ()).throw(RuntimeError(CANARY))
    with pytest.raises(AuditReadError, match="AUDIT_API_AUTH_FAILED"):
        reader_for(scope, database_path=tmp_path / "absent.sqlite3")
    assert not (tmp_path / "absent.sqlite3").exists()
    api.me_hook = None
    with pytest.raises(AuditReadError, match="AUDIT_IDENTITY_MISMATCH"):
        reader_for(scope, expected_user_id=svc.uid())


def test_private_space_governance_cannot_be_hidden_by_policy_filter(scope):
    env, _api, _source, _knowledge, _jid, _secret_id, _path = scope
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=svc.uid(), name="space-governance:" + env.space, updated_by=env.owner,
            config={"schema_version": 1, "space_id": env.space, "kind": "personal", "owner_id": env.reader}))
    with pytest.raises(AuditReadError, match="AUDIT_API_AUTH_FAILED"):
        reader_for(scope)


def test_local_governance_rechecks_api_to_database_authorization_race(scope):
    env, api, _source, _knowledge, _jid, _secret_id, _path = scope

    def privatize_after_me(me, count):
        if count == 1:
            with env.db.begin() as db:
                db.add(m.RuntimePolicy(id=svc.uid(), name="space-governance:" + env.space, updated_by=env.owner,
                    config={"schema_version": 1, "space_id": env.space, "kind": "personal", "owner_id": env.reader}))
        return me

    api.me_hook = privatize_after_me
    with pytest.raises(AuditReadError, match="AUDIT_OPEN_FAILED"):
        reader_for(scope)


@pytest.mark.parametrize("wiki_state", ["DRAFT", "IN_REVIEW"])
def test_real_unverified_build_with_review_source_matches_api_without_adapter_model_calls(scope, provider, wiki_state):
    env, _api, _source, _knowledge, _jid, _secret_id, _path = scope
    source = review_source(env)
    jid, built = build_draft(env, source)  # Synthetic provider setup only.
    rid, vid = built["created_resource_ids"][0], built["created_version_ids"][0]
    if wiki_state == "IN_REVIEW":
        before = env.call("GET", f"/versions/{vid}")
        reviewed = env.call("POST", f"/versions/{vid}/submit", {"review_scope": "reference"},
                            etag=before.headers["etag"])
        assert reviewed.status_code == 200 and reviewed.json()["state"] == "IN_REVIEW"
    before_calls = provider.calls
    with reader_for(scope, resource_ids=[source[0], rid], version_ids=[source[1], vid], job_ids=[jid]) as reader:
        for path in (f"/versions/{vid}", f"/resources/{rid}", f"/jobs/{jid}"):
            assert reader.get(path) == env.call("GET", path).json()
        report = reader.close_and_revalidate()
        assert report["status"] == "PASS" and report["versions_checked"] == 2
    assert provider.calls == before_calls
    with env.db() as db:
        assert db.get(m.ResourceVersion, source[1]).state == "IN_REVIEW"
        assert db.get(m.ResourceVersion, vid).state == wiki_state


def test_generated_page_without_supplied_lineage_never_leaks_before_final_check(scope):
    env, _api, _source, knowledge, _jid, _secret_id, _path = scope
    with env.db.begin() as db:
        db.get(m.ResourceVersion, knowledge[1]).origin = "AI_DRAFT"
        policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "wiki-provenance:" + knowledge[0]))
        db.delete(policy)
    with reader_for(scope, job_ids=()) as reader, pytest.raises(AuditReadError, match="AUDIT_PROVENANCE_SCOPE_REQUIRED"):
        reader.get(f"/versions/{knowledge[1]}")


def test_allowlisted_legacy_receipt_is_preserved_and_revalidated(scope):
    env, _api, source, knowledge, jid, _secret_id, _path = scope
    with env.db.begin() as db:
        db.get(m.ResourceVersion, knowledge[1]).origin = "AI_DRAFT"
        policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "wiki-provenance:" + knowledge[0]))
        db.delete(policy)
        db.add(m.RuntimePolicy(id=svc.uid(), name="wiki-build-receipt:" + jid, updated_by=env.owner,
            config={"space_id": env.space, "owner_id": env.owner, "result": {
                "created_resource_ids": [knowledge[0]], "created_version_ids": [knowledge[1]],
                "source_version_ids": [source[1]]}}))
    with reader_for(scope) as reader:
        assert reader.get(f"/versions/{knowledge[1]}")["origin"] == "AI_DRAFT"
        assert reader.close_and_revalidate()["status"] == "PASS"


def test_symlink_database_path_is_rejected_without_opening_it(scope, tmp_path):
    _env, _api, _source, _knowledge, _jid, _secret_id, path = scope
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(path)
    with pytest.raises(AuditReadError, match="AUDIT_DATABASE_PATH_INVALID"):
        reader_for(scope, database_path=alias)


@pytest.mark.parametrize("alias", [False, True])
def test_graph_uses_http_but_enforces_known_ids_and_revalidates_full_response(scope, alias):
    env, api, _source, _knowledge, _jid, _secret_id, _path = scope
    with reader_for(scope) as reader:
        route = f"/graph?space={env.space}" if alias else f"/wiki/graph?space_id={env.space}"
        graph = reader.get(route)
        assert graph == env.call("GET", f"/wiki/graph?space_id={env.space}").json()
        assert reader.close_and_revalidate()["status"] == "PASS"
    assert len([path for _method, path in api.calls if path.startswith("/wiki/graph?")]) == 2


def test_graph_cannot_expand_allowlist_and_changes_cannot_be_certified(scope):
    env, api, _source, _knowledge, _jid, _secret_id, _path = scope
    reader = reader_for(scope)
    path = f"/wiki/graph?space_id={env.space}"
    reader.get(path)

    def changed(body):
        result = copy.deepcopy(body)
        result["nodes"][0]["id"] = svc.uid()
        return result

    api.graph_hook = changed
    with pytest.raises(AuditReadError, match="AUDIT_GRAPH_OUT_OF_SCOPE"):
        reader.close_and_revalidate()
    assert reader.status == "FAILED"


def test_abandon_empty_and_thread_mismatch_never_return_pass(scope):
    _env, _api, _source, knowledge, _jid, _secret_id, _path = scope
    with reader_for(scope) as reader, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reader.get, f"/versions/{knowledge[1]}")
        with pytest.raises(AuditReadError, match="AUDIT_THREAD_MISMATCH"):
            future.result()
    assert reader.status == "CLOSED_UNVERIFIED"
    empty = reader_for(scope)
    with pytest.raises(AuditReadError, match="AUDIT_EMPTY_READ_SET"):
        empty.close_and_revalidate()
    assert empty.status == "FAILED"


def seed_admin_confirmation(env, record):
    """Only seed attestation metadata in tmp_path; never call approve/publish."""
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, record[1])
        resource = db.get(m.Resource, record[0])
        config = {"mode": admin_review.MODE, "version_id": version.id, "actor_id": env.owner,
                  "confirmed_at": svc.primitive(svc.now()), "reason": "合成管理员确认，仅用于隔离测试",
                  "content_sha256": version.content_sha256, "resource_access_epoch": resource.access_epoch,
                  "provenance_sha256": admin_review.provenance_hash(db, resource.id),
                  "dependency_version_ids": sorted(svc.dependency_ids(db, version)),
                  "previous_state": version.state, "independent_review": False,
                  "validity_attested_by_user": True, "legal_status_unchanged": version.legal_status,
                  "parsed_content_available": True}
        db.add(m.RuntimePolicy(id=svc.uid(), name="admin-review:" + version.id,
                               config=config, updated_by=env.owner))
    return config


def test_exact_allowlisted_admin_confirmation_matches_api_and_excludes_other_admin_policies(scope):
    env, _api, _source, knowledge, _jid, secret_id, _path = scope
    version_path, resource_path = f"/versions/{knowledge[1]}", f"/resources/{knowledge[0]}"
    before_version = env.call("GET", version_path).json()
    before_resource = env.call("GET", resource_path).json()
    config = seed_admin_confirmation(env, knowledge)
    forbidden_names = {"admin-review:" + svc.uid(), "admin-settings:" + knowledge[1],
                       "admin-review:" + knowledge[1] + ":extra", "admin-review-default"}
    with env.db.begin() as db:
        for name in forbidden_names:
            db.add(m.RuntimePolicy(id=svc.uid(), name=name, updated_by=env.owner,
                                   config={"credential_ciphertext": CANARY}))
    with reader_for(scope) as reader:
        assert reader.get(version_path) == before_version
        assert reader.get(resource_path) == before_resource
        version = reader._view.db.get(m.ResourceVersion, knowledge[1])
        assert admin_review.confirmation(reader._view.db, version) == config
        assert config["mode"] == "ADMIN_CONFIRMED" and config["independent_review"] is False
        rows = list(reader._view.db.scalars(select(m.RuntimePolicy)))
        assert not {row.name for row in rows} & forbidden_names
        assert not any(row.id == secret_id for row in rows)
        snapshot = reader._view.seal()
        allowed_name = "admin-review:" + knowledge[1]
        assert snapshot["metadata"]["policies"][allowed_name]["config"] == config
        assert CANARY not in str(snapshot)
        assert reader.close_and_revalidate()["status"] == "PASS"
    assert env.call("GET", version_path).json() == before_version
    assert env.call("GET", resource_path).json() == before_resource
    with env.db() as db:
        assert not list(db.scalars(select(m.Job).where(m.Job.kind == "PUBLISH")))


@pytest.mark.parametrize("change", ["insert", "delete", "revoke", "reason"])
def test_admin_confirmation_presence_and_revocation_are_revalidated_without_manuscript_changes(scope, change):
    env, _api, _source, knowledge, _jid, _secret_id, _path = scope
    if change != "insert":
        seed_admin_confirmation(env, knowledge)
    reader = reader_for(scope)
    path = f"/versions/{knowledge[1]}"
    original = reader.get(path)
    if change == "insert":
        seed_admin_confirmation(env, knowledge)
    else:
        with env.db.begin() as db:
            policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "admin-review:" + knowledge[1]))
            if change == "delete":
                db.delete(policy)
            else:
                field, value = ("mode", "REVOKED") if change == "revoke" else ("reason", "确认说明变更")
                policy.config = {**policy.config, field: value}
    assert env.call("GET", path).json() == original
    assert reader.get(path) == original
    with pytest.raises(AuditReadError, match="AUDIT_REVALIDATION_CHANGED"):
        reader.close_and_revalidate()
    assert reader.status == "FAILED" and reader._view is None
