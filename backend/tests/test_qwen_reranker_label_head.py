"""Selective output-head contracts with tiny random models; no pretrained weights."""
from types import SimpleNamespace

import pytest

from fund_kb.qwen_reranker import FULL_OUTPUT_PROJECTION, OUTPUT_PROJECTION, QwenReranker
from fund_kb.local_encoders import LocalEncoderError
from test_qwen_reranker import Tokenizer, candidate as candidate  # noqa: F401


class ExperimentalReranker(QwenReranker):
    """Explicit research path, never the production factory's default."""
    output_projection = OUTPUT_PROJECTION

    def _label_logits(self, inputs, masks):
        return self._experimental_label_logits(inputs, masks)


@pytest.fixture
def tiny(candidate):
    import torch
    from transformers import Qwen3Config, Qwen3ForCausalLM
    torch.manual_seed(719)
    config = Qwen3Config(vocab_size=10000, hidden_size=16, intermediate_size=32,
        num_hidden_layers=1, num_attention_heads=2, num_key_value_heads=2, head_dim=8,
        max_position_embeddings=512, tie_word_embeddings=False, attention_dropout=0.0)
    model = ExperimentalReranker(candidate)
    model._model = Qwen3ForCausalLM(config).eval()
    model._torch, model.device, model._tokenizer = torch, "cpu", Tokenizer()
    return model


def test_default_uses_original_full_head_without_experimental_cache(tiny, candidate):
    model = QwenReranker(candidate)
    model._model, model._torch, model.device, model._tokenizer = tiny._model, tiny._torch, "cpu", Tokenizer()
    widths = []
    hook = model._model.lm_head.register_forward_hook(lambda module, args, result: widths.append(result.shape[-1]))
    try:
        model._forward([[1, 2], [3, 4, 5]])
    finally:
        hook.remove()
    assert widths == [10000]
    assert model.output_projection == FULL_OUTPUT_PROJECTION
    assert model.projected_output_columns == 10000
    assert model._label_weight is None and model.batch_size == candidate.reranker_batch_size == 2


def test_counterexample_last_position_still_projects_unused_vocabulary(tiny):
    """Red on the pre-fix adapter: logits_to_keep=1 still calls the entire head."""
    import torch
    widths = []
    hook = tiny._model.lm_head.register_forward_hook(lambda module, args, result: widths.append(result.shape[-1]))
    try:
        values = tiny._forward([[1, 2], [3, 4, 5]])
    finally:
        hook.remove()
    assert len(values) == 2 and all(torch.isfinite(torch.tensor(values)))
    assert widths == [], "Full vocabulary head computed columns which scoring immediately discards"


def test_counterexample_merging_yes_no_weights_changes_bf16_rounding():
    import torch
    hidden = torch.tensor([[1.0078125]], dtype=torch.bfloat16)
    weights = torch.tensor([[1.0], [1.0078125]], dtype=torch.bfloat16)
    separately_rounded = torch.nn.functional.linear(hidden, weights).float()
    expected = separately_rounded[:, 0] - separately_rounded[:, 1]
    merged = torch.nn.functional.linear(hidden, (weights[0] - weights[1])[None]).float().flatten()
    assert expected.tolist() == [-0.0078125]
    assert merged.tolist() != expected.tolist()  # Keep two logits, then subtract in FP32.


def test_selected_projection_matches_full_forward_and_never_mutates_weights(tiny):
    import torch
    model = tiny._model
    inputs = torch.tensor([[0, 1, 2], [3, 4, 5]])
    masks = torch.tensor([[0, 1, 1], [1, 1, 1]])
    original = model.lm_head.weight.detach().clone()
    with torch.inference_mode():
        expected = model(input_ids=inputs, attention_mask=masks, logits_to_keep=1,
            use_cache=False, return_dict=True, output_hidden_states=False).logits[:, 0, [9693, 2152]].float()
        actual = tiny._label_logits(inputs, masks).float()
    torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-6)
    torch.testing.assert_close(torch.tensor(tiny._forward([[1, 2], [3, 4, 5]])),
        expected[:, 0] - expected[:, 1], rtol=1e-5, atol=1e-6)
    assert tiny._model is model and torch.equal(original, model.lm_head.weight)
    assert tuple(tiny._label_weight.shape) == (2, 16)
    assert tiny._label_weight.dtype == model.lm_head.weight.dtype
    assert tiny._label_weight.device == model.lm_head.weight.device
    assert not tiny._label_weight.requires_grad
    weight = tiny._label_weight
    tiny._forward([[1, 2]])
    assert tiny._label_weight is weight  # One copy per loaded model, not per batch.
    tiny.close()
    assert tiny._label_weight is None and tiny._model is None


