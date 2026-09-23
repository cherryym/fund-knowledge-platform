"""Durable, permission-checked vector projection lifecycle; sources stay immutable."""
from __future__ import annotations

import logging
from contextlib import contextmanager

from sqlalchemy import select

from . import models as m
from . import services as svc
from .ingestion import block_text, text_sha256
from .reference_evidence import _reference_lineage
from .wiki_catalog import CatalogAuthority, build_catalog

TASK = "VECTOR_INDEX"
PREFIX = "vector-receipt:"
log = logging.getLogger(__name__)
_UNSET = object()
RETRIEVAL_BINDING_FIELDS = frozenset({"profile_id", "fingerprint", "model", "dimensions"})


@contextmanager
def retrieval_registry_context(ctx):
    """Use the API/dispatcher registry; standalone queue callers own a temporary one."""
    state = getattr(getattr(getattr(ctx, "request", None), "app", None), "state", None)
    registry = getattr(ctx, "retrieval_registry", None) or getattr(state, "retrieval_registry", None)
    if registry is None and getattr(ctx.settings, "retrieval_profiles_file", None) is None:
        yield None
        return
    from .retrieval_registry import RetrievalProfileError
    owned = False
    try:
        if registry is None and getattr(ctx.settings, "retrieval_profiles_file", None) is not None:
            from .retrieval_registry import registry_for
            cached = getattr(ctx.settings, "_retrieval_registry", None)
            registry = registry_for(ctx.settings, getattr(state, "vector_index", None))
            owned = registry is not None and registry is not cached
        yield registry
    except RetrievalProfileError as exc:
        svc.fail(409, exc.code, "检索配置不可用或已变化，请核对原方案")
    finally:
        if owned:
            registry.close()


def freeze_retrieval_selection(registry, selection=None, *, require_frozen=False):
    """Validate a client choice or an immutable server binding without filling legacy rows."""
    if registry is None:
        if selection is not None or require_frozen:
            svc.fail(409, "RETRIEVAL_REGISTRY_UNAVAILABLE", "本轮检索配置尚未装配，不能替换为其他索引")
        return None
    if require_frozen and (not isinstance(selection, dict) or set(selection) != RETRIEVAL_BINDING_FIELDS):
        svc.fail(409, "RETRIEVAL_BINDING_REQUIRED", "任务缺少明确的检索绑定，请新建请求，不猜补历史配置")
    if selection is not None and (not isinstance(selection, dict) or not selection.get("profile_id")
            or set(selection) - RETRIEVAL_BINDING_FIELDS):
        svc.fail(422, "RETRIEVAL_SELECTION_INVALID", "检索选择仅接受服务器注册的方案标识和指纹")
    from .retrieval_registry import RetrievalProfileError
    try:
        requested = {key: selection[key] for key in ("profile_id", "fingerprint") if key in selection} if selection is not None else None
        frozen = registry.freeze(requested)
    except RetrievalProfileError as exc:
        svc.fail(409, exc.code, "检索配置不可用或指纹已变化，请核对原方案")
    if (not isinstance(frozen, dict) or set(frozen) != RETRIEVAL_BINDING_FIELDS
            or any(not isinstance(frozen[key], str) or not frozen[key] for key in ("profile_id", "fingerprint", "model"))
            or type(frozen["dimensions"]) is not int or frozen["dimensions"] <= 0):
        svc.fail(409, "RETRIEVAL_BINDING_INVALID", "服务器检索绑定不完整")
    if (require_frozen or isinstance(selection, dict) and RETRIEVAL_BINDING_FIELDS <= selection.keys()) and frozen != selection:
        svc.fail(409, "RETRIEVAL_BINDING_CHANGED", "任务冻结的检索配置已变化，不能静默换用其他方案")
    return dict(frozen)


def receipt_name(fingerprint, version_id):
    return f"{PREFIX}{fingerprint[:24]}:{version_id}"


def receipts_for(db, fingerprint, version_ids):
    ids = list(version_ids)
    values = {}
    for start in range(0, len(ids), 400):
        names = [receipt_name(fingerprint, vid) for vid in ids[start:start + 400]]
        for row in db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.in_(names))):
            if row.config.get("fingerprint") == fingerprint:
                values[row.config.get("version_id")] = row.config
    return values


