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


def _padded_slots(batches):
    return sum(len(batch) * max(map(len, batch)) for batch in batches)


def test_counterexample_insertion_padding_and_positional_scatter(fake, monkeypatch):
    """Executable reference for insertion-order batching, NOT the current scheduler.

    The checkout already sorts by length. This witnesses both the work avoided
    by that sort and the wrong answers produced if sorted scores are scattered
    by arrival position instead of the original candidate mapping.
    """
    model, _ = fake
    rows = {str(i): [i + 1] * length for i, length in enumerate((2, 100, 3, 101))}
    monkeypatch.setattr(model, "_frames", lambda query, text:
        [(rows[text], {"start_char": 0, "end_char": len(text)})])
    batches = []

    def forward(batch):
        batches.append(batch)
        return [-float(row[0]) for row in batch]

    monkeypatch.setattr(model, "_forward", forward)
    result = model.score("synthetic query", list(rows))
    original = list(rows.values())
    insertion = [original[:2], original[2:]]
    assert sum(map(len, original)) == 206
    assert _padded_slots(insertion) == 402  # 196 padding positions.
    assert _padded_slots(batches) == 208  # 2 padding positions.
    assert result == [-1.0, -2.0, -3.0, -4.0]
    wrong_positional_scatter = [-float(row[0]) for batch in batches for row in batch]
    assert wrong_positional_scatter == [-1.0, -3.0, -2.0, -4.0]
    assert wrong_positional_scatter != result


def test_counterexample_token_collision_must_not_merge_distinct_queries(fake, monkeypatch):
    """A normalizing tokenizer can map different queries to identical token IDs."""
    model, seen = fake
    monkeypatch.setattr(model, "_frames", lambda query, text:
        [([11, 12, 13], {"start_char": 0, "end_char": len(text)})])
    result = model.score_many([("query", ["duplicate", "duplicate"]), ("QUERY", ["duplicate"])])
    assert result == [[36.0, 36.0], [36.0]]
    assert len(seen) == 2  # Exact duplicates within one query still share a row.
    assert model.last_diagnostics["computed_window_count"] == 2
    assert model.last_diagnostics["reused_window_count"] == 1


def test_reordered_windows_keep_every_owner_and_negative_max(fake, monkeypatch):
    model, _ = fake
    rows = {
        ("q1", "long"): [[5] * 8, [9] * 2, [1] * 5],
        ("q1", "same-small"): [[9] * 2],
        ("q2", "long"): [[5] * 8],
        ("q2", "tail"): [[4] * 3],
    }
    monkeypatch.setattr(model, "_frames", lambda query, text:
        [(row, {"start_char": i, "end_char": i + 1}) for i, row in enumerate(rows[query, text])])
    batches = []

    def forward(batch):
        batches.append(batch)
        return [-float(row[0]) for row in batch]

    monkeypatch.setattr(model, "_forward", forward)
    requests = [("q1", ["long", "same-small", "long"]), ("q2", ["long", "tail"]),
                ("q1", ["long"]), ("empty", [])]
    assert model.score_many(requests) == [[-1.0, -9.0, -1.0], [-5.0, -4.0], [-1.0], []]
    diag = model.last_diagnostics
    assert diag["window_count"] == 12 and diag["computed_window_count"] == 5
    assert diag["reused_window_count"] == 7 and diag["pair_count"] == 6
    assert diag["actual_useful_tokens"] == 26
    assert diag["actual_padded_tokens"] == _padded_slots(batches) == 30
    assert diag["actual_padding_tokens"] == 4 and diag["actual_batch_count"] == 3
    assert diag["batching_strategy"] == "stable_token_length_ascending_v1"
    assert diag["deduplication_strategy"] == "exact_query_and_token_ids_call_scoped"
    assert [row[0] for batch in batches for row in batch] == [9, 4, 1, 5, 5]
    assert [window["input_tokens"] for window in diag["requests"][0]["windows"][0]] == [8, 2, 5]
    assert diag["requests"][3] == {"windows": [], "window_count": 0}


