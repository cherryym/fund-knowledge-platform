"""Non-secret, explicitly selected local embedding profile. Never reads API keys."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LocalRetrievalProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    retrieval_mode: Literal["hybrid"] = "hybrid"
    embedding_mode: Literal["fastembed", "transformers"] = "fastembed"
    embedding_model: str = Field(min_length=1, max_length=200)
    embedding_dimensions: int = Field(ge=8, le=65536)
    embedding_model_path: Path
    embedding_cache_dir: Path
    embedding_revision: str = Field(min_length=1, max_length=200)
    embedding_chunk_strategy: Literal["legacy_blocks", "semantic_sections_v2", "semantic_sections_v3"] = "legacy_blocks"
    embedding_max_tokens: int = Field(default=480, ge=64, le=32768)
    embedding_model_max_tokens: int = Field(default=512, ge=64, le=32768)
    embedding_device: Literal["auto", "cpu", "mps"] = "auto"
    embedding_dtype: Literal["float32", "float16", "bfloat16"] = "float32"
    embedding_query_instruction: str = Field(default="", max_length=1000)
    embedding_overlap_tokens: int = Field(default=64, ge=0, le=128)
    embedding_context_tokens: int = Field(default=64, ge=0, le=128)
    embedding_chunk_bytes: int = Field(default=448, ge=64, le=65536)
    embedding_overlap_bytes: int = Field(default=64, ge=0, le=8192)
    embedding_title_bytes: int = Field(default=96, ge=0, le=4096)
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    embedding_threads: int = Field(default=4, ge=1, le=32)
    embedding_allow_downloads: Literal[False] = False
    retrieval_strategy: Literal["version_rrf", "unit_rerank"] = "version_rrf"
    retrieval_unit_candidates: int = Field(default=80, ge=8, le=1000)
    retrieval_seed_units: int = Field(default=16, ge=1, le=200)
    reranker_mode: Literal["disabled", "local"] = "disabled"
    reranker_model: str = ""
    reranker_model_path: Path | None = None
    reranker_revision: str = ""
    reranker_device: Literal["auto", "cpu", "mps"] = "auto"
    reranker_max_tokens: int = Field(default=1024, ge=64, le=32768)
    reranker_batch_size: int = Field(default=8, ge=1, le=128)
    wiki_query_strategy: Literal["interactive", "adaptive", "universal"] = "interactive"
    wiki_query_target_seconds: float = Field(default=20, gt=0, le=600)

    @model_validator(mode="after")
    def budget(self):
        if self.embedding_title_bytes + self.embedding_overlap_bytes + 4 >= self.embedding_chunk_bytes:
            raise ValueError("Invalid embedding text budget")
        if self.embedding_context_tokens + self.embedding_overlap_tokens + 8 >= self.embedding_max_tokens:
            raise ValueError("Invalid semantic embedding token budget")
        if self.embedding_max_tokens > self.embedding_model_max_tokens:
            raise ValueError("Embedding input exceeds model capacity")
        if self.reranker_mode == "local" and not all((self.reranker_model, self.reranker_model_path, self.reranker_revision)):
            raise ValueError("Prepared local reranker required")
        return self


def read_local_profile(path, storage_dir):
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("LOCAL_RETRIEVAL_PROFILE_UNAVAILABLE")
    try:
        profile = LocalRetrievalProfile.model_validate(json.loads(path.read_text(encoding="utf-8")))
        root = Path(storage_dir).resolve()
        for target in (profile.embedding_model_path, profile.embedding_cache_dir,
                       *([profile.reranker_model_path] if profile.reranker_model_path else [])):
            if not target.is_absolute() or not target.resolve().is_relative_to(root):
                raise ValueError("LOCAL_EMBEDDING_PATH_OUTSIDE_STORAGE")
        return profile.model_dump(exclude={"schema_version"}, exclude_none=True)
    except (TypeError, ValueError, OSError):
        raise ValueError("LOCAL_RETRIEVAL_PROFILE_INVALID") from None


def prepared_development_settings(settings):
    """Use the local profile produced by explicit provisioning, never cloud secrets.

    Explicit environment settings and production deployments always win. Source
    initialization remains cheap: this reads a small public JSON, not weights.
    """
    if settings.app_env == "development" and settings.retrieval_profiles_file is None:
        bundle = settings.storage_dir / "retrieval-profiles.json"
        if bundle.is_file():
            settings = settings.model_copy(update={"retrieval_profiles_file": bundle})
    if settings.app_env != "development" or settings.retrieval_profile is not None:
        return settings
    if "retrieval_mode" in settings.model_fields_set or any(
            name.startswith("embedding_") for name in settings.model_fields_set):
        return settings
    path = settings.storage_dir / "retrieval-profile.json"
    if not path.is_file():
        return settings
    return settings.model_copy(update={**read_local_profile(path, settings.storage_dir), "retrieval_profile": path})