def current_receipts(db, vector, pages):
    if vector is None:
        return {}
    receipts = receipts_for(db, vector.embedding.fingerprint, [page["version_id"] for page in pages.values()])
    candidates = {page["version_id"]: receipts[page["version_id"]] for page in pages.values()
        if receipts.get(page["version_id"], {}).get("state") == "READY"
        and receipts[page["version_id"]].get("metadata_signature") == page["_metadata_signature"]}
    try:
        complete = vector.complete_projection_ids(list(candidates.values()))
    except Exception:  # noqa: BLE001 - an unavailable projection never reports full coverage.
        log.warning("Vector projection verification unavailable")
        return {}
    return {vid: row for vid, row in candidates.items() if row["projection_id"] in complete}


def prune_committed_projection(dispatcher, job_id, attempt, item):
    """Read the committed winner under DB locks before retiring only ready losers.

    Vector activation cannot delete the old generation before the SQL receipt
    commits. Another worker may have committed a newer receipt by this point,
    so never prune using the caller's earlier projection id.
    """
    vector = dispatcher.vector_index
    with dispatcher.session_factory.begin() as db:
        job = dispatcher._fence(db, job_id, attempt)
        user = dispatcher._user(db, job)
        authorize_index_job(db, user, job)
        db.scalar(select(m.Space).where(m.Space.id == job.payload["space_id"]).with_for_update())
        db.scalar(select(m.Resource).where(m.Resource.id == item["resource_id"]).with_for_update())
        db.scalar(select(m.ResourceVersion).where(m.ResourceVersion.id == item["version_id"]).with_for_update())
        receipt = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name ==
            receipt_name(vector.embedding.fingerprint, item["version_id"])).with_for_update())
        if receipt and receipt.config.get("state") in {"READY", "EMPTY"}:
            keep = [receipt.config["projection_id"]] if receipt.config["state"] == "READY" else []
            vector.prune_ready_projections(item["version_id"], keep)


def authorize_index_job(db, user, job):
    svc.space_access(db, user, job.payload["space_id"], None if job.payload.get("automatic") else "editor")
    for rid in job.payload.get("resource_ids", []):
        resource = db.get(m.Resource, rid)
        if resource is not None:
            svc.resource_access(db, user, resource, "read", allow_deleted=bool(resource.deleted_at))


def queue_index(ctx, *, resource_ids=None, force=False, automatic=False, retrieval_selection=None):
    """An explicit request queues one profile; automatic fan-out lives below."""
    with retrieval_registry_context(ctx) as registry:
        selection = retrieval_selection if retrieval_selection is not None else ctx.data.get("retrieval_selection")
        frozen = freeze_retrieval_selection(registry, selection)
        settings = registry.resolve(frozen["profile_id"], fingerprint=frozen["fingerprint"]).settings if frozen else ctx.settings
        return _queue_index(ctx, resource_ids=resource_ids, force=force, automatic=automatic,
            retrieval_selection=frozen, settings=settings)


