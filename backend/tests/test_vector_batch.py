"""Low-level batch contracts: synthetic encoders and memory-only Qdrant."""
from __future__ import annotations

import copy
import math
import socket
from collections import Counter
from types import SimpleNamespace
from uuid import uuid4

import pytest

from fund_kb.ai_transport import ProviderError
from fund_kb.ingestion import text_sha256
from fund_kb.retrieval import VectorIndex


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("NETWORK_FORBIDDEN")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def make_index(monkeypatch):
    opened = []

    def create(**changes):
        config = {"app_env": "development", "qdrant_path": ":memory:", "qdrant_url": None,
            "embedding_mode": "hashing", "embedding_model": "synthetic", "embedding_dimensions": 8,
            "embedding_chunk_bytes": 48, "embedding_title_bytes": 8, "embedding_overlap_bytes": 4,
            "embedding_batch_size": 2, "retrieval_strategy": "unit_rerank"}
        index = VectorIndex(SimpleNamespace(**(config | changes)))
        opened.append(index)

        def embed(texts, *, query=False):
            return [[float(sum(map(ord, text)) % 101 + 1), 1.0] + [0.0] * 6 for text in texts]

        monkeypatch.setattr(index.embedding, "embed", embed)
        return index

    yield create
    for index in opened:
        index.close()


def source(text="alpha synthetic source", *, version=None, ordinal=0):
    return {"resource_id": str(uuid4()), "version_id": version or str(uuid4()), "block_id": str(uuid4()),
        "title": "synthetic", "text": text, "content_sha256": text_sha256(text), "ordinal": ordinal,
        "block_type": "paragraph", "locator": {"source_page": 1}, "data": {"text": text}}


def activate(index, records, projection):
    receipt = index.stage_version(records, projection)
    index.activate_version(records[0]["version_id"], projection, receipt["chunk_count"])


def contents(index):
    points, offset = index._shared.client.scroll(index.collection, limit=1000, with_payload=True, with_vectors=True)
    assert offset is None
    return sorted((point.model_dump() for point in points), key=lambda point: str(point["id"]))