@pytest.mark.parametrize("batch_size", [1, 2, 3, 20])
def test_length_ties_deterministic_and_exact_tokens_preserved(fake, monkeypatch, batch_size):
    model, _ = fake
    model.batch_size = batch_size
    texts = ["b" * 80, "x", "a" * 80, "y", "b" * 80]
    frames = [model._frames("query", text)[0][0] for text in texts]
    expected_order = sorted(dict.fromkeys(tuple(row) for row in frames), key=len)
    calls = []

    def forward(rows):
        calls.extend(tuple(row) for row in rows)
        return [-float(sum(row)) for row in rows]

    monkeypatch.setattr(model, "_forward", forward)
    expected = [-float(sum(row)) for row in frames]
    assert model.score("query", texts) == expected
    assert calls == expected_order
    calls.clear()
    assert model.score("query", texts) == expected
    assert calls == expected_order  # No inference reuse leaks across calls.
    assert model._token_memo is None


@pytest.mark.parametrize("requests,expected", [([], []), ([("q", [])], [[]]),
    ([("q", []), ("q2", [])], [[], []])])
def test_empty_workload_diagnostics(candidate, requests, expected):
    model = QwenReranker(candidate)
    assert model.score_many(requests) == expected
    diag = model.last_diagnostics
    assert all(diag[key] == 0 for key in ("actual_useful_tokens", "actual_padded_tokens",
        "actual_padding_tokens", "actual_batch_count", "computed_window_count", "reused_window_count"))
    assert model._model is None and model._tokenizer is None


def test_empty_text_still_has_full_query_and_frame(fake):
    model, seen = fake
    expected = model._frames("", "")[0][0]
    assert model.score_many([("", ["", ""])]) == [[float(sum(expected) % 1000)] * 2]
    assert seen == [tuple(expected)]
    assert model.last_diagnostics["actual_useful_tokens"] == len(expected)
    assert model.last_diagnostics["actual_padding_tokens"] == 0


def test_diagnostics_have_no_query_document_path_or_token_ids(fake):
    import json
    model, _ = fake
    model.score("PRIVATE_QUERY_CANARY", ["PRIVATE_DOCUMENT_CANARY"])
    encoded = json.dumps(model.last_diagnostics)
    assert all(secret not in encoded for secret in ("PRIVATE_QUERY_CANARY", "PRIVATE_DOCUMENT_CANARY",
        str(model.path), model.instruction, "input_ids"))
    for key in ("actual_useful_tokens", "actual_padded_tokens", "actual_padding_tokens", "actual_batch_count"):
        assert type(model.last_diagnostics[key]) is int and model.last_diagnostics[key] >= 0


def test_diagnostics_match_actual_forward_tensors(candidate):
    import torch
    model = QwenReranker(candidate)
    actual = []

    class Causal:
        def __call__(self, input_ids, attention_mask, **kwargs):
            assert kwargs["logits_to_keep"] == 1 and kwargs["use_cache"] is False
            actual.append((int(attention_mask.sum()), input_ids.numel()))
            logits = torch.zeros((len(input_ids), 1, 10000), dtype=torch.float32)
            logits[:, 0, 9693] = -(input_ids * attention_mask).sum(dim=1).float()
            return SimpleNamespace(logits=logits)

    model._model, model._torch, model.device, model._tokenizer = Causal(), torch, "cpu", Tokenizer()
    scores = model.score("query", ["x", "long" * 200, "short" * 15, "long" * 200])
    diag = model.last_diagnostics
    assert len(scores) == 4 and scores[1] == scores[3] and all(score < 0 for score in scores)
    assert diag["actual_useful_tokens"] == sum(useful for useful, _ in actual)
    assert diag["actual_padded_tokens"] == sum(padded for _, padded in actual)
    assert diag["actual_padding_tokens"] == sum(padded - useful for useful, padded in actual)
    assert diag["actual_batch_count"] == len(actual)


@pytest.mark.parametrize("values", [[], [1.0, 2.0], [None], ["3.0"], [-float("inf")]])
def test_invalid_forward_alignment_fails_closed(fake, monkeypatch, values):
    model, _ = fake
    monkeypatch.setattr(model, "_forward", lambda rows: values)
    with pytest.raises(LocalEncoderError, match="RERANK_ALIGNMENT_FAILED"):
        model.score("query", ["single"])
    assert model._token_memo is None


def test_missing_window_fails_closed(fake, monkeypatch):
    model, _ = fake
    monkeypatch.setattr(model, "_frames", lambda query, text: [])
    with pytest.raises(LocalEncoderError, match="RERANK_ALIGNMENT_FAILED"):
        model.score("query", ["single"])


