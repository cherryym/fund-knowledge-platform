"""Execution-only optimizations: independent math, exact IDs, bounded call scope."""
from __future__ import annotations

import copy
import hashlib
import math
import socket
import unicodedata
from collections import Counter

import pytest
from test_local_encoders import _expected_rerank_pairs
from test_local_encoders import fake_models as _fake_models
from test_qwen_embedding import fake_qwen as _fake_qwen

from fund_kb import local_encoders as enc
from fund_kb import retrieval

fake_models, fake_qwen = _fake_models, _fake_qwen


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import huggingface_hub.utils._auth as auth
    import huggingface_hub.utils._headers as headers

    def forbidden(*args, **kwargs):
        raise AssertionError("NETWORK_OR_HOST_CREDENTIALS_FORBIDDEN")

    for target, name in ((socket.socket, "connect"), (socket, "create_connection"),
                         (auth, "get_token"), (headers, "get_token")):
        monkeypatch.setattr(target, name, forbidden)


def original_lexical(query, candidates):
    # Independent fixed formula, retaining original arithmetic and term order.
    terms = set(retrieval.tokenize(query))
    if not terms:
        return [0.0] * len(candidates)
    docs = [Counter(retrieval.tokenize(str(c.get("title", "")) + "\n" + str(c.get("text", ""))))
            for c in candidates]
    n = len(docs)
    mean_len = sum(sum(d.values()) for d in docs) / max(1, n) or 1.0
    df = {term: sum(term in d for d in docs) for term in terms}
    result = []
    for candidate, doc in zip(candidates, docs, strict=True):
        score = 0.0
        for term in terms:
            tf = doc[term]
            if tf:
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * sum(doc.values()) / mean_len))
        normal = unicodedata.normalize("NFKC", query).strip().lower()
        if normal and normal in str(candidate.get("text", "")).lower():
            score += 3.0
        if normal and normal in str(candidate.get("title", "")).lower():
            score += 5.0
        result.append(score)
    return result


@pytest.mark.parametrize("size", [0, 1, 81, 1003])
def test_lexical_all_queries_candidates_and_exact_scores(size, monkeypatch):
    queries = ["股票 停牌", "国债期货交割", "费用 赎回", "CAS 22 1.2/3", "资产ＡＢＣ", "", "的", "股票 停牌"]
    candidates = [{"title": f"规则{i % 7} 费用", "text": f"股票 停牌 国债期货交割 CAS 22 资产abc {i}"}
                  for i in range(size)]
    if candidates:
        candidates[-1] = {"title": None, "text": 123}
    expected = [original_lexical(q, candidates) for q in queries]
    frozen = copy.deepcopy(candidates)
    calls, tokenize = [], retrieval.tokenize

    def counted(text):
        calls.append(text)
        return tokenize(text)

    monkeypatch.setattr(retrieval, "tokenize", counted)
    actual = retrieval.lexical_scores_many(queries, candidates)
    assert actual == expected
    assert candidates == frozen
    assert len(calls) == len(queries) + len(candidates)
    assert len(actual) == len(queries) and all(len(row) == size for row in actual)
    assert retrieval.lexical_scores_many([], candidates) == []
    assert retrieval.lexical_scores_many(["", "的"], candidates) == [[0.] * size, [0.] * size]


def test_lexical_corpus_changes_are_not_cached_across_calls():
    candidates = [{"title": "", "text": "股票"}]
    first = retrieval.lexical_scores_many(["股票"], candidates)
    candidates[0]["text"] = "期货"
    assert first != retrieval.lexical_scores_many(["股票"], candidates)


@pytest.mark.parametrize("budget", [1, 262144])
def test_reranker_reuses_token_rows_but_executes_every_pair_and_tail(fake_models, budget):
    model = enc.LocalReranker(fake_models.settings)
    model._memo_token_budget = budget
    requests = [("q", ["abc", "x" * 199 + "★", "", "abc"]),
                ("zz", ["abc", "d" * 23 + "★"]), ("q", ["abc"])]
    pairs, scores, windows = _expected_rerank_pairs(requests, 12)
    actual = model.score_many(requests)
    diagnostic = copy.deepcopy(model.last_diagnostics)
    for row, expected in zip(actual, scores, strict=True):
        assert row == pytest.approx(expected)
    observed = [tuple(row[mask.bool()].tolist()) for ids, masks in fake_models.models[0].calls
                for row, mask in zip(ids, masks, strict=True)]
    assert observed == sorted(pairs, key=len)  # Includes every repeated pair.
    assert diagnostic["pair_count"] == 7 and diagnostic["window_count"] == len(pairs)
    assert [[[(w["start_token"], w["end_token"], w["input_tokens"]) for w in row]
             for row in request["windows"]] for request in diagnostic["requests"]] == windows
    assert model._token_memo is None
    business = [(texts, opts) for texts, opts in fake_models.tokenizers[0].calls
                if not opts.get("text_pair") and texts != ["A", "B"]]
    assert business[0][0] == ["q", "zz"]
    assert business[1][0].count("abc") == 1
    before = len(fake_models.tokenizers[0].calls)
    assert model.score_many(requests) == actual
    assert len(fake_models.tokenizers[0].calls) == before + 2


