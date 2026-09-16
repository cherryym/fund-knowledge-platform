"""Synthetic offline contracts; never download weights, read credentials, or use the app DB."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import socket
from collections import Counter
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from fund_kb import local_encoders as enc


@pytest.fixture(autouse=True)
def forbid_network_and_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("NETWORK_OR_HOST_CREDENTIAL_ACCESS_FORBIDDEN")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    # Public Hub 1.31+ handles token=False before consulting either token source.
    import huggingface_hub
    import huggingface_hub.utils._auth as auth
    import huggingface_hub.utils._headers as headers

    monkeypatch.setattr(huggingface_hub, "get_token", forbidden)
    monkeypatch.setattr(auth, "get_token", forbidden)
    monkeypatch.setattr(headers, "get_token", forbidden)


class FakeTokenizer:
    is_fast = True
    padding_side = "right"
    bos_token_id = 0
    eos_token_id = 2
    pad_token_id = 1

    def __init__(self):
        self.calls = []
        self.backend_tokenizer = SimpleNamespace(no_truncation=lambda: None, no_padding=lambda: None)

    def __call__(self, texts, **kwargs):
        assert kwargs["truncation"] is False
        assert kwargs["padding"] is False
        self.calls.append((list(texts), kwargs))
        rows = [[ord(char) + 10 for char in text] for text in texts]
        if kwargs.get("text_pair"):
            pairs = [[ord(char) + 10 for char in text] for text in kwargs["text_pair"]]
            rows = [[0] + left + [2, 2] + right + [2] for left, right in zip(rows, pairs, strict=True)]
        elif kwargs["add_special_tokens"]:
            rows = [[0] + row + [2] for row in rows]
        return {"input_ids": rows, "attention_mask": [[1] * len(row) for row in rows]}

    def num_special_tokens_to_add(self, pair):
        return 4 if pair else 2

    def prepare_for_model(self, ids, pair_ids, **kwargs):
        raise AssertionError("TRANSFORMERS5_REMOVED_TOKENIZER_METHOD")

    def pad(self, features, **kwargs):
        raise AssertionError("TRANSFORMERS5_REMOVED_TOKENIZER_METHOD")


class FakeModel:
    def __init__(self, role):
        self.role = role
        self.config = SimpleNamespace(hidden_size=1024, num_labels=1)
        self.calls = []
        self.moves = []

    def eval(self):
        return self

    def to(self, device):
        self.moves.append(device)
        return self

    def __call__(self, input_ids, attention_mask, **kwargs):
        import torch

        self.calls.append((input_ids.clone(), attention_mask.clone()))
        sums = (input_ids * attention_mask).sum(dim=1).float()
        if self.role == "reranker":
            return SimpleNamespace(logits=sums[:, None] / 1000)
        hidden = torch.zeros((len(input_ids), input_ids.shape[1], 1024))
        hidden[:, 0, 0] = sums / 1000
        hidden[:, 0, 1] = 1
        return SimpleNamespace(last_hidden_state=hidden)


@pytest.fixture
def fake_models(tmp_path, monkeypatch):
    import transformers

    config = {"model_type": "xlm-roberta", "max_position_embeddings": 8194, "pad_token_id": 1}
    (tmp_path / "config.json").write_text(json.dumps(config))
    checks, loaders = [], []
    tokenizers, models = [], []

    def verify(directory, name, pin):
        checks.append(name)
        return {"file": name}

    def load_tokenizer(path, **kwargs):
        loaders.append(("tokenizer", path, kwargs))
        tokenizer = FakeTokenizer()
        tokenizers.append(tokenizer)
        return tokenizer

    def load_model(role):
        def load(path, **kwargs):
            loaders.append((role, path, kwargs))
            model = FakeModel(role)
            models.append(model)
            return model
        return load

    monkeypatch.setattr(enc, "verify_model_file", verify)
    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", load_tokenizer)
    monkeypatch.setattr(transformers.AutoModel, "from_pretrained", load_model("embedding"))
    monkeypatch.setattr(transformers.AutoModelForSequenceClassification, "from_pretrained", load_model("reranker"))
    settings = SimpleNamespace(
        embedding_model=enc.MODEL_SPECS["embedding"]["repo"],
        embedding_revision=enc.MODEL_SPECS["embedding"]["revision"], embedding_model_path=tmp_path,
        embedding_device="cpu", embedding_dimensions=1024, embedding_model_max_tokens=8192,
        embedding_query_instruction="", embedding_batch_size=2,
        reranker_model=enc.MODEL_SPECS["reranker"]["repo"],
        reranker_revision=enc.MODEL_SPECS["reranker"]["revision"], reranker_model_path=tmp_path,
        reranker_device="cpu", reranker_max_tokens=12, reranker_batch_size=2,
    )
    return SimpleNamespace(settings=settings, checks=checks, loaders=loaders, models=models, tokenizers=tokenizers)


def test_token_count_loads_only_tokenizer_and_uses_real_lengths(fake_models):
    model = enc.LocalEmbedding(fake_models.settings)
    assert fake_models.loaders == []
    assert model.token_count("中文abc") == 7
    assert model.token_count("") == 2
    assert model.token_count("x" * 9000) == 9002  # counting itself never truncates/rejects
    assert [item[0] for item in fake_models.loaders] == ["tokenizer"]
    assert "pytorch_model.bin" not in fake_models.checks
    assert model._model is None


def test_local_flags_and_safe_weight_loading(fake_models):
    enc.LocalEmbedding(fake_models.settings).embed(["alpha"])
    enc.LocalReranker(fake_models.settings).score("q", ["beta"])
    for kind, path, options in fake_models.loaders:
        assert path == str(fake_models.settings.embedding_model_path)
        assert options["local_files_only"] is True
        assert options["trust_remote_code"] is False
        assert options["token"] is False
        assert len(options["revision"]) == 40
        if kind != "tokenizer":
            assert options["weights_only"] is True
            assert options["use_safetensors"] is (kind == "reranker")


def test_public_hub_token_false_never_resolves_host_token():
    from huggingface_hub.utils._headers import build_hf_headers

    assert "authorization" not in build_hf_headers(token=False)


def test_mixed_batch_counts_cls_l2_and_order(fake_models):
    import torch

    model = enc.LocalEmbedding(fake_models.settings)
    texts = ["短", "longer mixed中文", "短", ""]
    vectors = model.embed(texts)
    assert model.last_diagnostics["input_tokens"] == [3, len(texts[1]) + 2, 3, 2]
    assert len(vectors) == len(texts)
    assert all(len(row) == 1024 for row in vectors)
    assert all(sum(x*x for x in row) == pytest.approx(1, abs=1e-6) for row in vectors)
    singles = [model.embed([text])[0] for text in texts]
    assert torch.allclose(torch.tensor(vectors), torch.tensor(singles), atol=1e-7)
    assert vectors[0] == vectors[2]
    assert vectors[0] != vectors[1]


def test_embedding_rejects_overflow_in_later_batch_before_any_model_load(fake_models):
    fake_models.settings.embedding_model_max_tokens = 8
    model = enc.LocalEmbedding(fake_models.settings)
    with pytest.raises(enc.LocalEncoderError, match="index=2,tokens=9,limit=8"):
        model.embed(["a", "b", "1234567"])
    assert fake_models.models == []
    assert not model.last_diagnostics
    assert len(model.embed(["123456"])[0]) == 1024


def test_explicit_query_instruction_is_counted_not_applied_to_documents(fake_models):
    fake_models.settings.embedding_query_instruction = "prefix:"
    fake_models.settings.embedding_model_max_tokens = 9
    model = enc.LocalEmbedding(fake_models.settings)
    model.embed(["xx"])
    assert fake_models.tokenizers[0].calls[-1][0] == ["xx"]
    with pytest.raises(enc.LocalEncoderError, match="tokens=11,limit=9"):
        model.embed(["xx"], query=True)
    assert fake_models.tokenizers[0].calls[-1][0] == ["prefix:xx"]


@pytest.mark.parametrize("length,capacity,overlap", [(0, 1, 0), (1, 1, 0), (21, 8, 2), (8001, 7, 1), (10, 3, 2)])
def test_window_ranges_cover_every_token_with_final_tail(length, capacity, overlap):
    spans = list(enc.token_windows(length, capacity, overlap))
    assert spans[0][0] == 0
    assert spans[-1][1] == length
    assert {i for start, end in spans for i in range(start, end)} == set(range(length))
    assert all(0 <= end-start <= capacity for start, end in spans)
    assert all(left[1]-right[0] == overlap for left, right in pairwise(spans))


def test_reranking_all_windows_tail_and_candidate_alignment(fake_models):
    model = enc.LocalReranker(fake_models.settings)
    texts = ["abc", "x" * 19 + "★", "", "z", "abc"]
    scores = model.score("q", texts)
    diagnostic = model.last_diagnostics
    assert diagnostic["text_tokens"] == list(map(len, texts))
    assert diagnostic["query_tokens"] == 1
    assert diagnostic["document_window_capacity"] == 7
    assert len(diagnostic["windows"][1]) > 1
    assert diagnostic["windows"][1][-1]["end_token"] == 20
    assert scores[1] > max(scores[0], scores[2], scores[3])  # marker only in last window
    assert scores[0] == scores[4]
    # Independent hand calculation; include XLM-R's BOS/EOS pair template.
    for document, score, windows in zip(texts, scores, diagnostic["windows"], strict=True):
        expected = []
        covered = set()
        for window in windows:
            start, end = window["start_token"], window["end_token"]
            covered.update(range(start, end))
            expected.append((6 + ord("q") + 10 + sum(ord(c)+10 for c in document[start:end]))/1000)
            assert window["input_tokens"] <= 12
        assert covered == set(range(len(document)))
        assert score == pytest.approx(max(expected))
    singles = [model.score("q", [text])[0] for text in texts]
    assert scores == pytest.approx(singles)


def test_reranker_rejects_full_query_overflow_instead_of_truncating(fake_models):
    model = enc.LocalReranker(fake_models.settings)
    with pytest.raises(enc.LocalEncoderError, match="RERANK_QUERY_TOO_LONG"):
        model.score("q" * 8, ["tail"])
    assert fake_models.models == []


def _expected_rerank_pairs(requests, max_tokens):
    """Independent pair templates, including every duplicate and overlapping tail."""
    pairs, scores, windows = [], [], []
    for query, texts in requests:
        request_scores, request_windows = [], []
        for text in texts:
            capacity = max_tokens - len(query) - 4
            step = capacity - min(64, capacity // 4)
            document_scores, document_windows = [], []
            for start in range(0, max(1, len(text)), step):
                end = min(len(text), start + capacity)
                pair = tuple([0] + [ord(c) + 10 for c in query] + [2, 2]
                             + [ord(c) + 10 for c in text[start:end]] + [2])
                pairs.append(pair)
                document_scores.append(sum(pair) / 1000)
                document_windows.append((start, end, len(pair)))
                if end == len(text):
                    break
            request_scores.append(max(document_scores))
            request_windows.append(document_windows)
        scores.append(request_scores)
        windows.append(request_windows)
    return pairs, scores, windows


def test_score_many_retains_every_pair_window_and_original_position(fake_models):
    import torch

    fake_models.settings.reranker_batch_size = 3
    model = enc.LocalReranker(fake_models.settings)
    requests = [("q", ["abc", "x" * 199 + "★", "", "abc"]),
                ("zz", ["abc", "d" * 23 + "★"]), ("oversized but empty", []),
                ("", ["abc"]), ("q", ["abc"])]
    frozen = copy.deepcopy(requests)
    expected_pairs, expected_scores, expected_windows = _expected_rerank_pairs(requests, 12)
    result = model.score_many(requests)
    diagnostic = model.last_diagnostics
    assert requests == frozen
    assert len(result) == len(requests)
    for actual, expected in zip(result, expected_scores, strict=True):
        assert len(actual) == len(expected)
        assert actual == pytest.approx(expected)
    assert result[0][0] == result[0][3] == result[4][0]
    assert result[1][0] != result[0][0]  # same text, different full query
    assert diagnostic["dtype"] == "float32"
    assert diagnostic["pair_count"] == sum(len(texts) for _, texts in requests)
    assert diagnostic["request_count"] == len(requests)
    assert diagnostic["window_count"] == len(expected_pairs)
    assert diagnostic["truncated"] is False
    assert all(options["dtype"] == torch.float32 for kind, _, options in fake_models.loaders if kind != "tokenizer")
    observed_pairs = [tuple(row[mask.bool()].tolist())
                      for ids, masks in fake_models.models[0].calls
                      for row, mask in zip(ids, masks, strict=True)]
    assert Counter(observed_pairs) == Counter(expected_pairs)  # multiplicities must not be deduplicated
    assert observed_pairs == sorted(expected_pairs, key=len)
    for detail, request_windows in zip(diagnostic["requests"], expected_windows, strict=True):
        assert [[(w["start_token"], w["end_token"], w["input_tokens"]) for w in windows]
                for windows in detail["windows"]] == request_windows
    for actual, (query, texts) in zip(result, requests, strict=True):
        assert actual == pytest.approx(model.score(query, texts))


def test_score_many_length_buckets_reduce_padding_without_losing_pairs(fake_models):
    model = enc.LocalReranker(fake_models.settings)
    requests = [("q", ["a" * 7, "b", "c" * 6, "d", "e" * 5, "f"]), ("zz", ["g" * 6, "h"])]
    pairs, expected, _ = _expected_rerank_pairs(requests, 12)
    result = model.score_many(requests)
    padded_tokens = sum(ids.numel() for ids, _ in fake_models.models[0].calls)
    serial_padding = sum(max(map(len, pairs[i:i+2])) * len(pairs[i:i+2]) for i in range(0, len(pairs), 2))
    assert padded_tokens < serial_padding
    assert sum(int(masks.sum()) for _, masks in fake_models.models[0].calls) == sum(map(len, pairs))
    for actual, scores in zip(result, expected, strict=True):
        assert actual == pytest.approx(scores)


@pytest.mark.parametrize("requests", [[], [("q", [])], [("x" * 100, []), ("", [])]])
def test_score_many_empty_requests_do_not_load_tokenizer_or_weights(fake_models, requests):
    model = enc.LocalReranker(fake_models.settings)
    assert model.score_many(requests) == [[] for _ in requests]
    assert fake_models.loaders == []
    assert model.last_diagnostics == {}


@pytest.mark.parametrize("requests,error", [
    ("query", "EXPECTS_REQUEST_LIST"), (["ab"], "EXPECTS_QUERY_TEXTS_PAIR"),
    ([("q", [], "extra")], "EXPECTS_QUERY_TEXTS_PAIR"),
    ([(None, [])], "EXPECTS_TEXT"), ([("q", "single")], "EXPECTS_TEXT_LIST"),
    ([("q", [None])], "EXPECTS_TEXT_LIST"),
])
def test_score_many_validates_all_parameters_before_loading(fake_models, requests, error):
    model = enc.LocalReranker(fake_models.settings)
    with pytest.raises(enc.LocalEncoderError, match=error):
        model.score_many(requests)
    assert fake_models.loaders == []


def test_score_many_rejects_later_oversized_query_before_any_forward(fake_models):
    model = enc.LocalReranker(fake_models.settings)
    with pytest.raises(enc.LocalEncoderError, match="RERANK_QUERY_TOO_LONG"):
        model.score_many([("q", ["valid"]), ("q" * 8, ["tail"])])
    assert fake_models.models == []
    assert model.last_diagnostics == {}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), "shape"])
def test_score_many_invalid_later_batch_cannot_produce_partial_success(fake_models, monkeypatch, bad):
    import torch

    model = enc.LocalReranker(fake_models.settings)
    calls = []

    def forward(features):
        model._torch = torch
        calls.append(features)
        if len(calls) == 1:
            return torch.ones((len(features), 1))
        if bad == "shape":
            return torch.ones((len(features) + 1, 1))
        values = torch.ones((len(features), 1))
        values[-1, 0] = bad
        return values

    monkeypatch.setattr(model, "_forward", forward)
    with pytest.raises(enc.LocalEncoderError, match="RERANK_INVALID_OUTPUT"):
        model.score_many([("q", ["a", "b"]), ("zz", ["c", "d"])])
    assert len(calls) == 2
    assert model.last_diagnostics == {}


@pytest.mark.parametrize("role", ["embedding", "reranker"])
def test_empty_batches_are_zero_io_and_close_releases_models(fake_models, role):
    cls = enc.LocalEmbedding if role == "embedding" else enc.LocalReranker
    model = cls(fake_models.settings)
    assert (model.embed([]) if role == "embedding" else model.score("q", [])) == []
    assert fake_models.loaders == []
    model.token_count("x")
    model.close()
    model.close()
    assert model._model is model._tokenizer is model._torch is None
    assert model.token_count("x") == 3


@pytest.mark.parametrize("field,value,error", [
    ("embedding_dimensions", 512, "DIMENSION_MISMATCH"),
    ("embedding_revision", "main", "NOT_PINNED"),
    ("embedding_model", "untrusted/remote", "NOT_PINNED"),
    ("embedding_model_path", "BAAI/bge-m3", "PATH_REQUIRED"),
    ("embedding_device", "cuda", "INVALID_LOCAL_MODEL_DEVICE"),
    ("embedding_model_max_tokens", 8193, "CAPACITY_EXCEEDED"),
    ("embedding_batch_size", 0, "INVALID_LOCAL_ENCODER_SETTING"),
])
def test_bad_settings_fail_without_loading(fake_models, field, value, error):
    setattr(fake_models.settings, field, value)
    with pytest.raises(enc.LocalEncoderError, match=error):
        enc.LocalEmbedding(fake_models.settings)
    assert fake_models.loaders == []


@pytest.mark.parametrize("values", ["single string", [None], [7]])
def test_text_type_errors_do_not_include_contents(fake_models, values):
    with pytest.raises(enc.LocalEncoderError, match="EXPECTS_TEXT_LIST"):
        enc.LocalEmbedding(fake_models.settings).embed(values)


def test_auto_mps_selection_and_unavailable_cpu_reason(fake_models):
    model = enc.LocalEmbedding(fake_models.settings)
    model.requested_device = "auto"
    model._torch = SimpleNamespace(backends=SimpleNamespace(
        mps=SimpleNamespace(is_built=lambda: True, is_available=lambda: True)))
    model._select_device()
    assert model.device == "mps"
    model._torch.backends.mps.is_available = lambda: False
    with pytest.warns(RuntimeWarning, match="MPS_UNAVAILABLE"):
        model._select_device()
    assert model.device == "cpu"
    assert model.fallback_reason == "MPS_UNAVAILABLE"
    model.requested_device = "mps"
    with pytest.raises(enc.LocalEncoderError, match="REQUESTED_MPS_UNAVAILABLE"):
        model._select_device()


def test_auto_fallback_only_handles_mps_failures(fake_models):
    model = enc.LocalEmbedding(fake_models.settings)
    model.requested_device, model.device = "auto", "mps"
    model._model = FakeModel("embedding")
    model._torch = SimpleNamespace(mps=SimpleNamespace(empty_cache=lambda: None))
    assert model._fallback(RuntimeError("shape mismatch")) is False
    with pytest.warns(RuntimeWarning, match="MPS_OUT_OF_MEMORY"):
        assert model._fallback(RuntimeError("MPS backend out of memory")) is True
    assert model.device == "cpu"
    assert model._model.moves == ["cpu"]
    assert model.fallback_reason == "MPS_OUT_OF_MEMORY"


@pytest.mark.parametrize("role,bad", [("embedding", "nan"), ("embedding", "zero"),
                                      ("embedding", "shape"), ("reranker", "nan"), ("reranker", "shape")])
def test_invalid_outputs_fail_closed(fake_models, monkeypatch, role, bad):
    import torch

    cls = enc.LocalEmbedding if role == "embedding" else enc.LocalReranker
    model = cls(fake_models.settings)

    def forward(features):
        model._torch = torch
        width = 1024 if role == "embedding" else 1
        if bad == "shape":
            return torch.ones((len(features)+1, width))
        return torch.full((len(features), width), float("nan") if bad == "nan" else 0.0)

    monkeypatch.setattr(model, "_forward", forward)
    with pytest.raises(enc.LocalEncoderError, match="INVALID"):
        model.embed(["a"]) if role == "embedding" else model.score("q", ["a"])
    assert not model.last_diagnostics


def test_repository_hash_verification_rejects_same_size_corruption_and_symlink(tmp_path):
    path = tmp_path / "weight.bin"
    path.write_bytes(b"public synthetic bytes")
    data = path.read_bytes()
    pin = (len(data), "sha256:" + hashlib.sha256(data).hexdigest())
    assert enc.verify_model_file(tmp_path, path.name, pin)["sha256"] == pin[1].split(":")[1]
    git_pin = (len(data), "git:" + hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest())
    assert enc.verify_model_file(tmp_path, path.name, git_pin)["bytes"] == len(data)
    path.write_bytes(b"X" * len(data))
    with pytest.raises(enc.LocalEncoderError, match="HASH_MISMATCH"):
        enc.verify_model_file(tmp_path, path.name, pin)
    (tmp_path / "link.bin").symlink_to(path)
    with pytest.raises(enc.LocalEncoderError, match="MISSING_OR_INVALID"):
        enc.verify_model_file(tmp_path, "link.bin", pin)


def preparation_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "prepare-universal-models.py"
    spec = importlib.util.spec_from_file_location("universal_preparation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preparation_requires_explicit_download_before_any_io(tmp_path, monkeypatch):
    module = preparation_module()
    root = tmp_path / "must-not-be-created"
    monkeypatch.setattr(module, "MODEL_ROOT", root)
    with pytest.raises(SystemExit) as exc:
        module.main([])
    assert exc.value.code == 2
    assert not root.exists()


def test_manifest_candidate_keys_follow_integration_contract():
    candidate = preparation_module().candidate_settings("auto")
    assert candidate["embedding_mode"] == "transformers"
    assert candidate["embedding_chunk_strategy"] == "semantic_sections_v3"
    assert candidate["embedding_max_tokens"] == 768
    assert candidate["embedding_model_max_tokens"] == 8192
    assert candidate["embedding_device"] == "auto"
    assert candidate["reranker_mode"] == "local"
    assert candidate["reranker_max_tokens"] == 1024
    assert candidate["reranker_batch_size"] == 8


def test_download_environment_never_reads_inherited_hf_token(monkeypatch):
    module = preparation_module()

    class GuardedEnvironment(dict):
        def __getitem__(self, key):
            raise AssertionError("INHERITED_ENVIRONMENT_READ")

        def get(self, key, default=None):
            raise AssertionError("INHERITED_ENVIRONMENT_READ")

        def pop(self, key, default=None):
            raise AssertionError("INHERITED_ENVIRONMENT_READ")

    environment = GuardedEnvironment()
    monkeypatch.setattr(module, "os", SimpleNamespace(environ=environment))
    module.configure_download_environment()
    assert dict(environment)["HF_TOKEN"] == ""
    assert dict(environment)["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
    assert dict(environment)["HF_HOME"].startswith(str(module.MODEL_ROOT))


def test_preparation_manifest_is_new_candidate_and_does_not_touch_current_profile(tmp_path, monkeypatch):
    module = preparation_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "MODEL_ROOT", tmp_path / "data" / "universal-models")
    monkeypatch.setattr(module, "configure_download_environment", lambda transport: None)
    monkeypatch.setattr(module, "prepare_files", lambda settings, roles, transport, workers: {role: {"status": "synthetic"}
                                                                         for role in roles})
    monkeypatch.setattr(module, "run_probes", lambda settings: {"status": "synthetic"})
    (tmp_path / "data").mkdir()
    profile = tmp_path / "data" / "retrieval-profile.json"
    profile.write_bytes(b"immutable-current-profile")
    assert module.main(["--download", "--manifest-name", "prepared.json"]) == 0
    receipt = json.loads((module.MODEL_ROOT / "prepared.json").read_text())
    assert receipt["status"] == "LOCAL_MODELS_PROBE_PASSED"
    assert receipt["current_profile_written"] is False
    assert profile.read_bytes() == b"immutable-current-profile"
    with pytest.raises(SystemExit):
        module.main(["--download", "--manifest-name", "prepared.json"])
    with pytest.raises(SystemExit):
        module.main(["--download", "--manifest-name", "../retrieval-profile.json"])


@pytest.mark.parametrize("case", ["normal", "resume", "wrong_range", "wrong_hash"])
def test_ranged_download_checks_offsets_hash_and_uses_no_host_auth(tmp_path, monkeypatch, case):
    import httpx

    module = preparation_module()
    content = b"synthetic public model bytes"
    pin = (len(content), "sha256:" + hashlib.sha256(content).hexdigest())
    spec = {"repo": "BAAI/test-fixture", "revision": "a" * 40, "files": {"weight.bin": pin}}
    requests = []
    offset = 4 if case == "resume" else 0
    if offset:
        parts = tmp_path / ".parts" / "weight.bin"
        parts.mkdir(parents=True)
        (parts / f"{0:012d}-{len(content):012d}.part").write_bytes(content[:offset])

    class Response:
        status_code = 206

        def __init__(self):
            self.headers = {"content-range": f"bytes {offset}-{len(content)-1}/{len(content)}"}

        def __enter__(self):
            if case == "wrong_range":
                self.headers = {"content-range": "bytes 0-0/1"}
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size):
            yield b"X" * len(content) if case == "wrong_hash" else content[offset:]

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert "auth" not in kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, headers):
            requests.append((method, url, headers))
            return Response()

    monkeypatch.setattr(httpx, "Client", Client)
    if case.startswith("wrong_"):
        with pytest.raises(ValueError, match="CONTENT_RANGE_MISMATCH|HASH_MISMATCH"):
            module.download_ranges(spec, "weight.bin", tmp_path, 2)
        assert not (tmp_path / "weight.bin").exists()
    else:
        module.download_ranges(spec, "weight.bin", tmp_path, 2)
        assert (tmp_path / "weight.bin").read_bytes() == content
    assert requests[0][0] == "GET"
    assert f"/resolve/{spec['revision']}/" in requests[0][1]
    assert requests[0][2]["Range"] == f"bytes={offset}-{len(content)-1}"
    assert "authorization" not in requests[0][2]


@pytest.mark.parametrize("url", ["http://modelscope.cn/file", "https://127.0.0.1/file",
    "https://user:secret@modelscope.cn/file", "https://modelscope.cn:8443/file",
    "https://modelscope.cn.evil.example/file", "https://unobserved.modelscope.cn/file"])
def test_mirror_rejects_unapproved_redirect_before_lookup(url):
    with pytest.raises(ValueError, match="UNAPPROVED_MODELSCOPE_REDIRECT"):
        preparation_module().mirror_hostname(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254",
                                     "100.64.0.1", "224.0.0.1", "::1", "192.0.2.1"])
def test_mirror_rejects_nonpublic_dns_results(address):
    with pytest.raises(ValueError, match="DNS_NOT_PUBLIC"):
        preparation_module().public_dns_addresses({"Status": 0, "Answer": [{"type": 1, "data": address}]})


def test_mirror_connections_preserve_host_sni_tls_and_redirect_limits(monkeypatch):
    import httpx

    module = preparation_module()
    mirror = module.ModelScopeMirror(enc.MODEL_SPECS["reranker"])
    resolved, calls = [], []

    def address(host):
        resolved.append(host)
        return "39.99.133.195"

    class Response:
        def __init__(self, status, headers):
            self.status_code, self.headers = status, headers

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["verify"] is True
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, target, headers, extensions):
            calls.append((method, str(target), headers, extensions))
            if len(calls) == 1:
                return Response(302, {"location": "https://cdn-lfs-cn-1.modelscope.cn/blob?opaque=synthetic"})
            return Response(206, {})

    monkeypatch.setattr(mirror, "address", address)
    monkeypatch.setattr(httpx, "Client", Client)
    with mirror.stream(mirror.weight_url(), {"Range": "bytes=42-99"}) as response:
        assert response.status_code == 206
    assert resolved == ["modelscope.cn", "cdn-lfs-cn-1.modelscope.cn"]
    for (_, target, headers, extensions), host in zip(calls, resolved, strict=True):
        assert target.startswith("https://39.99.133.195/")
        assert headers["Host"] == extensions["sni_hostname"] == host
        assert headers["Range"] == "bytes=42-99"
        assert "authorization" not in {key.lower() for key in headers}


@pytest.mark.parametrize("different", [False, True])
def test_mirror_requires_exact_original_hf_weight_hash(monkeypatch, different):
    from contextlib import contextmanager

    module = preparation_module()
    spec = enc.MODEL_SPECS["reranker"]
    mirror = module.ModelScopeMirror(spec)
    size, pin = spec["files"][spec["weight"]]
    payload = {"Code": 200, "Data": {"Files": [{"Path": spec["weight"], "Size": size,
                "Sha256": "0" * 64 if different else pin.split(":", 1)[1]}]}}

    @contextmanager
    def stream(url):
        assert "Revision=" + module.MODELSCOPE_REVISIONS[spec["repo"]] in url
        yield SimpleNamespace(iter_bytes=lambda size: iter([json.dumps(payload).encode()]))

    monkeypatch.setattr(mirror, "stream", stream)
    if different:
        with pytest.raises(ValueError, match="NOT_IDENTICAL_TO_HF_PIN"):
            mirror.verify_identity()
    else:
        mirror.verify_identity()
