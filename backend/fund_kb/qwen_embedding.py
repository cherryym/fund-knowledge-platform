"""Pinned offline Qwen3-Embedding-4B, independent of the BGE CLS adapter."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .local_encoders import LocalEncoderError, _positive_setting, _texts, _TokenizationMemo, verify_model_file
from .qwen_model_spec import QWEN4B_SPEC

ADAPTER = "qwen3-last-token-v1"
DIMENSION = 2560
MAX_TOKENS = 32768
DTYPES = {"float32", "float16", "bfloat16"}


class QwenEmbedding(_TokenizationMemo):
    """Native Qwen hidden states, last non-padding token, float32 L2 output.

    Weights/inference retain the explicitly configured dtype and device. Auto
    requires MPS; CPU is used only when explicitly selected. No device fallback,
    quantization, remote code, downloads, chat templates or truncated inputs.
    """

    def __init__(self, settings):
        self.settings = settings
        self.model_name = getattr(settings, "embedding_model", None)
        self.revision = getattr(settings, "embedding_revision", None)
        if self.model_name != QWEN4B_SPEC["repo"] or self.revision != QWEN4B_SPEC["revision"]:
            raise LocalEncoderError("LOCAL_MODEL_IDENTITY_NOT_PINNED")
        path = getattr(settings, "embedding_model_path", None)
        if not path or not Path(path).is_absolute() or Path(path).is_symlink() or not Path(path).is_dir():
            raise LocalEncoderError("LOCAL_MODEL_PATH_REQUIRED")
        self.path = Path(path).resolve()
        self.dimension = _positive_setting(settings, "embedding_dimensions", DIMENSION)
        if self.dimension != DIMENSION:
            raise LocalEncoderError("EMBEDDING_MODEL_DIMENSION_MISMATCH")
        self.max_tokens = _positive_setting(settings, "embedding_model_max_tokens", MAX_TOKENS)
        if self.max_tokens > MAX_TOKENS:
            raise LocalEncoderError("LOCAL_MODEL_CAPACITY_EXCEEDED")
        self.batch_size = _positive_setting(settings, "embedding_batch_size", 2)
        self.dtype = getattr(settings, "embedding_dtype", "float32")
        if self.dtype not in DTYPES:
            raise LocalEncoderError("INVALID_QWEN_EMBEDDING_DTYPE")
        self.requested_device = getattr(settings, "embedding_device", "auto")
        if self.requested_device not in {"auto", "cpu", "mps"}:
            raise LocalEncoderError("INVALID_LOCAL_MODEL_DEVICE")
        self.query_instruction = getattr(settings, "embedding_query_instruction", "")
        if not isinstance(self.query_instruction, str):
            raise LocalEncoderError("INVALID_EMBEDDING_QUERY_INSTRUCTION")
        self._lock = threading.RLock()
        self._tokenizer = self._model = self._torch = None
        self.device = None
        self.last_diagnostics = {}
        self.verified_files = {}

    def _verify_file(self, name):
        path = self.path / name
        # Also reject symlinked intermediate directories, e.g. 1_Pooling/.
        if path.resolve() != path:
            raise LocalEncoderError("QWEN_MODEL_FILE_PATH_UNSAFE")
        record = verify_model_file(self.path, name, QWEN4B_SPEC["files"][name])
        self.verified_files[name] = record

    def _metadata(self):
        # from_pretrained prefers an unsharded weight/adapter if present. Such
        # files must not bypass the pinned shard index or tokenizer identity.
        extra_loader_files = {"pytorch_model.bin", "pytorch_model.bin.index.json", "adapter_config.json",
            "added_tokens.json", "special_tokens_map.json", "chat_template.jinja", "additional_chat_templates",
            "quantization_config.json"}
        for path in self.path.iterdir():
            if path.name not in QWEN4B_SPEC["files"] and (
                    path.name.endswith(".safetensors") or path.name in extra_loader_files):
                raise LocalEncoderError("QWEN_UNPINNED_LOADER_FILE")
        for name in QWEN4B_SPEC["files"]:
            if name not in QWEN4B_SPEC["weights"]:
                self._verify_file(name)
        config = json.loads((self.path / "config.json").read_text(encoding="utf-8"))
        tokenizer_config = json.loads((self.path / "tokenizer_config.json").read_text(encoding="utf-8"))
        index = json.loads((self.path / "model.safetensors.index.json").read_text(encoding="utf-8"))
        pooling = json.loads((self.path / "1_Pooling/config.json").read_text(encoding="utf-8"))
        if (config.get("model_type") != "qwen3" or config.get("hidden_size") != DIMENSION
                or config.get("auto_map") or config.get("quantization_config") or tokenizer_config.get("auto_map")):
            raise LocalEncoderError("QWEN_UNSUPPORTED_ARCHITECTURE")
        capacity = config.get("max_position_embeddings")
        if type(capacity) is not int or self.max_tokens > capacity:
            raise LocalEncoderError("LOCAL_MODEL_CAPACITY_EXCEEDED")
        weights = index.get("weight_map")
        if (not isinstance(weights, dict) or not weights or
                any(not isinstance(key, str) or not key or not isinstance(value, str)
                    for key, value in weights.items()) or set(weights.values()) != set(QWEN4B_SPEC["weights"])):
            raise LocalEncoderError("QWEN_UNPINNED_SHARD_INDEX")
        if (pooling.get("word_embedding_dimension") != DIMENSION or pooling.get("pooling_mode_lasttoken") is not True
                or any(value for key, value in pooling.items()
                    if key.startswith("pooling_mode_") and key != "pooling_mode_lasttoken")):
            raise LocalEncoderError("QWEN_POOLING_CONFIGURATION_MISMATCH")
        return config

    def _load_tokenizer(self):
        if self._tokenizer is None:
            self._metadata()
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(str(self.path), revision=self.revision,
                local_files_only=True, trust_remote_code=False, token=False, use_fast=True, padding_side="left")
            if (not tokenizer.is_fast or tokenizer.padding_side not in {"left", "right"}
                    or type(tokenizer.pad_token_id) is not int or tokenizer.pad_token_id < 0):
                raise LocalEncoderError("QWEN_INVALID_TOKENIZER_PADDING")
            tokenizer.backend_tokenizer.no_truncation()
            tokenizer.backend_tokenizer.no_padding()
            self._tokenizer = tokenizer
        return self._tokenizer

    def _encode(self, texts):
        return self._memoized_encode(texts, True, self._encode_uncached)

    def _encode_uncached(self, texts):
        result = self._load_tokenizer()(texts, add_special_tokens=True, padding=False, truncation=False,
            return_attention_mask=True, return_token_type_ids=False)
        try:
            ids, masks = result["input_ids"], result["attention_mask"]
            if (len(ids) != len(texts) or len(masks) != len(ids) or
                    any(len(row) != len(mask) or any(type(v) is not int or v < 0 for v in row)
                        or any(type(v) is not int or v != 1 for v in mask)
                        for row, mask in zip(ids, masks, strict=True))):
                raise LocalEncoderError("LOCAL_TOKENIZER_ALIGNMENT_FAILED")
            if any(encoding.overflowing for encoding in getattr(result, "encodings", None) or []):
                raise LocalEncoderError("EMBEDDING_INPUT_TRUNCATED")
            return ids
        except (KeyError, TypeError) as exc:
            raise LocalEncoderError("LOCAL_TOKENIZER_ALIGNMENT_FAILED") from exc

    def token_count(self, text: str, *, query: bool = False) -> int:
        if not isinstance(text, str):
            raise LocalEncoderError("LOCAL_ENCODER_EXPECTS_TEXT")
        with self._lock:
            return len(self._encode([self.query_instruction + text if query else text])[0])

    def _select_device(self):
        if self.requested_device == "cpu":
            self.device = "cpu"
            return
        mps = getattr(self._torch.backends, "mps", None)
        if mps is None or not mps.is_built() or not mps.is_available():
            raise LocalEncoderError("REQUESTED_MPS_UNAVAILABLE")
        if os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK") == "1":
            raise LocalEncoderError("QWEN_MPS_FALLBACK_FORBIDDEN")
        self.device = "mps"

    def _load_model(self):
        if self._model is not None:
            return
        self._load_tokenizer()
        # Counting can precede inference. Revalidate metadata before the loader
        # rereads it, and hash BOTH complete shards before any model load.
        self._metadata()
        for name in QWEN4B_SPEC["weights"]:
            self._verify_file(name)
        import torch
        from transformers import AutoModel

        self._torch = torch
        self._select_device()
        try:
            model = AutoModel.from_pretrained(str(self.path), revision=self.revision, local_files_only=True,
                trust_remote_code=False, token=False, weights_only=True, use_safetensors=True,
                dtype=getattr(torch, self.dtype), attn_implementation="sdpa")
            if model.config.model_type != "qwen3" or model.config.hidden_size != DIMENSION:
                raise LocalEncoderError("LOCAL_MODEL_OUTPUT_CONFIGURATION_MISMATCH")
            model.eval()
            model.to(self.device)
            if any(parameter.device.type != self.device or parameter.dtype != getattr(torch, self.dtype)
                   for parameter in model.parameters() if parameter.is_floating_point()):
                raise LocalEncoderError("QWEN_MODEL_DEVICE_OR_DTYPE_MISMATCH")
            self._model = model
        except LocalEncoderError:
            raise
        except Exception as exc:  # No implicit CPU/dtype fallback or supplied-text diagnostics.
            raise LocalEncoderError("QWEN_MODEL_LOAD_FAILED") from exc

    def _forward(self, rows):
        self._load_model()
        torch = self._torch
        width = max(map(len, rows))
        inputs = torch.full((len(rows), width), self._tokenizer.pad_token_id, dtype=torch.long)
        masks = torch.zeros_like(inputs)
        for index, row in enumerate(rows):
            start = width - len(row) if self._tokenizer.padding_side == "left" else 0
            inputs[index, start:start + len(row)] = torch.tensor(row, dtype=torch.long)
            masks[index, start:start + len(row)] = 1
        if (width > self.max_tokens or masks.sum(dim=1).tolist() != list(map(len, rows)) or
                any(inputs[i][masks[i].bool()].tolist() != row for i, row in enumerate(rows))):
            raise LocalEncoderError("LOCAL_MODEL_PADDING_OR_LENGTH_MISMATCH")
        try:
            with torch.inference_mode():
                masks = masks.to(self.device)
                output = self._model(input_ids=inputs.to(self.device), attention_mask=masks,
                    return_dict=True, use_cache=False, output_hidden_states=False)
                hidden = output.last_hidden_state
                if tuple(hidden.shape) != (len(rows), width, self.dimension):
                    raise LocalEncoderError("EMBEDDING_INVALID_OUTPUT")
                if hidden.device.type != self.device or hidden.dtype != getattr(torch, self.dtype):
                    raise LocalEncoderError("QWEN_OUTPUT_DEVICE_OR_DTYPE_MISMATCH")
                # Mask positions, not token values: a literal pad-token ID can
                # still be real input. This works for either padding direction.
                positions = torch.arange(width, device=masks.device).expand_as(masks)
                last = positions.masked_fill(masks == 0, -1).max(dim=1).values
                if (last < 0).any():
                    raise LocalEncoderError("EMBEDDING_EMPTY_TEXT")
                values = hidden[torch.arange(len(rows), device=hidden.device), last]
                return values.detach().float().cpu()
        except LocalEncoderError:
            raise
        except (RuntimeError, NotImplementedError) as exc:
            raise LocalEncoderError("QWEN_LOCAL_INFERENCE_FAILED") from exc

    def embed(self, texts, query=False) -> list[list[float]]:
        texts = _texts(texts)
        with self.tokenization_scope() if query else self._lock:
            started = time.monotonic()
            self.last_diagnostics = {}
            if not texts:
                return []
            ids = self._encode([self.query_instruction + text if query else text for text in texts])
            counts = list(map(len, ids))
            for index, count in enumerate(counts):
                if not count:
                    raise LocalEncoderError("EMBEDDING_EMPTY_TEXT")
                if count > self.max_tokens:
                    raise LocalEncoderError(f"EMBEDDING_INPUT_TOO_LONG:index={index},tokens={count},limit={self.max_tokens}")
            vectors = []
            for start in range(0, len(ids), self.batch_size):
                rows = ids[start:start + self.batch_size]
                values = self._forward(rows)
                if tuple(values.shape) != (len(rows), self.dimension) or not self._torch.isfinite(values).all():
                    raise LocalEncoderError("EMBEDDING_INVALID_OUTPUT")
                norms = self._torch.linalg.vector_norm(values, dim=1)
                if (norms <= 0).any() or not self._torch.isfinite(norms).all():
                    raise LocalEncoderError("EMBEDDING_INVALID_NORM")
                vectors.extend(self._torch.nn.functional.normalize(values, p=2, dim=1).tolist())
            self.last_diagnostics = {"model": self.model_name, "revision": self.revision, "adapter": ADAPTER,
                "device": self.device, "requested_device": self.requested_device, "dtype": self.dtype,
                "normalization_dtype": "float32", "dimensions": self.dimension, "max_tokens": self.max_tokens,
                "input_tokens": counts, "pooling": "last_nonpadding_token", "normalization": "l2",
                "query": bool(query), "truncated": False, "cpu_fallback_reason": None,
                "elapsed_seconds": round(time.monotonic() - started, 6)}
            return vectors

    def close(self):
        with self._lock:
            self._clear_token_memo()
            self._model = self._tokenizer = None
            if self._torch is not None and self.device == "mps":
                self._torch.mps.empty_cache()
            self._torch = self.device = None
            self.verified_files = {}
