"""Lossless multi-query scheduling and fresh authorization boundary regressions."""
from contextlib import contextmanager
import copy

import pytest
from sqlalchemy import update

from test_hybrid_semantic_candidates import env as env  # noqa: PLC0414
from test_hybrid_semantic_candidates import source, indexed, semantic_hit
from fund_kb import models as m
from fund_kb.universal_retrieval import search_many_catalog, search_many_catalog_serial


def enable(vector):
    vector.settings.retrieval_strategy = "unit_rerank"
    vector.settings.retrieval_unit_candidates = 80
    vector.settings.reranker_mode = "local"
    vector.settings.reranker_model = "synthetic-complete-pair-scorer"
    vector.settings.reranker_revision = "synthetic-v1"
    vector.calls = {"dense_batches": [], "rerank_batches": [], "receipts": 0}
    complete = vector.complete_projection_ids
    def receipts(rows):
        vector.calls["receipts"] += 1
        return complete(rows)
    def score(query, texts):
        return [float(len(text) + (50 if query in text else 0)) for text in texts]
    def search_many(queries, versions, *, limit, **kwargs):
        vector.calls["dense_batches"].append(list(queries))
        return [vector.search(q, versions, limit=limit, **kwargs) for q in queries]
    def rerank_many(requests):
        vector.calls["rerank_batches"].append(copy.deepcopy(requests))
        return [score(q, texts) for q, texts in requests]
    vector.complete_projection_ids = receipts
    vector.search_many = search_many
    vector.rerank_many = rerank_many
    vector.rerank = score


def batch(env, vector, pages, queries=None, **kwargs):
    with env.db() as db:
        return search_many_catalog(db, env.owner, env.space, queries or ["事项0", "事项1", "事项2"],
            vector=vector, pages=pages, session_factory=env.db, **kwargs)


def setup(env):
    a = source(env, title="来源甲", texts=[("paragraph", {"text": f"完整事项{i}及其全部条件与例外。"}) for i in range(9)])
    b = source(env, title="来源乙", kind="knowledge", texts=[("paragraph", {"text": "后续事项与来源的对应解释。"})])
    hits = [semantic_hit(a, indices=(i,)) for i in range(len(a))] + [semantic_hit(b, indices=(0,))]
    vector, pages = indexed(env, hits)
    enable(vector)
    return a, b, vector, pages


def test_all_directions_pairs_candidates_and_source_spans_match_serial(env):
    _, _, vector, pages = setup(env)
    queries = ["事项0", "事项1", "事项2", "后续事项", "全部条件", "事项0"]
    with env.db() as db:
        serial = search_many_catalog_serial(db, env.owner, env.space, queries,
            pages=pages, vector=vector, session_factory=env.db)
    before = copy.deepcopy(pages)
    result = batch(env, vector, pages, queries)
    assert result["units"] == serial["units"]
    assert result["hits"] == serial["hits"]
    assert result["warnings"] == serial["warnings"] == []
    assert pages == before
    assert len(vector.calls["dense_batches"]) == 1
    assert vector.calls["dense_batches"][0] == queries[:-1]
    assert len(vector.calls["rerank_batches"][0]) == 5
    assert result["batch_execution"]["query_document_pairs"] == 50
    assert result["batch_execution"]["all_query_directions_preserved"] is True
    assert result["batch_execution"]["shared_candidate_blocks_checked"] == 10
    assert all(s["timing_scope"] == "shared_batch_not_additive" for s in result["searches"])


def test_three_projection_checks_not_three_per_query(env):
    _, _, vector, pages = setup(env)
    result = batch(env, vector, pages, [f"意图{i}" for i in range(11)])
    assert len(result["searches"]) == 11
    assert vector.calls["receipts"] == 3


def test_followup_schedule_keeps_single_query_inference_but_shares_fresh_source_checks(env):
    from fund_kb.batch_retrieval import search_catalog_batch
    from fund_kb.universal_retrieval import _merge_results
    _, _, vector, pages = setup(env)
    queries = ["事项0", "事项1", "后续事项"]
    with env.db() as db:
        serial = search_many_catalog_serial(db, env.owner, env.space, queries,
            pages=pages, vector=vector, session_factory=env.db)
    vector.calls["receipts"] = 0
    rows, receipt = search_catalog_batch(env.owner, env.space, queries, pages=pages,
        vector=vector, session_factory=env.db, inference_schedule="serial_equivalent")
    actual = _merge_results(queries, rows, pages, "reference", 0)
    assert actual["units"] == serial["units"] and actual["hits"] == serial["hits"]
    assert vector.calls["receipts"] == 3
    assert vector.calls["dense_batches"] == vector.calls["rerank_batches"] == []
    assert receipt["inference_schedule"] == "serial_equivalent"


