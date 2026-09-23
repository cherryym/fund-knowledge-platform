"""Rebase sanitized Mac profiles for Linux CPU; do not change vector identity.

Run against a RESTORED COPY only. New files go into production-profiles/ and
never overwrite the original profiles, vectors, database or model weights.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath


def rebase_profile(value, storage="/app/data"):
    from fund_kb.retrieval_profile import LocalRetrievalProfile
    from fund_kb.retrieval import EmbeddingProvider
    from fund_kb.settings import Settings
    original = LocalRetrievalProfile.model_validate(value)
    clean = original.model_dump(mode="json", exclude_none=True)
    for key in ("embedding_model_path", "reranker_model_path"):
        if not clean.get(key):
            continue
        parts = PurePosixPath(clean[key]).parts
        if parts.count("universal-models") != 1 or ".." in parts:
            raise ValueError("MODEL_PATH_NOT_IN_PREPARED_MODEL_TREE")
        tail = parts[parts.index("universal-models"):]
        clean[key] = str(PurePosixPath(storage, *tail))
    clean.update(embedding_cache_dir=str(PurePosixPath(storage, "model-cache")),
                 embedding_device="cpu", reranker_device="cpu")
    before = EmbeddingProvider(Settings.model_construct(**original.model_dump(exclude={"schema_version"}))).fingerprint
    after = EmbeddingProvider(Settings.model_construct(**LocalRetrievalProfile.model_validate(clean).model_dump(exclude={"schema_version"}))).fingerprint
    if before != after:
        raise ValueError("REBASING_CHANGED_EMBEDDING_FINGERPRINT")
    return clean, {"embedding_fingerprint": after, "embedding_model": clean["embedding_model"],
        "embedding_dtype_preserved": clean["embedding_dtype"], "reranker_model": clean["reranker_model"],
        "device": "cpu", "cross_platform_quality_and_speed": "NOT_EVALUATED"}


def prepare(storage):
    storage = Path(storage).resolve()
    destination = storage / "production-profiles"
    if destination.exists():
        raise ValueError("PRODUCTION_PROFILES_EXIST_NO_OVERWRITE")
    registry = json.loads((storage / "retrieval-profiles.json").read_text())
    from fund_kb.retrieval_registry import ProfileManifest
    ProfileManifest.model_validate(registry)
    profiles, receipts = {}, {}
    for item in registry["profiles"]:
        name = PurePosixPath(item["profile_file"]).name
        if name in profiles:
            raise ValueError("PROFILE_FILENAME_COLLISION")
        source = storage / "retrieval-profiles" / name
        if source.is_symlink() or not source.is_file():
            raise ValueError("RESTORED_PROFILE_MISSING")
        profiles[name], receipts[item["id"]] = rebase_profile(json.loads(source.read_text()), str(storage))
        item["profile_file"] = str(destination / name)
    if registry["default_profile_id"] not in receipts:
        raise ValueError("DEFAULT_PROFILE_MISSING")
    destination.mkdir(mode=0o700)
    for name, value in profiles.items():
        (destination / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")
    (destination / "registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf8")
    result = {"status": "PASS", "profiles": receipts, "default_profile_id": registry["default_profile_id"],
        "model_inference_executed": False, "original_profiles_changed": False, "vectors_changed": False}
    (destination / "rebasing-receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", default="/app/data")
    args = parser.parse_args()
    print(json.dumps(prepare(args.storage), ensure_ascii=False))