def test_tied_embeddings_remain_tied(tiny):
    import torch
    model = tiny._model
    model.lm_head.weight = model.model.embed_tokens.weight
    original = model.model.embed_tokens.weight.detach().clone()
    tiny._forward([[1, 2]])
    assert model.lm_head.weight is model.model.embed_tokens.weight
    assert torch.equal(original, model.model.embed_tokens.weight)


@pytest.mark.parametrize("bad", ["bias", "short-vocabulary", "wrong-dtype", "custom-head"])
def test_unsupported_heads_fail_closed(tiny, bad):
    import torch
    if bad == "bias":
        tiny._model.lm_head = torch.nn.Linear(16, 10000, bias=True)
    elif bad == "short-vocabulary":
        tiny._model.lm_head = torch.nn.Linear(16, 100, bias=False)
    elif bad == "wrong-dtype":
        tiny._model.lm_head = tiny._model.lm_head.double()
    else:
        tiny._model.lm_head = torch.nn.Sequential(tiny._model.lm_head)
    with pytest.raises(LocalEncoderError, match="UNSUPPORTED_RERANK_HEAD"):
        tiny._forward([[1, 2]])


@pytest.mark.parametrize("kind", ["wrong-shape", "nonfinite"])
def test_invalid_selected_logits_fail_closed(tiny, monkeypatch, kind):
    import torch
    if kind == "wrong-shape":
        monkeypatch.setattr(tiny, "_label_logits", lambda *_: torch.ones(2, 1))
    else:
        monkeypatch.setattr(tiny, "_label_logits", lambda *_: torch.tensor([[float("nan"), 1.0], [1.0, 2.0]]))
    with pytest.raises(LocalEncoderError, match="INVALID_OUTPUT"):
        tiny._forward([[1, 2], [3, 4]])


def test_invalid_backbone_shape_fails_closed(tiny, monkeypatch):
    import torch
    monkeypatch.setattr(tiny._model.model, "forward", lambda **_: SimpleNamespace(last_hidden_state=torch.zeros(1, 2, 9)))
    with pytest.raises(LocalEncoderError, match="INVALID_OUTPUT"):
        tiny._forward([[1, 2]])