def _queue_index(ctx, *, resource_ids, force, automatic, retrieval_selection, settings):
    if automatic and (settings.retrieval_mode != "hybrid" or not settings.hybrid_index_auto_sync):
        return None
    space_id = ctx.data.get("space_id") if resource_ids is None else None
    if resource_ids:
        resources = [ctx.db.get(m.Resource, rid) for rid in resource_ids]
        if any(resource is None for resource in resources):
            return None
        space_id = resources[0].space_id
        if any(resource.space_id != space_id for resource in resources):
            svc.fail(422, "INDEX_SPACE_MISMATCH", "索引对象必须属于同一知识库")
    svc.space_access(ctx.db, ctx.user, space_id, None if automatic else "editor")
    requested = set(resource_ids or [])
    # Reuse queued work; running work may have frozen an older source snapshot,
    # so a later edit needs a queued successor instead of being lost.
    for job in ctx.db.scalars(select(m.Job).where(m.Job.owner_id == ctx.user.id,
            m.Job.kind == "COMPILE", m.Job.state == "QUEUED")):
        if job.payload.get("task") != TASK or job.payload.get("space_id") != space_id:
            continue
        if job.cancel_requested or job.payload.get("retrieval_selection") != retrieval_selection:
            continue
        covered = set(job.payload.get("resource_ids", []))
        if (not covered or requested and requested <= covered) and (not force or job.payload.get("force")):
            return job
    payload = {"task": TASK, "space_id": space_id, "resource_ids": sorted(requested),
        "force": bool(force), "automatic": bool(automatic),
        **({"retrieval_selection": retrieval_selection} if retrieval_selection is not None else {})}
    dedupe = f"VECTOR_INDEX:{retrieval_selection['profile_id']}:{retrieval_selection['fingerprint']}:{svc.uid()}" if retrieval_selection else None
    job = svc.create_job(ctx, "COMPILE", payload, dedupe_key=dedupe)
    if retrieval_selection is not None:
        job.result = {"retrieval_selection": dict(retrieval_selection)}
    return job


def _queue_resource_jobs(ctx, resource_id):
    with retrieval_registry_context(ctx) as registry:
        if registry is None:
            job = _queue_index(ctx, resource_ids=[resource_id], force=False, automatic=True,
                retrieval_selection=None, settings=ctx.settings)
            return [job] if job is not None else []
        jobs = []
        # Freeze every target before writing any job, so one unavailable
        # profile cannot quietly leave a partially queued dual update.
        targets = []
        for profile_id in registry.enabled_ids():
            frozen = freeze_retrieval_selection(registry, {"profile_id": profile_id})
            runtime = registry.resolve(profile_id, fingerprint=frozen["fingerprint"])
            targets.append((frozen, runtime.settings))
        for frozen, settings in targets:
            job = _queue_index(ctx, resource_ids=[resource_id], force=False, automatic=True,
                retrieval_selection=frozen, settings=settings)
            if job is not None:
                jobs.append(job)
        return jobs


def queue_resource_index(ctx, resource_id):
    jobs = _queue_resource_jobs(ctx, resource_id)
    # Keep the historical single-job return type; every profile has its own
    # committed Job/Outbox and is present in ctx.dispatch, including BGE.
    return jobs[0] if jobs else None


def queue_removed_version(ctx, resource_id, version_id):
    jobs = _queue_resource_jobs(ctx, resource_id)
    for job in jobs:
        job.payload = {**job.payload, "removed_version_ids": sorted({
            *job.payload.get("removed_version_ids", []), version_id})}
    return jobs[0] if jobs else None


def follow_mutation(ctx, result):
    """Only reviewed content mutations can request derived projection work."""
    operations = {"submitVersion", "reviewVersion", "replaceDraftRelations", "confirmAdminReview",
        "createWikiLocalImport", "saveWikiEntryMaintenance", "reviewWikiMaintenanceProposal",
        "resolveWikiMaintenanceConflict", "updateDocumentSourceProperties"}
    if ctx.operation not in operations or not isinstance(result, svc.Result):
        return
    body = result.body if isinstance(result.body, dict) else {}
    resource_ids = set()
    if body.get("resource_id"):
        resource_ids.add(body["resource_id"])
    for vid in (body.get("version_id"), body.get("new_version_id"), body.get("created_version_id")):
        version = ctx.db.get(m.ResourceVersion, vid) if isinstance(vid, str) else None
        if version:
            resource_ids.add(version.resource_id)
    if ctx.operation in {"submitVersion", "reviewVersion", "replaceDraftRelations", "confirmAdminReview"}:
        version = ctx.db.get(m.ResourceVersion, ctx.id)
        if version:
            resource_ids.add(version.resource_id)
    if ctx.operation == "saveWikiEntryMaintenance":
        resource_ids.add(ctx.id)
    if ctx.operation in {"reviewWikiMaintenanceProposal", "resolveWikiMaintenanceConflict"}:
        policy = ctx.db.get(m.RuntimePolicy, ctx.id)
        if policy and isinstance(policy.config.get("resource_ids"), list):
            resource_ids.update(policy.config["resource_ids"])
    for rid in sorted(resource_ids):
        resource = ctx.db.get(m.Resource, rid)
        if resource and resource.kind in {"document", "knowledge"}:
            queue_resource_index(ctx, rid)


