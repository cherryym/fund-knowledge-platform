"""Explicit operator entry for one authorized library's rebuildable vector index."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from datetime import timedelta
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))


def source_fingerprint(factory,version_ids):
    from sqlalchemy import select
    from fund_kb import models as m, services as svc
    digest=hashlib.sha256()
    counts={"versions":0,"blocks":0,"relations":0,"citations":0}
    with factory() as db:
        ids=sorted(version_ids)
        for start in range(0,len(ids),400):
            batch=ids[start:start+400]
            statements={
                "versions":select(m.ResourceVersion.id,m.ResourceVersion.revision,m.ResourceVersion.content_sha256,
                    m.ResourceVersion.title,m.ResourceVersion.state,m.ResourceVersion.legal_status,
                    m.Resource.revision,m.Resource.access_epoch,m.Resource.deleted_at,m.Resource.suspended,m.Resource.active_release_id)
                    .join(m.Resource,m.Resource.id==m.ResourceVersion.resource_id).where(m.ResourceVersion.id.in_(batch)).order_by(m.ResourceVersion.id),
                "blocks":select(m.ContentBlock.version_id,m.ContentBlock.block_id,m.ContentBlock.ordinal,m.ContentBlock.block_type,
                    m.ContentBlock.data,m.ContentBlock.locator,m.ContentBlock.search_text,m.ContentBlock.content_sha256)
                    .where(m.ContentBlock.version_id.in_(batch)).order_by(m.ContentBlock.version_id,m.ContentBlock.block_id),
                "relations":select(m.RelationEdge.id,m.RelationEdge.source_version_id,m.RelationEdge.target_resource_id,
                    m.RelationEdge.relation_type,m.RelationEdge.conditions,m.RelationEdge.evidence_version_id,m.RelationEdge.evidence_block_id)
                    .where(m.RelationEdge.source_version_id.in_(batch)).order_by(m.RelationEdge.id),
                "citations":select(m.EvidenceLink.id,m.EvidenceLink.from_version_id,m.EvidenceLink.from_block_id,
                    m.EvidenceLink.to_version_id,m.EvidenceLink.to_block_id,m.EvidenceLink.purpose)
                    .where(m.EvidenceLink.from_version_id.in_(batch)).order_by(m.EvidenceLink.id),
            }
            for kind,statement in statements.items():
                for row in db.execute(statement):
                    counts[kind]+=1
                    digest.update(json.dumps(svc.primitive([kind,*row]),sort_keys=True,ensure_ascii=False,separators=(",",":")).encode())
    return {"sha256":digest.hexdigest(),"counts":counts}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--run",action="store_true",help="Create and execute an actual indexing job")
    parser.add_argument("--force",action="store_true")
    parser.add_argument("--user-name",default="李明")
    parser.add_argument("--space-name",default="基金运营部")
    parser.add_argument("--profile",type=Path,default=ROOT/"data"/"retrieval-profile.json")
    args=parser.parse_args()
    from sqlalchemy import select
    from fund_kb import models as m, services as svc
    from fund_kb.db import build_engine, make_session_factory
    from fund_kb.settings import Settings
    from fund_kb.retrieval import VectorIndex
    from fund_kb.jobs import JobDispatcher
    from fund_kb.vector_indexing import index_plan, queue_index
    from local_vector_runtime import operator_vector_settings
    settings=operator_vector_settings(Settings(retrieval_profile=args.profile.resolve()))
    engine=build_engine(settings.database_url)
    factory=make_session_factory(engine)
    with factory() as db:
        users=list(db.scalars(select(m.User).where(m.User.display_name==args.user_name)))
        spaces=list(db.scalars(select(m.Space).where(m.Space.name==args.space_name)))
        if len(users)!=1 or len(spaces)!=1:
            raise RuntimeError("AMBIGUOUS_INDEX_ACTOR_OR_SPACE")
        user_id,space_id=users[0].id,spaces[0].id
        plan=index_plan(db,users[0],space_id)
    summary={"mode":"execute" if args.run else "plan_only","user_id":user_id,"space_id":space_id,
        "authorized_versions":len(plan),"model":settings.embedding_model,"dimensions":settings.embedding_dimensions,
        "source_documents_modified":False,"generation_model_calls":0,
        "vector_backend":"service" if settings.qdrant_url else "embedded"}
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    if not args.run:
        engine.dispose();return
    before=source_fingerprint(factory,[item["version_id"] for item in plan])
    vector=VectorIndex(settings)
    dispatcher=JobDispatcher(settings,factory,vector)
    try:
        with factory.begin() as db:
            ctx=SimpleNamespace(db=db,user=db.get(m.User,user_id),settings=settings,
                data={"space_id":space_id},dispatch=[],request=SimpleNamespace(state=SimpleNamespace(trace_id="operator:vector-index")))
            job=queue_index(ctx,force=args.force)
            job_id=job.id
            # Reserve before commit, so an older still-running app worker cannot
            # claim an extension task it has not yet loaded.
            if job.state != "QUEUED":
                raise RuntimeError("INDEX_JOB_NOT_RESERVABLE")
            job.state,job.stage,job.attempts="RUNNING","CLAIMED",job.attempts+1
            job.lease_until=svc.now()+timedelta(seconds=dispatcher.lease_seconds)
            attempt=job.attempts
            dispatcher._audit(db,job,"job.operator_claimed",{"actor_source":"explicit_local_maintenance"})
        print(json.dumps({"job_id":job_id,"stage":"INDEXING"}),flush=True)
        success=dispatcher.run_claimed(job_id,attempt)
        with factory() as db:
            job=db.get(m.Job,job_id)
            result={"job_id":job_id,"success":success,"state":job.state,"error_code":job.error_code,"result":job.result}
        after=source_fingerprint(factory,[item["version_id"] for item in plan])
        result["source_integrity"]={"before":before,"after":after,"unchanged":before==after}
        output=ROOT/"data"/"vector-index-runs"
        output.mkdir(parents=True,exist_ok=True)
        (output/f"{job_id}.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
        if result["state"] != "SUCCEEDED":
            raise SystemExit(1)
    finally:
        dispatcher.close();vector.close();engine.dispose()


if __name__=="__main__":
    main()