@pytest.fixture
def head_probe():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts/probe-qwen-label-head.py"
    spec = importlib.util.spec_from_file_location("test_qwen_head_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paired_probe_keeps_tokens_batches_mapping_and_projection_counts(head_probe, tiny):
    import json
    model = head_probe.HeadProbe(SimpleNamespace(reranker_model=tiny.model_name, reranker_revision=tiny.revision,
        reranker_model_path=tiny.path, reranker_device="cpu", reranker_dtype="float32",
        reranker_max_tokens=512, reranker_batch_size=2, reranker_instruction=tiny.instruction))
    model._model, model._torch, model.device, model._tokenizer = tiny._model, tiny._torch, "cpu", Tokenizer()
    requests = [("SYNTHETIC_QUERY_A", ["doc-one", "doc-two", "doc-one"]), ("SYNTHETIC_QUERY_B", ["doc-one"])]
    report = head_probe.measure_case(model, "synthetic", requests, 2, 1e-6)
    for pair in report["rounds"]:
        assert pair["comparison"]["within_tolerance"]
        assert pair["comparison"]["same_batch_schedule"] and pair["comparison"]["exact_token_and_mapping_match"]
        full, label = (pair["variants"][v] for v in head_probe.VARIANTS)
        assert full["pair_count"] == label["pair_count"] == 4
        assert full["computed_window_count"] == label["computed_window_count"] == 3
        assert full["actual_projected_logits"] == 30000 and label["actual_projected_logits"] == 6
        assert full["actual_useful_tokens"] == label["actual_useful_tokens"]
        assert full["actual_padded_tokens"] == label["actual_padded_tokens"]
    encoded = json.dumps(report)
    assert all(query not in encoded and all(text not in encoded for text in texts) for query, texts in requests)
    assert str(tiny.path) not in encoded


def test_probe_reads_only_whitelisted_profile_fields(head_probe, candidate):
    import json
    profile = candidate.reranker_model_path / "retrieval-profile.json"
    values = {**vars(candidate), "reranker_model_path": str(candidate.reranker_model_path),
              "unrelated_setting": "UNRELATED_CANARY"}
    profile.write_text(json.dumps(values))
    settings, digest = head_probe.profile_settings(profile)
    assert vars(settings) == {key: value for key, value in values.items() if key != "unrelated_setting"}
    assert len(digest) == 64


def test_probe_rejects_env_path_before_reading(head_probe, tmp_path, monkeypatch):
    from pathlib import Path
    def forbidden(*args):
        pytest.fail("unexpected file read")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    with pytest.raises(RuntimeError, match="NON_SECRET_RETRIEVAL_PROFILE"):
        head_probe.profile_settings(tmp_path / ".env")


@pytest.mark.parametrize("where", ["existing", "profile-data", "model"])
def test_probe_invalid_output_never_writes_failure_report(head_probe, tmp_path, candidate, monkeypatch, where):
    import json
    folder = tmp_path / "config"
    folder.mkdir()
    model_path = tmp_path / "weights"
    model_path.mkdir()
    values = {**vars(candidate), "reranker_model_path": str(model_path)}
    profile = folder / "retrieval-profile.json"
    profile.write_text(json.dumps(values))
    output = {"existing": profile, "profile-data": folder / "new.json", "model": model_path / "new.json"}[where]
    original = profile.read_bytes()
    def forbidden(*args):
        pytest.fail("path rejection reached model setup")
    monkeypatch.setattr(head_probe, "_script", forbidden)
    assert head_probe.main(["--profile", str(profile), "--output", str(output)]) == 1
    assert profile.read_bytes() == original
    if where != "existing":
        assert not output.exists()


def test_shared_probe_retains_all_synthetic_query_candidate_pairs(head_probe):
    cases = head_probe.workloads("shared")
    assert [sum(len(docs) for _, docs in requests) for _, requests in cases] == [880, 720]
    for _, requests in cases:
        assert len({query for query, _ in requests}) == len(requests)
        assert all(len(docs) == len(set(docs)) == 80 for _, docs in requests)
        assert all(docs == requests[0][1] for _, docs in requests)


def test_batch_size_experiment_is_explicit_and_retains_pairs(head_probe, tiny, candidate):
    model = head_probe.BatchProbe(candidate)
    model._model, model._torch, model.device, model._tokenizer = tiny._model, tiny._torch, "cpu", Tokenizer()
    requests = [("q1", ["x" * (i + 1) for i in range(9)]), ("q2", ["x" * 3])]
    before = model.variant(requests, head_probe.BATCH_VARIANTS[0])
    after = model.variant(requests, head_probe.BATCH_VARIANTS[1])
    comparison = head_probe._compare(before, after, 1e-5, require_same_batches=False)
    assert comparison["exact_token_and_mapping_match"] and comparison["complete_window_coverage"]
    assert comparison["same_batch_schedule"] is False
    assert before["diagnostics"]["pair_count"] == after["diagnostics"]["pair_count"] == 10
    assert before["diagnostics"]["computed_window_count"] == after["diagnostics"]["computed_window_count"] == 10
    assert before["diagnostics"]["actual_batch_count"] == 5
    assert after["diagnostics"]["actual_batch_count"] == 2
    assert before["diagnostics"]["output_projection"] == after["diagnostics"]["output_projection"]
    with pytest.raises(RuntimeError, match="PROBE_BATCHES_CHANGED"):
        head_probe._compare(before, after, 1e-5)
    repeat = model.variant(requests, head_probe.BATCH_VARIANTS[1])
    assert head_probe._compare(after, repeat, 0.0)["exact_scores"]
    assert candidate.reranker_batch_size == QwenReranker(candidate).batch_size == 2


def test_profile_symlink_cannot_redirect_to_env(head_probe, tmp_path, monkeypatch):
    from pathlib import Path
    profile = tmp_path / "retrieval-profile.json"
    profile.symlink_to(tmp_path / ".env")
    def forbidden(*args):
        pytest.fail("symlink rejection attempted a file read")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    with pytest.raises(RuntimeError, match="NON_SECRET_RETRIEVAL_PROFILE"):
        head_probe.profile_settings(profile)
