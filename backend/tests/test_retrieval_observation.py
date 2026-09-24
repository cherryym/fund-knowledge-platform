import copy
import json

import pytest

from fund_kb.retrieval_observation import observation_summary, observe_candidates, rank_candidates


def rows():
    return [{"unit_id": f"u{i}", "resource_id": "r", "version_id": "v", "page_id": "W1",
        "score": .2 - i * .01, "fusion_rank": i + 1, "text": "PRIVATE_BODY_DO_NOT_EXPORT",
        "channels": ["bm25", "vector"], "channel_ranks": {"bm25": i + 1, "vector": 2 - i},
        "source_spans": [{"block_id": f"b{i}", "content_sha256": "a" * 64, "start": 0, "end": 12,
                          "private_token": "PRIVATE_SECRET_DO_NOT_EXPORT"}]} for i in range(2)]


def test_ranking_is_pure_and_accepts_negative_logits():
    units = rows()
    before = copy.deepcopy(units)
    output = rank_candidates(units, [-4.0, -1.0])
    assert units == before and [u["unit_id"] for u in output] == ["u1", "u0"]


@pytest.mark.parametrize("scores", [[3], [True, 3], [3, float("nan")], [3, float("inf")], [3, "5"], None])
def test_no_partial_score_mutation_on_invalid_output(scores):
    units = rows()
    before = copy.deepcopy(units)
    with pytest.raises(ValueError):
        rank_candidates(units, scores)
    assert units == before


def test_lineage_contains_exact_identity_not_body_or_query():
    trace = observe_candidates("PRIVATE_QUERY", rank_candidates(rows(), [-4.0, -1.0]),
        {"mode": "local_cross_encoder"}, phases_ms={"reranking_ms": 2.5, "private": 123})
    assert trace["candidate_count"] == trace["reranked_count"] == 2 and trace["unscored_count"] == 0
    assert trace["units"][0]["fusion_rank"] == 2 and trace["units"][0]["rank"] == 1
    assert trace["units"][0]["source_spans"][0]["content_sha256"] == "a" * 64
    assert "PRIVATE" not in json.dumps(trace)
    assert trace["phases_ms"] == {"reranking_ms": 2.5}
    assert "units" not in observation_summary(trace)
    assert trace["all_corpus_recall"] == "NOT_EVALUATED"


def test_unscored_tail_is_visible_not_rerank_success():
    ranked = rank_candidates(rows()[:1], [0]) + rows()[1:]
    trace = observe_candidates("test", ranked, {"mode": "local_cross_encoder", "timing_scope": "shared_batch"})
    assert trace["reranked_count"] == trace["unscored_count"] == 1
    assert trace["timing_scope"] == "shared_batch"


def test_missing_timings_remain_missing_not_zero():
    trace = observe_candidates("test", [], {}, phases_ms={"reranking_ms": float("nan")})
    assert trace["phases_ms"] == {} and trace["candidate_count"] == 0