@pytest.fixture
def padding_probe():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "probe-qwen-padding.py"
    spec = importlib.util.spec_from_file_location("test_qwen_padding_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe_fake(padding_probe, candidate, monkeypatch):
    def fake_load(model):
        model.device = "cpu"
    monkeypatch.setattr(QwenReranker, "_load_model", fake_load)
    monkeypatch.setattr(QwenReranker, "_load_tokenizer", lambda self: Tokenizer())
    monkeypatch.setattr(QwenReranker, "_forward", lambda self, rows: [-float(sum(row) % 1000) for row in rows])
    return padding_probe.ProbeReranker(candidate)


def test_probe_pairing_with_synthetic_model_only(padding_probe, probe_fake):
    import json
    report = padding_probe.run_probe(probe_fake)
    assert report["status"] == "PASS" and report["professional_accuracy"] == "NOT_EVALUATED"
    assert report["pre_change_checkout_already_length_sorted"] is True
    assert report["aggregation"] == "max_raw_logit_all_windows"
    assert report["rounds"][0]["execution_order"] == padding_probe.STRATEGIES
    assert report["rounds"][1]["execution_order"] == tuple(reversed(padding_probe.STRATEGIES))
    for round_ in report["rounds"]:
        comparison = round_["comparison"]
        assert comparison["exact_token_and_mapping_match"] and comparison["complete_window_coverage"]
        assert comparison["within_tolerance"] and comparison["same_full_ranking"]
        assert comparison["max_abs_window_logit_delta"] == comparison["max_abs_candidate_logit_delta"] == 0
        assert comparison["padded_token_reduction"] > 0
        metrics = round_["strategies"]
        assert len({item["actual_useful_tokens"] for item in metrics.values()}) == 1
    risk = report["long_window_max_risk"]
    assert risk["status"] == "REVIEW_REQUIRED" and risk["aggregation_changed"] is False
    for strategy in risk["strategies"].values():
        docs = strategy["candidates"]
        assert len(docs) == 5 and docs[1]["window_count"] > 1
        assert docs[1] == docs[4] and strategy["duplicate_candidate_max_delta"] == 0
        assert all(doc["max"] == max(doc["window_scores"]) for doc in docs)
    encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)
    requests, _, _ = padding_probe.synthetic_cases()
    assert all(query not in encoded and all(text not in encoded for text in texts) for query, texts in requests)
    assert str(probe_fake.path) not in encoded and "trace" not in report
    assert probe_fake._model is None  # This test did not load real weights.


def test_probe_pairing_rejects_token_or_mapping_changes(padding_probe, probe_fake):
    import copy
    before = probe_fake.measure([("q", ["synthetic"])], padding_probe.INSERTION_ORDER)
    after = copy.deepcopy(before)
    after["trace"] = (("different-query", before["trace"][0][1]),)
    with pytest.raises(RuntimeError, match="TOKENS_OR_WINDOW_MAPPING_CHANGED"):
        padding_probe._comparison(before, after, 0.05, 0.01)


def test_probe_near_tie_rank_change_is_not_hidden_by_tolerance(padding_probe, probe_fake):
    import copy
    before = probe_fake.measure([("q", ["a", "b"])], padding_probe.INSERTION_ORDER)
    after = copy.deepcopy(before)
    before["scores"], after["scores"] = [[0.0, 0.001]], [[0.001, 0.0]]
    for snapshot in (before, after):
        for doc, score in zip(snapshot["documents"], snapshot["scores"][0], strict=True):
            doc["window_scores"] = [score]
            doc["max"] = doc["min"] = doc["mean"] = score
    result = padding_probe._comparison(before, after, 0.05, 0.01)
    assert result["within_tolerance"] is True
    assert result["same_full_ranking"] is False and result["same_top1"] is False
    assert result["max_abs_window_logit_delta"] == pytest.approx(0.001)


def test_probe_unexercised_long_windows_are_inconclusive(padding_probe, probe_fake):
    probe_fake.max_tokens = 32768
    report = padding_probe.run_probe(probe_fake)
    assert report["status"] == "INCONCLUSIVE"
    assert report["long_window_max_risk"]["multi_window_test_exercised"] is False


