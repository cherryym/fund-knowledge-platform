"""Real temporary rebuilds: unchanged counts must still expose a new generation."""
import copy
import json
import os
from pathlib import Path

import pytest

from fund_kb import models as m, services as svc
from fund_kb.vector_indexing import receipt_name
from test_dual_retrieval_api import applications, env, synthetic_backend  # noqa: F401
from test_wiki import page


def status(env, profile="qwen3-4b"):
    response = env.call("GET", f"/retrieval/status?space_id={env.space}&profile_id={profile}")
    assert response.status_code == 200, response.text
    return response.json()


def rebuild(env, profile="qwen3-4b", force=True):
    queued = env.call("POST", "/retrieval/index-jobs", {"space_id": env.space, "force": force,
        "retrieval_selection": {"profile_id": profile}})
    assert queued.status_code == 202, queued.text
    jid = queued.json()["id"]
    assert env.dispatcher.run(jid)
    return jid


def test_rebuilding_same_material_updates_generation_not_cardinality(env):
    page(env, "合成来源", "相同资料反复重建，不修改原文。", kind="document")
    first_id = rebuild(env)
    first = status(env)
    second_id = rebuild(env)
    second = status(env)
    assert first["coverage"] == second["coverage"]
    assert first["index_snapshot"]["generation"] != second["index_snapshot"]["generation"]
    for reply, jid in [(first, first_id), (second, second_id)]:
        snapshot = reply["index_snapshot"]
        assert snapshot["last_rebuild"]["id"] == snapshot["latest_job"]["id"] == jid
        assert snapshot["last_rebuild"]["application_state"] == "CURRENT_GENERATION_VERIFIED"
        assert snapshot["last_rebuild"]["matched_current_versions"] == 1
        assert snapshot["last_indexed_at"] and snapshot["checked_at"] and snapshot["last_rebuild"]["completed_at"].endswith("Z")
    # Another read/navigation does not invent a new generation or lose the receipt.
    again = status(env)
    assert again["index_snapshot"]["generation"] == second["index_snapshot"]["generation"]
    assert again["index_snapshot"]["latest_job"]["id"] == second_id
    if directory := os.environ.get("INDEX_REFRESH_CONTRACT_DIR"):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        for name, response in (("before", first), ("after", second)):
            with (path / (name + ".json")).open("x") as handle:
                json.dump(response, handle, ensure_ascii=False, indent=2)


def test_history_and_current_catalog_remain_separate_and_profile_bound(env):
    original = page(env, "合成来源V1", "旧正文完整保存。", kind="document")
    page(env, "合成来源V2", "新正文完整保存。", kind="document", resource_id=original[0])
    jid = rebuild(env)
    value = status(env)
    assert value["coverage"]["catalog_pages"] == value["coverage"]["indexed_pages"] == 1
    assert value["index_snapshot"]["latest_job"]["result"]["total_versions"] == 2
    assert value["index_snapshot"]["last_rebuild"]["id"] == jid
    other = status(env, "bge-m3")
    assert other["index_snapshot"]["last_rebuild"] is None and other["index_snapshot"]["latest_job"] is None
    assert other["index_snapshot"]["generation"] is None


def test_new_content_requires_sync_and_newer_partial_write_does_not_claim_old_full_generation(env):
    source = page(env, "合成来源", "原始正文。", kind="document")
    jid = rebuild(env)
    with env.db.begin() as db:
        snapshot_hash = db.get(m.ResourceVersion, source[1]).content_sha256
    page(env, "另一合成来源", "新加入的正文。", kind="document")
    stale = status(env)["index_snapshot"]
    assert stale["state"] == "SYNC_REQUIRED"
    assert stale["last_rebuild"]["application_state"] == "CURRENT_STATE_UNVERIFIED"
    incremental = rebuild(env, force=False)
    refreshed = status(env)["index_snapshot"]
    assert refreshed["latest_job"]["id"] == incremental and refreshed["last_rebuild"]["id"] == jid
    assert refreshed["state"] == "CURRENT_CATALOG_INDEXED"
    assert refreshed["last_rebuild"]["application_state"] == "CURRENT_GENERATION_DIFFERS"
    with env.db() as db:
        assert db.get(m.ResourceVersion, source[1]).content_sha256 == snapshot_hash


def test_other_users_jobs_are_not_disclosed_and_read_only_get_does_not_change_database(env):
    page(env, "合成来源", "只读可见正文。", kind="document")
    jid = rebuild(env)
    env.login(env.reader)
    first = status(env)
    assert first["index_snapshot"]["state"] == "CURRENT_CATALOG_INDEXED"
    assert first["index_snapshot"]["last_rebuild"] is None and first["index_snapshot"]["latest_job"] is None
    assert jid not in str(first)
    with env.db() as db:
        job_before = copy.deepcopy(svc.job_dict(db.get(m.Job, jid)))
    status(env)
    with env.db() as db:
        assert svc.job_dict(db.get(m.Job, jid)) == job_before


@pytest.mark.parametrize("state,failed", [("FAILED", 1), ("CANCELLED", 0), ("SUCCEEDED", 1), ("SUCCEEDED", None)])
def test_terminal_job_without_complete_success_never_claims_rebuild_applied(env, state, failed):
    page(env, "合成来源", "正文。", kind="document")
    jid = rebuild(env)
    with env.db.begin() as db:
        job = db.get(m.Job, jid)
        job.state = state
        result = dict(job.result)
        if failed is None:
            result.pop("failed_versions", None)
        else:
            result["failed_versions"] = failed
        job.result = result
    value = status(env)["index_snapshot"]
    assert value["last_rebuild"]["counts_verified"] is False
    assert value["last_rebuild"]["application_state"] == "OPERATION_NOT_FULLY_SUCCESSFUL"


def test_unknown_index_date_and_unavailable_vector_never_become_current(env, monkeypatch):
    source = page(env, "合成来源", "正文。", kind="document")
    rebuild(env)
    vector = env.registry.resolve("qwen3-4b").vector
    with env.db.begin() as db:
        receipt = db.query(m.RuntimePolicy).filter_by(name=receipt_name(vector.embedding.fingerprint, source[1])).one()
        receipt.config = {**receipt.config, "indexed_at": "not-a-date"}
    assert status(env)["index_snapshot"]["last_indexed_at"] is None
    original = vector.status()
    monkeypatch.setattr(vector, "status", lambda: {**original, "available": False})
    assert status(env)["index_snapshot"]["last_rebuild"]["application_state"] == "CURRENT_STATE_UNVERIFIED"
