"""Authenticated projection management and read-only hybrid discovery API."""
import importlib.util
from pathlib import Path

from . import services as svc
from .hybrid_retrieval import search_catalog
from .retrieval_registry import RetrievalProfileError
from .vector_indexing import queue_index, status_for

UUID = {"type":"string","format":"uuid"}
SCHEMAS = {}
SELECTION = {"$ref": "#/components/schemas/RetrievalSelection"}


def response(description, status="200"):
    return {status:{"description":description,"content":{"application/json":{"schema":{"type":"object"}}}}}


def body(properties, required):
    return {"required":True,"content":{"application/json":{"schema":{"type":"object",
        "additionalProperties":False,"properties":properties,"required":required}}}}


PATHS = {
    "/retrieval/profiles":{"get":{"operationId":"getRetrievalProfiles",
        "parameters":[{"name":"space_id","in":"query","required":True,"schema":UUID}],
        "responses":response("Server-owned embedding profiles; no document bodies or generated answers")}},
    "/retrieval/status":{"get":{"operationId":"getRetrievalStatus",
        "parameters":[{"name":"space_id","in":"query","required":True,"schema":UUID},
            {"name":"profile_id","in":"query","required":False,"schema":{"type":"string","maxLength":64}}],
        "responses":response("Current authorized projection coverage")}},
    "/retrieval/index-jobs":{"post":{"operationId":"createVectorIndexJob",
        "requestBody":body({"space_id":UUID,"force":{"type":"boolean","default":False},
            "retrieval_selection":SELECTION},["space_id"]),
        "responses":response("Durable local/remote vector indexing job","202")}},
    "/retrieval/search":{"post":{"operationId":"searchHybridKnowledge",
        "requestBody":body({"space_id":UUID,"query":{"type":"string","minLength":1,"maxLength":10000},
            "scope":{"enum":["reference","formal"],"default":"reference"},
            "limit":{"type":"integer","minimum":1,"maximum":100,"default":12},
            "context":{"$ref":"#/components/schemas/Context"},
            "retrieval_selection":SELECTION},["space_id","query"]),
        "responses":response("Current-authorized candidates with verified source snippets, not answer evidence; no generator is called")}},
}


def selected_runtime(ctx, selection=None):
    registry = getattr(ctx.request.app.state, "retrieval_registry", None)
    if registry is None:
        if selection is not None:
            svc.fail(409, "RETRIEVAL_PROFILES_NOT_ENABLED", "当前部署尚未启用可切换检索方案")
        return None
    try:
        frozen = registry.freeze(selection)
        runtime = registry.resolve(frozen["profile_id"], fingerprint=frozen["fingerprint"])
    except RetrievalProfileError as exc:
        svc.fail(409, exc.code, exc.message)
    embedding = runtime.vector.embedding
    if (runtime.selection() != frozen or embedding.fingerprint != frozen["fingerprint"]
            or embedding.model != frozen["model"] or embedding.dimension != frozen["dimensions"]):
        svc.fail(409, "RETRIEVAL_RUNTIME_MISMATCH", "实际检索模型与所选方案不一致，请重载检索服务")
    return runtime


def _local_model_status(settings, role):
    """Cheap prerequisites only; full content hashes remain the loader's responsibility.

    Do not construct an encoder, import torch, count tokens, read weights or
    infer here. File metadata is checked afresh so an incomplete download or a
    removed shard cannot remain available because of a cached directory check.
    """
    from .local_encoders import MODEL_SPECS
    from .qwen_model_spec import QWEN4B_SPEC

    model = getattr(settings, f"{role}_model", None)
    spec = QWEN4B_SPEC if role == "embedding" and model == QWEN4B_SPEC["repo"] else MODEL_SPECS[role]
    if model != spec["repo"] or getattr(settings, f"{role}_revision", None) != spec["revision"]:
        return False, "LOCAL_MODEL_IDENTITY_NOT_PINNED"
    packages = ("transformers", "torch", "safetensors")
    if any(importlib.util.find_spec(package) is None for package in packages):
        return False, "DEPENDENCY_UNAVAILABLE"
    configured = getattr(settings, f"{role}_model_path", None)
    if not configured:
        return False, "LOCAL_MODEL_UNAVAILABLE"
    path = Path(configured)
    try:
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            return False, "LOCAL_MODEL_UNAVAILABLE"
        path = path.resolve()
        for name, (size, _) in spec["files"].items():
            target = path / name
            if (target.resolve() != target or not target.is_file() or target.stat().st_size != size):
                return False, "LOCAL_MODEL_FILES_INCOMPLETE"
    except (OSError, RuntimeError):
        return False, "LOCAL_MODEL_FILES_INCOMPLETE"
    return True, "LOCAL_FILES_PRESENT_NOT_VERIFIED"