def test_unknown_inference_schedule_does_not_silently_change_computation(env):
    from fund_kb.batch_retrieval import search_catalog_batch
    _, _, vector, pages = setup(env)
    with pytest.raises(ValueError, match="INVALID_INFERENCE_SCHEDULE"):
        search_catalog_batch(env.owner, env.space, ["事项"], pages=pages, vector=vector,
            session_factory=env.db, inference_schedule="unknown")


def test_no_sql_session_held_during_vector_or_encoder_waits(env):
    _, _, vector, pages = setup(env)
    open_sessions = []
    @contextmanager
    def factory():
        with env.db() as db:
            open_sessions.append(db)
            try:
                yield db
            finally:
                open_sessions.remove(db)
    for method in ("search_many", "lexical_search", "rerank_many"):
        original = getattr(vector, method)
        def guarded(*args, _original=original, **kwargs):
            assert not open_sessions
            return _original(*args, **kwargs)
        setattr(vector, method, guarded)
    result = search_many_catalog(None, env.owner, env.space, ["前提", "后续"], pages=pages,
        vector=vector, session_factory=factory)
    assert result["units"]


@pytest.mark.parametrize("phase", ["search_many", "rerank_many"])
def test_revocation_during_model_or_vector_wait_removes_all_affected_units(env, phase):
    a, _, vector, pages = setup(env)
    original = getattr(vector, phase)
    def revoke(*args, **kwargs):
        with env.db.begin() as db:
            db.execute(update(m.Resource).where(m.Resource.id == a[0]["resource_id"]).values(suspended=True))
        return original(*args, **kwargs)
    setattr(vector, phase, revoke)
    result = batch(env, vector, pages)
    assert all(u["resource_id"] != a[0]["resource_id"] for u in result["units"])
    assert all(h["resource_id"] != a[0]["resource_id"] for h in result["hits"])


def test_current_source_hash_is_checked_once_for_union_not_trusted_from_vector(env):
    a, _, vector, pages = setup(env)
    original = vector.search_many
    def corrupt(*args, **kwargs):
        with env.db.begin() as db:
            db.execute(update(m.ContentBlock).where(m.ContentBlock.version_id == a[0]["version_id"])
                .values(search_text="CORRUPTED_UNTRUSTED_TEXT"))
        return original(*args, **kwargs)
    vector.search_many = corrupt
    result = batch(env, vector, pages)
    assert all(u["version_id"] != a[0]["version_id"] for u in result["units"])
    assert "CORRUPTED" not in repr(result)


def test_missing_projection_after_rerank_is_not_served_from_initial_ready(env):
    _, _, vector, pages = setup(env)
    original = vector.rerank_many
    def remove(*args, **kwargs):
        result = original(*args, **kwargs)
        vector.complete_projection_ids = lambda rows: set()
        return result
    vector.rerank_many = remove
    result = batch(env, vector, pages)
    assert result["units"] == result["hits"] == []


@pytest.mark.parametrize("bad", [[], [[float("nan")]], None])
def test_invalid_batch_scores_do_not_fabricate_partial_success(env, bad):
    _, _, vector, pages = setup(env)
    vector.rerank_many = lambda requests: bad
    result = batch(env, vector, pages)
    assert "RERANKER_UNAVAILABLE" in result["warnings"]
    assert result["units"] and all("rerank_score" not in u for u in result["units"])


def test_malformed_dense_batch_is_explicit_bm25_still_available(env):
    _, _, vector, pages = setup(env)
    vector.search_many = lambda *a, **k: [[]]
    result = batch(env, vector, pages)
    assert "VECTOR_CHANNEL_UNAVAILABLE" in result["warnings"]
    assert result["units"] and all(u["channels"] == ["bm25"] for u in result["units"])


def test_cancellation_between_phases_stops_without_reranking(env):
    _, _, vector, pages = setup(env)
    steps = []
    def check():
        steps.append(1)
        if len(steps) == 2:
            raise RuntimeError("MODEL_JOB_CANCELLED_OR_STALE")
    with pytest.raises(RuntimeError, match="CANCELLED"):
        batch(env, vector, pages, checkpoint=check)
    assert vector.calls["rerank_batches"] == []


def test_empty_authorized_pages_never_mean_search_everything(env):
    _, _, vector, _ = setup(env)
    result = batch(env, vector, {})
    assert result["units"] == []
    assert vector.calls["dense_batches"] == []
    assert "VECTOR_INDEX_NOT_READY" in result["warnings"]
