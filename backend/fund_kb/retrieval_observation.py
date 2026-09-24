"""Body-free candidate lineage and strict, atomic rerank application.

All inputs must already be admitted by the source reader. This module cannot
grant access or certify relevance, legal effect, or claim support. The returned
denominator is the actual finite candidate pool, never the complete corpus.
"""
from __future__ import annotations

import math
from hashlib import sha256


def rerank_pool_size(settings, units):
    policy = getattr(settings, "retrieval_rerank_policy", "ranked_prefix")
    if policy == "complete_pool":
        return len(units)
    if policy == "ranked_prefix":
        return min(getattr(settings, "retrieval_unit_candidates", 80), len(units))
    raise ValueError("INVALID_RERANK_POLICY")


def validated_scores(scores, count):
    if (not isinstance(scores, (list, tuple)) or len(scores) != count
            or any(type(s) not in (int, float) or not math.isfinite(s) for s in scores)):
        raise ValueError("RERANK_SCORES_INVALID")
    return scores


def rank_candidates(units, scores):
    """Validate the complete response before copying/applying any score."""
    validated_scores(scores, len(units))
    ranked = [{**unit, "rerank_score": score} for unit, score in zip(units, scores, strict=True)]
    return sorted(ranked, key=lambda u: (-u["rerank_score"], -u["score"], u["unit_id"]))


def observe_candidates(query, ordered, reranking, *, phases_ms=None):
    rows, channels = [], {}
    for rank, unit in enumerate(ordered, 1):
        scored = type(unit.get("rerank_score")) in (int, float) and math.isfinite(unit["rerank_score"])
        channel_ranks = {key: value for key, value in unit.get("channel_ranks", {}).items()
                         if key in {"bm25", "vector", "catalog"} and type(value) is int and value > 0}
        for channel in set(unit.get("channels", [])) & {"bm25", "vector", "catalog"}:
            channels[channel] = channels.get(channel, 0) + 1
        row = {key: unit[key] for key in ("unit_id", "resource_id", "version_id", "page_id")}
        row.update(rank=rank, fusion_rank=unit.get("fusion_rank"), channel_ranks=channel_ranks,
                   reranked=scored, source_kind=unit.get("source_kind", unit.get("kind")),
                   source_spans=[{key: span[key] for key in
                       ("block_id", "content_sha256", "start", "end") if key in span}
                       for span in unit.get("source_spans", [])])
        rows.append(row)
    scored = sum(row["reranked"] for row in rows)
    return {"version": "candidate_lineage_v1", "query_sha256": sha256(query.encode()).hexdigest(),
        "scope": "current_authorized_candidate_pool", "candidate_count": len(rows),
        "reranked_count": scored, "unscored_count": len(rows) - scored,
        "channel_counts": channels, "reranking_state": reranking.get("mode", "unknown"),
        "candidate_policy": reranking.get("candidate_policy", "unknown"),
        "phases_ms": {key: round(value, 3) for key, value in (phases_ms or {}).items()
                      if key in {"initial_authority_ms", "candidate_search_ms", "candidate_verification_and_fusion_ms",
                                 "reranking_ms", "final_authority_ms"}
                      and type(value) in (int, float) and math.isfinite(value) and value >= 0},
        "timing_scope": reranking.get("timing_scope", "single_query"),
        "units": rows, "source_text_included": False, "all_corpus_recall": "NOT_EVALUATED",
        "semantic_support": "NOT_EVALUATED"}


def observation_summary(trace):
    """Cheap progress/history projection; full spans stay in explicit search results."""
    return {key: trace[key] for key in ("version", "query_sha256", "scope", "candidate_count",
        "reranked_count", "unscored_count", "channel_counts", "reranking_state", "phases_ms",
        "timing_scope", "candidate_policy", "source_text_included", "all_corpus_recall", "semantic_support") if key in trace}