def records_for_version(db, user, space_id, version_id):
    version = db.get(m.ResourceVersion, version_id)
    resource = db.get(m.Resource, version.resource_id) if version else None
    if not resource or resource.space_id != space_id or resource.kind not in {"document", "knowledge"}:
        svc.fail(404, "INDEX_SOURCE_UNAVAILABLE", "索引来源不可用")
    svc.space_access(db, user, space_id)
    lineage = _reference_lineage(db, user, version, reading=True)
    metadata = CatalogAuthority(db, user).check(version_id)
    records = []
    for block in db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == version_id)
            .order_by(m.ContentBlock.ordinal, m.ContentBlock.block_id)):
        if block.search_text != block_text({"block_type": block.block_type, "data": block.data}) \
                or block.content_sha256 != text_sha256(block.search_text):
            svc.fail(409, "INDEX_SOURCE_HASH_MISMATCH", "索引来源内容校验未通过")
        records.append({"resource_id": resource.id, "version_id": version.id, "block_id": block.block_id,
            "title": version.title, "text": block.search_text, "content_sha256": block.content_sha256,
            "locator": block.locator or {}, "ordinal": block.ordinal, "kind": resource.kind,
            "source_kind": resource.kind, "knowledge_type": version.knowledge_type,
            "block_type": block.block_type, "data": block.data})
    return records, metadata, svc.digest(lineage)


def index_plan(db, user, space_id, resource_ids=(), *, automatic=False):
    svc.space_access(db, user, space_id, None if automatic else "editor")
    query = select(m.Resource).where(m.Resource.space_id == space_id,
        m.Resource.kind.in_(["document", "knowledge"]), m.Resource.deleted_at.is_(None), m.Resource.suspended.is_(False))
    if resource_ids:
        query = query.where(m.Resource.id.in_(resource_ids))
    resources = list(db.scalars(query))
    access = CatalogAuthority(db, user)
    access.resources.update({resource.id: resource for resource in resources})
    plan = []
    for start in range(0, len(resources), 400):
        ids = [resource.id for resource in resources[start:start + 400]]
        versions = list(db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id.in_(ids))
            .order_by(m.ResourceVersion.resource_id, m.ResourceVersion.version_no)))
        access.versions.update({version.id: version for version in versions})
        for version in versions:
            try:
                stamp = access.check(version.id)
            except svc.APIError:
                continue
            plan.append({"resource_id": version.resource_id, "version_id": version.id, "metadata_signature": stamp})
    return plan