def _profile_readiness(runtime, backend, *, can_edit):
    settings = runtime.settings
    embedding_status = backend.get("embedding_status", "UNKNOWN")
    if settings.embedding_mode == "transformers":
        # Unknown/new provider failures must not be overwritten by a file check.
        if embedding_status in {"EXPLICIT_LOCAL_FILES_NOT_VERIFIED", "LOCAL_FILES_NOT_VERIFIED", "READY"}:
            embedding_ready, embedding_status = _local_model_status(settings, "embedding")
        else:
            embedding_ready = False
    else:
        # Mere configuration/cache-directory presence does not verify an HTTP
        # service or an unknown local adapter. Never turn UNKNOWN into READY.
        embedding_ready = embedding_status in {"READY", "READY_DEVELOPMENT_ONLY"}
    reranker_ready, reranker_status = True, "DISABLED"
    if settings.reranker_mode == "local":
        reranker_ready, reranker_status = _local_model_status(settings, "reranker")
    model_ready = embedding_ready and reranker_ready
    backend_ready = backend.get("available") is True and not backend.get("closed", False)
    exists = backend.get("collection_exists") is True
    chunks = backend.get("ready_chunks")
    index_ready = backend_ready and exists and type(chunks) is int and chunks > 0
    index_state = ("UNAVAILABLE" if not backend_ready else "NOT_BUILT" if not exists
                   else "UNKNOWN" if type(chunks) is not int or chunks < 0
                   else "READY" if index_ready else "EMPTY")
    available = model_ready and index_ready
    return {"available": available, "state": "READY" if available else "NOT_READY",
        "model_ready": model_ready, "model_available": model_ready,
        "embedding_status": embedding_status, "reranker_status": reranker_status,
        "index_ready": index_ready, "index_available": index_ready, "index_state": index_state,
        "can_index": bool(can_edit and embedding_ready and backend_ready),
        "readiness_check": "prerequisites_only"}


def profiles(ctx):
    svc.space_access(ctx.db, ctx.user, ctx.query["space_id"])
    registry = getattr(ctx.request.app.state, "retrieval_registry", None)
    if registry is None:
        return svc.Result({"default_profile_id": None, "items": [], "enabled": False})
    can_edit = bool(svc.roles(ctx.db, ctx.user, ctx.query["space_id"]) & {"admin", "editor"})
    items = []
    for ident in registry.enabled_ids():
        runtime = selected_runtime(ctx, {"profile_id": ident})
        status = runtime.vector.status()
        items.append({"id": runtime.id, "name": runtime.name, "model": runtime.settings.embedding_model,
            "dimensions": runtime.settings.embedding_dimensions, "fingerprint": runtime.fingerprint,
            "is_default": ident == registry.default_id, **_profile_readiness(runtime, status, can_edit=can_edit),
            "bm25": True,
            "reranker_model": runtime.settings.reranker_model,
            "brand": "qwen" if runtime.settings.embedding_model.startswith("Qwen/") else "baai",
            "note": "各方案使用独立向量空间；模型状态仅检查运行前提，完整Hash与推理另行验证；"
                "索引就绪仅表示存在可用向量，当前空间覆盖请查看索引状态；选择不会调用答疑大模型"})
    return svc.Result({"default_profile_id": registry.default_id, "items": items, "enabled": True},
        headers={"Cache-Control": "private, no-store"})


def status(ctx):
    selection = {"profile_id": ctx.query["profile_id"]} if ctx.query.get("profile_id") else None
    runtime = selected_runtime(ctx, selection)
    value = status_for(ctx, vector=runtime.vector, settings=runtime.settings,
        retrieval_selection=runtime.selection()) if runtime else status_for(ctx)
    if runtime:
        value["retrieval_selection"] = runtime.selection()
    return svc.Result(value)


def create_index(ctx):
    runtime = selected_runtime(ctx, ctx.data.get("retrieval_selection"))
    vector = runtime.vector if runtime else ctx.request.app.state.vector_index
    settings = runtime.settings if runtime else ctx.settings
    if vector is None or settings.retrieval_mode != "hybrid":
        svc.fail(409,"VECTOR_INDEX_NOT_ENABLED","尚未启用语义索引，请先准备本地嵌入配置并重载服务")
    if settings.embedding_mode == "http" and not settings.embedding_allow_document_transfer:
        svc.fail(409,"EMBEDDING_DOCUMENT_TRANSFER_NOT_AUTHORIZED","尚未授权向嵌入服务传输资料")
    job = queue_index(ctx,force=ctx.data.get("force",False))
    return svc.Result(svc.job_dict(job),202)


def search(ctx):
    runtime = selected_runtime(ctx, ctx.data.get("retrieval_selection"))
    settings = runtime.settings if runtime else ctx.settings
    if settings.embedding_mode == "http" and not settings.embedding_allow_document_transfer:
        svc.fail(409,"EMBEDDING_DOCUMENT_TRANSFER_NOT_AUTHORIZED","尚未授权向嵌入服务传输查询")
    data = ctx.data
    vector = runtime.vector if runtime else ctx.request.app.state.vector_index
    factory = getattr(ctx.request.app.state, "read_session_factory", None)
    if (vector is not None and factory is not None
            and getattr(vector.settings, "retrieval_strategy", None) == "unit_rerank"
            and callable(getattr(vector, "search_many", None)) and callable(getattr(vector, "rerank_many", None))):
        from .batch_retrieval import search_catalog_batch
        from .wiki_catalog import build_catalog
        scope, context = data.get("scope", "reference"), data.get("context", {})
        pages = build_catalog(ctx.db, ctx.user, data["space_id"], context, scope=scope)
        results, batch = search_catalog_batch(ctx.user, data["space_id"], [data["query"]],
            pages=pages, vector=vector, scope=scope, context=context,
            limit=data.get("limit", 12), session_factory=factory)
        return svc.Result({**results[0], "batch_execution": batch,
            **({"retrieval_selection": runtime.selection()} if runtime else {})})
    result = search_catalog(ctx.db,ctx.user,data["space_id"],data["query"],
        vector=vector,scope=data.get("scope","reference"),
        context=data.get("context",{}),limit=data.get("limit",12))
    return svc.Result({**result, **({"retrieval_selection": runtime.selection()} if runtime else {})})


def replay_authority(ctx,cached):
    if ctx.operation != "createVectorIndexJob":
        return False
    svc.space_access(ctx.db,ctx.user,ctx.data["space_id"],"editor")
    return True


HANDLERS = {"getRetrievalStatus":status,"getRetrievalProfiles":profiles,
    "createVectorIndexJob":create_index,"searchHybridKnowledge":search}
