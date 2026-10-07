"""Merge model-planned searches at source-unit grain, with no model invocation."""
from __future__ import annotations

import time

from . import hybrid_retrieval
from .retrieval_observation import observation_summary


def search_many_catalog(db, user, space_id, queries, *, pages, vector=None, scope="reference",
                        context=None, limit=24, checkpoint=None, session_factory=None, prefetched=None):
    """`prefetched` maps a query to the (result, receipt) the same batch search already returned for it (the
    question, searched while the planner was answering); only the other queries are searched here."""
    normalized = list(dict.fromkeys(q.strip() for q in queries if isinstance(q, str) and q.strip()))
    if (normalized and vector is not None
            and getattr(vector.settings, "retrieval_strategy", None) == "unit_rerank"
            and session_factory is not None and callable(getattr(vector, "search_many", None))
            and callable(getattr(vector, "rerank_many", None))):
        from .batch_retrieval import search_catalog_batch
        started = time.monotonic()
        early = {q: value for q, value in (prefetched or {}).items() if q in normalized}
        rest = [q for q in normalized if q not in early]
        by_query, batch = {q: result for q, (result, _) in early.items()}, None
        if rest:
            results, batch = search_catalog_batch(user, space_id, rest, pages=pages, vector=vector,
                scope=scope, context=context, limit=limit, checkpoint=checkpoint, session_factory=session_factory)
            by_query.update(zip(rest, results, strict=True))
        batch = _with_prefetched(batch, [receipt for _, receipt in early.values()])
        results = [by_query[q] for q in normalized]
        merged = _merge_results(normalized, results, pages, scope, started)
        merged["candidate_preview_stats"]["source_blocks_checked"] = batch.get("shared_candidate_blocks_checked", 0)
        return {**merged, "batch_execution": batch}
    return search_many_catalog_serial(db, user, space_id, queries, pages=pages, vector=vector,
        scope=scope, context=context, limit=limit, checkpoint=checkpoint, session_factory=session_factory)


def _with_prefetched(batch, early):
    """One receipt for the planned searches: counts include the prefetched queries, whose time overlapped the
    planning model call and is reported apart, never added to this search's wall time."""
    if not early:
        return batch
    keys = ("query_count", "query_document_pairs", "shared_candidate_blocks_checked")
    prefetched = {key: sum(receipt.get(key, 0) for receipt in early) for key in keys}
    prefetched.update(timing_ms=round(sum(receipt.get("timing_ms", 0.0) for receipt in early), 3),
                      overlapped_with="model_planning")
    combined = dict(batch) if batch else {**early[0], **{key: 0 for key in keys}, "timing_ms": 0.0, "phases_ms": {}}
    for key in keys:
        combined[key] = combined.get(key, 0) + prefetched[key]
    combined["prefetched"] = prefetched
    return combined


def search_many_catalog_serial(db, user, space_id, queries, *, pages, vector=None, scope="reference",
                               context=None, limit=24, checkpoint=None, session_factory=None):
    """Compatibility path and fixed-computation reference for performance QA."""
    started = time.monotonic()
    results = []
    for query in dict.fromkeys(q.strip() for q in queries if isinstance(q, str) and q.strip()):
        if checkpoint:
            checkpoint()
        if session_factory is not None:
            # Do not hold one SQL read snapshot through several model computations
            # or open nested transactions for job cancellation/authority checks.
            with session_factory() as query_db:
                result = hybrid_retrieval.search_catalog(query_db, user, space_id, query, pages=pages, vector=vector,
                    scope=scope, context=context, limit=limit)
        else:
            result = hybrid_retrieval.search_catalog(db, user, space_id, query, pages=pages, vector=vector,
                scope=scope, context=context, limit=limit)
        results.append(result)
    return _merge_results(queries, results, pages, scope, started)


def _merge_results(queries, results, pages, scope, started):
    units, hits = [], {}
    for result in results:
        query = result["query"]
        for rank, unit in enumerate(result.get("units", []), 1):
            # Preserve each query's rank; taking a minimum then applying it to
            # every query would invent coverage. The pure router merges exact
            # duplicates while retaining these per-query priorities.
            units.append({**unit, "query_rank": rank, "matched_queries": [query]})
        for hit in result["hits"]:
            previous = hits.get(hit["page_id"])
            if previous is None:
                hits[hit["page_id"]] = {**hit, "candidate_snippets": list(hit.get("candidate_snippets", []))}
                continue
            previous["score"] = max(previous["score"], hit["score"])
            previous["matched_block_ids"] = list(dict.fromkeys([
                *previous.get("matched_block_ids", []), *hit.get("matched_block_ids", [])]))
            previous["channels"] = list(dict.fromkeys([*previous["channels"], *hit["channels"]]))
            known = {s.get("unit_id", repr(s)) for s in previous["candidate_snippets"]}
            previous["candidate_snippets"].extend(s for s in hit.get("candidate_snippets", [])
                if s.get("unit_id", repr(s)) not in known)
    warnings = list(dict.fromkeys(w for result in results for w in result["warnings"]))
    return {"query": queries[0] if queries else "", "scope": scope, "mode": "universal_unit_retrieval",
        "hits": sorted(hits.values(), key=lambda h: (-h["score"], h["page_id"])),
        "units": units,
        "catalog_pages": len(pages), "indexed_catalog_pages": min((r["indexed_catalog_pages"] for r in results), default=0),
        "total_candidates": len(hits), "returned": len(hits), "warnings": warnings,
        "timing_ms": round((time.monotonic() - started) * 1000, 3), "evidence_preview": False,
        "candidate_preview_stats": {"verified_snippets": len({u["unit_id"] for u in units}),
            "source_blocks_checked": sum(r.get("candidate_preview_stats", {}).get("source_blocks_checked", 0) for r in results)},
        "retrieval_observations": [observation_summary(r["retrieval_trace"]) for r in results if "retrieval_trace" in r],
        "searches": [{key: r[key] for key in ("query", "mode", "timing_ms", "timing_scope", "warnings", "returned", "reranking", "fusion_weights") if key in r}
                     for r in results]}