def run_index_job(dispatcher, job_id, attempt):
    from .jobs import Cancelled, JobError, LeaseLost
    scoped = dispatcher._retrieval_job_view(job_id, attempt)
    if scoped is not dispatcher:
        return run_index_job(scoped, job_id, attempt)
    vector = dispatcher.vector_index
    if vector is None or dispatcher.settings.retrieval_mode != "hybrid":
        raise JobError("VECTOR_INDEX_NOT_ENABLED")
    if vector.embedding.mode == "http" and not dispatcher.settings.embedding_allow_document_transfer:
        raise JobError("EMBEDDING_DOCUMENT_TRANSFER_NOT_AUTHORIZED")
    with dispatcher.read_session_factory() as db:
        job = db.get(m.Job, job_id)
        user = dispatcher._user(db, job)
        authorize_index_job(db, user, job)
        space_id, force = job.payload["space_id"], bool(job.payload.get("force"))
        removed = []
        for vid in job.payload.get("removed_version_ids", []):
            if db.get(m.ResourceVersion,vid) is not None:
                continue
            receipts = list(db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like(f"{PREFIX}%:{vid}"))))
            if receipts and all(row.config.get("space_id") == space_id and
                    (not job.payload.get("resource_ids") or row.config.get("resource_id")
                     in job.payload["resource_ids"]) for row in receipts):
                removed.append(vid)
        plan = index_plan(db, user, space_id, job.payload.get("resource_ids", []), automatic=bool(job.payload.get("automatic")))
        old = receipts_for(db, vector.embedding.fingerprint, [item["version_id"] for item in plan])
    vector.prepare_collection()
    backend_exists = bool(vector.status().get("collection_exists"))
    counts = {"phase": "INDEXING", "total_versions": len(plan), "completed_versions": 0,
        "indexed_versions": 0, "skipped_versions": 0, "empty_versions": 0, "failed_versions": 0,
        "indexed_blocks": 0, "indexed_chunks": 0, "retired_versions":len(removed), "failures": []}
    if removed:
        vector.delete_versions(removed)
    dispatcher._checkpoint(job_id, attempt, "PLANNING_VECTOR_INDEX", counts)
    for item in plan:
        vid = item["version_id"]
        projection = svc.digest(["vector-projection-v1", vector.embedding.fingerprint, vid,
            item["metadata_signature"], job_id, attempt])
        active = False
        try:
            previous = old.get(vid, {})
            if not force and backend_exists and previous.get("metadata_signature") == item["metadata_signature"] \
                    and (previous.get("state") == "EMPTY" or previous.get("state") == "READY"
                         and vector.projection_is_complete(vid, previous.get("projection_id"), previous.get("chunk_count", 0))):
                counts["skipped_versions"] += 1
                counts["empty_versions"] += int(previous.get("state") == "EMPTY")
                counts["completed_versions"] += 1
                counts["indexed_blocks"] += int(previous.get("block_count", 0))
                counts["indexed_chunks"] += int(previous.get("chunk_count", 0))
                prune_committed_projection(dispatcher, job_id, attempt, item)
                dispatcher._checkpoint(job_id, attempt, "SKIPPING_CURRENT_VECTOR_INDEX", counts)
                continue

            def checkpoint(version_id=vid, metadata_signature=item["metadata_signature"]):
                with dispatcher.read_session_factory() as db:
                    current_job = db.get(m.Job, job_id)
                    if not current_job or current_job.cancel_requested:
                        raise Cancelled()
                    if current_job.attempts != attempt or current_job.state != "RUNNING" \
                            or current_job.lease_until is None or current_job.lease_until <= svc.now():
                        raise LeaseLost()
                    current_user = dispatcher._user(db, current_job)
                    authorize_index_job(db, current_user, current_job)
                    if CatalogAuthority(db, current_user).check(version_id) != metadata_signature:
                        raise JobError("INDEX_SOURCE_CHANGED")

            checkpoint()
            with dispatcher.read_session_factory() as db:
                user = dispatcher._user(db, db.get(m.Job, job_id))
                records, stamp, source_hash = records_for_version(db, user, space_id, vid)
            if stamp != item["metadata_signature"]:
                raise JobError("INDEX_SOURCE_CHANGED")
            dispatcher._checkpoint(job_id, attempt, "EMBEDDING", {**counts, "current_version": vid})
            prepared = vector.stage_version(records, projection, checkpoint=checkpoint,
                progress=lambda done, version_id=vid: dispatcher._checkpoint(job_id, attempt, "EMBEDDING", {
                    **counts, "current_version": version_id, "current_version_chunks": done})) if records else {"block_count": 0, "chunk_count": 0}
            with dispatcher.session_factory.begin() as db:
                job = dispatcher._fence(db, job_id, attempt)
                user = dispatcher._user(db, job)
                authorize_index_job(db, user, job)
                db.scalar(select(m.Space).where(m.Space.id == space_id).with_for_update())
                db.scalar(select(m.Resource).where(m.Resource.id == item["resource_id"]).with_for_update())
                db.scalar(select(m.ResourceVersion).where(m.ResourceVersion.id == vid).with_for_update())
                _, current_stamp, current_hash = records_for_version(db, user, space_id, vid)
                if current_stamp != stamp or current_hash != source_hash:
                    raise JobError("INDEX_SOURCE_CHANGED")
                if prepared["chunk_count"]:
                    vector.activate_version(vid, projection, prepared["chunk_count"])
                name = receipt_name(vector.embedding.fingerprint, vid)
                receipt = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == name))
                config = {"space_id": space_id, "resource_id": item["resource_id"], "version_id": vid,
                    "fingerprint": vector.embedding.fingerprint, "projection_id": projection,
                    "metadata_signature": stamp, "source_hash": source_hash,
                    "block_count": prepared["block_count"], "chunk_count": prepared["chunk_count"],
                    "state": "READY" if prepared["chunk_count"] else "EMPTY", "indexed_at": svc.primitive(svc.now())}
                if receipt:
                    svc.bump(db, receipt, config=config, updated_by=user.id)
                else:
                    db.add(m.RuntimePolicy(id=svc.uid(), name=name, config=config, updated_by=user.id))
                dispatcher._audit(db, job, "vector.version_indexed", {"version_id": vid,
                    "projection_id": projection, "blocks": prepared["block_count"], "chunks": prepared["chunk_count"]})
            active = True
            try:
                prune_committed_projection(dispatcher, job_id, attempt, item)
            except (Cancelled, LeaseLost):
                raise
            except Exception:  # noqa: BLE001 - current receipt is safe; obsolete ready projections can be retried.
                counts["cleanup_pending_versions"] = counts.get("cleanup_pending_versions", 0) + 1
                log.warning("Old vector generation cleanup pending for job=%s", job_id)
            counts["indexed_versions"] += int(bool(prepared["chunk_count"]))
            counts["empty_versions"] += int(not prepared["chunk_count"])
            counts["skipped_versions"] += int(not prepared["chunk_count"])
            counts["indexed_blocks"] += prepared["block_count"]
            counts["indexed_chunks"] += prepared["chunk_count"]
        except (Cancelled, LeaseLost):
            raise
        except (svc.APIError, JobError) as exc:
            counts["failed_versions"] += 1
            counts["failures"].append({"version_id": vid, "code": exc.code})
        except Exception as exc:
            counts["failed_versions"] += 1
            counts["completed_versions"] += 1
            counts["failures"].append({"version_id":vid,"code":getattr(exc,"code","VECTOR_INDEX_FAILED")})
            dispatcher._checkpoint(job_id,attempt,"INDEXING_FAILED",counts)
            raise
        finally:
            if not active:
                # Only this staged generation may be removed. Published/source
                # documents and other versions are never cleanup targets.
                try:
                    vector.discard_projection(vid, projection)
                except Exception:  # noqa: BLE001 - cancelled work stays hidden and cleanup can be retried.
                    log.warning("Vector staged cleanup pending for job=%s",job_id)
        counts["completed_versions"] += 1
        dispatcher._checkpoint(job_id, attempt, "INDEXING", counts)
    if counts["failed_versions"]:
        raise JobError("VECTOR_INDEX_PARTIAL_FAILURE")
    with dispatcher.session_factory.begin() as db:
        job = dispatcher._fence(db, job_id, attempt)
        user = dispatcher._user(db, job)
        authorize_index_job(db, user, job)
        dispatcher._succeed(db, job, {**counts, "phase": "COMPLETED", "fingerprint": vector.embedding.fingerprint})


