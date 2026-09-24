"""Pinned Qwen3 yes/no reranking, complete source windows, no generation/tools."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

from .local_encoders import LocalEncoderError, _positive_setting, _texts, _TokenizationMemo, verify_model_file
from .qwen_reranker_spec import DEFAULT_RERANK_INSTRUCTION, QWEN_RERANKER_SPEC

ADAPTER = "qwen3-reranker-yes-no-v1"
BATCHING_STRATEGY = "stable_token_length_ascending_v1"
OUTPUT_PROJECTION = "yes_no_weight_rows_v1"
FULL_OUTPUT_PROJECTION = "full_vocab_last_token_v1"
LABEL_TOKEN_IDS = (9693, 2152)
PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. '
          'Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
SUFFIX = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'


class QwenReranker(_TokenizationMemo):
    """Raw logit(yes)-logit(no), max over all lossless windows per candidate.

    Scores rank relevance only, not source validity or business truth. Full
    queries are repeated in each window. Only document windows may be split;
    every source character remains covered. No auto-download, remote code,
    dtype/device fallback or change to the embedding/index fingerprint.
    """

    batching_strategy = BATCHING_STRATEGY
    output_projection = FULL_OUTPUT_PROJECTION
    projected_output_columns = 0  # Filled from the actual output; empty work projects nothing.

    def __init__(self, settings):
        self.model_name = getattr(settings, "reranker_model", None)
        self.revision = getattr(settings, "reranker_revision", None)
        if (self.model_name, self.revision) != (QWEN_RERANKER_SPEC["repo"], QWEN_RERANKER_SPEC["revision"]):
            raise LocalEncoderError("LOCAL_MODEL_IDENTITY_NOT_PINNED")
        path = getattr(settings, "reranker_model_path", None)
        if not path or not Path(path).is_absolute() or Path(path).resolve() != Path(path) or not Path(path).is_dir():
            raise LocalEncoderError("LOCAL_MODEL_PATH_REQUIRED")
        self.path = Path(path)
        self.max_tokens = _positive_setting(settings, "reranker_max_tokens", 2048)
        if self.max_tokens > 32768:
            raise LocalEncoderError("LOCAL_MODEL_CAPACITY_EXCEEDED")
        self.batch_size = _positive_setting(settings, "reranker_batch_size", 2)
        self.dtype = getattr(settings, "reranker_dtype", "float32")
        self.requested_device = getattr(settings, "reranker_device", "auto")
        if self.dtype not in {"float32", "float16", "bfloat16"} or self.requested_device not in {"auto", "cpu", "mps"}:
            raise LocalEncoderError("INVALID_QWEN_RERANKER_RUNTIME")
        instruction = getattr(settings, "reranker_instruction", "")
        if not isinstance(instruction, str) or len(instruction) > 1000:
            raise LocalEncoderError("INVALID_RERANK_INSTRUCTION")
        self.instruction = instruction or DEFAULT_RERANK_INSTRUCTION
        self._lock = threading.RLock()
        self._tokenizer = self._model = self._torch = None
        self._label_weight = None
        self.device = None
        self.verified_files = {}
        self.last_diagnostics = {}

    def _verify(self, name):
        path = self.path / name
        if path.resolve() != path:
            raise LocalEncoderError("QWEN_MODEL_FILE_PATH_UNSAFE")
        self.verified_files[name] = verify_model_file(self.path, name, QWEN_RERANKER_SPEC["files"][name])

    def _metadata(self):
        forbidden = {"pytorch_model.bin", "pytorch_model.bin.index.json", "adapter_config.json", "added_tokens.json",
                     "additional_chat_templates", "quantization_config.json"}
        for path in self.path.iterdir():
            if path.name not in QWEN_RERANKER_SPEC["files"] and (path.name.endswith(".safetensors") or path.name in forbidden):
                raise LocalEncoderError("QWEN_UNPINNED_LOADER_FILE")
        for name in QWEN_RERANKER_SPEC["files"]:
            if name not in QWEN_RERANKER_SPEC["weights"]:
                self._verify(name)
        config = json.loads((self.path / "config.json").read_text())
        tokenizer = json.loads((self.path / "tokenizer_config.json").read_text())
        index = json.loads((self.path / "model.safetensors.index.json").read_text())
        labels = json.loads((self.path / "1_LogitScore/config.json").read_text())
        if (config.get("model_type") != "qwen3" or config.get("architectures") != ["Qwen3ForCausalLM"]
                or config.get("auto_map") or config.get("quantization_config") or tokenizer.get("auto_map")
                or type(config.get("max_position_embeddings")) is not int
                or config["max_position_embeddings"] < self.max_tokens):
            raise LocalEncoderError("QWEN_UNSUPPORTED_RERANK_ARCHITECTURE")
        weights = index.get("weight_map")
        if not isinstance(weights, dict) or not weights or set(weights.values()) != set(QWEN_RERANKER_SPEC["weights"]):
            raise LocalEncoderError("QWEN_UNPINNED_SHARD_INDEX")
        if labels != {"true_token_id": 9693, "false_token_id": 2152}:
            raise LocalEncoderError("QWEN_RERANK_LABEL_MISMATCH")

    def _load_tokenizer(self):
        if self._tokenizer is None:
            self._metadata()
            from transformers import AutoTokenizer
            tokenizer = AutoTokenizer.from_pretrained(str(self.path), revision=self.revision, local_files_only=True,
                trust_remote_code=False, token=False, use_fast=True, padding_side="left")
            if (not tokenizer.is_fast or tokenizer.padding_side != "left" or type(tokenizer.pad_token_id) is not int
                    or tokenizer.pad_token_id < 0 or tokenizer.convert_tokens_to_ids("yes") != 9693
                    or tokenizer.convert_tokens_to_ids("no") != 2152):
                raise LocalEncoderError("QWEN_INVALID_RERANK_TOKENIZER")
            tokenizer.backend_tokenizer.no_truncation()
            tokenizer.backend_tokenizer.no_padding()
            self._tokenizer = tokenizer
        return self._tokenizer

    def _encode(self, texts):
        return self._memoized_encode(texts, False, self._encode_uncached)

    def _encode_uncached(self, texts):
        encoded = self._load_tokenizer()(texts, add_special_tokens=False, padding=False, truncation=False,
            return_attention_mask=False, return_token_type_ids=False)
        rows = encoded["input_ids"]
        if len(rows) != len(texts) or any(type(v) is not int or v < 0 for row in rows for v in row) \
                or any(e.overflowing for e in getattr(encoded, "encodings", None) or []):
            raise LocalEncoderError("LOCAL_TOKENIZER_ALIGNMENT_FAILED")
        return rows

    def _frames(self, query, text):
        prefix, suffix = self._encode([PREFIX, SUFFIX])
        head = f"<Instruct>: {self.instruction}\n<Query>: {query}\n<Document>: "
        fixed = len(prefix) + len(suffix)
        budget = self.max_tokens - fixed
        header = self._encode([head])[0]
        if len(header) >= budget:
            raise LocalEncoderError("RERANK_QUERY_TOO_LONG")
        complete = self._encode([head + text])[0]
        if len(complete) <= budget:
            return [(prefix + complete + suffix, {"start_char": 0, "end_char": len(text)})]
        # Fast-tokenizer offsets choose source character windows; re-tokenize the
        # complete instructed pair, never concatenate independently encoded words
        # or decode token fragments into altered source text.
        encoded = self._load_tokenizer()(text, add_special_tokens=False, padding=False, truncation=False,
            return_offsets_mapping=True, return_attention_mask=False, return_token_type_ids=False)
        offsets = encoded["offset_mapping"]
        boundaries = sorted({0, len(text), *(int(pair[0]) for pair in offsets), *(int(pair[1]) for pair in offsets)})
        if any(left < 0 or right < left or right > len(text) for left, right in offsets):
            raise LocalEncoderError("QWEN_INVALID_TOKEN_OFFSETS")
        capacity = max(1, budget - len(header))
        frames, start = [], 0
        while start < len(boundaries) - 1:
            end = min(len(boundaries) - 1, start + capacity)
            while end > start:
                body = self._encode([head + text[boundaries[start]:boundaries[end]]])[0]
                if len(body) <= budget:
                    break
                if end == start + 1:
                    raise LocalEncoderError("QWEN_RERANK_WINDOW_CANNOT_FIT")
                end = max(start + 1, end - max(1, len(body) - budget))
            if end <= start:
                raise LocalEncoderError("QWEN_RERANK_WINDOW_CANNOT_FIT")
            frames.append((prefix + body + suffix, {"start_char": boundaries[start], "end_char": boundaries[end]}))
            if end == len(boundaries) - 1:
                break
            start = max(start + 1, end - min(64, (end - start) // 4))
        return frames

    def _load_model(self):
        if self._model is not None:
            return
        self._load_tokenizer()
        self._metadata()
        for name in QWEN_RERANKER_SPEC["weights"]:
            self._verify(name)
        import torch
        from transformers import AutoModelForCausalLM
        self._torch = torch
        if self.requested_device == "cpu":
            self.device = "cpu"
        else:
            if not torch.backends.mps.is_built() or not torch.backends.mps.is_available():
                raise LocalEncoderError("REQUESTED_MPS_UNAVAILABLE")
            if os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK") == "1":
                raise LocalEncoderError("QWEN_MPS_FALLBACK_FORBIDDEN")
            self.device = "mps"
        model = AutoModelForCausalLM.from_pretrained(str(self.path), revision=self.revision, local_files_only=True,
            trust_remote_code=False, token=False, weights_only=True, use_safetensors=True,
            dtype=getattr(torch, self.dtype), attn_implementation="sdpa")
        model.eval().to(self.device)
        if model.config.model_type != "qwen3" or any(
            p.device.type != self.device or p.dtype != getattr(torch, self.dtype)
            for p in model.parameters() if p.is_floating_point()
        ):
            raise LocalEncoderError("QWEN_MODEL_DEVICE_OR_DTYPE_MISMATCH")
        self._model = model

    def _prepare_label_head(self):
        # This pinned causal head is a bias-free linear map. Gather two original
        # rows once, without replacing/mutating the (possibly tied) full weights.
        # Never merge the rows: BF16 logits must round separately before their
        # FP32 subtraction, exactly as in the full-vocabulary scoring contract.
        torch = self._torch
        from transformers.models.qwen3.modeling_qwen3 import Qwen3ForCausalLM, Qwen3Model
        head = getattr(self._model, "lm_head", None)
        if (type(self._model) is not Qwen3ForCausalLM or type(self._model.model) is not Qwen3Model
                or type(head) is not torch.nn.Linear or head.bias is not None
                or head.weight.ndim != 2 or head.weight.shape[0] <= max(LABEL_TOKEN_IDS)
                or head.weight.device.type != self.device
                or head.weight.dtype != getattr(torch, self.dtype)):
            raise LocalEncoderError("QWEN_UNSUPPORTED_RERANK_HEAD")
        with torch.inference_mode():
            indices = torch.tensor(LABEL_TOKEN_IDS, dtype=torch.long, device=head.weight.device)
            self._label_weight = head.weight.detach().index_select(0, indices).contiguous()

    def _label_logits(self, inputs, masks):
        # Production default intentionally retains the original full-vocabulary
        # path. The two-row experiment was numerically equivalent on the tested
        # native weights but did NOT demonstrate an end-to-end performance win.
        output = self._model(input_ids=inputs, attention_mask=masks,
            use_cache=False, return_dict=True, logits_to_keep=1, output_hidden_states=False)
        logits = output.logits
        if logits.ndim != 3 or tuple(logits.shape[:2]) != (len(inputs), 1) or logits.shape[2] <= 9693:
            raise LocalEncoderError("QWEN_RERANK_INVALID_OUTPUT")
        self.projected_output_columns = int(logits.shape[2])
        return logits[:, -1, list(LABEL_TOKEN_IDS)]

    def _experimental_label_logits(self, inputs, masks):
        """Explicit probe-subclass entry only; no Settings/profile opt-in or default activation."""
        if self._label_weight is None:
            self._prepare_label_head()
        # Same decoder, final norm, masks and positions as Qwen3ForCausalLM.
        # Only the final linear output width changes; no intermediate states,
        # sequence positions, document windows or attention work are skipped.
        output = self._model.model(input_ids=inputs, attention_mask=masks,
            use_cache=False, return_dict=True, output_hidden_states=False)
        hidden = output.last_hidden_state
        if (hidden.ndim != 3 or tuple(hidden.shape[:2]) != tuple(inputs.shape)
                or hidden.shape[2] != self._label_weight.shape[1]):
            raise LocalEncoderError("QWEN_RERANK_INVALID_OUTPUT")
        self.projected_output_columns = 2
        return self._torch.nn.functional.linear(hidden[:, -1:, :], self._label_weight)[:, 0, :]

    def _forward(self, rows):
        self._load_model()
        torch = self._torch
        width = max(map(len, rows))
        if width > self.max_tokens:
            raise LocalEncoderError("LOCAL_MODEL_CAPACITY_EXCEEDED")
        inputs = torch.full((len(rows), width), self._tokenizer.pad_token_id, dtype=torch.long)
        masks = torch.zeros_like(inputs)
        for i, row in enumerate(rows):
            inputs[i, width - len(row):] = torch.tensor(row, dtype=torch.long)
            masks[i, width - len(row):] = 1
        with torch.inference_mode():
            values = self._label_logits(inputs.to(self.device), masks.to(self.device))
            if tuple(values.shape) != (len(rows), 2):
                raise LocalEncoderError("QWEN_RERANK_INVALID_OUTPUT")
            values = values.float()
            if not torch.isfinite(values).all():
                raise LocalEncoderError("QWEN_RERANK_INVALID_OUTPUT")
            return (values[:, 0] - values[:, 1]).cpu().tolist()

    def score(self, query, texts):
        return self.score_many([(query, texts)])[0]

    def _ordered_frame_keys(self, unique):
        # Stable ties retain first occurrence in request/document/window order.
        # Keys, not sorted positions, own the scatter mapping. Keep this small
        # scheduling seam so the offline probe can compare insertion order with
        # the same framing, deduplication, forward pass and max aggregation.
        return sorted(unique, key=lambda key: len(key[1]))

    def score_many(self, requests):
        if not isinstance(requests, (list, tuple)):
            raise LocalEncoderError("LOCAL_RERANK_EXPECTS_REQUEST_LIST")
        prepared = []
        for request in requests:
            if not isinstance(request, (list, tuple)) or len(request) != 2 or not isinstance(request[0], str):
                raise LocalEncoderError("LOCAL_RERANK_EXPECTS_QUERY_TEXTS_PAIR")
            prepared.append((request[0], _texts(request[1])))
        with self.tokenization_scope():
            started = time.monotonic()
            scores = [[-math.inf] * len(texts) for _, texts in prepared]
            work, details = [], []
            for request_index, (query, texts) in enumerate(prepared):
                windows = []
                for document_index, text in enumerate(texts):
                    frames = self._frames(query, text)
                    windows.append([dict(location, input_tokens=len(ids)) for ids, location in frames])
                    work.extend((request_index, document_index, ids) for ids, _ in frames)
                details.append({"windows": windows, "window_count": sum(map(len, windows))})
            # Reuse exact token frames only for the exact same query in THIS
            # call. Tokenizer normalization can give distinct queries identical
            # IDs; keep their work separate even in that case. The instruction
            # and full pair framing remain in the IDs. Never cache scores across
            # calls, change a window or omit a candidate/window occurrence.
            unique = {}
            for i, j, ids in work:
                unique.setdefault((prepared[i][0], tuple(ids)), []).append((i, j))
            frames = self._ordered_frame_keys(unique)
            actual_batches = actual_useful = actual_padded = 0
            for offset in range(0, len(frames), self.batch_size):
                keys = frames[offset:offset + self.batch_size]
                batch = [key[1] for key in keys]
                values = self._forward(batch)
                if len(values) != len(batch) or any(isinstance(v, bool) or not isinstance(v, (float, int))
                                                  or not math.isfinite(v) for v in values):
                    raise LocalEncoderError("RERANK_ALIGNMENT_FAILED")
                actual_batches += 1
                actual_useful += sum(map(len, batch))
                actual_padded += len(batch) * max(map(len, batch))
                for key, value in zip(keys, values, strict=True):
                    for i, j in unique[key]:
                        scores[i][j] = max(scores[i][j], float(value))
            if any(not all(math.isfinite(value) for value in row) for row in scores):
                raise LocalEncoderError("RERANK_ALIGNMENT_FAILED")
            self.last_diagnostics = {"model": self.model_name, "revision": self.revision, "adapter": ADAPTER,
                "device": self.device, "dtype": self.dtype, "max_tokens": self.max_tokens, "truncated": False,
                "score_kind": "yes_minus_no_logit", "aggregation": "max_raw_logit_all_windows",
                "request_count": len(prepared), "pair_count": sum(len(texts) for _, texts in prepared),
                "window_count": len(work), "requests": details,
                "computed_window_count": len(frames), "reused_window_count": len(work) - len(frames),
                "batching_strategy": self.batching_strategy,
                "deduplication_strategy": "exact_query_and_token_ids_call_scoped",
                "actual_batch_count": actual_batches, "actual_useful_tokens": actual_useful,
                "actual_padded_tokens": actual_padded,
                "actual_padding_tokens": actual_padded - actual_useful,
                "output_projection": self.output_projection,
                "projected_output_columns": self.projected_output_columns,
                "actual_projected_logits": len(frames) * self.projected_output_columns,
                "elapsed_seconds": round(time.monotonic() - started, 6)}
            return scores

    def close(self):
        with self._lock:
            self._clear_token_memo()
            self._label_weight = None
            self.projected_output_columns = 0
            self._model = self._tokenizer = None
            if self._torch is not None and self.device == "mps":
                self._torch.mps.empty_cache()
            self._torch = self.device = None
            self.verified_files = {}