def test_nested_cache_is_exact_and_cleared_on_failure_and_close(fake_models):
    model = enc.LocalEmbedding(fake_models.settings)
    model.token_count("initialize")
    tokenizer = fake_models.tokenizers[0]
    tokenizer.calls.clear()
    with pytest.raises(RuntimeError):  # noqa: SIM117 - exercise nested scope lifetimes explicitly.
        with model.tokenization_scope():
            with model.tokenization_scope():
                assert model.token_count("Ａ") == model.token_count("Ａ")
                assert model.token_count("A") == 3
                ids = model._encode(["A"], special_tokens=True)
                ids[0][1] = -1
                assert model._encode(["A"], special_tokens=True)[0][1] == ord("A") + 10
                # Special-token modes cannot alias one another.
                assert model._encode(["A"], special_tokens=False) == [[ord("A") + 10]]
            assert len(tokenizer.calls) == 3
            model.close()
            assert model._token_memo["rows"] == {}
            raise RuntimeError("synthetic")
    assert model._token_memo is None


def test_qwen_full_query_preflight_and_all_slices_share_only_this_call(fake_qwen):
    index = object.__new__(retrieval.VectorIndex)
    index.settings = fake_qwen.settings
    index.embedding = retrieval.EmbeddingProvider(fake_qwen.settings)
    query = "a" * 101
    result = index._embed_queries([query, "short"])
    prefix = fake_qwen.settings.embedding_query_instruction
    calls = Counter(text for batch in fake_qwen.tokenizers[0].calls for text in batch)
    assert calls == Counter({prefix + query: 1, prefix + "short": 1,
                             prefix + "a" * 48: 1, prefix + "a" * 5: 1})
    observed = [row[mask.bool()].tolist() for ids, masks in fake_qwen.models[0].calls
                for row, mask in zip(ids, masks, strict=True)]
    assert len(observed) == 4  # Both identical 48-character slices still execute.
    assert observed[0] == observed[1]
    assert len(result) == 2 and all(len(v) == 2560 for v in result)
    assert index.embedding._model._token_memo is None
    assert index._embed_queries([query, "short"]) == result
    assert Counter(text for batch in fake_qwen.tokenizers[0].calls for text in batch) == calls + calls


def test_qwen_invalid_input_does_not_leave_tokens_or_partially_run_model(fake_qwen):
    provider = retrieval.EmbeddingProvider(fake_qwen.settings)
    with pytest.raises(retrieval.ProviderError, match="EMBEDDING_INPUT_TOO_LONG"):
        provider.embed(["short", "x" * 129], query=True)
    assert not fake_qwen.models
    assert provider._model._token_memo is None
    assert len(provider.embed(["short"], query=True)) == 1
    assert provider._model._token_memo is None


def test_provider_scope_is_lazy_for_fully_injected_backend(fake_qwen, monkeypatch):
    provider = retrieval.EmbeddingProvider(fake_qwen.settings)
    index = object.__new__(retrieval.VectorIndex)
    index.embedding, index.settings = provider, fake_qwen.settings

    def forbidden():
        pytest.fail("NO_REAL_ADAPTER_FOR_INJECTED_BACKEND")

    monkeypatch.setattr(provider, "_local_transformer", forbidden)
    monkeypatch.setattr(provider, "query_token_count", lambda text: len(text))
    monkeypatch.setattr(provider, "embed", lambda texts, query=False: [[1., *([0.] * 2559)] for _ in texts])
    with provider.tokenization_scope(), provider.tokenization_scope():
        assert provider._model is None
    assert len(index._embed_queries(["synthetic", "full query"])) == 2
    assert provider._model is None and provider._token_scope is None
    assert not fake_qwen.loaders and not fake_qwen.models


@pytest.mark.parametrize("identity", ["sha256", "git"])
def test_file_validation_keeps_complete_pin_and_independent_sha256(tmp_path, monkeypatch, identity):
    data = b"full pinned synthetic bytes\x00" * 4096
    path = tmp_path / "weights.bin"
    path.write_bytes(data)
    git_sha = hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()
    sha = hashlib.sha256(data).hexdigest()
    expected = f"{identity}:" + (git_sha if identity == "git" else sha)
    original, calls = hashlib.sha1, []

    def track(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(hashlib, "sha1", track)
    assert enc.verify_model_file(tmp_path, "weights.bin", (len(data), expected)) == {
        "file": "weights.bin", "bytes": len(data), "sha256": sha, "repository_identity": expected}
    assert len(calls) == (1 if identity == "git" else 0)
    path.write_bytes(data[:-1] + b"X")
    with pytest.raises(enc.LocalEncoderError, match="LOCAL_MODEL_HASH_MISMATCH"):
        enc.verify_model_file(tmp_path, "weights.bin", (len(data), expected))