def test_probe_network_and_credentials_blocked(padding_probe):
    import socket
    import huggingface_hub
    import huggingface_hub.utils._auth as auth
    import huggingface_hub.utils._headers as headers
    helper = padding_probe._script("prepare-universal-models")
    with padding_probe.offline_guard(helper):
        with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
            calls = [lambda: tcp.connect(("192.0.2.1", 80)), lambda: tcp.connect_ex(("192.0.2.1", 80)),
                lambda: socket.create_connection(("192.0.2.1", 80)),
                lambda: socket.getaddrinfo("example.invalid", 80),
                lambda: socket.gethostbyname("example.invalid"),
                lambda: udp.sendto(b"synthetic", ("192.0.2.1", 80)),
                huggingface_hub.get_token, auth.get_token, headers.get_token]
            for call in calls:
                with pytest.raises(RuntimeError, match="NETWORK_OR_TOKEN_ACCESS_FORBIDDEN"):
                    call()


def test_probe_help_never_loads_weights(padding_probe, monkeypatch, capsys):
    def forbidden(*args):
        pytest.fail("help attempted model loading")
    monkeypatch.setattr(QwenReranker, "_load_model", forbidden)
    with pytest.raises(SystemExit) as result:
        padding_probe.main(["--help"])
    assert result.value.code == 0
    assert "--directory" in capsys.readouterr().out


def test_probe_rejects_existing_output_before_model_or_environment(padding_probe, candidate, monkeypatch):
    def forbidden(*args):
        pytest.fail("rejected output reached setup")
    monkeypatch.setattr(padding_probe, "_script", forbidden)
    before = list(candidate.reranker_model_path.iterdir())
    assert padding_probe.main(["--directory", str(candidate.reranker_model_path),
        "--output", str(candidate.reranker_model_path)]) == 1
    assert list(candidate.reranker_model_path.iterdir()) == before


@pytest.mark.parametrize("where", ["model", "application-data", "dangling-symlink"])
def test_probe_rejects_unsafe_output(padding_probe, tmp_path, monkeypatch, where):
    directory = tmp_path / "model"
    directory.mkdir()
    monkeypatch.setattr(padding_probe, "ROOT", tmp_path)
    output = {"model": directory / "new", "application-data": tmp_path / "data" / "new",
              "dangling-symlink": tmp_path / "link"}[where]
    if where == "dangling-symlink":
        output.symlink_to(tmp_path / "missing")
    assert padding_probe.main(["--directory", str(directory), "--output", str(output)]) == 1
    assert not output.exists()


@pytest.mark.parametrize("option,value", [("--repeats", "1"), ("--repeats", "3"),
    ("--batch-size", "0"), ("--max-tokens", "32769"), ("--atol", "nan"), ("--rtol", "-1")])
def test_probe_rejects_invalid_numeric_options(padding_probe, tmp_path, option, value):
    output = tmp_path / "never-created"
    assert padding_probe.main(["--directory", str(tmp_path), "--output", str(output), option, value]) == 1
    assert not output.exists()


def test_probe_failure_report_redacts_exception_and_never_loads_weights(padding_probe, tmp_path, monkeypatch, capsys):
    import json
    import torch
    directory, output = tmp_path / "weights", tmp_path / "report"
    directory.mkdir()
    # Isolate the probe's process-local environment changes in this unit test.
    monkeypatch.setattr(padding_probe.os, "environ", {})
    monkeypatch.setattr(torch, "set_num_threads", lambda count: None)
    def fail(*args):
        raise RuntimeError(f"PRIVATE_QUERY_CANARY {directory}")
    def forbidden(*args):
        pytest.fail("failure test attempted real weights")
    monkeypatch.setattr(padding_probe, "run_probe", fail)
    monkeypatch.setattr(QwenReranker, "_load_model", forbidden)
    assert padding_probe.main(["--directory", str(directory), "--output", str(output), "--device", "cpu"]) == 1
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "FAIL" and report["error"] == "PROBE_EXECUTION_FAILED"
    encoded = json.dumps(report) + capsys.readouterr().out
    assert "PRIVATE_QUERY_CANARY" not in encoded and str(directory) not in encoded
    assert padding_probe.os.environ["HF_HUB_OFFLINE"] == "1"
    assert padding_probe.os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert padding_probe.os.environ["HF_HOME"].startswith(str(output))
    assert list(directory.iterdir()) == []
