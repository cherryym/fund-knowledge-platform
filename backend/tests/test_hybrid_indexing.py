"""Real temporary DB/Qdrant lifecycle and API; synthetic corpus, no network models."""
from __future__ import annotations

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from test_wiki import env as env  # noqa: PLC0414 - pytest fixture import
from test_wiki import page

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.jobs import JobDispatcher
from fund_kb.retrieval import VectorIndex
from fund_kb.vector_indexing import receipt_name


def enable(env):
    env.settings = env.settings.model_copy(update={"retrieval_mode":"hybrid","embedding_mode":"hashing",
        "embedding_model":"synthetic-hashing","embedding_dimensions":384})
    env.app.state.settings = env.settings
    vector = VectorIndex(env.settings)
    env.app.state.vector_index = vector
    return vector


def execute_index(env, vector, *, force=False):
    response = env.call("POST","/retrieval/index-jobs",{"space_id":env.space,"force":force})
    assert response.status_code == 202, response.text
    jid=response.json()["id"]
    dispatcher=JobDispatcher(env.settings,env.db,vector)
    try:
        dispatcher.run(jid)
    finally:
        dispatcher.close()
    return jid


def test_readonly_discovery_has_no_body_leak_or_job_write(env):
    visible=page(env,"基金费用口径",text="BODY_MUST_NOT_BE_IN_DISCOVERY_RESULT")
    hidden=page(env,"隐藏费用条款",text="HIDDEN_BODY",restricted=True)
    env.login(env.reader)
    with env.db() as db:
        counts=[db.scalar(select(func.count()).select_from(cls)) for cls in (m.Job,m.AuditEvent,m.IdempotencyRecord)]
    response=env.call("POST","/retrieval/search",{"space_id":env.space,"query":"费用","scope":"reference"})
    assert response.status_code == 200,response.text
    body=response.json()
    assert body["mode"] == "wiki_fallback" and body["evidence_preview"] is False
    assert visible[0] in {hit["resource_id"] for hit in body["hits"]}
    assert hidden[0] not in response.text and "HIDDEN_BODY" not in response.text
    assert "BODY_MUST_NOT" not in response.text
    with env.db() as db:
        assert counts == [db.scalar(select(func.count()).select_from(cls)) for cls in (m.Job,m.AuditEvent,m.IdempotencyRecord)]


def test_index_roles_and_space_boundaries(env):
    page(env)
    vector=enable(env)
    try:
        env.login(env.reader)
        status=env.call("GET",f"/retrieval/status?space_id={env.space}")
        assert status.status_code == 200 and status.json()["permissions"]["can_index"] is False
        assert env.call("POST","/retrieval/index-jobs",{"space_id":env.space}).status_code == 403
        assert env.call("GET",f"/retrieval/status?space_id={svc.uid()}").status_code in {403,404}
    finally:
        vector.close()


def test_real_index_complete_repeated_sync_and_force_generation(env):
    source=page(env,"估值规则来源",kind="document",text="停牌股票需要核对适用估值规则。"*50)
    knowledge=page(env,"停牌股票估值",text="停牌期间的估值处理，应结合具体适用条件。"*30,cites=[source])
    vector=enable(env)
    try:
        jid=execute_index(env,vector)
        with env.db() as db:
            job=db.get(m.Job,jid)
            assert job.state == "SUCCEEDED", (job.error_code,job.result)
            assert job.version_id is None
            assert job.result["total_versions"] == job.result["completed_versions"] == 2
            assert job.result["indexed_versions"] == 2 and job.result["indexed_chunks"] > 2
            old_projection=db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name ==
                receipt_name(vector.embedding.fingerprint,source[1]))).config["projection_id"]
        status=env.call("GET",f"/retrieval/status?space_id={env.space}").json()
        assert status["coverage"]["catalog_pages"] == status["coverage"]["indexed_pages"] == 2
        assert status["coverage"]["dirty_pages"] == 0
        query=env.call("POST","/retrieval/search",{"space_id":env.space,"query":"停牌股票估值"})
        assert query.status_code == 200,query.text
        assert {hit["resource_id"] for hit in query.json()["hits"]} == {source[0],knowledge[0]}
        assert any("bm25" in hit["channels"] for hit in query.json()["hits"])
        assert "DEVELOPMENT_HASHING_NOT_SEMANTIC" in query.json()["warnings"]
        second=execute_index(env,vector)
        with env.db() as db:
            job=db.get(m.Job,second)
            assert job.state == "SUCCEEDED" and job.result["skipped_versions"] == 2
            assert job.result["indexed_versions"] == 0
        forced=execute_index(env,vector,force=True)
        with env.db() as db:
            assert db.get(m.Job,forced).state == "SUCCEEDED"
            new_projection=db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name ==
                receipt_name(vector.embedding.fingerprint,source[1]))).config["projection_id"]
            assert new_projection != old_projection
        assert vector.search("停牌",[source[1]],allowed_projection_ids=[old_projection]) == []
    finally:
        vector.close()


