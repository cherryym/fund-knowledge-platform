"""Small SSE snapshots; full answers, diagnostics and authorization are not cached."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_reference_security_review import queued_run
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import page

from fund_kb import api_consultation, providers
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.answer_preview import previews
from fund_kb.answer_progress import progress_projection


def parse(response):
    assert response.status_code == 200, response.text
    return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))


def event_id(response):
    return next(line[4:] for line in response.text.splitlines() if line.startswith("id: "))


def test_projection_has_explicit_small_whitelist_and_keeps_all_final_data_out():
    run = SimpleNamespace(id="r", thread_id="t", state="RUNNING", created_at=svc.now(), error_code=None,
        model_snapshot={"model_id":"selected-model", "api_key":"synthetic-secret", "base_url":"private",
            "query_path":{"deferred_unit_ids":["x"] * 10000}, "hybrid_retrieval":{"queries":["x"] * 10000},
            "wiki_reading":{"loaded_blocks":999, "page_titles":["private-title"] * 10000},
            "last_request":{"phase":"synthesis","state":"waiting", "diagnostic":{"raw":"private"},
                "usage":{"prompt_tokens":2000,"api_key":"private"}},
            "reading_progress":{"stage":"synthesis","total_characters":10000,"raw":"private"}},
        policy_snapshot={})
    job = SimpleNamespace(id="j", stage="GENERATING", attempts=2)
    result = progress_projection(run, job, invalidated=False)
    encoded = json.dumps(result)
    assert len(encoded) < 1600 and "private" not in encoded and "synthetic-secret" not in encoded
    assert result["model_snapshot"]["wiki_reading"] == {"loaded_blocks":999}
    assert result["model_snapshot"]["last_request"]["usage"] == {"prompt_tokens":2000}
    assert "response" not in result and "query_path" not in encoded
    assert result["progress_version"] == 1 and result["source_check"] == "current_access"


def test_progress_does_not_build_full_answer_projection_and_same_state_reuses_event_id(env, monkeypatch):
    rid, jid = queued_run(env)
    with env.db.begin() as db:
        run = db.get(m.ConsultationRun, rid)
        run.model_snapshot = {"model_id":"synthetic", "model_invoked":True, "model_request_count":1,
            "query_path":{"deferred_unit_ids":["irrelevant-to-progress"] * 10000},
            "reading_progress":{"stage":"loading_sections","loaded_blocks":1}}
    monkeypatch.setattr(api_consultation, "_run_projection", lambda *a: pytest.fail("Progress must not render the full answer"))
    with env.db() as db:
        before = db.scalar(select(func.count()).select_from(m.AuditEvent))
    response = env.call("GET", f"/runs/{rid}/events")
    data = parse(response)
    assert data["run_id"] == rid and data["job_id"] == jid
    assert "query_path" not in data["model_snapshot"] and len(response.content) < 2000
    repeated = env.client.get(f"/api/v1/runs/{rid}/events", headers={"Last-Event-ID":event_id(response)})
    assert repeated.status_code == 200 and "data:" not in repeated.text and ": unchanged" in repeated.text
    with env.db.begin() as db:
        run = db.get(m.ConsultationRun, rid)
        run.model_snapshot = {**run.model_snapshot,"reading_progress":{"stage":"loading_sections","loaded_blocks":2}}
    changed = env.call("GET", f"/runs/{rid}/events")
    assert parse(changed)["model_snapshot"]["reading_progress"]["loaded_blocks"] == 2
    assert event_id(changed) != event_id(response)
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.AuditEvent)) == before
    env.login(env.reader)
    assert env.call("GET", f"/runs/{rid}/events").status_code == 404


def test_terminal_progress_omits_full_answer_and_invalidated_source_metadata(env):
    rid, _ = queued_run(env)
    with env.db.begin() as db:
        run = db.get(m.ConsultationRun, rid)
        run.state, run.response = "COMPLETED", {"full": "complete-answer-must-use-GET-run"}
        run.invalidated_at = svc.now()
        run.model_snapshot = {"model_id":"synthetic", "wiki_reading":{"loaded_blocks":999}, "evidence_count":30}
    response = env.call("GET", f"/runs/{rid}/events")
    data = parse(response)
    assert data["invalidated"] and data["state"] == "COMPLETED"
    assert "event: completed" in response.text and "complete-answer" not in response.text
    assert "wiki_reading" not in data["model_snapshot"] and "evidence_count" not in data["model_snapshot"]


def test_progress_preview_keeps_real_fresh_source_fence_and_revokes_changed_body(env):
    from fund_kb.reference_evidence import reference_evidence
    from fund_kb.source_reading_policy import policy_stamp
    _, vid, bid = page(env, "合成来源", "完整合成来源正文。", kind="document")
    cid = svc.uid()
    rid, jid = queued_run(env, selection={"connection_id":cid,"model_id":"synthetic"})
    model = {"id":cid, "owner_user_id":env.owner, "revision":1,"allow_document_transfer":True,
        "context_space_id":env.space,"protocol":"responses"}
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=cid,name=providers.NAMESPACE+cid,updated_by=env.owner,
            config={"owner_user_id":env.owner,"enabled":True,"allow_document_transfer":True}))
        rows = reference_evidence(db, env.owner, env.space, reading=True, version_ids={vid})
        run = db.get(m.ConsultationRun,rid)
        run.state = "RUNNING"
        run.model_snapshot = {"answer_engine":"wiki_reader","model_request_count":1,"last_request":{"phase":"synthesis","attempt":1}}
        run.evidence_snapshot = [{**{k:row[k] for k in ("resource_id","version_id","block_id","content_sha256","reference_signature")},"evidence_id":"E1"} for row in rows]
        entry = previews.begin(run_id=rid,job_id=jid,attempt=1,owner_id=env.owner,request_number=1,
            request_hash=svc.digest(run.request),evidence_hash=svc.digest(run.evidence_snapshot),model=model,
            catalog_stamp=None,policy_stamp=policy_stamp(db,env.space))
    try:
        previews.update(entry,"公开段落。[E1]\n\n")
        first = env.call("GET",f"/runs/{rid}/events")
        assert parse(first)["model_snapshot"]["public_preview"]["text"] == "公开段落。[E1]"
        previews.update(entry,"公开段落。[E1]\n\n第二段。\n\n")
        updated = env.call("GET",f"/runs/{rid}/events")
        assert event_id(first) != event_id(updated)
        with env.db.begin() as db:
            block = db.get(m.ContentBlock,(vid,bid))
            block.search_text = "模拟原文被替换但旧Hash未更新"
        hidden = parse(env.call("GET",f"/runs/{rid}/events"))
        assert "public_preview" not in hidden["model_snapshot"] and previews.get(rid) is None
    finally:
        previews.discard(rid)
