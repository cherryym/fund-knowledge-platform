"""Qwen re-ranking: synthetic token/model interfaces, no network or model downloads."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from fund_kb.local_encoders import LocalEncoderError, create_reranker
from fund_kb.qwen_reranker import PREFIX, SUFFIX, QwenReranker
from fund_kb.qwen_reranker_spec import QWEN_RERANKER_SPEC


class Tokenizer:
    pad_token_id = 0
    padding_side = "left"

    def __call__(self, texts, **kwargs):
        assert kwargs["truncation"] is False and kwargs["add_special_tokens"] is False
        if isinstance(texts, str):
            return {"input_ids": list(map(ord, texts)), "offset_mapping": [(i, i + 1) for i in range(len(texts))]}
        return {"input_ids": [list(map(ord, text)) for text in texts]}


@pytest.fixture
def candidate(tmp_path):
    return SimpleNamespace(reranker_model=QWEN_RERANKER_SPEC["repo"], reranker_revision=QWEN_RERANKER_SPEC["revision"],
        reranker_model_path=tmp_path, reranker_device="cpu", reranker_dtype="float32",
        reranker_max_tokens=512, reranker_batch_size=2, reranker_instruction="Find relevant passages.")


@pytest.fixture
def fake(candidate, monkeypatch):
    model = QwenReranker(candidate)
    monkeypatch.setattr(model, "_load_tokenizer", lambda: Tokenizer())
    seen = []
    def forward(rows):
        seen.extend(rows)
        return [float(sum(row) % 1000) for row in rows]
    monkeypatch.setattr(model, "_forward", forward)
    return model, seen


def test_factory_selects_causal_reranker_without_loading(candidate):
    model = create_reranker(candidate)
    assert isinstance(model, QwenReranker) and model._model is None and model._tokenizer is None


def test_profile_readiness_accepts_qwen_reranker_without_loading(candidate, monkeypatch):
    from fund_kb import qwen_reranker_spec
    from fund_kb.api_retrieval import _local_model_status
    small = {**QWEN_RERANKER_SPEC, "files": {"synthetic-model": (3, "unused-in-stat-only-check")}}
    monkeypatch.setattr(qwen_reranker_spec, "QWEN_RERANKER_SPEC", small)
    (candidate.reranker_model_path / "synthetic-model").write_bytes(b"abc")
    assert _local_model_status(candidate, "reranker") == (True, "LOCAL_FILES_PRESENT_NOT_VERIFIED")
    (candidate.reranker_model_path / "synthetic-model").write_bytes(b"a")
    assert _local_model_status(candidate, "reranker") == (False, "LOCAL_MODEL_FILES_INCOMPLETE")


def test_exact_official_pair_framing(fake):
    model, _ = fake
    frames = model._frames("查询", "文档全文")
    expected = PREFIX + "<Instruct>: Find relevant passages.\n<Query>: 查询\n<Document>: 文档全文" + SUFFIX
    assert len(frames) == 1
    assert frames[0][0] == list(map(ord, expected))


def test_long_text_complete_coverage_tail_and_query_repetition(fake):
    model, seen = fake
    text = "首段条件。" + "连续原文与例外。" * 200 + "FINAL_TAIL"
    scores = model.score("完整查询", [text, "短段落"])
    windows = model.last_diagnostics["requests"][0]["windows"][0]
    covered = set()
    for window in windows:
        covered.update(range(window["start_char"], window["end_char"]))
    assert covered == set(range(len(text)))
    assert all(len(row) <= 512 for row in seen)
    assert any("FINAL_TAIL" in "".join(map(chr, row)) for row in seen)
    assert all("<Query>: 完整查询" in "".join(map(chr, row)) for row in seen)
    assert len(scores) == 2 and model.last_diagnostics["truncated"] is False


def test_batch_alignment_and_window_max_equal_serial(fake):
    model, _ = fake
    requests = [("问题甲", ["段落" * 200, "相关短句"]), ("问题乙", ["尾部" * 140]), ("问题丙", [])]
    batch = model.score_many(requests)
    serial = [model.score(query, texts) for query, texts in requests]
    assert batch == serial


def test_identical_frames_reused_but_distinct_questions_never_collapsed(fake):
    model, seen = fake
    text = "完整条件和例外。" * 120 + "TAIL"
    result = model.score_many([("问甲", [text, text]), ("问甲", [text]), ("问乙", [text])])
    diag = model.last_diagnostics
    assert result[0][0] == result[0][1] == result[1][0]
    assert diag["window_count"] > diag["computed_window_count"] == len(seen)
    assert diag["reused_window_count"] == diag["window_count"] - len(seen)
    assert diag["truncated"] is False and diag["pair_count"] == 4
    assert any("<Query>: 问乙" in "".join(map(chr, row)) for row in seen)
    assert len({tuple(row) for row in seen}) == len(seen)


def test_oversized_query_fails_before_any_inference(fake):
    model, seen = fake
    with pytest.raises(LocalEncoderError, match="RERANK_QUERY_TOO_LONG"):
        model.score_many([("短查询", ["文档"]), ("query" * 400, ["文档"])])
    assert not seen


@pytest.mark.parametrize("requests", [[], [("question", [])]])
def test_empty_input_does_not_load_weights(candidate, requests):
    model = QwenReranker(candidate)
    assert model.score_many(requests) == ([] if not requests else [[]])
    assert model._model is None and model._tokenizer is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
def test_nonfinite_or_invalid_scores_fail(fake, monkeypatch, bad):
    model, _ = fake
    monkeypatch.setattr(model, "_forward", lambda rows: [bad] * len(rows))
    with pytest.raises(LocalEncoderError, match="ALIGNMENT"):
        model.score("query", ["text"])


def test_changing_only_reranker_does_not_change_vector_fingerprint(candidate):
    from fund_kb.retrieval import EmbeddingProvider
    from fund_kb.settings import Settings
    before = Settings(app_env="test")
    after = before.model_copy(update=vars(candidate))
    assert EmbeddingProvider(before).fingerprint == EmbeddingProvider(after).fingerprint


@pytest.mark.parametrize("field,value", [("reranker_model", "Qwen/Qwen3-Embedding-4B"),
    ("reranker_revision", "main"), ("reranker_dtype", "int4"), ("reranker_device", "cuda"),
    ("reranker_max_tokens", 99999), ("reranker_instruction", 5)])
def test_invalid_settings_fail_without_loading(candidate, field, value):
    setattr(candidate, field, value)
    with pytest.raises(LocalEncoderError):
        QwenReranker(candidate)


def test_forward_uses_last_token_logits_no_generation_left_padding(candidate):
    import torch
    model = QwenReranker(candidate)
    calls = []
    class Causal:
        def __call__(self, **values):
            calls.append(values)
            assert values["logits_to_keep"] == 1 and values["use_cache"] is False
            logits = torch.zeros((2, 1, 10000))
            logits[:, 0, 9693] = torch.tensor([4.0, -2.0])
            logits[:, 0, 2152] = torch.tensor([1.0, 3.0])
            return SimpleNamespace(logits=logits)
    model._model = Causal()
    model._torch, model.device, model._tokenizer = torch, "cpu", Tokenizer()
    assert model._forward([[1, 2], [3, 4, 5]]) == [3.0, -5.0]
    assert calls[0]["attention_mask"].tolist() == [[0, 1, 1], [1, 1, 1]]
