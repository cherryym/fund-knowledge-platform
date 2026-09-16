"""Multi-query retrieval with shared work, never a cached authorization decision.

Each of the three metadata/receipt boundaries uses a NEW database transaction.
No SQL session spans encoder/vector waits. All queries and query-document pairs
are retained; only repeated IO, tokenization and immutable slice reads are shared.
The caller still revalidates actual sources before model dispatch and delivery.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import time

from . import hybrid_retrieval as hybrid
from . import models as m, services as svc
from .projection_read import projection_read
from .retrieval import lexical_scores_many
from .vector_indexing import current_receipts
from .wiki_catalog import build_catalog, catalog_signature


def _read_state(db, user_id, space_id, pages, scope, context, vector):
    with projection_read(db):
        user = db.get(m.User, user_id)
        svc.space_access(db, user, space_id)
        current = {p["version_id"]: p for p in build_catalog(db, user, space_id, context, scope=scope).values()}
        admitted = {pid: p for pid, p in pages.items() if p["version_id"] in current
            and p["resource_id"] == current[p["version_id"]]["resource_id"]
            and p.get("_metadata_signature") == current[p["version_id"]].get("_metadata_signature")}
        return admitted, current_receipts(db, vector, admitted)


def search_catalog_batch(user, space_id, queries, *, pages, vector, scope="reference",
                         context=None, limit=24, checkpoint=None, session_factory):
    """Return one complete single-query result per query plus shared timing data."""
    started = time.monotonic()
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        svc.fail(422, "INVALID_RETRIEVAL_LIMIT", "候选数量须介于1至100")
    if scope not in {"reference", "formal"}:
        svc.fail(422, "INVALID_RETRIEVAL_SCOPE", "检索范围无效")
    if any(not isinstance(q, str) or not q.strip() for q in queries):
        svc.fail(422, "EMPTY_RETRIEVAL_QUERY", "请输入检索问题")
    if not queries:
        return [], {"strategy": "shared_multi_query", "query_count": 0}
    context = context or {}
    user_id = user if isinstance(user, str) else user.id
    check = checkpoint or (lambda: None)
    check()
    phase = {}
    phase_start = time.monotonic()
    with session_factory() as db:
        initial_pages, ready = _read_state(db, user_id, space_id, pages, scope, context, vector)
    phase["initial_authority_ms"] = (time.monotonic() - phase_start) * 1000
    count = getattr(vector.settings, "retrieval_unit_candidates", 80)
    shared_warnings = []
    channel_rows = [{"bm25": [], "vector": []} for _ in queries]
    phase_start = time.monotonic()
    if not ready:
        shared_warnings.append("VECTOR_INDEX_NOT_READY")
    else:
        versions = sorted(ready)
        projections = [ready[vid]["projection_id"] for vid in versions]
        key = svc.digest([user_id, space_id, scope, catalog_signature(initial_pages), projections])
        semantic = True
        if vector.embedding.mode == "http" and not getattr(vector.settings, "embedding_allow_document_transfer", False):
            semantic = False
            shared_warnings.append("EMBEDDING_DOCUMENT_TRANSFER_NOT_AUTHORIZED")
        elif vector.embedding.mode == "hashing":
            semantic = False
            shared_warnings.append("DEVELOPMENT_HASHING_NOT_SEMANTIC")

        def lexical():
            # The adapter reuses this exact scoped inverted index across queries.
            # Each winning payload is still fetched and checked, not served stale.
            return [vector.lexical_search(q, versions, limit=count,
                allowed_projection_ids=projections, cache_key=key) for q in queries]

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fkb-query-batch") as pool:
            bm25 = pool.submit(lexical)
            dense = pool.submit(vector.search_many, queries, versions, limit=count,
                allowed_projection_ids=projections) if semantic else None
            for channel, future, warning in (("bm25", bm25, "LEXICAL_INDEX_UNAVAILABLE"),
                ("vector", dense, "VECTOR_CHANNEL_UNAVAILABLE")):
                if future is None:
                    continue
                try:
                    results = future.result()
                    if not isinstance(results, list) or len(results) != len(queries) or any(not isinstance(r, list) for r in results):
                        raise ValueError("QUERY_BATCH_ALIGNMENT_INVALID")
                    for row, result in zip(channel_rows, results, strict=True):
                        row[channel] = result
                except Exception:  # noqa: BLE001 - failed channel is explicit, never partial silent success.
                    shared_warnings.append(warning)
    phase["candidate_search_ms"] = (time.monotonic() - phase_start) * 1000
    check()

    phase_start = time.monotonic()
    with session_factory() as db, projection_read(db):
        current_pages, ready = _read_state(db, user_id, space_id, initial_pages, scope, context, vector)
        channel_rows = [{channel: [h for h in rows if isinstance(h, dict)
            and isinstance(h.get("version_id"), str) and h["version_id"] in ready
            and h.get("projection_id") == ready[h["version_id"]].get("projection_id")]
            for channel, rows in item.items()} for item in channel_rows]
        blocks, headings, checked_blocks = hybrid._candidate_blocks(db,
            [h for item in channel_rows for rows in item.values() for h in rows])
        ordered_pages = list(current_pages.values())
        metadata = [{"title": p["title"], "text": " ".join([p.get("category", ""), *p.get("aliases", [])])}
            for p in ordered_pages]
        prepared = []
        metadata_scores = lexical_scores_many(queries, metadata)
        for query, channels, scores in zip(queries, channel_rows, metadata_scores, strict=True):
            catalog_order = [ordered_pages[i]["version_id"] for i in sorted(range(len(scores)),
                key=lambda i: (-scores[i], ordered_pages[i]["version_id"])) if scores[i] > 0]
            warnings = list(shared_warnings)
            units, weights = hybrid._fused_unit_candidates(query, current_pages, channels,
                catalog_order, blocks, headings, warnings)
            prepared.append({"query": query, "units": units, "weights": weights, "warnings": warnings})
    phase["candidate_verification_and_fusion_ms"] = (time.monotonic() - phase_start) * 1000
    check()

    phase_start = time.monotonic()
    for item in prepared:
        item["reranking"] = {"mode": "disabled", "model": None, "input_units": min(count, len(item["units"])),
            "elapsed_ms": 0.0, "timing_scope": "shared_batch", "business_accuracy": "NOT_EVALUATED"}
    if any(item["units"] for item in prepared) and getattr(vector.settings, "reranker_mode", "disabled") == "local":
        try:
            requests = [(item["query"], [u["title"] + "\n" + " / ".join(u.get("section_path", []))
                + "\n" + u["text"] for u in item["units"][:count]]) for item in prepared]
            scores = vector.rerank_many(requests)
            if len(scores) != len(prepared):
                raise ValueError("RERANK_BATCH_ALIGNMENT_INVALID")
            # Validate the whole batch before mutating any unit/rank.
            if any(values is None or len(values) != len(request[1]) or
                   any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in values)
                   for values, request in zip(scores, requests, strict=True)):
                raise ValueError("RERANK_BATCH_SCORES_INVALID")
            for item, values in zip(prepared, scores, strict=True):
                batch, tail = item["units"][:count], item["units"][count:]
                for unit, score in zip(batch, values, strict=True):
                    unit["rerank_score"] = score
                batch.sort(key=lambda u: (-u["rerank_score"], -u["score"], u["unit_id"]))
                item["units"] = batch + tail
                item["reranking"].update(mode="local_cross_encoder", model=vector.settings.reranker_model,
                    revision=vector.settings.reranker_revision)
        except Exception:  # noqa: BLE001 - no invented fallback score or hidden loss of a query.
            for item in prepared:
                item["warnings"].append("RERANKER_UNAVAILABLE")
                item["reranking"]["mode"] = "unavailable"
    phase["reranking_ms"] = (time.monotonic() - phase_start) * 1000
    check()

    phase_start = time.monotonic()
    with session_factory() as db:
        final_pages, final_ready = _read_state(db, user_id, space_id, current_pages, scope, context, vector)
    admitted = {p["version_id"] for p in final_pages.values()
        if p["version_id"] in final_ready and p["version_id"] in ready
        and final_ready[p["version_id"]].get("projection_id") == ready[p["version_id"]].get("projection_id")}
    if any(vid in final_ready and final_ready[vid].get("projection_id") != stamp.get("projection_id")
            for vid, stamp in ready.items()):
        for item in prepared:
            item["warnings"].append("VECTOR_PROJECTION_CHANGED_DURING_READ")
    phase["final_authority_ms"] = (time.monotonic() - phase_start) * 1000
    check()
    elapsed = (time.monotonic() - started) * 1000
    results = []
    for item in prepared:
        item["reranking"]["elapsed_ms"] = round(phase["reranking_ms"], 3)
        result = hybrid._format_unit_results(item["query"], scope, current_pages, final_ready,
            [u for u in item["units"] if u["version_id"] in admitted], item["weights"], item["reranking"],
            checked_blocks, item["warnings"], elapsed, limit)
        result["timing_scope"] = "shared_batch_not_additive"
        results.append(result)
    return results, {"strategy": "shared_multi_query", "query_count": len(queries),
        "all_query_directions_preserved": True, "authority_rounds": 3,
        "shared_candidate_blocks_checked": checked_blocks,
        "query_document_pairs": sum(min(count, len(item["units"])) for item in prepared),
        "timing_ms": round(elapsed, 3), "phases_ms": {k: round(v, 3) for k, v in phase.items()},
        "generation_model_calls": 0, "source_bodies_written": False}