@pytest.mark.parametrize("strategy", ["unit_rerank", "version_rrf"])
def test_search_many_matches_singles_order_filters_limits_and_uses_batches(make_index, monkeypatch, strategy):
    index = make_index(retrieval_strategy=strategy)
    first, second, hidden = source("alpha"), source("beta"), source("gamma")
    alternate = source("other projection", version=first["version_id"])
    activate(index, [first], "allowed-a")
    activate(index, [second], "allowed-b")
    activate(index, [alternate], "excluded")
    activate(index, [hidden], "allowed-a")
    allowed = [second["version_id"], first["version_id"], first["version_id"]]
    projections = ["allowed-b", "allowed-a", "allowed-a"]
    queries = ["alpha", "", "gamma", "alpha", " ", "beta", "other"]
    expected = [index.search(q, allowed, 2, allowed_projection_ids=projections) for q in queries]
    before, fingerprint, generation = contents(index), index.embedding.fingerprint, index._shared.state.generation
    frozen = copy.deepcopy((queries, allowed, projections))
    embeddings, batches = [], []
    original_embed, original_batch = index.embedding.embed, index._shared.client.query_batch_points

    def embed(texts, **kwargs):
        embeddings.append(list(texts))
        assert kwargs == {"query": True}
        return original_embed(texts, **kwargs)

    def batch(**kwargs):
        batches.append(kwargs["requests"])
        for request in kwargs["requests"]:
            assert request.filter == index._read_filter(sorted(set(allowed)), sorted(set(projections)))
            assert request.limit == 2 and request.with_payload is True and request.with_vector is False
        return original_batch(**kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("BATCH_READ_MUST_NOT_WRITE_OR_USE_SINGLE_POINT_QUERY")

    monkeypatch.setattr(index.embedding, "embed", embed)
    monkeypatch.setattr(index._shared.client, "query_batch_points", batch)
    for name in ("query_points", "query_points_groups", "create_collection", "create_payload_index",
                 "upsert", "delete", "set_payload"):
        monkeypatch.setattr(index._shared.client, name, forbidden)
    actual = index.search_many(queries, allowed, 2, allowed_projection_ids=projections)
    assert actual == expected
    assert (queries, allowed, projections) == frozen
    assert len(actual) == len(queries) and actual[1] == actual[4] == []
    assert actual[0] == actual[3] and actual[0] is not actual[3]
    assert [len(items) for items in embeddings] == [2, 2, 1]
    assert [len(items) for items in batches] == [2, 2, 1]
    assert [text for batch in embeddings for text in batch] == [q for q in queries if q.strip()]
    assert all(hit["version_id"] in allowed and hit["projection_id"] in projections for row in actual for hit in row)
    assert all(hit["block_id"] not in {hidden["block_id"], alternate["block_id"]} for row in actual for hit in row)
    assert contents(index) == before
    assert index.embedding.fingerprint == fingerprint and index._shared.state.generation == generation


@pytest.mark.parametrize("chunk_strategy", ["semantic_sections_v2", "semantic_sections_v3"])
@pytest.mark.parametrize("strategy", ["version_rrf", "unit_rerank"])
def test_search_many_preserves_semantic_group_or_unit_candidates(make_index, monkeypatch, chunk_strategy, strategy):
    index = make_index(embedding_chunk_strategy=chunk_strategy, retrieval_strategy=strategy)
    allowed, projections = [], []
    for number in range(4):
        version = str(uuid4())
        rows = [source(f"第{i}条 合成条款{number} alpha beta {i}。", version=version, ordinal=i) for i in range(1, 8)]
        for row in rows:
            row["resource_id"] = rows[0]["resource_id"]
        projection = f"projection-{number}"
        activate(index, rows, projection)
        allowed.append(version)
        projections.append(projection)
    queries = ["alpha", "beta", "alpha"]
    expected = [index.search(q, allowed, 2, allowed_projection_ids=projections) for q in queries]
    calls = []
    original_groups = index._shared.client.query_points_groups
    original_batch = index._shared.client.query_batch_points

    def grouped(**kwargs):
        assert kwargs["group_by"] == "version_id" and kwargs["group_size"] == 3 and kwargs["limit"] == 2
        calls.append("group")
        return original_groups(**kwargs)

    def batch(**kwargs):
        calls.append("batch")
        return original_batch(**kwargs)

    monkeypatch.setattr(index._shared.client, "query_points_groups", grouped)
    monkeypatch.setattr(index._shared.client, "query_batch_points", batch)
    actual = index.search_many(queries, allowed, 2, allowed_projection_ids=projections)
    assert actual == expected
    if strategy == "version_rrf":
        assert calls == ["group"] * len(queries)
        for row in actual:
            counts = Counter(hit["version_id"] for hit in row)
            assert len(counts) == 2 and max(counts.values()) == 3
    else:
        assert calls == ["batch", "batch"]
        assert all(len(row) == 2 for row in actual)


@pytest.mark.parametrize("case", ["empty_queries", "empty_versions", "empty_projections", "blank", "zero", "negative"])
def test_search_many_no_work_is_zero_io_and_keeps_empty_slots(make_index, monkeypatch, case):
    index = make_index()
    queries, allowed, projections, limit = ["alpha", "", "beta"], [str(uuid4())], None, 20
    if case == "empty_queries":
        queries = []
    elif case == "empty_versions":
        allowed = []
    elif case == "empty_projections":
        projections = []
    elif case == "blank":
        queries = ["", " \n", "\t"]
    else:
        limit = 0 if case == "zero" else -1

    def forbidden(*args, **kwargs):
        raise AssertionError("EMPTY_SCOPE_MUST_NOT_PERFORM_IO")

    monkeypatch.setattr(index, "_ensure", forbidden)
    monkeypatch.setattr(index.embedding, "embed", forbidden)
    assert index.search_many(queries, allowed, limit, allowed_projection_ids=projections) == [[] for _ in queries]


def test_search_many_missing_collection_never_creates_or_embeds(make_index, monkeypatch):
    index = make_index()
    monkeypatch.setattr(index.embedding, "embed", lambda *a, **k: pytest.fail("must not embed"))
    monkeypatch.setattr(index._shared.client, "create_collection", lambda *a, **k: pytest.fail("must not create"))
    assert index.search_many(["alpha", "beta"], [str(uuid4())]) == [[], []]
    assert index._shared.client.get_collections().collections == []


@pytest.mark.parametrize("queries,allowed,projections,error", [
    ("alpha", [], None, "INVALID_SEARCH_QUERIES"), ([None], [], None, "INVALID_SEARCH_QUERIES"),
    (["alpha"], ["bad-uuid"], None, "UUID"), (["alpha"], [str(uuid4())], [""], "INVALID_PROJECTION_ID"),
    (["alpha"], [str(uuid4())], [1], "INVALID_PROJECTION_ID"),
])
def test_search_many_validates_parameters_before_io(make_index, monkeypatch, queries, allowed, projections, error):
    index = make_index()
    monkeypatch.setattr(index, "_ensure", lambda *a, **k: pytest.fail("invalid arguments must not query"))
    with pytest.raises(ValueError, match=error):
        index.search_many(queries, allowed, allowed_projection_ids=projections)


def test_search_many_closed_and_empty_scopes_match_single_behavior(make_index):
    index = make_index()
    allowed = [str(uuid4())]
    index.close()
    assert index.search_many([], allowed) == []
    assert index.search_many(["alpha", "beta"], []) == [[], []]
    for method, query in ((index.search, "alpha"), (index.search_many, ["alpha"])):
        with pytest.raises(RuntimeError, match="VECTOR_INDEX_CLOSED"):
            method(query, allowed)


def test_search_many_long_queries_pool_every_tail_independently(make_index, monkeypatch):
    index = make_index()
    index.prepare_collection()
    chunk = "中" * 16
    queries = ["short", chunk * 4 + "needle", "b" * 96 + "尾", "", "short"]
    inputs, sent = [], []

    def embed(texts, *, query=False):
        assert query and len(texts) <= 2 and all(len(text.encode()) <= 48 for text in texts)
        inputs.extend(texts)
        return [[0., 1., 0.] + [0.] * 5 if text in {"needle", "尾"} else
                [0., 0., 1.] + [0.] * 5 if text.startswith("b") else [1., 0., 0.] + [0.] * 5
                for text in texts]

    def batch(**kwargs):
        sent.extend(request.query for request in kwargs["requests"])
        return [SimpleNamespace(points=[]) for _ in kwargs["requests"]]

    monkeypatch.setattr(index.embedding, "embed", embed)
    monkeypatch.setattr(index._shared.client, "query_batch_points", batch)
    assert index.search_many(queries, [str(uuid4())]) == [[] for _ in queries]
    assert inputs == ["short", chunk, chunk, chunk, chunk, "needle", "b" * 48, "b" * 48, "尾", "short"]
    expected = [[1., 0., 0.], [192., 6., 0.], [0., 3., 96.], [1., 0., 0.]]
    assert len(sent) == len(expected)
    for vector, values in zip(sent, expected, strict=True):
        norm = math.sqrt(sum(value * value for value in values))
        assert vector == pytest.approx([value / norm for value in values] + [0.] * 5)


def test_search_many_semantic_v3_keeps_model_query_capacity(make_index, monkeypatch):
    index = make_index(embedding_chunk_strategy="semantic_sections_v3", embedding_max_tokens=96,
        embedding_model_max_tokens=128, embedding_overlap_tokens=16, embedding_context_tokens=16)
    index.prepare_collection()
    original_embed, seen = index.embedding.embed, []

    def embed(texts, **kwargs):
        seen.extend(texts)
        return original_embed(texts, **kwargs)

    monkeypatch.setattr(index.embedding, "embed", embed)
    assert index.search_many(["a" * 105, "b" * 160], [str(uuid4())]) == [[], []]
    assert seen[0] == "a" * 105  # exceeds document budget but fits full query budget
    assert "".join(seen[1:]) == "b" * 160 and all(len(text.encode()) <= 48 for text in seen[1:])


@pytest.mark.parametrize("bad", ["count", "dimension", "zero", "boolean", "string", "nan", "inf", "-inf"])
def test_search_many_validates_every_embedding_batch_before_query(make_index, monkeypatch, bad):
    index = make_index()
    index.prepare_collection()
    calls = []

    def embed(texts, **kwargs):
        calls.append(list(texts))
        vectors = [[1.] + [0.] * 7 for _ in texts]
        if len(calls) == 2:
            if bad == "count":
                return vectors[:-1]
            if bad == "dimension":
                vectors[-1] = [1.]
            elif bad == "zero":
                vectors[-1] = [0.] * 8
            else:
                vectors[-1][0] = {"boolean": True, "string": "1"}.get(bad, bad)
                if bad in {"nan", "inf", "-inf"}:
                    vectors[-1][0] = float(bad)
        return vectors

    monkeypatch.setattr(index.embedding, "embed", embed)
    monkeypatch.setattr(index._shared.client, "query_batch_points", lambda *a, **k: pytest.fail("invalid vectors must not query"))
    with pytest.raises(ProviderError, match="EMBEDDING_"):
        index.search_many(["a", "b", "c", "d"], [str(uuid4())])
    assert len(calls) == 2


@pytest.mark.parametrize("tamper,code", [
    ({"version_id": str(uuid4())}, "ACL_FILTER"), ({"projection_id": "secret"}, "PROJECTION_FILTER"),
    ({"ready": False}, "READINESS_FILTER"), ({"projection_schema": "wrong"}, "SCHEMA_MISMATCH"),
    ({"embedding_fingerprint": "wrong"}, "SCHEMA_MISMATCH"), ({"chunk_sha256": "0" * 64}, "CHUNK_PAYLOAD"),
    ({"chunk_end": 999}, "CHUNK_PAYLOAD"),
])
def test_search_many_checks_later_candidate_permissions_and_integrity(make_index, monkeypatch, tamper, code):
    index = make_index()
    row = source()
    activate(index, [row], "allowed")
    payload = contents(index)[0]["payload"]
    calls = []

    def batch(**kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(points=[SimpleNamespace(payload=payload, score=1.)]),
                SimpleNamespace(points=[SimpleNamespace(payload=payload | tamper, score=1.)])]

    monkeypatch.setattr(index._shared.client, "query_batch_points", batch)
    with pytest.raises(RuntimeError, match=code):
        index.search_many(["alpha", "beta"], [row["version_id"]], allowed_projection_ids=["allowed"])
    assert len(calls) == 1


def test_search_many_checks_semantic_spans_for_every_candidate(make_index, monkeypatch):
    index = make_index(embedding_chunk_strategy="semantic_sections_v3")
    row = source("第一条 合成条款。")
    activate(index, [row], "allowed")
    payload = contents(index)[0]["payload"]
    payload["source_spans"][0]["text_end"] += 1
    monkeypatch.setattr(index._shared.client, "query_batch_points", lambda **kw:
        [SimpleNamespace(points=[SimpleNamespace(payload=payload, score=1.)]) for _ in kw["requests"]])
    with pytest.raises(RuntimeError, match="SEMANTIC_SPANS_INVALID"):
        index.search_many(["alpha"], [row["version_id"]], allowed_projection_ids=["allowed"])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "1.0"])
