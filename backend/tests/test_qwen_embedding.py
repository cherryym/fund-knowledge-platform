"""Offline synthetic Qwen contracts; never load real weights or host credentials."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import socket
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from fund_kb import qwen_embedding as qwen
from fund_kb.ai_transport import ProviderError
from fund_kb.ingestion import text_sha256
from fund_kb.local_encoders import MODEL_SPECS, LocalEncoderError
from fund_kb.qwen_model_spec import QWEN4B_SPEC
from fund_kb.retrieval import EmbeddingProvider, VectorIndex


@pytest.fixture(autouse=True)
def forbid_network_and_credentials(monkeypatch):
    import huggingface_hub
    import huggingface_hub.utils._auth as auth
    import huggingface_hub.utils._headers as headers

    def forbidden(*args, **kwargs):
        raise AssertionError("QWEN_TEST_NETWORK_OR_CREDENTIAL_ACCESS_FORBIDDEN")

    for target, name in ((socket.socket, "connect"), (socket, "create_connection"), (socket, "getaddrinfo"),
            (huggingface_hub, "get_token"), (auth, "get_token"), (headers, "get_token")):
        monkeypatch.setattr(target, name, forbidden)


class Tokenizer:
    is_fast = True
    pad_token_id = 1

    def __init__(self, side):
        self.padding_side = side
        self.calls, self.disabled = [], []
        self.backend_tokenizer = SimpleNamespace(no_truncation=lambda: self.disabled.append("truncation"),
            no_padding=lambda: self.disabled.append("padding"))

    def __call__(self, texts, **kwargs):
        assert kwargs == {"add_special_tokens": True, "padding": False, "truncation": False,
                          "return_attention_mask": True, "return_token_type_ids": False}
        self.calls.append(list(texts))
        ids = [[1 if char == "¤" else ord(char) + 10 for char in text] for text in texts]
        return {"input_ids": ids, "attention_mask": [[1] * len(row) for row in ids]}


@pytest.fixture
def fake_qwen(tmp_path, monkeypatch):
    import torch
    import transformers

    spec = copy.deepcopy(QWEN4B_SPEC)
    contents = {name: b"synthetic shard bytes: " + name.encode() for name in spec["weights"]}
    metadata = {"config.json": {"model_type": "qwen3", "hidden_size": 2560, "max_position_embeddings": 40960},
        "tokenizer_config.json": {"tokenizer_class": "Qwen2Tokenizer"}, "tokenizer.json": {}, "vocab.json": {},
        "model.safetensors.index.json": {"metadata": {"total_size": sum(map(len, contents.values()))},
            "weight_map": {"embed_tokens.weight": spec["weights"][0], "norm.weight": spec["weights"][1]}},
        "1_Pooling/config.json": {"word_embedding_dimension": 2560, "pooling_mode_lasttoken": True,
            "pooling_mode_cls_token": False, "pooling_mode_mean_tokens": False, "include_prompt": True},
        "modules.json": [], "config_sentence_transformers.json": {}}
    contents.update({name: json.dumps(value).encode() for name, value in metadata.items()})
    contents["merges.txt"] = b"# synthetic tokenizer fixture\n"
    for name, data in contents.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        spec["files"][name] = (len(data), "sha256:" + hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(qwen, "QWEN4B_SPEC", spec)
    loaders, tokenizers, models = [], [], []

    class Model(torch.nn.Module):
        def __init__(self, dtype):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.ones(1, dtype=dtype), requires_grad=False)
            self.config = SimpleNamespace(model_type="qwen3", hidden_size=2560)
            self.calls = []

        def forward(self, input_ids, attention_mask, **kwargs):
            assert kwargs == {"return_dict": True, "use_cache": False, "output_hidden_states": False}
            assert not self.training and not torch.is_grad_enabled()
            self.calls.append((input_ids.clone(), attention_mask.clone()))
            hidden = torch.zeros((*input_ids.shape, 2560), dtype=self.weight.dtype, device=input_ids.device)
            hidden[:, :, 0] = ((input_ids * attention_mask).cumsum(1) % 997).to(self.weight.dtype) / 997
            hidden[:, :, 1] = (input_ids % 251 + 1).to(self.weight.dtype) / 251
            hidden[:, :, 2] = 1
            hidden[attention_mask == 0] = 99  # A padding/CLS pooling bug is observable.
            return SimpleNamespace(last_hidden_state=hidden)

    def load_tokenizer(path, **kwargs):
        loaders.append(("tokenizer", kwargs))
        tokenizer = Tokenizer(kwargs["padding_side"])
        tokenizers.append(tokenizer)
        return tokenizer

    def load_model(path, **kwargs):
        loaders.append(("model", kwargs))
        model = Model(kwargs["dtype"])
        models.append(model)
        return model

    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", load_tokenizer)
    monkeypatch.setattr(transformers.AutoModel, "from_pretrained", load_model)
    settings = SimpleNamespace(embedding_mode="transformers", embedding_model=spec["repo"],
        embedding_revision=spec["revision"], embedding_model_path=tmp_path, embedding_dimensions=2560,
        embedding_device="cpu", embedding_dtype="bfloat16", embedding_batch_size=2,
        embedding_model_max_tokens=128, embedding_max_tokens=96, embedding_query_instruction="Instruct: synthetic.\nQuery: ",
        embedding_chunk_strategy="semantic_sections_v3", embedding_overlap_tokens=8, embedding_context_tokens=8,
        embedding_chunk_bytes=48, embedding_title_bytes=8, embedding_overlap_bytes=4)

    def update_json(name, value):
        data = json.dumps(value).encode()
        (tmp_path / name).write_bytes(data)
        spec["files"][name] = (len(data), "sha256:" + hashlib.sha256(data).hexdigest())

    return SimpleNamespace(settings=settings, spec=spec, contents=contents, metadata=metadata,
        loaders=loaders, tokenizers=tokenizers, models=models, update_json=update_json)


@pytest.mark.parametrize("dtype", ["bfloat16", "float32", "float16"])
@pytest.mark.parametrize("side", ["left", "right"])
def test_last_nonpadding_pooling_preserves_masks_dtype_duplicates_and_order(fake_qwen, dtype, side):
    import torch

    fake_qwen.settings.embedding_dtype = dtype
    model = qwen.QwenEmbedding(fake_qwen.settings)
    model._load_tokenizer().padding_side = side
    texts = ["中文🙂", "longer English", "العربية", "中文🙂", "café", "pad-token¤"]
    before = list(texts)
    vectors = model.embed(texts)
    assert texts == before and len(vectors) == len(texts)
    assert vectors[0] == vectors[3]
    for text, vector in zip(texts, vectors, strict=True):
        ids = [1 if char == "¤" else ord(char) + 10 for char in text]
        expected = torch.zeros(2560, dtype=getattr(torch, dtype))
        expected[0] = torch.tensor(sum(ids) % 997, dtype=expected.dtype) / 997
        expected[1] = torch.tensor(ids[-1] % 251 + 1, dtype=expected.dtype) / 251
        expected[2] = 1
        expected = expected.float()
        expected /= torch.linalg.vector_norm(expected)
        assert vector == pytest.approx(expected.tolist(), abs=1e-7)
        assert math.fsum(value * value for value in vector) == pytest.approx(1, abs=1e-6)
    observed = []
    for ids, masks in fake_qwen.models[0].calls:
        assert ids.dtype == masks.dtype == torch.long
        assert len(ids) <= 2
        for row, mask in zip(ids, masks, strict=True):
            observed.append(row[mask.bool()].tolist())
            count = int(mask.sum())
            assert mask.tolist() == ([0] * (len(mask) - count) + [1] * count if side == "left"
                                     else [1] * count + [0] * (len(mask) - count))
    assert observed == [[1 if c == "¤" else ord(c) + 10 for c in text] for text in texts]
    assert model.last_diagnostics["input_tokens"] == list(map(len, texts))
    assert model.last_diagnostics["dtype"] == dtype
    assert model.last_diagnostics["normalization_dtype"] == "float32"
    assert model.last_diagnostics["pooling"] == "last_nonpadding_token"
    for text, vector in zip(texts, vectors, strict=True):
        assert model.embed([text])[0] == pytest.approx(vector, abs=1e-7)


def test_lazy_token_count_prefix_budget_and_all_file_pins_before_model_load(fake_qwen):
    provider = EmbeddingProvider(fake_qwen.settings)
    assert provider._model is None and fake_qwen.loaders == []
    assert provider.token_count("中文") == 2
    assert provider.query_token_count("中文") == len(fake_qwen.settings.embedding_query_instruction) + 2
    model = provider._model
    assert isinstance(model, qwen.QwenEmbedding) and model._model is None
    assert set(model.verified_files) == set(fake_qwen.spec["files"]) - set(fake_qwen.spec["weights"])
    provider.embed(["document"])
    assert fake_qwen.tokenizers[0].calls[-1] == ["document"]
    provider.embed(["query"], query=True)
    assert fake_qwen.tokenizers[0].calls[-1] == [fake_qwen.settings.embedding_query_instruction + "query"]
    assert set(model.verified_files) == set(fake_qwen.spec["files"])
    for name, record in model.verified_files.items():
        assert record["sha256"] == hashlib.sha256(fake_qwen.contents[name]).hexdigest()
    assert fake_qwen.tokenizers[0].disabled == ["truncation", "padding"]
    import torch
    for kind, kwargs in fake_qwen.loaders:
        assert kwargs["revision"] == QWEN4B_SPEC["revision"]
        assert kwargs["local_files_only"] is True and kwargs["trust_remote_code"] is False and kwargs["token"] is False
        if kind == "model":
            assert kwargs["weights_only"] is kwargs["use_safetensors"] is True
            assert kwargs["dtype"] == torch.bfloat16 and kwargs["attn_implementation"] == "sdpa"
            assert "quantization_config" not in kwargs and "load_in_4bit" not in kwargs


def test_provider_counts_full_instruction_before_accepting_query(fake_qwen):
    provider = EmbeddingProvider(fake_qwen.settings)
    prefix = fake_qwen.settings.embedding_query_instruction
    fits = "a" * (128 - len(prefix))
    assert provider.query_token_count(fits) == 128
    with pytest.raises(ProviderError, match="EMBEDDING_INPUT_TOO_LONG"):
        provider.embed([fits + "尾"], query=True)
    assert fake_qwen.models == []
    assert len(provider.embed([fits], query=True)[0]) == 2560


def test_query_pooling_counts_prefix_and_keeps_all_slices(fake_qwen):
    index = object.__new__(VectorIndex)
    index.settings = fake_qwen.settings
    index.embedding = EmbeddingProvider(fake_qwen.settings)
    query = "a" * 101
    assert index.embedding.token_count(query) <= 128 < index.embedding.query_token_count(query)
    vectors = index._embed_queries([query, "short"])
    prefix = fake_qwen.settings.embedding_query_instruction
    observed = []
    for ids, masks in fake_qwen.models[0].calls:
        for row, mask in zip(ids, masks, strict=True):
            text = "".join(chr(value - 10) for value in row[mask.bool()].tolist())
            assert text.startswith(prefix) and len(text) <= 128
            observed.append(text[len(prefix):])
    assert observed == ["a" * 48, "a" * 48, "a" * 5, "short"]
    assert "".join(observed[:-1]) == query and len(vectors) == 2


def test_full_32k_input_keeps_tail_and_later_oversize_fails_before_forward(fake_qwen, monkeypatch):
    import torch

    fake_qwen.settings.embedding_model_max_tokens = 32768
    model = qwen.QwenEmbedding(fake_qwen.settings)
    observed = []

    def forward(rows):
        model._torch = torch
        observed.extend(copy.deepcopy(rows))
        return torch.ones((len(rows), 2560), dtype=torch.float32)

    monkeypatch.setattr(model, "_forward", forward)
    text = "中" * 32767 + "★"
    assert len(model.embed([text])[0]) == 2560
    assert len(observed[0]) == 32768 and observed[0][-1] == ord("★") + 10
    assert model.last_diagnostics["input_tokens"] == [32768] and model.last_diagnostics["truncated"] is False
    observed.clear()
    with pytest.raises(LocalEncoderError, match="index=2,tokens=32769,limit=32768"):
        model.embed(["a", "b", text + "尾"])
    assert observed == [] and model.last_diagnostics == {}


@pytest.mark.parametrize("name", list(QWEN4B_SPEC["files"]))
def test_every_metadata_and_weight_file_is_hashed_before_loading(fake_qwen, name):
    data = fake_qwen.contents[name]
    (fake_qwen.settings.embedding_model_path / name).write_bytes(bytes([data[0] ^ 1]) + data[1:])
    with pytest.raises(LocalEncoderError, match="HASH_MISMATCH"):
        qwen.QwenEmbedding(fake_qwen.settings).embed(["synthetic"])
    assert fake_qwen.models == []


@pytest.mark.parametrize("replacement", ["../unfixed.safetensors", "/tmp/unfixed.safetensors",
    "https://example.invalid/shard", "model.safetensors", None])
def test_shard_index_cannot_load_any_unpinned_or_missing_shard(fake_qwen, replacement):
    index = copy.deepcopy(fake_qwen.metadata["model.safetensors.index.json"])
    index["weight_map"]["norm.weight"] = replacement if replacement is not None else fake_qwen.spec["weights"][0]
    fake_qwen.update_json("model.safetensors.index.json", index)
    with pytest.raises(LocalEncoderError, match="QWEN_UNPINNED_SHARD_INDEX"):
        qwen.QwenEmbedding(fake_qwen.settings).embed(["synthetic"])
    assert fake_qwen.loaders == []


@pytest.mark.parametrize("name", ["model.safetensors", "adapter_config.json", "added_tokens.json", "special_tokens_map.json"])
def test_extra_loader_files_cannot_shadow_pinned_shards_or_tokenizer(fake_qwen, name):
    (fake_qwen.settings.embedding_model_path / name).write_bytes(b"untrusted")
    with pytest.raises(LocalEncoderError, match="QWEN_UNPINNED_LOADER_FILE"):
        qwen.QwenEmbedding(fake_qwen.settings).embed(["synthetic"])
    assert fake_qwen.loaders == []


def test_symlinked_pooling_directory_cannot_escape_fixed_files(fake_qwen):
    directory = fake_qwen.settings.embedding_model_path
    (directory / "1_Pooling").rename(directory / "moved-pooling")
    (directory / "1_Pooling").symlink_to(directory / "moved-pooling", target_is_directory=True)
    with pytest.raises(LocalEncoderError, match="FILE_PATH_UNSAFE"):
        qwen.QwenEmbedding(fake_qwen.settings).token_count("synthetic")


@pytest.mark.parametrize("change,error", [({"model_type": "xlm-roberta"}, "UNSUPPORTED_ARCHITECTURE"),
    ({"hidden_size": 1024}, "UNSUPPORTED_ARCHITECTURE"), ({"auto_map": {"AutoModel": "remote.Code"}}, "UNSUPPORTED_ARCHITECTURE"),
    ({"quantization_config": {"bits": 4}}, "UNSUPPORTED_ARCHITECTURE"), ({"max_position_embeddings": 32}, "CAPACITY_EXCEEDED")])
def test_wrong_architecture_quantization_or_capacity_fails_before_loader(fake_qwen, change, error):
    fake_qwen.update_json("config.json", fake_qwen.metadata["config.json"] | change)
    with pytest.raises(LocalEncoderError, match=error):
        qwen.QwenEmbedding(fake_qwen.settings).embed(["synthetic"])
    assert fake_qwen.loaders == []


@pytest.mark.parametrize("change", [{"pooling_mode_lasttoken": False}, {"pooling_mode_mean_tokens": True},
    {"word_embedding_dimension": 1024}])
def test_pooling_metadata_must_match_native_last_token_contract(fake_qwen, change):
    fake_qwen.update_json("1_Pooling/config.json", fake_qwen.metadata["1_Pooling/config.json"] | change)
    with pytest.raises(LocalEncoderError, match="POOLING_CONFIGURATION_MISMATCH"):
        qwen.QwenEmbedding(fake_qwen.settings).embed(["synthetic"])
    assert fake_qwen.loaders == []


@pytest.mark.parametrize("field,value,error", [("embedding_dtype", "int4", "DTYPE"),
    ("embedding_dimensions", 1024, "DIMENSION"), ("embedding_revision", "main", "NOT_PINNED"),
    ("embedding_device", "cuda", "DEVICE"), ("embedding_model_max_tokens", 32769, "CAPACITY"),
    ("embedding_batch_size", 0, "SETTING"), ("embedding_query_instruction", None, "INSTRUCTION")])
def test_invalid_configuration_is_rejected_without_loading(fake_qwen, field, value, error):
    setattr(fake_qwen.settings, field, value)
    with pytest.raises(LocalEncoderError, match=error):
        qwen.QwenEmbedding(fake_qwen.settings)
    assert fake_qwen.loaders == []


@pytest.mark.parametrize("texts", ["single string", [None], [42], [["nested"]]])
def test_invalid_text_lists_fail_before_io(fake_qwen, texts):
    with pytest.raises(LocalEncoderError, match="EXPECTS_TEXT_LIST"):
        qwen.QwenEmbedding(fake_qwen.settings).embed(texts)
    assert fake_qwen.loaders == []


def test_empty_batch_is_zero_io_and_empty_text_is_not_fabricated(fake_qwen):
    model = qwen.QwenEmbedding(fake_qwen.settings)
    assert model.embed([]) == [] and model.last_diagnostics == {} and fake_qwen.loaders == []
    with pytest.raises(LocalEncoderError, match="EMBEDDING_EMPTY_TEXT"):
        model.embed([""])
    assert fake_qwen.models == []
    model.close()
    assert model._model is model._tokenizer is model._torch is model.device is None


@pytest.mark.parametrize("result", [{"input_ids": [[42]], "attention_mask": [[0]]},
    {"input_ids": [[42]], "attention_mask": [[]]}, {"input_ids": [[True]], "attention_mask": [[1]]},
    {"input_ids": [[42]], "attention_mask": []}, {"input_ids": []}, {}])
def test_bad_tokenizer_counts_ids_or_masks_are_rejected(fake_qwen, result):
    model = qwen.QwenEmbedding(fake_qwen.settings)
    model._tokenizer = lambda *a, **k: result
    with pytest.raises(LocalEncoderError, match="ALIGNMENT_FAILED"):
        model.embed(["synthetic"])
    assert fake_qwen.models == []


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "zero", "shape"])
def test_nonfinite_or_misaligned_later_batch_never_returns_partial_vectors(fake_qwen, monkeypatch, bad):
    import torch

    model = qwen.QwenEmbedding(fake_qwen.settings)
    calls = []

    def forward(rows):
        model._torch = torch
        calls.append(rows)
        if len(calls) == 1:
            return torch.ones((len(rows), 2560))
        if bad == "shape":
            return torch.ones((len(rows) + 1, 2560))
        values = torch.ones((len(rows), 2560))
        values[-1] = 0 if bad == "zero" else float(bad)
        return values

    monkeypatch.setattr(model, "_forward", forward)
    with pytest.raises(LocalEncoderError, match="EMBEDDING_INVALID"):
        model.embed(["a", "b", "c", "d"])
    assert len(calls) == 2 and model.last_diagnostics == {}


@pytest.mark.parametrize("bad,error", [("shape", "EMBEDDING_INVALID_OUTPUT"),
    ("dtype", "OUTPUT_DEVICE_OR_DTYPE_MISMATCH"), ("nan", "EMBEDDING_INVALID_OUTPUT"),
    ("mps_error", "QWEN_LOCAL_INFERENCE_FAILED")])
def test_native_forward_output_and_backend_failure_are_not_silently_accepted(fake_qwen, monkeypatch, bad, error):
    model = qwen.QwenEmbedding(fake_qwen.settings)
    model._load_model()
    original, calls = model._model.forward, []

    def broken(*args, **kwargs):
        calls.append(1)
        if bad == "mps_error":
            raise NotImplementedError("synthetic MPS unsupported operation")
        result = original(*args, **kwargs)
        if bad == "shape":
            result.last_hidden_state = result.last_hidden_state[..., :8]
        elif bad == "dtype":
            result.last_hidden_state = result.last_hidden_state.float()
        else:
            result.last_hidden_state[:, -1, 0] = float("nan")
        return result

    monkeypatch.setattr(model._model, "forward", broken)
    with pytest.raises(LocalEncoderError, match=error):
        model.embed(["synthetic"])
    assert calls == [1] and model.last_diagnostics == {}


@pytest.mark.parametrize("requested", ["auto", "mps"])
def test_mps_unavailable_or_operator_fallback_never_selects_cpu(fake_qwen, monkeypatch, requested):
    model = qwen.QwenEmbedding(fake_qwen.settings)
    model.requested_device = requested
    mps = SimpleNamespace(is_built=lambda: True, is_available=lambda: False)
    model._torch = SimpleNamespace(backends=SimpleNamespace(mps=mps))
    with pytest.raises(LocalEncoderError, match="REQUESTED_MPS_UNAVAILABLE"):
        model._select_device()
    assert model.device is None
    mps.is_available = lambda: True
    monkeypatch.setenv("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    with pytest.raises(LocalEncoderError, match="QWEN_MPS_FALLBACK_FORBIDDEN"):
        model._select_device()
    assert model.device is None
    monkeypatch.setenv("PYTORCH_ENABLE_MPS_FALLBACK", "0")
    model._select_device()
    assert model.device == "mps"


def test_qwen_fingerprint_binds_adapter_dtype_and_preserves_exact_bge_namespace(fake_qwen, monkeypatch):
    from fund_kb import retrieval

    captured, original = [], retrieval.json.dumps

    def capture(value, *args, **kwargs):
        if isinstance(value, dict) and "model_adapter" in value:
            captured.append(copy.deepcopy(value))
        return original(value, *args, **kwargs)

    monkeypatch.setattr(retrieval.json, "dumps", capture)
    first = EmbeddingProvider(fake_qwen.settings)
    assert captured[-1]["model_adapter"] == "qwen3-last-token-v1" and captured[-1]["dtype"] == "bfloat16"
    changed = copy.copy(fake_qwen.settings)
    changed.embedding_dtype = "float32"
    assert EmbeddingProvider(changed).fingerprint != first.fingerprint
    changed.embedding_query_instruction += "extra"
    assert EmbeddingProvider(changed).fingerprint != first.fingerprint
    bge = SimpleNamespace(embedding_mode="transformers", embedding_model=MODEL_SPECS["embedding"]["repo"],
        embedding_revision=MODEL_SPECS["embedding"]["revision"], embedding_dimensions=1024,
        embedding_chunk_strategy="semantic_sections_v3", embedding_max_tokens=768, embedding_model_max_tokens=8192,
        embedding_query_instruction="", embedding_dtype="float32")
    for dtype in ("float32", "bfloat16"):
        bge.embedding_dtype = dtype
        assert EmbeddingProvider(bge).fingerprint == "02919242d0cedd3244fdc54b678f49e9420336efe495cfeedf6b512b329c2e11"
        assert "dtype" not in captured[-1] and captured[-1]["model_adapter"] == "local-transformers-cls-v1"


def test_qwen_semantic_v3_index_inputs_keep_all_original_source_spans(fake_qwen):
    index = object.__new__(VectorIndex)
    index.settings = fake_qwen.settings
    index.embedding = EmbeddingProvider(fake_qwen.settings)
    rid, vid, bid = str(uuid4()), str(uuid4()), str(uuid4())
    text = "第一条 合成条件。" + "必须完整保留条件与例外。" * 40 + "尾部标记★"
    source = {"resource_id": rid, "version_id": vid, "block_id": bid, "ordinal": 0, "title": "合成标题",
        "text": text, "content_sha256": text_sha256(text), "block_type": "paragraph", "data": {"text": text}}
    before = copy.deepcopy(source)
    prepared, membership = index._prepare([source], "synthetic", ready=False)
    assert len(prepared) > 1 and membership[0][1] == len(prepared)
    covered = set()
    for payload, embedding_text in prepared:
        assert index.embedding.token_count(embedding_text) <= 96
        assert payload["embedding_fingerprint"] == index.embedding.fingerprint
        for span in payload["source_spans"]:
            assert text[span["start"]:span["end"]] == payload["text"][span["text_start"]:span["text_end"]]
            covered.update(range(span["start"], span["end"]))
    assert covered == set(range(len(text))) and source == before
    index.embedding.embed([embedding_text for _, embedding_text in prepared])
    assert fake_qwen.tokenizers[0].calls[-1] == [embedding_text for _, embedding_text in prepared]
    assert all(not value.startswith(fake_qwen.settings.embedding_query_instruction)
               for value in fake_qwen.tokenizers[0].calls[-1])


def test_probe_requires_explicit_run_and_does_not_construct_model(fake_qwen, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts/probe-qwen4b-model.py"
    spec = importlib.util.spec_from_file_location("qwen_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "QwenEmbedding", lambda *a, **k: pytest.fail("plan must not load a model"))
    assert module.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "plan" and result["weights_loaded"] is False
    assert result["device"] == "mps" and result["dtype"] == "bfloat16"
    assert result["generation_model_calls"] == 0


def test_probe_report_pipeline_with_synthetic_encoder_only(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/probe-qwen4b-model.py"
    spec = importlib.util.spec_from_file_location("qwen_probe_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    closed = []

    class Encoder:
        def __init__(self, settings):
            self.verified_files = {name: {"file": name} for name in QWEN4B_SPEC["files"]}

        def token_count(self, text, query=False):
            return len(text) + (10 if query else 0)

        def embed(self, texts, query=False):
            if any(self.token_count(text, query) > 32768 for text in texts):
                raise LocalEncoderError("EMBEDDING_INPUT_TOO_LONG")
            self.last_diagnostics = {"input_tokens": [self.token_count(text, query) for text in texts],
                "device": "mps", "dtype": "bfloat16"}
            vectors = []
            for text in texts:
                row = [0.] * 2560
                row[int("Earth" in text or "地球" in text)] = 1.
                vectors.append(row)
            return vectors

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "QwenEmbedding", Encoder)
    settings = SimpleNamespace(embedding_device="mps", embedding_dtype="bfloat16",
        embedding_model=QWEN4B_SPEC["repo"], embedding_revision=QWEN4B_SPEC["revision"])
    result = module.probe(settings)
    assert result["status"] == "PASS" and result["oversized_input_rejected"] is True
    assert result["batch_single_cosine"] == [1.] * 5 and result["duplicate_cosine"] == 1.
    assert result["generation_model_calls"] == 0 and result["index_written"] is False
    assert result["business_sources_read"] is False and closed == [True]
