"""Copy verified local vectors to the authenticated loopback service, without re-embedding."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))


def payload_hash(point):
    return hashlib.sha256(json.dumps(point.payload,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()


def verify_payload(expected, actual, path=()):
    """Text/identifiers/hashes are exact; only PDF bbox JSON float ULPs may differ."""
    if type(expected) is not type(actual):
        raise RuntimeError("MIGRATION_PAYLOAD_TYPE_MISMATCH")
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            raise RuntimeError("MIGRATION_PAYLOAD_KEYS_MISMATCH")
        return max((verify_payload(value, actual[key], (*path, key))
                    for key, value in expected.items()), default=0.)
    if isinstance(expected, list):
        if len(expected) != len(actual):
            raise RuntimeError("MIGRATION_PAYLOAD_LENGTH_MISMATCH")
        return max((verify_payload(a, b, (*path, i))
                    for i, (a, b) in enumerate(zip(expected, actual))), default=0.)
    if expected == actual:
        return 0.
    if isinstance(expected, float) and path[:2] == ("locator", "bbox") \
            and len(path) == 3 and math.isfinite(expected) and math.isfinite(actual):
        error = abs(expected - actual)
        if error <= 1e-9:
            return error
    raise RuntimeError("MIGRATION_PAYLOAD_MISMATCH")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--source-job",required=True)
    args=parser.parse_args()
    from sqlalchemy import select
    import numpy as np
    from qdrant_client import models
    from fund_kb import models as m
    from fund_kb.db import build_engine,make_session_factory
    from fund_kb.settings import Settings
    from fund_kb.retrieval import VectorIndex
    settings=Settings(retrieval_profile=ROOT/"data"/"retrieval-profile.json")
    engine=build_engine(settings.database_url)
    factory=make_session_factory(engine)
    with factory() as db:
        job=db.get(m.Job,args.source_job)
        if not job or job.state!="SUCCEEDED" or job.payload.get("task")!="VECTOR_INDEX":
            raise RuntimeError("SOURCE_INDEX_JOB_NOT_COMPLETE")
        fingerprint=job.result["fingerprint"]
        for active in db.scalars(select(m.Job).where(m.Job.kind=="COMPILE",m.Job.state.in_(["RUNNING","QUEUED"]))):
            if active.payload.get("task")=="VECTOR_INDEX":
                raise RuntimeError("INDEX_STILL_RUNNING")
    manifest_path=ROOT/"data"/"qdrant-native.json"
    manifest=json.loads(manifest_path.read_text())
    if manifest.get("url")!="http://127.0.0.1:6333":
        raise RuntimeError("TARGET_IS_NOT_OWNED_LOOPBACK_SERVICE")
    key_file=Path(manifest["key_file"])
    if key_file.resolve()!=(ROOT/"data"/"private"/"qdrant-api.key").resolve():
        raise RuntimeError("TARGET_KEY_REFERENCE_INVALID")
    source=VectorIndex(settings.model_copy(update={"qdrant_url":None,"qdrant_api_key":None}))
    target=VectorIndex(settings.model_copy(update={"qdrant_url":manifest["url"],"qdrant_api_key":key_file.read_text().strip()}))
    receipt_path=ROOT/"data"/"qdrant-service-migration.json"
    try:
        if source.embedding.fingerprint!=fingerprint or target.embedding.fingerprint!=fingerprint:
            raise RuntimeError("PROJECTION_GENERATION_MISMATCH")
        if not source._ensure():
            raise RuntimeError("SOURCE_COLLECTION_MISSING")
        previous=json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        client=target._shared.client
        if client.collection_exists(target.collection) and client.count(target.collection,exact=True).count:
            if previous.get("fingerprint")!=fingerprint:
                raise RuntimeError("TARGET_NOT_EMPTY_REVIEW_REQUIRED")
        target._ensure(create=True)
        client.update_collection(target.collection,optimizers_config=models.OptimizersConfigDiff(indexing_threshold=0))
        receipt={"state":"COPYING","source_job":args.source_job,"fingerprint":fingerprint,
            "collection":source.collection,"generation_model_calls":0,"embedding_recomputed":False}
        receipt_path.write_text(json.dumps(receipt,indent=2))
        expected=source._shared.client.count(source.collection,exact=True).count
        hashes,vectors,payloads={},{},{}
        offset=None
        copied=0
        while True:
            points,offset=source._shared.client.scroll(source.collection,limit=256,offset=offset,with_payload=True,with_vectors=True)
            if points:
                client.upsert(target.collection,points=[models.PointStruct(id=point.id,vector=point.vector,payload=point.payload) for point in points],wait=True)
                for point in points:
                    hashes[str(point.id)]=payload_hash(point)
                    payloads[str(point.id)]=point.payload
                    vectors[str(point.id)]=np.asarray(point.vector,dtype=np.float32)
                copied+=len(points)
                if copied%4096==0:
                    print(json.dumps({"stage":"copying","copied":copied,"expected":expected}),flush=True)
            if offset is None:
                break
        count=client.count(target.collection,exact=True).count
        if copied!=expected or count!=expected:
            raise RuntimeError("MIGRATION_POINT_COUNT_MISMATCH")
        verified,max_error,max_coordinate_error,offset=0,0.,0.,None
        while True:
            points,offset=client.scroll(target.collection,limit=256,offset=offset,with_payload=True,with_vectors=True)
            for point in points:
                pid=str(point.id)
                if pid not in hashes:
                    raise RuntimeError("MIGRATION_POINT_ID_MISMATCH")
                original_payload=payloads.pop(pid)
                if hashes.pop(pid)!=payload_hash(point):
                    max_coordinate_error=max(max_coordinate_error,verify_payload(original_payload,point.payload))
                old=vectors.pop(pid,None)
                new=np.asarray(point.vector,dtype=np.float32)
                if old is None or old.shape!=new.shape:
                    raise RuntimeError("MIGRATION_VECTOR_SHAPE_MISMATCH")
                error=float(np.max(np.abs(old-new)))
                if not np.isfinite(error) or error>2e-6:
                    raise RuntimeError("MIGRATION_VECTOR_VALUES_MISMATCH")
                max_error=max(max_error,error)
                verified+=1
            if offset is None:
                break
        if hashes or vectors or payloads or verified!=expected:
            raise RuntimeError("MIGRATION_INCOMPLETE")
        client.update_collection(target.collection,optimizers_config=models.OptimizersConfigDiff(indexing_threshold=20000))
        receipt.update(state="VERIFIED",source_points=expected,target_points=count,verified_points=verified,
            max_vector_absolute_error=max_error,max_pdf_coordinate_absolute_error=max_coordinate_error,
            payload_noncoordinate_fields_exact=True,source_retained=True)
        receipt_path.write_text(json.dumps(receipt,indent=2))
        manifest.update(activation_ready=True,collection=target.collection,migration_receipt=str(receipt_path))
        manifest_path.write_text(json.dumps(manifest,indent=2))
        print(json.dumps(receipt,indent=2),flush=True)
    finally:
        source.close();target.close();engine.dispose()


if __name__=="__main__":
    main()