def test_single_and_batch_search_reject_invalid_candidate_scores(make_index, monkeypatch, value):
    index = make_index()
    row = source()
    activate(index, [row], "allowed")
    hit = SimpleNamespace(payload=contents(index)[0]["payload"], score=value)
    monkeypatch.setattr(index._shared.client, "query_points", lambda **k: SimpleNamespace(points=[hit]))
    monkeypatch.setattr(index._shared.client, "query_batch_points", lambda **k: [SimpleNamespace(points=[hit])])
    for method, query in ((index.search, "alpha"), (index.search_many, ["alpha"])):
        with pytest.raises(RuntimeError, match="VECTOR_INVALID_SCORE"):
            method(query, [row["version_id"]], allowed_projection_ids=["allowed"])


@pytest.mark.parametrize("count", [0, 1, 3])
def test_search_many_rejects_misaligned_backend_response_count(make_index, monkeypatch, count):
    index = make_index()
    index.prepare_collection()
    monkeypatch.setattr(index._shared.client, "query_batch_points", lambda **k: [SimpleNamespace(points=[])] * count)
    with pytest.raises(RuntimeError, match="VECTOR_QUERY_BATCH_COUNT_MISMATCH"):
        index.search_many(["alpha", "beta"], [str(uuid4())])


def test_rerank_many_calls_local_batch_once_and_preserves_request_order(make_index):
    index = make_index(reranker_mode="local")
    requests = [("q", ["same", "other", "same"]), ("zz", ["same"]), ("empty", [])]
    frozen, calls = copy.deepcopy(requests), []

    def score(query, texts):
        return [float(sum(map(ord, query + text))) for text in texts]

    def score_many(items):
        calls.append(copy.deepcopy(items))
        return [score(query, texts) for query, texts in items]

    index._reranker = SimpleNamespace(score=score, score_many=score_many, close=lambda: None)
    expected = [index.rerank(query, texts) for query, texts in requests]
    assert index.rerank_many(requests) == expected
    assert expected[0][0] != expected[1][0]
    assert requests == frozen and calls == [frozen]
    assert index.rerank_many([]) == [] and calls == [frozen]


def test_rerank_many_disabled_and_closed_behavior(make_index):
    index = make_index()
    requests = [("q", ["a"]), ("empty", [])]
    assert index.rerank_many(requests) == [None, None]
    assert index._reranker is None
    index.settings.reranker_mode = "local"
    index.close()
    assert index.rerank_many([]) == []
    with pytest.raises(RuntimeError, match="VECTOR_INDEX_CLOSED"):
        index.rerank_many(requests)


@pytest.mark.parametrize("result", [None, [], [[1.]], [[1.], [2.], [3.]], [[1., 2.], [2.]],
    [[1.], None], [[1.], "2"], [[1.], [True]], [[1.], ["2"]],
    [[1.], [float("nan")]], [[1.], [float("inf")]], [[1.], [-float("inf")]]])
def test_rerank_many_rejects_all_misaligned_or_invalid_scores(make_index, result):
    index = make_index(reranker_mode="local")
    index._reranker = SimpleNamespace(score_many=lambda items: result, close=lambda: None)
    with pytest.raises(ProviderError, match="RERANKER_INVALID_RESPONSE"):
        index.rerank_many([("q", ["a"]), ("zz", ["b"])])
