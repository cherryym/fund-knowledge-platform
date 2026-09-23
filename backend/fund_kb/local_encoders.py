"""Pinned, offline BAAI dense embeddings and complete-window cross-encoding.

Model cards: https://huggingface.co/BAAI/bge-m3 and
https://huggingface.co/BAAI/bge-reranker-v2-m3. No FlagEmbedding/remote Python.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import warnings
from contextlib import contextmanager
from pathlib import Path

# Public BAAI repository metadata checked 2026-09-11. Small files use their
# Git blob IDs; LFS files use the repository's SHA256, NOT a post-download TOFU.
MODEL_SPECS = {
    "embedding": {
        "repo": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "weight": "pytorch_model.bin",
        "files": {
            "config.json": (687, "git:e6eda1c72da8f9dc30fdd9b69c73d35af3b7a7ad"),
            "tokenizer_config.json": (444, "git:dc69ac559dcba2694012009aaa108c614541789a"),
            "special_tokens_map.json": (964, "git:b1879d702821e753ffe4245048eee415d54a9385"),
            "tokenizer.json": (17098108, "sha256:21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08"),
            "sentencepiece.bpe.model": (5069051, "sha256:cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865"),
            "pytorch_model.bin": (2271145830, "sha256:b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38"),
        },
    },
    "reranker": {
        "repo": "BAAI/bge-reranker-v2-m3",
        "revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        "weight": "model.safetensors",
        "files": {
            "config.json": (795, "git:9f62673cb00ec41dcec8947b9ed16f6f2eb23ba2"),
            "tokenizer_config.json": (1173, "git:328a00a9a560aadcf2a3064f917517359eb3cc26"),
            "special_tokens_map.json": (964, "git:b1879d702821e753ffe4245048eee415d54a9385"),
            "tokenizer.json": (17098273, "sha256:69564b696052886ed0ac63fa393e928384e0f8caada38c1f4864a9bfbf379c15"),
            "sentencepiece.bpe.model": (5069051, "sha256:cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865"),
            "model.safetensors": (2271071852, "sha256:d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286"),
        },
    },
}


class LocalEncoderError(ValueError):
    """Stable, text-free diagnostics; never include supplied business text."""


def create_reranker(settings):
    if getattr(settings, "reranker_model", None) == "Qwen/Qwen3-Reranker-4B":
        from .qwen_reranker import QwenReranker
        return QwenReranker(settings)
    return LocalReranker(settings)


def verify_model_file(directory: Path, name: str, pin: tuple[int, str]) -> dict:
    """Validate a fixed file before loading; return independently computed SHA256."""
    path = directory / name
    size, identity = pin
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        raise LocalEncoderError(f"LOCAL_MODEL_FILE_MISSING_OR_INVALID:{name}")
    digest = hashlib.sha256()
    git = hashlib.sha1(f"blob {size}\0".encode(), usedforsecurity=False) if identity.startswith("git:") else None
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
            if git is not None:
                git.update(block)
    actual = git.hexdigest() if git is not None else digest.hexdigest()
    if actual != identity.split(":", 1)[1]:
        raise LocalEncoderError(f"LOCAL_MODEL_HASH_MISMATCH:{name}")
    return {"file": name, "bytes": size, "sha256": digest.hexdigest(), "repository_identity": identity}


def _positive_setting(settings, name, default):
    value = getattr(settings, name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LocalEncoderError(f"INVALID_LOCAL_ENCODER_SETTING:{name}")
    return value


def _texts(value):
    if not isinstance(value, (list, tuple)) or any(not isinstance(text, str) for text in value):
        raise LocalEncoderError("LOCAL_ENCODER_EXPECTS_TEXT_LIST")
    return list(value)


def token_windows(length: int, capacity: int, overlap: int):
    """Half-open token spans, including the final tail. No window-count ceiling."""
    if length < 0 or capacity < 1 or not 0 <= overlap < capacity:
        raise LocalEncoderError("INVALID_RERANK_WINDOW_BUDGET")
    start = 0
    while True:
        end = min(length, start + capacity)
        yield start, end
        if end == length:
            return
        start = end - overlap


class _TokenizationMemo:
    """Exact token rows for one locked operation; never an authorization cache.

    No text normalization, decoding or inference deduplication. The token budget
    only limits retained memo entries: an uncached input is still fully encoded.
    Nested preflight/embedding calls share rows, then release all text and IDs.
    """

    _memo_token_budget = 262144

    @contextmanager
    def tokenization_scope(self):
        with self._lock:
            previous = getattr(self, "_token_memo", None)
            if previous is not None:
                yield
                return
            self._token_memo = {"rows": {}, "tokens": 0}
            try:
                yield
            finally:
                self._token_memo = None

    def _memoized_encode(self, texts, special_tokens, compute):
        memo = getattr(self, "_token_memo", None)
        if memo is None:
            return compute(texts)
        cache = memo["rows"]
        missing = list(dict.fromkeys(text for text in texts if (special_tokens, text) not in cache))
        # Validate every new tokenizer result through the adapter's existing
        # uncached path before it can supply any preflight or inference row.
        fresh = dict(zip(missing, compute(missing) if missing else [], strict=True))
        rows = [list(cache[(special_tokens, text)] if (special_tokens, text) in cache else fresh[text])
                for text in texts]
        for text, row in fresh.items():
            # Charge empty rows too, bounding retained keys for empty-token text.
            cost = max(1, len(row))
            if memo["tokens"] + cost <= self._memo_token_budget:
                cache[(special_tokens, text)] = tuple(row)
                memo["tokens"] += cost
        return rows

    def _clear_token_memo(self):
        memo = getattr(self, "_token_memo", None)
        if memo is not None:
            memo["rows"].clear()
            memo["tokens"] = 0


class _LocalEncoder(_TokenizationMemo):
    role = ""

    def __init__(self, settings):
        self.settings = settings
        self.spec = MODEL_SPECS[self.role]
        self.model_name = getattr(settings, f"{self.role}_model", None)
        self.revision = getattr(settings, f"{self.role}_revision", None)
        if self.model_name != self.spec["repo"] or self.revision != self.spec["revision"]:
            raise LocalEncoderError("LOCAL_MODEL_IDENTITY_NOT_PINNED")
        path = getattr(settings, f"{self.role}_model_path", None)
        if not path or not Path(path).is_absolute() or not Path(path).is_dir():
            raise LocalEncoderError("LOCAL_MODEL_PATH_REQUIRED")
        self.path = Path(path).resolve()
        self.requested_device = getattr(settings, f"{self.role}_device", "auto")
        if self.requested_device not in {"auto", "cpu", "mps"}:
            raise LocalEncoderError("INVALID_LOCAL_MODEL_DEVICE")
        budget_name = "embedding_model_max_tokens" if self.role == "embedding" else "reranker_max_tokens"
        self.max_tokens = _positive_setting(settings, budget_name, 8192 if self.role == "embedding" else 1024)
        self.batch_size = _positive_setting(settings, f"{self.role}_batch_size", 8)
        if self.max_tokens > 8192:
            raise LocalEncoderError("LOCAL_MODEL_CAPACITY_EXCEEDED")
        self._tokenizer = None
        self._model = None
        self._torch = None
        self._lock = threading.RLock()
        self.device = None
        self.fallback_reason = None
        self.last_diagnostics = {}

    def _load_tokenizer(self):
        if self._tokenizer is not None:
            return self._tokenizer
        # A counting call reads only configuration/tokenizer assets, never weights.
        for name, pin in self.spec["files"].items():
            if name != self.spec["weight"]:
                verify_model_file(self.path, name, pin)
        config = json.loads((self.path / "config.json").read_text(encoding="utf-8"))
        if config.get("model_type") != "xlm-roberta" or config.get("auto_map"):
            raise LocalEncoderError("LOCAL_MODEL_UNSUPPORTED_ARCHITECTURE")
        # XLM-R position IDs start at padding_idx+1, not zero.
        capacity = config["max_position_embeddings"] - config["pad_token_id"] - 1
        if self.max_tokens > capacity:
            raise LocalEncoderError("LOCAL_MODEL_CAPACITY_EXCEEDED")
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            str(self.path), revision=self.revision, local_files_only=True,
            trust_remote_code=False, token=False, use_fast=True,
        )
        if not tokenizer.is_fast or tokenizer.padding_side != "right":
            raise LocalEncoderError("LOCAL_TOKENIZER_REQUIRES_FAST_RIGHT_PADDING")
        tokenizer.backend_tokenizer.no_truncation()
        tokenizer.backend_tokenizer.no_padding()
        # Transformers 5 removed prepare_for_model/pad. Confirm the pinned
        # XLM-R pair template through its real tokenizer before constructing
        # token-ID windows directly; never decode and retokenize window tails.
        opts = {"padding": False, "truncation": False, "return_attention_mask": True,
                "return_token_type_ids": False}
        left, right = tokenizer(["A", "B"], add_special_tokens=False, **opts)["input_ids"]
        paired = tokenizer(["A"], text_pair=["B"], add_special_tokens=True, **opts)["input_ids"][0]
        expected = ([tokenizer.bos_token_id] + left + [tokenizer.eos_token_id] * 2
                    + right + [tokenizer.eos_token_id])
        if (paired != expected or tokenizer.num_special_tokens_to_add(pair=True) != 4
                or tokenizer.pad_token_id != config["pad_token_id"]):
            raise LocalEncoderError("LOCAL_TOKENIZER_PAIR_TEMPLATE_MISMATCH")
        self._tokenizer = tokenizer
        return tokenizer

    def _encode(self, texts, *, special_tokens):
        return self._memoized_encode(texts, special_tokens,
            lambda missing: self._encode_uncached(missing, special_tokens=special_tokens))

    def _encode_uncached(self, texts, *, special_tokens):
        tokenizer = self._load_tokenizer()
        result = tokenizer(texts, add_special_tokens=special_tokens, padding=False, truncation=False,
                           return_attention_mask=True, return_token_type_ids=False)
        ids = result["input_ids"]
        if len(ids) != len(texts) or any(len(mask) != len(row) or any(v != 1 for v in mask)
                                       for row, mask in zip(ids, result["attention_mask"], strict=True)):
            raise LocalEncoderError("LOCAL_TOKENIZER_ALIGNMENT_FAILED")
        return ids

    def token_count(self, text: str) -> int:
        """Actual single-text token count INCLUDING BOS/EOS, without loading weights.

        Embedding query prefixes are applied only by embed(query=True); callers
        budgeting an instructed query must count the prefix together with text.
        """
        if not isinstance(text, str):
            raise LocalEncoderError("LOCAL_ENCODER_EXPECTS_TEXT")
        with self._lock:
            return len(self._encode([text], special_tokens=True)[0])

    def _select_device(self):
        if self.requested_device == "cpu":
            self.device = "cpu"
            return
        mps = getattr(self._torch.backends, "mps", None)
        if mps is not None and mps.is_built() and mps.is_available():
            self.device = "mps"
            return
        if self.requested_device == "mps":
            raise LocalEncoderError("REQUESTED_MPS_UNAVAILABLE")
        self.device = "cpu"
        self.fallback_reason = "MPS_NOT_BUILT" if mps is None or not mps.is_built() else "MPS_UNAVAILABLE"
        warnings.warn(f"LOCAL_ENCODER_CPU_FALLBACK:{self.fallback_reason}", RuntimeWarning, stacklevel=3)

    def _fallback(self, exc):
        # Only auto permits a backend fallback. Arbitrary model errors remain errors.
        if self.requested_device != "auto" or self.device != "mps":
            return False
        if not isinstance(exc, (RuntimeError, NotImplementedError)):
            return False
        message = str(exc).lower()
        if not any(word in message for word in ("mps", "metal")):
            return False
        self._model.to("cpu")
        self.device = "cpu"
        self.fallback_reason = "MPS_OUT_OF_MEMORY" if "out of memory" in message else "MPS_OPERATION_FAILED"
        self._torch.mps.empty_cache()
        warnings.warn(f"LOCAL_ENCODER_CPU_FALLBACK:{self.fallback_reason}", RuntimeWarning, stacklevel=3)
        return True

    def _load_model(self):
        if self._model is not None:
            return
        self._load_tokenizer()
        verify_model_file(self.path, self.spec["weight"], self.spec["files"][self.spec["weight"]])
        import torch
        from transformers import AutoModel, AutoModelForSequenceClassification

        self._torch = torch
        self._select_device()
        loader = AutoModel if self.role == "embedding" else AutoModelForSequenceClassification
        self._model = loader.from_pretrained(
            str(self.path), revision=self.revision, local_files_only=True, trust_remote_code=False,
            token=False, weights_only=True, use_safetensors=self.role == "reranker", dtype=torch.float32,
        )
        if self._model.config.hidden_size != 1024 or (
            self.role == "reranker" and self._model.config.num_labels != 1
        ):
            self._model = None
            raise LocalEncoderError("LOCAL_MODEL_OUTPUT_CONFIGURATION_MISMATCH")
        self._model.eval()
        try:
            self._model.to(self.device)
        except (RuntimeError, NotImplementedError) as exc:
            if not self._fallback(exc):
                self._model = None
                raise

    def _forward(self, features):
        self._load_model()
        lengths = [len(item["input_ids"]) for item in features]
        width = max(lengths)
        padded = {
            "input_ids": self._torch.full((len(features), width), self._tokenizer.pad_token_id,
                                          dtype=self._torch.long),
            "attention_mask": self._torch.zeros((len(features), width), dtype=self._torch.long),
        }
        for index, feature in enumerate(features):
            padded["input_ids"][index, :lengths[index]] = self._torch.tensor(
                feature["input_ids"], dtype=self._torch.long)
            padded["attention_mask"][index, :lengths[index]] = self._torch.tensor(
                feature["attention_mask"], dtype=self._torch.long)
        if padded["attention_mask"].sum(dim=1).tolist() != lengths or max(lengths) > self.max_tokens:
            raise LocalEncoderError("LOCAL_MODEL_PADDING_OR_LENGTH_MISMATCH")
        # Verify the order and all unpadded IDs, not the common padded batch width.
        for index, feature in enumerate(features):
            if padded["input_ids"][index, :lengths[index]].tolist() != feature["input_ids"]:
                raise LocalEncoderError("LOCAL_MODEL_INPUT_ALIGNMENT_FAILED")
        for attempt in range(2):
            try:
                with self._torch.inference_mode():
                    output = self._model(**{k: v.to(self.device) for k, v in padded.items()}, return_dict=True)
                    values = output.last_hidden_state[:, 0, :] if self.role == "embedding" else output.logits
                    # Move before returning so the full hidden state is released between batches.
                    return values.detach().float().cpu()
            except (RuntimeError, NotImplementedError) as exc:
                if attempt or not self._fallback(exc):
                    raise
        raise LocalEncoderError("LOCAL_MODEL_INFERENCE_FAILED")  # defensive, not a partial success

    def _diagnostics(self, started, **fields):
        return {"model": self.model_name, "revision": self.revision, "device": self.device,
                "requested_device": self.requested_device, "cpu_fallback_reason": self.fallback_reason,
                "dtype": "float32", "max_tokens": self.max_tokens,
                "elapsed_seconds": round(time.monotonic() - started, 6), "truncated": False, **fields}

    def close(self):
        with self._lock:
            self._clear_token_memo()
            self._model = None
            self._tokenizer = None
            if self._torch is not None and self.device == "mps":
                self._torch.mps.empty_cache()
            self._torch = None
            self.device = None
            self.fallback_reason = None


class LocalEmbedding(_LocalEncoder):
    """BGE-M3 dense channel only: first token (CLS) pooling, float32, L2."""

    role = "embedding"

    def __init__(self, settings):
        super().__init__(settings)
        self.dimension = _positive_setting(settings, "embedding_dimensions", 1024)
        if self.dimension != 1024:
            raise LocalEncoderError("EMBEDDING_MODEL_DIMENSION_MISMATCH")
        self.query_instruction = getattr(settings, "embedding_query_instruction", "")
        if not isinstance(self.query_instruction, str):
            raise LocalEncoderError("INVALID_EMBEDDING_QUERY_INSTRUCTION")

    def embed(self, texts, query=False) -> list[list[float]]:
        texts = _texts(texts)
        with self.tokenization_scope() if query else self._lock:
            started = time.monotonic()
            self.last_diagnostics = {}
            if not texts:
                return []
            # M3 needs no default instruction. Any explicitly configured prefix is
            # concatenated verbatim and included in every query's actual token count.
            prepared = [self.query_instruction + text if query else text for text in texts]
            ids = self._encode(prepared, special_tokens=True)
            counts = [len(row) for row in ids]
            for index, count in enumerate(counts):
                if count > self.max_tokens:
                    raise LocalEncoderError(f"EMBEDDING_INPUT_TOO_LONG:index={index},tokens={count},limit={self.max_tokens}")
            vectors = []
            for start in range(0, len(ids), self.batch_size):
                rows = ids[start:start + self.batch_size]
                values = self._forward([{"input_ids": row, "attention_mask": [1] * len(row)} for row in rows])
                if tuple(values.shape) != (len(rows), self.dimension) or not self._torch.isfinite(values).all():
                    raise LocalEncoderError("EMBEDDING_INVALID_OUTPUT")
                norms = self._torch.linalg.vector_norm(values, dim=1)
                if (norms <= 0).any() or not self._torch.isfinite(norms).all():
                    raise LocalEncoderError("EMBEDDING_INVALID_NORM")
                vectors.extend(self._torch.nn.functional.normalize(values, p=2, dim=1).tolist())
            self.last_diagnostics = self._diagnostics(started, input_tokens=counts, dimensions=self.dimension,
                                                       pooling="cls", normalization="l2", query=bool(query))
            return vectors


class LocalReranker(_LocalEncoder):
    """Raw sequence-classification logits, max over ALL overlapping document windows.

    Query is retained in full in every window. An oversized query is explicitly
    rejected. max-logit is a retrieval heuristic (longer texts have more chances
    to match), not a probability or a business-accuracy certificate.
    """

    role = "reranker"

    def score(self, query: str, texts) -> list[float]:
        return self.score_many([(query, texts)])[0]

    def score_many(self, requests: list[tuple[str, list[str]]]) -> list[list[float]]:
        """Score every query/document pair, batching windows by actual token length.

        Requests, documents and window diagnostics retain their input order. Only
        inference order changes; all windows use the original full query and the
        same float32 model, with independent max pooling for each supplied pair.
        """
        if not isinstance(requests, (list, tuple)):
            raise LocalEncoderError("LOCAL_RERANK_EXPECTS_REQUEST_LIST")
        prepared = []
        for request in requests:
            if not isinstance(request, (list, tuple)) or len(request) != 2:
                raise LocalEncoderError("LOCAL_RERANK_EXPECTS_QUERY_TEXTS_PAIR")
            query, texts = request
            if not isinstance(query, str):
                raise LocalEncoderError("LOCAL_ENCODER_EXPECTS_TEXT")
            prepared.append((query, _texts(texts)))
        with self.tokenization_scope():
            started = time.monotonic()
            self.last_diagnostics = {}
            scores = [[-math.inf] * len(texts) for _, texts in prepared]
            active = [i for i, (_, texts) in enumerate(prepared) if texts]
            if not active:
                return scores
            queries = self._encode([prepared[i][0] for i in active], special_tokens=False)
            documents = self._encode([text for i in active for text in prepared[i][1]], special_tokens=False)
            tokenizer = self._load_tokenizer()
            overhead = tokenizer.num_special_tokens_to_add(pair=True)
            encoded, details, work = {}, [], []
            cursor = 0
            query_rows = dict(zip(active, queries, strict=True))
            for request_index, (_, texts) in enumerate(prepared):
                if not texts:
                    details.append({"text_tokens": [], "window_count": 0, "windows": []})
                    continue
                query_ids = query_rows[request_index]
                rows = documents[cursor:cursor + len(texts)]
                cursor += len(texts)
                encoded[request_index] = (query_ids, rows)
                capacity = self.max_tokens - len(query_ids) - overhead
                if capacity < 1:
                    raise LocalEncoderError(f"RERANK_QUERY_TOO_LONG:tokens={len(query_ids)},limit={self.max_tokens}")
                overlap = min(64, capacity // 4)
                windows = [[] for _ in texts]
                for document_index, document in enumerate(rows):
                    for start, end in token_windows(len(document), capacity, overlap):
                        expected = len(query_ids) + end - start + overhead
                        record = {"start_token": start, "end_token": end, "input_tokens": expected}
                        windows[document_index].append(record)
                        # Keep offsets until inference, avoiding copies of the full
                        # query and document token IDs for every overlapping window.
                        work.append((request_index, document_index, record))
                details.append({"query_tokens": len(query_ids), "text_tokens": [len(row) for row in rows],
                    "pair_special_tokens": overhead, "document_window_capacity": capacity,
                    "overlap_tokens": overlap, "window_count": sum(map(len, windows)), "windows": windows,
                    "aggregation": "max_raw_logit_all_windows"})

            work.sort(key=lambda item: item[2]["input_tokens"])
            for offset in range(0, len(work), self.batch_size):
                batch = work[offset:offset + self.batch_size]
                pending = []
                for request_index, document_index, record in batch:
                    query_ids, rows = encoded[request_index]
                    document = rows[document_index]
                    start, end = record["start_token"], record["end_token"]
                    paired = ([tokenizer.bos_token_id] + query_ids + [tokenizer.eos_token_id] * 2
                              + document[start:end] + [tokenizer.eos_token_id])
                    item = {"input_ids": paired, "attention_mask": [1] * len(paired)}
                    expected = record["input_tokens"]
                    if len(item["input_ids"]) != expected or expected > self.max_tokens:
                        raise LocalEncoderError("RERANK_WINDOW_ALIGNMENT_FAILED")
                    pending.append(item)
                logits = self._forward(pending)
                if tuple(logits.shape) != (len(pending), 1) or not self._torch.isfinite(logits).all():
                    raise LocalEncoderError("RERANK_INVALID_OUTPUT")
                for (request_index, document_index, record), value in zip(batch, logits[:, 0].tolist(), strict=True):
                    record["score"] = value
                    scores[request_index][document_index] = max(scores[request_index][document_index], value)
            if any(len(row) != len(texts) or not all(math.isfinite(value) for value in row)
                   for row, (_, texts) in zip(scores, prepared, strict=True)):
                raise LocalEncoderError("RERANK_ALIGNMENT_FAILED")
            # Keep the established single-query diagnostics contract intact.
            fields = details[0] if len(prepared) == 1 else {
                "requests": details, "request_count": len(prepared), "pair_count": len(documents),
                "window_count": len(work), "aggregation": "max_raw_logit_all_windows",
            }
            self.last_diagnostics = self._diagnostics(started, **fields)
            return scores
