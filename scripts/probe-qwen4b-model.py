"""Explicit synthetic MPS probe; no downloads, generation, credentials or source DB."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fund_kb.local_encoders import LocalEncoderError
from fund_kb.qwen_embedding import ADAPTER, DIMENSION, MAX_TOKENS, QwenEmbedding
from fund_kb.qwen_model_spec import QWEN4B_SPEC, QWEN_QUERY_INSTRUCTION


def probe(settings):
    if settings.embedding_device != "mps" or settings.embedding_dtype != "bfloat16":
        raise LocalEncoderError("PROBE_REQUIRES_MPS_BFLOAT16")
    started = time.monotonic()
    model = QwenEmbedding(settings)
    documents = ["The capital of France is Paris.", "法国的首都是巴黎。",
        "La capital de Francia es París.", "Earth takes approximately one year to orbit the Sun.",
        "法国的首都是巴黎。"]
    queries = ["Which city is the capital of France?", "地球绕太阳一周大约需要多久？"]
    try:
        counts = [model.token_count(text) for text in documents]
        query_counts = [model.token_count(text, query=True) for text in queries]
        vectors = model.embed(documents)
        document_diagnostics = dict(model.last_diagnostics)
        singles = [model.embed([text])[0] for text in documents]
        query_vectors = model.embed(queries, query=True)
        query_diagnostics = dict(model.last_diagnostics)
        if (document_diagnostics["input_tokens"] != counts or query_diagnostics["input_tokens"] != query_counts
                or document_diagnostics["device"] != "mps" or query_diagnostics["device"] != "mps"
                or document_diagnostics["dtype"] != "bfloat16" or query_diagnostics["dtype"] != "bfloat16"):
            raise LocalEncoderError("PROBE_INPUT_OR_DEVICE_MISMATCH")
        for row in vectors + singles + query_vectors:
            if (len(row) != DIMENSION or not all(math.isfinite(value) for value in row)
                    or abs(math.fsum(value * value for value in row) - 1) > 1e-5):
                raise LocalEncoderError("PROBE_INVALID_VECTOR")
        def cosine(a, b):
            return math.fsum(x * y for x, y in zip(a, b, strict=True))
        agreements = [cosine(a, b) for a, b in zip(vectors, singles, strict=True)]
        if min(agreements) < 0.99 or cosine(vectors[1], vectors[4]) < 0.99:
            raise LocalEncoderError("PROBE_BATCH_ALIGNMENT_FAILED")
        ranks = [sorted(range(len(documents)), key=lambda i: -cosine(query, vectors[i]))
                 for query in query_vectors]
        if ranks[0][0] not in {0, 1, 2, 4} or ranks[1][0] != 3:
            raise LocalEncoderError("PROBE_SYNTHETIC_RETRIEVAL_FAILED")
        # Tokenization only: exceeding 32k must fail before a forward, not run
        # a quadratic full-window benchmark as part of this small smoke probe.
        oversized = "synthetic " * (MAX_TOKENS + 1)
        rejected = False
        try:
            model.embed([oversized], query=True)
        except LocalEncoderError as exc:
            if not str(exc).startswith("EMBEDDING_INPUT_TOO_LONG"):
                raise
            rejected = True
        if not rejected:
            raise LocalEncoderError("PROBE_OVERSIZED_INPUT_NOT_REJECTED")
        if set(model.verified_files) != set(QWEN4B_SPEC["files"]):
            raise LocalEncoderError("PROBE_FILE_VERIFICATION_INCOMPLETE")
        return {"status": "PASS", "model": settings.embedding_model, "revision": settings.embedding_revision,
            "adapter": ADAPTER, "device": "mps", "dtype": "bfloat16", "dimensions": DIMENSION,
            "normalization_dtype": "float32", "document_diagnostics": document_diagnostics,
            "query_diagnostics": query_diagnostics, "batch_single_cosine": agreements,
            "duplicate_cosine": cosine(vectors[1], vectors[4]), "synthetic_document_ranks": ranks,
            "oversized_input_rejected": rejected, "files": list(model.verified_files.values()),
            "elapsed_seconds": round(time.monotonic() - started, 3), "generation_model_calls": 0,
            "business_sources_read": False, "index_written": False, "business_accuracy": "NOT_EVALUATED"}
    finally:
        model.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Explicitly load the pinned local weights and run MPS/BF16.")
    parser.add_argument("--model-path", type=Path,
        default=ROOT / "data/universal-models/qwen3-embedding-4b" / QWEN4B_SPEC["revision"])
    parser.add_argument("--batch-size", type=int, default=2, choices=range(1, 17))
    parser.add_argument("--output", type=Path, help="Save the synthetic probe receipt, without credentials or source documents.")
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps({"mode": "plan", "model": QWEN4B_SPEC["repo"], "revision": QWEN4B_SPEC["revision"],
            "device": "mps", "dtype": "bfloat16", "dimensions": DIMENSION, "requires_explicit_run": True,
            "weights_loaded": False, "generation_model_calls": 0}, ensure_ascii=False))
        return 0
    # This is a standalone process. Set before torch/transformers import; never
    # enable implicit MPS-to-CPU operator fallback to obtain a passing probe.
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
    settings = SimpleNamespace(embedding_model=QWEN4B_SPEC["repo"], embedding_revision=QWEN4B_SPEC["revision"],
        embedding_model_path=args.model_path, embedding_dimensions=DIMENSION, embedding_model_max_tokens=MAX_TOKENS,
        embedding_batch_size=args.batch_size, embedding_device="mps", embedding_dtype="bfloat16",
        embedding_query_instruction=QWEN_QUERY_INSTRUCTION)
    try:
        report = probe(settings)
    except LocalEncoderError as exc:
        print(json.dumps({"status": "FAIL", "error_code": str(exc), "generation_model_calls": 0}, ensure_ascii=False))
        return 1
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
