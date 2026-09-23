"""Create NEW non-secret dual-retrieval profiles. No downloads, indexing or overwrites."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from fund_kb.local_encoders import MODEL_SPECS
from fund_kb.qwen_model_spec import QWEN4B_SPEC, QWEN_QUERY_INSTRUCTION
from fund_kb.qwen_reranker_spec import QWEN_RERANKER_SPEC
from fund_kb.retrieval_profile import LocalRetrievalProfile


def profiles(root, device, default, reranker="qwen3-4b"):
    root = Path(root).resolve()
    model_root = root / "universal-models"
    rerank = QWEN_RERANKER_SPEC if reranker == "qwen3-4b" else MODEL_SPECS["reranker"]
    rerank_directory = "qwen3-reranker-4b" if reranker == "qwen3-4b" else "bge-reranker-v2-m3"
    common = {"schema_version": 1, "retrieval_mode": "hybrid", "embedding_mode": "transformers",
        "embedding_chunk_strategy": "semantic_sections_v3", "embedding_max_tokens": 768,
        "embedding_device": device, "embedding_cache_dir": str(model_root / ".cache"),
        "embedding_overlap_tokens": 64, "embedding_context_tokens": 64,
        "embedding_allow_downloads": False, "retrieval_strategy": "unit_rerank",
        "retrieval_unit_candidates": 80, "retrieval_seed_units": 6,
        "reranker_mode": "local", "reranker_model": rerank["repo"], "reranker_revision": rerank["revision"],
        "reranker_model_path": str(model_root / rerank_directory / rerank["revision"]),
        "reranker_device": device,
        "reranker_dtype": "bfloat16" if reranker == "qwen3-4b" and device != "cpu" else "float32",
        "reranker_max_tokens": 2048 if reranker == "qwen3-4b" else 1024,
        "reranker_batch_size": 2 if reranker == "qwen3-4b" else 8,
        "wiki_query_strategy": "universal"}
    qwen, bge = QWEN4B_SPEC, MODEL_SPECS["embedding"]
    values = {
        "qwen3-4b": {**common, "embedding_model": qwen["repo"], "embedding_revision": qwen["revision"],
            "embedding_model_path": str(model_root / "qwen3-embedding-4b" / qwen["revision"]),
            "embedding_model_max_tokens": 32768, "embedding_dimensions": 2560,
            "embedding_dtype": "float32" if device == "cpu" else "bfloat16",
            "embedding_batch_size": 4, "embedding_query_instruction": QWEN_QUERY_INSTRUCTION},
        "bge-m3": {**common, "embedding_model": bge["repo"], "embedding_revision": bge["revision"],
            "embedding_model_path": str(model_root / "bge-m3" / bge["revision"]),
            "embedding_model_max_tokens": 8192, "embedding_dimensions": 1024,
            "embedding_dtype": "float32", "embedding_batch_size": 8},
    }
    for value in values.values(): LocalRetrievalProfile.model_validate(value)
    result = {f"retrieval-profiles/{key}.json": value for key, value in values.items()}
    result["retrieval-profile.json"] = values[default]
    result["retrieval-profiles.json"] = {"schema_version": 1, "default_profile_id": default,
        "profiles": [{"id": key, "name": value["embedding_model"], "profile_file": f"retrieval-profiles/{key}.json"}
            for key, value in values.items()]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Create new profiles only; never overwrite")
    parser.add_argument("--device", choices=["cpu", "mps", "auto"], default="cpu")
    parser.add_argument("--default", choices=["qwen3-4b", "bge-m3"], default="qwen3-4b")
    parser.add_argument("--reranker", choices=["qwen3-4b", "bge-m3"], default="qwen3-4b")
    args = parser.parse_args()
    root = ROOT / "data"
    result = profiles(root, args.device, args.default, args.reranker)
    if args.write:
        if root.is_symlink(): raise RuntimeError("DATA_SYMLINK_FORBIDDEN")
        targets = [root / path for path in result]
        if any(p.exists() or p.is_symlink() or p.parent.is_symlink() for p in targets):
            raise RuntimeError("PROFILE_ALREADY_EXISTS_OR_UNSAFE_REVIEW_MANUALLY")
        for path, value in result.items():
            target = root / path; target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as out: json.dump(value, out, ensure_ascii=False, indent=2)
    print(json.dumps({"mode": "written" if args.write else "plan", "default": args.default, "device": args.device,
        "files": list(result), "models_downloaded": False, "index_built": False,
        "note": "Prepare both pinned embeddings and reranker first; do not start hybrid without them."}))


if __name__ == "__main__": main()