def test_revocation_and_metadata_change_remove_ready_discovery_immediately(env):
    item=page(env,"估值费用规则",text="费用口径需要核对。")
    vector=enable(env)
    try:
        execute_index(env,vector)
        env.login(env.reader)
        assert env.call("POST","/retrieval/search",{"space_id":env.space,"query":"费用"}).json()["hits"]
        with env.db.begin() as db:
            resource=db.get(m.Resource,item[0]);resource.restricted=True;resource.access_epoch+=1
        response=env.call("POST","/retrieval/search",{"space_id":env.space,"query":"费用"})
        assert response.status_code == 200 and response.json()["hits"] == []
        assert item[0] not in response.text and item[1] not in response.text
        env.login()
        with env.db.begin() as db:
            resource=db.get(m.Resource,item[0]);resource.restricted=False;resource.revision+=1
        status=env.call("GET",f"/retrieval/status?space_id={env.space}").json()
        assert status["coverage"]["indexed_pages"] == 0 and status["coverage"]["dirty_pages"] == 1
    finally:
        vector.close()


def test_index_cancel_never_activates_partial_vectors_and_owner_can_retry(env,monkeypatch):
    item=page(env,"完整段落",text="中文向量取消验收。"*300)
    vector=enable(env)
    try:
        created=env.call("POST","/retrieval/index-jobs",{"space_id":env.space})
        jid=created.json()["id"]
        original=vector.embedding.embed
        def cancel_after_compute(texts,**kwargs):
            result=original(texts,**kwargs)
            with env.db.begin() as db:
                db.get(m.Job,jid).cancel_requested=True
            return result
        monkeypatch.setattr(vector.embedding,"embed",cancel_after_compute)
        dispatcher=JobDispatcher(env.settings,env.db,vector)
        try: dispatcher.run(jid)
        finally: dispatcher.close()
        with env.db() as db:
            assert db.get(m.Job,jid).state == "CANCELLED"
            assert db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name ==
                receipt_name(vector.embedding.fingerprint,item[1]))) is None
        assert vector.search("取消",[item[1]]) == []
        retried=env.call("POST",f"/jobs/{jid}/retry",{})
        assert retried.status_code in {200,202},retried.text
    finally:
        vector.close()


def test_index_bad_source_hash_is_explicit_failure_not_full_success(env):
    item=page(env,"需核对正文",text="原文快照。")
    with env.db.begin() as db:
        db.get(m.ContentBlock,(item[1],item[2])).data={"text":"被更改的内容","text_format":"markdown"}
    vector=enable(env)
    try:
        jid=execute_index(env,vector)
        with env.db() as db:
            job=db.get(m.Job,jid)
            assert job.state == "FAILED",job.result
            assert job.result["failed_versions"] == 1 and job.result["indexed_versions"] == 0
    finally:
        vector.close()


def test_missing_receipted_projection_is_dirty_and_incrementally_repaired(env):
    item=page(env,"可恢复投影",text="索引缺失必须真实重建，不能仅凭回执跳过。")
    vector=enable(env)
    try:
        execute_index(env,vector)
        vector.delete_versions([item[1]])
        status=env.call("GET",f"/retrieval/status?space_id={env.space}").json()
        assert status["coverage"]["indexed_pages"] == 0
        assert status["coverage"]["dirty_pages"] == 1
        jid=execute_index(env,vector)
        with env.db() as db:
            job=db.get(m.Job,jid)
            assert job.state == "SUCCEEDED",job.result
            assert job.result["indexed_versions"] == 1
            assert job.result["skipped_versions"] == 0
        assert env.call("GET",f"/retrieval/status?space_id={env.space}").json()["coverage"]["indexed_pages"] == 1
    finally:
        vector.close()


def test_receipt_commit_failure_keeps_previous_projection_readable(env):
    item=page(env,"事务恢复验证",text="旧投影应保留，直至新回执提交成功。")
    vector=enable(env)
    receipt_key=receipt_name(vector.embedding.fingerprint,item[1])
    try:
        execute_index(env,vector)
        with env.db() as db:
            before=dict(db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name==receipt_key)).config)
        failures=[]

        def fail_receipt_commit(session):
            # The worker has its own Session subclass and may already have
            # flushed the receipt. Inspect this transaction, not session.dirty.
            row=session.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name==receipt_key))
            if not failures and row and row.config.get("projection_id") != before["projection_id"]:
                failures.append("commit")
                raise RuntimeError("SYNTHETIC_SQL_COMMIT_FAILURE")

        event.listen(Session,"before_commit",fail_receipt_commit)
        try:
            jid=execute_index(env,vector,force=True)
        finally:
            event.remove(Session,"before_commit",fail_receipt_commit)
        assert failures == ["commit"]
        with env.db() as db:
            assert db.get(m.Job,jid).state == "FAILED"
            assert db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name==receipt_key)).config == before
        assert vector.search("投影",[item[1]],allowed_projection_ids=[before["projection_id"]])
        status=env.call("GET",f"/retrieval/status?space_id={env.space}").json()
        assert status["coverage"]["indexed_pages"] == 1
        recovered=execute_index(env,vector)
        with env.db() as db:
            job=db.get(m.Job,recovered)
            assert job.state == "SUCCEEDED" and job.result["skipped_versions"] == 1
        hits=vector.search("投影",[item[1]])
        assert hits and {hit["projection_id"] for hit in hits} == {before["projection_id"]}
    finally:
        vector.close()
