"""Unit-grain discovery with synthetic local scores; no real provider calls."""
import copy

import pytest
from sqlalchemy import update
from test_hybrid_semantic_candidates import discover, indexed, semantic_hit, source
from test_hybrid_semantic_candidates import env as env  # noqa: PLC0414

from fund_kb import hybrid_retrieval as hybrid
from fund_kb import models as m
from fund_kb.unit_fusion import fuse_units, query_weights


def enable(vector, scorer):
    vector.settings.retrieval_strategy = "unit_rerank"
    vector.settings.retrieval_unit_candidates = 80
    vector.settings.reranker_mode = "local"
    vector.settings.reranker_model = "synthetic-cross-encoder"
    vector.settings.reranker_revision = "synthetic-r1"
    vector.rerank = scorer


def test_distinct_sections_same_document_are_all_kept_and_reranked(env):
    rows = source(env, texts=[("paragraph", {"text": f"完整处理事项{i}。"}) for i in range(7)])
    hits = [semantic_hit(rows, indices=(i,)) for i in range(7)]
    vector, pages = indexed(env, hits)
    seen = []
    def score(query, texts):
        seen.extend(texts)
        return [100.0 if "事项6" in t else 0 for t in texts]
    enable(vector, score)
    result = discover(env, vector, pages)
    assert len(result["units"]) == len(seen) == 7
    assert "事项6" in result["units"][0]["text"]
    assert len(result["hits"][0]["candidate_snippets"]) == 7
    assert len(result["hits"][0]["matched_block_ids"]) == 7
    assert all(u["is_answer_evidence"] is False for u in result["units"])
    assert result["reranking"]["mode"] == "local_cross_encoder"
    assert vector.limits == [80, 80]


def test_after_reranker_wait_changed_metadata_filters_all_units(env):
    rows = source(env)
    vector, pages = indexed(env, [semantic_hit(rows)])
    with env.db() as db:
        def score(query, texts):
            # Same-connection synthetic state change. The worker's separate
            # read-transaction revocation guard is covered by job security tests.
            db.execute(update(m.Resource).where(m.Resource.id == rows[0]["resource_id"]).values(suspended=True))
            return [1.0] * len(texts)
        enable(vector, score)
        result = hybrid.search_catalog(db, env.owner, env.space, "selectorneedle", vector=vector, pages=pages)
    assert result["reranking"]["mode"] == "local_cross_encoder"
    assert result["units"] == [] and result["hits"] == [], result["reranking"]


def test_reranker_failure_is_explicit_without_fabricating_semantic_scores(env):
    rows = source(env)
    vector, pages = indexed(env, [semantic_hit(rows)])
    def fail(*args):
        raise RuntimeError("fake_secret_should_not_be_in_diagnostic")
    enable(vector, fail)
    result = discover(env, vector, pages)
    assert result["units"] and "rerank_score" not in result["units"][0]
    assert result["reranking"]["mode"] == "unavailable"
    assert "RERANKER_UNAVAILABLE" in result["warnings"]
    assert "fake_secret" not in repr(result)


def test_fusion_deduplicates_identical_spans_but_not_different_parts():
    rows = [{"text": f"第{i}个事项", "resource_id": "r", "version_id": "v",
             "source_spans": [{"block_id": f"b{i}"}], "block_ids": [f"b{i}"]} for i in range(6)]
    before = copy.deepcopy(rows)
    result, weights = fuse_units("如何处理", {"vector": rows + [rows[0]], "bm25": rows[::-1]}, [])
    assert len(result) == 6 and all(set(u["channels"]) == {"vector", "bm25"} for u in result)
    assert weights["profile"] == "multi_aspect" and rows == before


@pytest.mark.parametrize("query,profile", [("第十二条原文", "exact_reference"),
    ("完整业务流程", "multi_aspect"), ("某项不熟悉的概念", "semantic")])
def test_weights_describe_query_language_not_named_financial_answers(query, profile):
    value = query_weights(query)
    assert value["profile"] == profile
    assert value["vector"] + value["bm25"] + value["catalog"] == pytest.approx(1)
