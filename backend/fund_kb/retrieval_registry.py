"""Server-owned embedding profiles, separate vector namespaces and frozen selections.

No credentials in the manifest, no frontend-supplied model paths, and no global
model switch while a worker runs. Source authorization remains request-local.
"""
from __future__ import annotations

import copy
import json
import re
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .retrieval_profile import read_local_profile


class RetrievalProfileError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(code)


class ProfileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    profile_file: Path
    enabled: bool = True


class ProfileManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    default_profile_id: str = Field(min_length=1, max_length=64)
    profiles: list[ProfileEntry] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique(self):
        if len({p.id for p in self.profiles}) != len(self.profiles):
            raise ValueError("Duplicate retrieval profile")
        if self.default_profile_id not in {p.id for p in self.profiles if p.enabled}:
            raise ValueError("Default retrieval profile must be enabled")
        return self


@dataclass(frozen=True)
class RetrievalRuntime:
    id: str
    name: str
    settings: object
    vector: object
    fingerprint: str

    def selection(self):
        return {"profile_id": self.id, "fingerprint": self.fingerprint,
            "model": self.settings.embedding_model, "dimensions": self.settings.embedding_dimensions}


def _can_reuse_vector(vector, settings, fingerprint):
    if vector is None or getattr(vector, "_closed", False):
        return False
    if vector.embedding.fingerprint != fingerprint:
        return False
    original = getattr(vector, "settings", None)
    # The namespace identifies the embedding, not the backend, credentials,
    # local model files or reranker used by this particular client.
    keys = {key for key in type(settings).model_fields if key.startswith(("embedding_", "reranker_"))}
    keys.update({"qdrant_path", "qdrant_url", "qdrant_api_key", "retrieval_strategy",
                 "retrieval_unit_candidates", "retrieval_seed_units", "hybrid_candidate_limit"})
    return original is not None and all(getattr(original, key) == getattr(settings, key) for key in keys)


class RetrievalRegistry:
    def __init__(self, settings, default_vector=None):
        self.settings = settings
        self._default_vector = default_vector
        self._lock = threading.RLock()
        self._runtimes, self._definitions = {}, {}
        self.closed = False
        source = getattr(settings, "retrieval_profiles_file", None)
        if source is None:
            # Explicit single-profile construction is useful to operators/tests;
            # registry_for otherwise keeps the original no-registry behavior.
            ident = {"Qwen/Qwen3-Embedding-4B": "qwen3-4b", "BAAI/bge-m3": "bge-m3"}.get(
                settings.embedding_model, "default")
            self.default_id = ident
            self._definitions[ident] = (settings.embedding_model, None, {})
            return
        try:
            root = Path(settings.storage_dir).resolve()
            path = Path(source)
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or not path.resolve().is_relative_to(root) or path.stat().st_size > 65536):
                raise ValueError("Invalid registry path")
            manifest = ProfileManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
            self.default_id = manifest.default_profile_id
            for entry in manifest.profiles:
                if not entry.enabled:
                    continue
                target = entry.profile_file if entry.profile_file.is_absolute() else root / entry.profile_file
                if target.is_symlink() or target.resolve() != target or not target.is_relative_to(root):
                    raise ValueError("Invalid profile path")
                profile = read_local_profile(target, root)
                self._definitions[entry.id] = (entry.name, target, profile)
        except (ValueError, TypeError, OSError, RuntimeError):
            raise RetrievalProfileError("RETRIEVAL_REGISTRY_INVALID", "检索方案配置不可用，请联系管理员") from None

    def enabled_ids(self):
        return tuple(self._definitions)

    def resolve(self, profile_id=None, *, fingerprint=None):
        from .retrieval import EmbeddingProvider, VectorIndex
        ident = self.default_id if profile_id is None else profile_id
        if not isinstance(ident, str) or ident not in self._definitions:
            raise RetrievalProfileError("RETRIEVAL_PROFILE_NOT_FOUND", "所选检索方案不存在或已停用")
        if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
            raise RetrievalProfileError("RETRIEVAL_PROFILE_CHANGED", "检索方案版本已变化，请重新选择")
        with self._lock:
            if self.closed:
                raise RetrievalProfileError("RETRIEVAL_REGISTRY_CLOSED", "检索服务正在重载，请稍后重试")
            if ident not in self._runtimes:
                name, path, values = self._definitions[ident]
                effective = self.settings.model_copy(update={**values, **({"retrieval_profile": path} if path else {})})
                try:
                    expected = EmbeddingProvider(effective).fingerprint
                except (ValueError, TypeError):
                    raise RetrievalProfileError("RETRIEVAL_REGISTRY_INVALID", "检索方案配置不可用，请联系管理员") from None
                if fingerprint is not None and fingerprint != expected:
                    raise RetrievalProfileError("RETRIEVAL_PROFILE_CHANGED", "检索方案版本已变化，请重新选择")
                vector = self._default_vector
                if not _can_reuse_vector(vector, effective, expected):
                    vector = VectorIndex(effective)
                self._runtimes[ident] = RetrievalRuntime(ident, name, effective, vector, expected)
            runtime = self._runtimes[ident]
            if fingerprint is not None and fingerprint != runtime.fingerprint:
                raise RetrievalProfileError("RETRIEVAL_PROFILE_CHANGED", "检索方案版本已变化，请重新选择")
            if getattr(runtime.vector, "_closed", False):
                raise RetrievalProfileError("RETRIEVAL_REGISTRY_CLOSED", "检索服务正在重载，请稍后重试")
            return runtime

    def freeze(self, selection=None):
        if selection is not None and (not isinstance(selection, dict)
                or set(selection) - {"profile_id", "fingerprint", "model", "dimensions"}
                or not isinstance(selection.get("profile_id"), str)):
            raise RetrievalProfileError("RETRIEVAL_SELECTION_INVALID", "检索方案选择无效")
        selection = selection or {}
        runtime = self.resolve(selection.get("profile_id"), fingerprint=selection.get("fingerprint"))
        frozen = runtime.selection()
        if any(selection.get(key, frozen[key]) != frozen[key] for key in ("model", "dimensions")):
            raise RetrievalProfileError("RETRIEVAL_PROFILE_CHANGED", "检索方案版本已变化，请重新选择")
        return frozen

    def scoped_dispatcher(self, dispatcher, selection):
        frozen = self.freeze(selection)
        runtime = self.resolve(frozen["profile_id"], fingerprint=frozen["fingerprint"])
        view = copy.copy(dispatcher)
        view.settings = runtime.settings
        view.vector_index = runtime.vector
        view.retrieval_registry = self
        view._retrieval_selection = frozen
        view._retrieval_profile_bound = True
        return view

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            seen = set()
            with ExitStack() as cleanup:
                for runtime in self._runtimes.values():
                    if runtime.vector is not self._default_vector and id(runtime.vector) not in seen:
                        cleanup.callback(runtime.vector.close)
                        seen.add(id(runtime.vector))


_REGISTRY_LOCK = threading.RLock()


def registry_for(settings, vector=None):
    if getattr(settings, "retrieval_profiles_file", None) is None or settings.retrieval_mode != "hybrid":
        return None
    with _REGISTRY_LOCK:
        registry = getattr(settings, "_retrieval_registry", None)
        if (registry is None or registry.closed or registry.settings is not settings
                or vector is not None and registry._default_vector is not vector):
            registry = RetrievalRegistry(settings, vector)
            object.__setattr__(settings, "_retrieval_registry", registry)
        return registry