def status_for(ctx, *, vector=_UNSET, settings=None, retrieval_selection=None):
    if vector is not _UNSET:
        with retrieval_registry_context(ctx) as registry:
            frozen = retrieval_selection
            if registry is not None and vector is not None and frozen is None:
                requested = ctx.query.get("profile_id")
                if requested is None:
                    default = registry.resolve()
                    requested = default.id if default.fingerprint == vector.embedding.fingerprint else None
                    if requested is None:
                        matches = [ident for ident in registry.enabled_ids()
                            if registry.resolve(ident).fingerprint == vector.embedding.fingerprint]
                        if len(matches) != 1:
                            svc.fail(409, "RETRIEVAL_RUNTIME_MISMATCH", "状态查询无法确定所选索引的绑定")
                        requested = matches[0]
                frozen = freeze_retrieval_selection(registry, {"profile_id": requested})
            if frozen is not None and (vector is None or vector.embedding.fingerprint != frozen["fingerprint"]):
                svc.fail(409, "RETRIEVAL_RUNTIME_MISMATCH", "状态查询的索引与冻结选择不一致")
            return _status_for(ctx, vector, settings or ctx.settings, frozen)
    with retrieval_registry_context(ctx) as registry:
        selection = retrieval_selection or ({"profile_id": ctx.query["profile_id"]} if ctx.query.get("profile_id") else None)
        frozen = freeze_retrieval_selection(registry, selection)
        if frozen:
            runtime = registry.resolve(frozen["profile_id"], fingerprint=frozen["fingerprint"])
            return _status_for(ctx, runtime.vector, runtime.settings, frozen)
        return _status_for(ctx, ctx.request.app.state.vector_index, settings or ctx.settings, None)


def _public_reranking(backend):
    """Expose runtime identity/load state, never paths, secrets or raw diagnostics.

    A status read does not load the model or prove it ran in any consultation.
    Missing/invalid fields remain unknown rather than becoming disabled/false.
    """
    value = backend.get("reranking")
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in ("mode", "model", "revision")
              if isinstance(value.get(key), str)}
    if type(value.get("loaded")) is bool:
        result["loaded"] = value["loaded"]
    return {"reranking": result} if result else {}


def _public_model_runtime(backend):
    raw = backend.get("model_runtime")
    if not isinstance(raw,dict):
        return {}
    fields = {"runtime_id","process_id","scope","policy","supported","state","phase","attempts",
              "started_at","completed_at","error_code","self_tested","elapsed_ms"}
    return {"model_runtime": {key:value for key,value in raw.items() if key in fields and
        (value is None or type(value) in (str,int,float,bool))}}


def _status_for(ctx, vector, settings, retrieval_selection):
    space_id = ctx.query["space_id"]
    pages = build_catalog(ctx.db, ctx.user, space_id, scope="reference")
    ready = current_receipts(ctx.db, vector, pages)
    backend = vector.status() if vector is not None else {"backend":"qdrant","mode":"disabled","available":False,"status":"DISABLED"}
    if backend.get("collection_exists") is False:
        ready = {}
    jobs = [job for job in ctx.db.scalars(select(m.Job).where(m.Job.owner_id == ctx.user.id,
        m.Job.kind == "COMPILE", m.Job.state.in_(["QUEUED", "RUNNING"])))
        if job.payload.get("task") == TASK and job.payload.get("space_id") == space_id
        and job.payload.get("retrieval_selection") == retrieval_selection]
    enabled = vector is not None and settings.retrieval_mode == "hybrid"
    from .index_observation import observe_index
    observation = observe_index(ctx.db, ctx.user, space_id, vector, pages, ready, backend, retrieval_selection)
    return {"space_id":space_id,"mode":settings.retrieval_mode,"enabled":enabled,
        "index_snapshot": observation,
        **({"retrieval_selection": retrieval_selection} if retrieval_selection is not None else {}),
        "vector":{**{key:backend[key] for key in ("backend","mode","available","status","collection","embedding_mode") if key in backend},
                  **_public_reranking(backend), **_public_model_runtime(backend)},
        "embedding":{"mode":settings.embedding_mode,"model":settings.embedding_model,
            "dimensions":settings.embedding_dimensions,"development_only":settings.embedding_mode == "hashing"},
        "coverage":{"catalog_pages":len(pages),"indexed_pages":len(ready),"dirty_pages":len(pages)-len(ready),
            "indexed_blocks":sum(row.get("block_count",0) for row in ready.values()),
            "indexed_chunks":sum(row.get("chunk_count",0) for row in ready.values())},
        "permissions":{"can_index":bool(svc.roles(ctx.db,ctx.user,space_id) & {"admin","editor"})},
        "active_job":svc.job_dict(max(jobs,key=lambda job:job.created_at)) if jobs else None,
        "notes":["覆盖统计只包含当前可读目录版本；历史版本也可独立索引，但不会冒充现行依据。",
            "索引是可重建投影，实际阅读与回答继续核验来源、权限和版本。"]}
