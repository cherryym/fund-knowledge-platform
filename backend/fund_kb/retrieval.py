"""Real Qdrant storage with fail-closed version filtering and Chinese lexical fusion.

Callers supply CURRENT authorized/eligible records, including source dependency
ACL checks. Qdrant is a rebuildable projection, never an authorization authority.
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import logging
import math
import re
import sys
import threading
import time
import unicodedata
from array import array
from collections import Counter, OrderedDict
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5
from weakref import WeakValueDictionary

from .ai_transport import ProviderError, post_json
from .embedding_chunks import split_text_for_embedding
from .ingestion import block_text, text_sha256

_TOKEN_LOCK = threading.RLock()
_TOKENIZER = None
_STOP = {"的", "了", "和", "是", "在", "请", "如何", "什么", "怎么", "是否", "一个", "有哪些", "应该", "进行"}
PROJECTION_SCHEMA = "utf8-chunks-bm25-v1"
SEMANTIC_PROJECTION_SCHEMA = "semantic-sections-spans-v2"
UNIVERSAL_PROJECTION_SCHEMA = "semantic-sections-spans-v3"
_LEGACY_PROJECTION = "legacy-upsert-v1"
_LOCAL_INPUT_BYTES = 448  # Conservative room below BGE's 512-token window, including its special tokens.
_BM25_CACHE_TTL = 60.0
_BM25_CACHE_MAX_ENTRIES = 8
_BM25_CACHE_MAX_BYTES = 256 * 1024 * 1024


def _integer_setting(settings, name: str, default: int, *, minimum: int = 1) -> int:
    value = getattr(settings, name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"INVALID_{name.upper()}")
    return value


def _normalize_vectors(vectors, count: int, dimension: int) -> list[list[float]]:
    """Reject malformed outputs and normalize without overflow/underflow."""
    try:
        if len(vectors) != count:
            raise ProviderError("EMBEDDING_COUNT_MISMATCH")
        normalized = []
        for vector in vectors:
            if isinstance(vector, (str, bytes, dict)):
                raise ProviderError("EMBEDDING_INVALID_VECTOR")
            if len(vector) != dimension:
                raise ProviderError("EMBEDDING_DIMENSION_MISMATCH")
            if any(isinstance(x, bool) or not isinstance(x, Real) for x in vector):
                raise ProviderError("EMBEDDING_INVALID_VECTOR")
            values = [float(x) for x in vector]
            if not all(math.isfinite(x) for x in values):
                raise ProviderError("EMBEDDING_INVALID_VECTOR")
            scale = max(abs(x) for x in values)
            if scale == 0:
                raise ProviderError("EMBEDDING_EMPTY_TEXT")
            scaled = [x / scale for x in values]
            norm = math.sqrt(math.fsum(x * x for x in scaled))
            normalized.append([x / norm for x in scaled])
        return normalized
    except (ValueError, TypeError, OverflowError) as exc:
        raise ProviderError("EMBEDDING_INVALID_VECTOR") from exc


def tokenize(text: str) -> list[str]:
    """Deterministic Chinese segmentation; dictionary loaded in memory without jieba disk cache."""
    global _TOKENIZER
    text = unicodedata.normalize("NFKC", text).lower()
    with _TOKEN_LOCK:
        if _TOKENIZER is None:
            import jieba
            jieba.setLogLevel(logging.WARNING)
            tokenizer = jieba.Tokenizer()
            tokenizer.FREQ, tokenizer.total = tokenizer.gen_pfdict(tokenizer.get_dict_file())
            tokenizer.initialized = True
            _TOKENIZER = tokenizer
        words = list(_TOKENIZER.cut_for_search(text, HMM=False))
    tokens = [w.strip() for w in words if re.search(r"[a-z0-9\u3400-\u9fff]", w)
              and w.strip() not in _STOP]
    # Preserve exact article/product numbers as well as Chinese bigrams for unseen terminology.
    tokens += re.findall(r"[a-z0-9]+(?:[._/-][a-z0-9]+)+", text)
    for phrase in re.findall(r"[\u3400-\u9fff]{2,}", text):
        tokens.extend(phrase[i:i + 2] for i in range(len(phrase) - 1) if phrase[i:i + 2] not in _STOP)
    return tokens


def query_words(text: str) -> list[str]:
    """Atomic search words for routing, without cross-word fallback bigrams.

    The vector/BM25 tokenizer above deliberately retains unknown-word bigrams.
    Those must not make '期货交割' outrank reordered '国债…期货…交割' in a
    graph traversal merely because an artificial word boundary matches.
    """
    tokenize(text)  # Initialize the same in-memory dictionary; no disk cache.
    normal = unicodedata.normalize("NFKC", text).lower()
    with _TOKEN_LOCK:
        words = list(_TOKENIZER.cut_for_search(normal, HMM=False))
    return list(dict.fromkeys(w.strip() for w in words
        if re.search(r"[a-z0-9\u3400-\u9fff]", w) and w.strip() not in _STOP))


def lexical_scores(query: str, candidates: list[dict]) -> list[float]:
    return lexical_scores_many([query], candidates)[0]


def lexical_scores_many(queries: list[str], candidates: list[dict]) -> list[list[float]]:
    """Reuse only this supplied corpus's pure counts, preserving per-query math.

    The caller owns current source/ACL admission. Nothing persists after the
    call, and a query's set iteration and floating-point operation order retain
    the single-query scoring formula.
    """
    all_terms = [set(tokenize(query)) for query in queries]
    if not any(all_terms):
        return [[0.0] * len(candidates) for _ in queries]
    docs = [Counter(tokenize(str(c.get("title", "")) + "\n" + str(c.get("text", "")))) for c in candidates]
    n = len(docs)
    lengths = [sum(d.values()) for d in docs]
    mean_len = sum(lengths) / max(1, n) or 1.0
    df = {term: sum(term in d for d in docs) for term in set().union(*all_terms)}
    output = []
    for query, terms in zip(queries, all_terms, strict=True):
        scores = []
        norm_q = unicodedata.normalize("NFKC", query).strip().lower()
        if not terms:
            output.append([0.0] * n)
            continue
        for record, doc, length in zip(candidates, docs, lengths, strict=True):
            score = 0.0
            for term in terms:
                tf = doc[term]
                if tf:
                    idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / mean_len))
            if norm_q and norm_q in str(record.get("text", "")).lower():
                score += 3.0
            if norm_q and norm_q in str(record.get("title", "")).lower():
                score += 5.0
            scores.append(score)
        output.append(scores)
    return output


class EmbeddingProvider:
    def __init__(self, settings):
        self.settings = settings
        self.mode = str(getattr(settings, "embedding_mode", "hashing"))
        self.model = str(getattr(settings, "embedding_model", "") or "")
        self.is_qwen = self.mode == "transformers" and self.model == "Qwen/Qwen3-Embedding-4B"
        self.dimension = int(getattr(settings, "embedding_dimensions", 384))
        if self.dimension < 8 or self.dimension > 65536:
            raise ValueError("INVALID_EMBEDDING_DIMENSION")
        self._model = None
        self._counting_tokenizer = None
        self._lock = threading.RLock()
        if self.mode not in {"hashing", "http", "fastembed", "transformers"}:
            raise ValueError("UNSUPPORTED_EMBEDDING_MODE")
        if self.mode == "hashing" and getattr(settings, "app_env", "development") == "production":
            raise ValueError("DEVELOPMENT_EMBEDDING_FORBIDDEN")
        # chunk_bytes is the TOTAL input budget, including the bounded title prefix.
        self.chunk_bytes = _integer_setting(settings, "embedding_chunk_bytes", 448)
        self.overlap_bytes = _integer_setting(settings, "embedding_overlap_bytes", 64, minimum=0)
        self.title_bytes = _integer_setting(settings, "embedding_title_bytes", 96, minimum=0)
        self.batch_size = _integer_setting(settings, "embedding_batch_size", 32)
        self.threads = _integer_setting(settings, "embedding_threads", 4)
        self.chunk_strategy = str(getattr(settings, "embedding_chunk_strategy", "legacy_blocks"))
        if self.chunk_strategy not in {"legacy_blocks", "semantic_sections_v2", "semantic_sections_v3"}:
            raise ValueError("INVALID_EMBEDDING_CHUNK_STRATEGY")
        self.semantic = self.chunk_strategy in {"semantic_sections_v2", "semantic_sections_v3"}
        self.max_tokens = _integer_setting(settings, "embedding_max_tokens", 480)
        self.overlap_tokens = _integer_setting(settings, "embedding_overlap_tokens", 64, minimum=0)
        self.context_tokens = _integer_setting(settings, "embedding_context_tokens", 64, minimum=0)
        capacity = (_integer_setting(settings, "embedding_model_max_tokens", 512)
                    if self.chunk_strategy == "semantic_sections_v3" else 480)
        if self.max_tokens > capacity or self.overlap_tokens + self.context_tokens + 8 >= self.max_tokens:
            raise ValueError("INVALID_EMBEDDING_SEMANTIC_TOKEN_BUDGET")
        self.projection_schema = (UNIVERSAL_PROJECTION_SCHEMA if self.chunk_strategy == "semantic_sections_v3" else
                                  SEMANTIC_PROJECTION_SCHEMA if self.semantic else PROJECTION_SCHEMA)
        if self.title_bytes >= self.chunk_bytes or self.overlap_bytes >= self.chunk_bytes - self.title_bytes:
            raise ValueError("INVALID_EMBEDDING_CHUNK_BUDGET")
        if self.mode == "fastembed":
            if self.chunk_strategy == "legacy_blocks" and self.chunk_bytes > _LOCAL_INPUT_BYTES:
                raise ValueError("EMBEDDING_UNSAFE_LOCAL_INPUT_BUDGET")
            if self.model.lower() == "baai/bge-small-zh-v1.5" and self.dimension != 512:
                raise ValueError("EMBEDDING_MODEL_DIMENSION_MISMATCH")
        # Distinct projection namespace for every embedding/preprocessing change.
        spec = {"mode": self.mode, "model": "chinese-hashing-v1" if self.mode == "hashing" else self.model,
                "dimension": self.dimension, "normalization": "l2", "text_version": "block-text-v1",
                "model_revision": getattr(settings, "embedding_revision", None),
                "projection_schema": self.projection_schema, "chunk_bytes": self.chunk_bytes,
                "overlap_bytes": self.overlap_bytes, "title_bytes": self.title_bytes,
                "title_prefix": "utf8-prefix-with-newline-v1", "lexical_version": "jieba-search-bigrams-v1"}
        if self.semantic:
            spec.update(chunk_strategy=self.chunk_strategy, max_tokens=self.max_tokens,
                overlap_tokens=self.overlap_tokens, context_tokens=self.context_tokens,
                text_version="source-section-spans-v2", title_prefix="section-path-token-bounded-v2")
        if self.chunk_strategy == "semantic_sections_v3":
            spec.update(text_version="source-section-spans-v3", structure_version="v3",
                model_max_tokens=capacity, query_instruction=getattr(settings, "embedding_query_instruction", ""),
                model_adapter="local-transformers-cls-v1" if self.mode == "transformers" else self.mode)
        if self.is_qwen:
            # Keep every existing BGE namespace byte-for-byte unchanged. Only
            # Qwen gains an explicit adapter/dtype identity and native dimension.
            from .qwen_embedding import ADAPTER, DIMENSION, DTYPES
            dtype = getattr(settings, "embedding_dtype", "float32")
            if dtype not in DTYPES:
                raise ValueError("INVALID_QWEN_EMBEDDING_DTYPE")
            if self.dimension != DIMENSION:
                raise ValueError("EMBEDDING_MODEL_DIMENSION_MISMATCH")
            spec.update(model_adapter=ADAPTER, dtype=dtype,
                model_max_tokens=getattr(settings, "embedding_model_max_tokens", capacity),
                query_instruction=getattr(settings, "embedding_query_instruction", ""))
        self.fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()

    def _local_transformer(self):
        # Caller holds the provider lock. Constructors remain lazy and never
        # download weights or touch the vector store.
        if self._model is None:
            if self.is_qwen:
                from .qwen_embedding import QwenEmbedding
                self._model = QwenEmbedding(self.settings)
            else:
                from .local_encoders import LocalEmbedding
                self._model = LocalEmbedding(self.settings)
        scope = getattr(self, "_token_scope", None)
        if scope is not None and scope["model"] is not self._model:
            scope["stack"].enter_context(getattr(self._model, "tokenization_scope", nullcontext)())
            scope["model"] = self._model
        return self._model

    def query_token_count(self, text: str) -> int:
        if self.is_qwen:
            with self._lock:
                return self._local_transformer().token_count(text, query=True)
        return self.token_count(text)

    @contextmanager
    def tokenization_scope(self):
        # Match the existing provider -> adapter lock order. A full query batch
        # may nest provider.embed preflight without retaining any cross-call text.
        if self.mode != "transformers":
            yield
            return
        with self._lock:
            if getattr(self, "_token_scope", None) is not None:
                yield
                return
            # Opening a scope must not construct an adapter. In particular,
            # injected/counting-only backends may never need a local model.
            # _local_transformer enters the adapter scope on its first real use.
            with ExitStack() as stack:
                self._token_scope = {"stack": stack, "model": None}
                try:
                    yield
                finally:
                    self._token_scope = None

    def token_count(self, text: str) -> int:
        """Local model tokenizer, without truncation. No embedding or downloads.

        Hashing/test and remote providers use a conservative byte upper bound;
        deployed FastEmbed uses the exact pinned model tokenizer.
        """
        if self.mode == "transformers":
            with self._lock:
                return self._local_transformer().token_count(text)
        if self.mode != "fastembed":
            return len(text.encode("utf-8")) + 2
        with self._lock:
            if self._counting_tokenizer is None:
                from tokenizers import Tokenizer
                model_path = getattr(self.settings, "embedding_model_path", None)
                path = Path(model_path) / "tokenizer.json" if model_path else None
                if path is None or not path.is_file():
                    raise ProviderError("EMBEDDING_LOCAL_TOKENIZER_UNAVAILABLE")
                self._counting_tokenizer = Tokenizer.from_file(str(path))
                self._counting_tokenizer.no_truncation()
                self._counting_tokenizer.no_padding()
            return len(self._counting_tokenizer.encode(text).ids)

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        if self.mode == "hashing":
            vectors = []
            for text in texts:
                vector = [0.0] * self.dimension
                for token, count in Counter(tokenize(text)).items():
                    digest = hashlib.sha256(token.encode()).digest()
                    index = int.from_bytes(digest[:8], "big") % self.dimension
                    vector[index] += (1 if digest[8] & 1 else -1) * (1 + math.log(count))
                vectors.append(vector)
        elif self.mode == "transformers":
            with self.tokenization_scope() if query else self._lock:
                model = self._local_transformer()
                input_limit = (getattr(self.settings, "embedding_model_max_tokens", self.max_tokens)
                               if query else self.max_tokens)
                count = self.query_token_count if query and self.is_qwen else self.token_count
                if any(count(text) > input_limit for text in texts):
                    raise ProviderError("EMBEDDING_INPUT_TOO_LONG")
                vectors = model.embed(texts, query=query)
        elif self.mode == "http":
            if not self.model or not getattr(self.settings, "embedding_base_url", None):
                raise ProviderError("EMBEDDING_NOT_CONFIGURED")
            result = post_json(self.settings.embedding_base_url, "embeddings",
                               {"model": self.model, "input": texts},
                               getattr(self.settings, "embedding_api_key", None),
                               getattr(self.settings, "embedding_timeout_seconds", 20))
            try:
                items = sorted(result["data"], key=lambda item: item["index"])
                if [item["index"] for item in items] != list(range(len(texts))):
                    raise ValueError()
                vectors = [item["embedding"] for item in items]
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError("EMBEDDING_INVALID_RESPONSE") from exc
        else:
            if self.chunk_strategy == "legacy_blocks" and any(len(text.encode("utf-8")) > self.chunk_bytes for text in texts):
                raise ProviderError("EMBEDDING_INPUT_TOO_LONG")
            if self.semantic and any(self.token_count(text) > self.max_tokens for text in texts):
                raise ProviderError("EMBEDDING_INPUT_TOO_LONG")
            # Defaults NEVER initiate downloads, even if a model name is configured.
            cache = getattr(self.settings, "embedding_cache_dir", None)
            model_path = getattr(self.settings, "embedding_model_path", None)
            allow_download = bool(getattr(self.settings, "embedding_allow_downloads", False))
            if (not self.model or (model_path and not Path(model_path).is_dir()) or
                    (not model_path and not allow_download and (not cache or not Path(cache).is_dir()))):
                raise ProviderError("EMBEDDING_LOCAL_MODEL_UNAVAILABLE")
            with self._lock:
                if self._model is None:
                    try:
                        from fastembed import TextEmbedding
                        self._model = TextEmbedding(model_name=self.model, cache_dir=str(cache) if cache else None,
                                                    specific_model_path=str(model_path) if model_path else None,
                                                    local_files_only=True if model_path else not allow_download,
                                                    threads=self.threads)
                    except ImportError as exc:
                        raise ProviderError("FASTEMBED_DEPENDENCY_UNAVAILABLE") from exc
                    except Exception as exc:
                        raise ProviderError("EMBEDDING_LOCAL_MODEL_UNAVAILABLE") from exc
                try:
                    tokenizer = getattr(getattr(self._model, "model", None), "tokenizer", None)
                    if tokenizer is not None and any(e.overflowing for e in tokenizer.encode_batch(texts)):
                        raise ProviderError("EMBEDDING_INPUT_TRUNCATED")
                    operation = self._model.query_embed if query else self._model.passage_embed
                    vectors = [list(v) for v in operation(texts, batch_size=self.batch_size, parallel=None)]
                except ProviderError:
                    raise
                except Exception as exc:
                    raise ProviderError("EMBEDDING_LOCAL_INFERENCE_FAILED") from exc
        return _normalize_vectors(vectors, len(texts), self.dimension)


@dataclass
class _IndexState:
    lock: object = field(default_factory=threading.RLock)
    generation: int = 0
    lexical_cache: OrderedDict = field(default_factory=OrderedDict)
    cache_bytes: int = 0

    def invalidate(self):
        with self.lock:
            self.generation += 1
            self.lexical_cache.clear()
            self.cache_bytes = 0


@dataclass
class _Shared:
    client: object
    lock: object
    references: int = 1
    writers: dict[tuple[str, str, str], object] = field(default_factory=dict)
    state: _IndexState = field(default_factory=_IndexState)
    cache_namespace: object = field(default_factory=object)


_REGISTRY: dict[str, _Shared] = {}
_REGISTRY_LOCK = threading.RLock()
_REMOTE_STATES: WeakValueDictionary = WeakValueDictionary()


def _key(record: dict) -> tuple:
    return str(record.get("version_id", "")), str(record.get("block_id", ""))


def _record(record: dict) -> dict:
    result = dict(record)
    for name in ("resource_id", "version_id", "block_id"):
        try:
            result[name] = str(UUID(str(record[name])))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("INVALID_RECORD_ID") from exc
    result["text"] = str(record.get("text", block_text(record)))
    result["title"] = str(record.get("title", ""))
    result["locator"] = dict(record.get("locator") or {})
    sha = text_sha256(result["text"])
    if record.get("content_sha256") and record["content_sha256"] != sha:
        raise ValueError("CONTENT_HASH_MISMATCH")
    result["content_sha256"] = sha
    # Payload contains typed JSON only, never ORM objects, credentials or arbitrary fields.
    keep = {"resource_id", "version_id", "block_id", "title", "text", "locator", "content_sha256",
            "block_type", "data", "knowledge_type", "required_facts", "applicability", "valid_from", "valid_to",
            "release_id", "source_verified", "access_epoch", "conflict_group", "source_kind"}
    keep.add("ordinal")
    return {k: v for k, v in result.items() if k in keep}


def _projection_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("INVALID_PROJECTION_ID")
    return value


def _field(key: str, value):
    from qdrant_client import models
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def _projection_filter(version_id: str, projection_id: str, *, ready=None):
    from qdrant_client import models
    conditions = [_field("version_id", version_id), _field("projection_id", projection_id)]
    if ready is not None:
        conditions.append(_field("ready", ready))
    return models.Filter(must=conditions)


def _legacy_payload(payload: dict) -> bool:
    # Read compatibility for historical direct Qdrant writers, never for projections.
    return not any(key in payload for key in ("ready", "projection_id", "projection_schema", "chunk_index"))


class VectorIndex:
    def __init__(self, settings):
        from qdrant_client import QdrantClient
        self.settings = settings
        self.embedding = EmbeddingProvider(settings)
        self._reranker = None
        self._reranker_lock = threading.RLock()
        self.collection = "fkb_" + self.embedding.fingerprint[:24]
        self._closed = False
        self._last_error = None
        self._ready = False
        self.mode = "remote" if getattr(settings, "qdrant_url", None) else "local"
        self._registry_key = None
        if self.mode == "remote":
            from .ai_transport import endpoint
            url = str(settings.qdrant_url)
            endpoint(url, "collections")  # TLS/userinfo/URL validation, no requests.
            key = getattr(settings, "qdrant_api_key", None)
            if hasattr(key, "get_secret_value"):
                key = key.get_secret_value()
            self._shared = _Shared(QdrantClient(url=url, api_key=key, timeout=10,
                                               check_compatibility=False, trust_env=False), threading.RLock())
            with _REGISTRY_LOCK:
                # Share invalidation across clients of this backend. Cached rows are
                # additionally namespaced by the client for separate remote credentials.
                state_key = url.rstrip("/")
                if state_key not in _REMOTE_STATES:
                    _REMOTE_STATES[state_key] = self._shared.state
                else:
                    self._shared.state = _REMOTE_STATES[state_key]
        else:
            path = str(getattr(settings, "qdrant_path", ":memory:"))
            if path == ":memory:":
                self._shared = _Shared(QdrantClient(":memory:"), threading.RLock())
            else:
                path = str(Path(path).resolve())
                self._registry_key = path
                with _REGISTRY_LOCK:
                    if path in _REGISTRY:
                        self._shared = _REGISTRY[path]
                        self._shared.references += 1
                    else:
                        kwargs = {"path": path}
                        if "force_disable_check_same_thread" in inspect.signature(QdrantClient).parameters:
                            kwargs["force_disable_check_same_thread"] = True
                        else:
                            raise RuntimeError("QDRANT_CLIENT_CROSS_THREAD_SUPPORT_REQUIRED")
                        self._shared = _Shared(QdrantClient(**kwargs), threading.RLock())
                        _REGISTRY[path] = self._shared

    def _ensure(self, *, create=False):
        from qdrant_client import models
        if self._closed:
            raise RuntimeError("VECTOR_INDEX_CLOSED")
        client = self._shared.client
        if not client.collection_exists(self.collection):
            if not create:
                return False
            client.create_collection(self.collection, vectors_config=models.VectorParams(
                size=self.embedding.dimension, distance=models.Distance.COSINE))
        info = client.get_collection(self.collection)
        config = info.config.params.vectors
        if not isinstance(config, models.VectorParams) or config.size != self.embedding.dimension or config.distance != models.Distance.COSINE:
            raise RuntimeError("VECTOR_COLLECTION_CONFIG_MISMATCH")
        if create and self.mode == "remote":
            # Setup/maintenance writes also repair existing collections. A read
            # never creates indexes; retain every ACL/generation filter instead
            # of dropping unindexed predicates to hide poor query performance.
            fields = {name: models.PayloadSchemaType.KEYWORD for name in
                      ("version_id", "projection_id", "projection_schema", "embedding_fingerprint")}
            fields["ready"] = models.PayloadSchemaType.BOOL
            existing = info.payload_schema or {}
            for name, field_type in fields.items():
                if name not in existing:
                    client.create_payload_index(self.collection, name, field_schema=field_type, wait=True)
        self._ready = True
        return True

    def prepare_collection(self):
        """Explicit write-side setup, including physical payload indexes."""
        with self._shared.lock:
            self._ensure(create=True)

    def _prepare(self, records: list[dict], projection_id: str, *, ready: bool):
        # Freeze only the whitelisted JSON projection, without modifying caller-owned records.
        records = json.loads(json.dumps([_record(r) for r in records], ensure_ascii=False, allow_nan=False))
        if len({_key(r) for r in records}) != len(records):
            raise ValueError("DUPLICATE_PROJECTION_BLOCK")
        if self.embedding.semantic:
            return self._prepare_semantic(records, projection_id, ready=ready)
        prepared = []
        block_chunks = []
        for record in records:
            title = record["title"]
            title.encode("utf-8")  # Reject invalid Unicode even beyond the embedding prefix.
            prefix_chars = []
            prefix_bytes = 1  # The separating newline also consumes the title budget.
            for char in title:
                width = len(char.encode("utf-8"))
                if prefix_bytes + width > self.embedding.title_bytes:
                    break
                prefix_chars.append(char)
                prefix_bytes += width
            prefix = "".join(prefix_chars) + "\n" if prefix_chars else ""
            chunks = split_text_for_embedding(
                record["text"], max_bytes=self.embedding.chunk_bytes - len(prefix.encode("utf-8")),
                overlap_bytes=self.embedding.overlap_bytes,
            )
            title_tf = Counter(tokenize(title))
            block_chunks.append((record, len(chunks)))
            for chunk in chunks:
                tf = title_tf + Counter(tokenize(chunk.text))
                payload = {
                    **record, "text": chunk.text, "projection_id": projection_id, "ready": ready,
                    "projection_schema": PROJECTION_SCHEMA, "embedding_fingerprint": self.embedding.fingerprint,
                    "parent_version_id": record["version_id"], "parent_block_id": record["block_id"],
                    "parent_content_sha256": record["content_sha256"], "chunk_index": chunk.index,
                    "chunk_start": chunk.start, "chunk_end": chunk.end, "start": chunk.start, "end": chunk.end,
                    "chunk_sha256": text_sha256(chunk.text), "bm25_tf": dict(tf), "bm25_length": sum(tf.values()),
                }
                if len(chunks) > 1:
                    # Avoid copying a complete source block into every child; short legacy records keep data.
                    payload.pop("data", None)
                prepared.append((payload, prefix + chunk.text))
        return prepared, block_chunks

    def _prepare_semantic(self, records, projection_id, *, ready):
        from .semantic_embedding import build_semantic_units
        if len({(r["resource_id"], r["version_id"]) for r in records}) > 1:
            raise ValueError("PROJECTION_RECORDS_MUST_SHARE_VERSION")
        units = build_semantic_units(records, token_count=self.embedding.token_count,
            max_tokens=self.embedding.max_tokens, overlap_tokens=self.embedding.overlap_tokens,
            context_tokens=self.embedding.context_tokens,
            **({"structure_version": "v3"} if self.embedding.chunk_strategy == "semantic_sections_v3" else {}))
        originals = {r["block_id"]: r for r in records}
        memberships = Counter()
        coverage = {bid: [] for bid in originals}
        prepared = []
        for unit in units:
            anchor = originals[unit["block_ids"][0]]
            tf = Counter(tokenize(unit["embedding_text"]))
            payload = {**anchor, "text": unit["text"], "projection_id": projection_id, "ready": ready,
                "projection_schema": self.embedding.projection_schema, "embedding_fingerprint": self.embedding.fingerprint,
                "parent_version_id": anchor["version_id"], "parent_block_id": anchor["block_id"],
                "parent_content_sha256": anchor["content_sha256"],
                "chunk_index": unit["unit_index"], "chunk_start": 0, "chunk_end": len(unit["text"]),
                "start": 0, "end": len(unit["text"]), "chunk_sha256": text_sha256(unit["text"]),
                "source_spans": unit["source_spans"], "block_ids": unit["block_ids"],
                "section_id": unit["section_id"], "section_title": unit["section_title"],
                "section_path": unit["section_path"], "unit_id": unit["unit_id"],
                "bm25_tf": dict(tf), "bm25_length": sum(tf.values())}
            payload.pop("data", None)
            self._check_semantic_spans(payload)
            for span in unit["source_spans"]:
                original = originals[span["block_id"]]
                if original["content_sha256"] != span["content_sha256"] or original["text"][span["start"]:span["end"]] != unit["text"][span["text_start"]:span["text_end"]]:
                    raise ValueError("SEMANTIC_SOURCE_SPAN_MISMATCH")
                coverage[span["block_id"]].append((span["start"], span["end"]))
            memberships.update(unit["block_ids"])
            prepared.append((payload, unit["embedding_text"]))
        for bid, original in originals.items():
            cursor = 0
            for start, end in sorted(coverage[bid]):
                if original["text"][cursor:start].strip():
                    raise ValueError("SEMANTIC_SOURCE_COVERAGE_INCOMPLETE")
                cursor = max(cursor, end)
            if original["text"][cursor:].strip():
                raise ValueError("SEMANTIC_SOURCE_COVERAGE_INCOMPLETE")
        # Counts retain every source block, even a whitespace-only source block
        # that intentionally does not create a semantically empty vector.
        return prepared, [(r, memberships[r["block_id"]]) for r in records]

    def _check_semantic_spans(self, payload):
        spans = payload.get("source_spans")
        if not isinstance(spans, list) or not spans or not isinstance(payload.get("section_path"), list):
            raise RuntimeError("VECTOR_SEMANTIC_SPANS_INVALID")
        cursor, ids = 0, []
        for span in spans:
            if not isinstance(span, dict) or not isinstance(span.get("block_id"), str):
                raise RuntimeError("VECTOR_SEMANTIC_SPANS_INVALID")  # noqa: TRY004 - uniform invalid-index error
            try:
                UUID(span["block_id"])
            except ValueError as exc:
                raise RuntimeError("VECTOR_SEMANTIC_SPANS_INVALID") from exc
            a, b, x, y = [span.get(k) for k in ("start", "end", "text_start", "text_end")]
            if (any(type(v) is not int for v in (a, b, x, y)) or a < 0 or b <= a or x < cursor
                    or y <= x or y > len(payload["text"]) or b-a != y-x
                    or type(span.get("ordinal")) is not int
                    or not isinstance(span.get("content_sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", span["content_sha256"])
                    or payload["text"][cursor:x].strip()):
                raise RuntimeError("VECTOR_SEMANTIC_SPANS_INVALID")
            cursor = y
            if span["block_id"] not in ids:
                ids.append(span["block_id"])
        if (payload["text"][cursor:].strip() or ids != payload.get("block_ids")
                or payload["block_id"] != ids[0]
                or payload["content_sha256"] != spans[0]["content_sha256"]):
            raise RuntimeError("VECTOR_SEMANTIC_SPANS_INVALID")

    def _check_writer(self, writer):
        if writer is not None and self._shared.writers.get(writer[0]) is not writer[1]:
            raise RuntimeError("PROJECTION_BUILD_CANCELLED")

    def _mutate(self, operation, *args, **kwargs):
        # Invalidate before AND after an attempt, including uncertain/partial failures.
        self._shared.state.invalidate()
        try:
            return operation(*args, **kwargs)
        finally:
            self._shared.state.invalidate()

    def _write_batches(self, prepared, *, checkpoint=None, progress=None, writer=None):
        from qdrant_client import models
        written = 0
        for start in range(0, len(prepared), self.embedding.batch_size):
            if checkpoint is not None:
                checkpoint()
            batch = prepared[start:start + self.embedding.batch_size]
            vectors = _normalize_vectors(self.embedding.embed([text for _, text in batch]),
                                         len(batch), self.embedding.dimension)
            points = []
            for (payload, _), vector in zip(batch, vectors, strict=True):
                identity = json.dumps([self.collection, payload["version_id"], payload["projection_id"],
                                       payload["block_id"], payload["chunk_index"]])
                points.append(models.PointStruct(id=str(uuid5(NAMESPACE_URL, identity)),
                                                 vector=vector, payload=payload))
            with self._shared.lock:
                self._check_writer(writer)
                self._ensure(create=True)
                self._mutate(self._shared.client.upsert, self.collection, points=points, wait=True)
            written += len(points)
            if progress is not None:
                progress(written)
            if checkpoint is not None:
                checkpoint()
        with self._shared.lock:
            self._check_writer(writer)
            self._last_error = None
        return written

    def stage_version(self, records: list[dict], projection_id: str, *, checkpoint=None, progress=None) -> dict:
        """Write a replaceable, invisible staging projection; cancellation leaves it discardable.

        Callbacks run before/after each persisted batch WITHOUT the shared vector lock.
        An in-process reservation prevents competing writers or premature activation of
        this projection; it is not held as a lock across callbacks or embedding. Callers
        still revalidate their source and receipt under database locks before activation.
        """
        projection_id = _projection_id(projection_id)
        if projection_id == _LEGACY_PROJECTION:
            raise ValueError("RESERVED_PROJECTION_ID")
        prepared, block_chunks = self._prepare(records, projection_id, ready=False)
        versions = {record["version_id"] for record, _ in block_chunks}
        resources = {record["resource_id"] for record, _ in block_chunks}
        if len(versions) > 1 or len(resources) > 1:
            raise ValueError("PROJECTION_RECORDS_MUST_SHARE_VERSION")
        if not block_chunks:
            return {"block_count": 0, "chunk_count": 0}
        version_id = next(iter(versions))
        writer_key = (self.collection, version_id, projection_id)
        token = object()
        with self._shared.lock:
            if writer_key in self._shared.writers:
                raise ValueError("PROJECTION_BUILD_IN_PROGRESS")
            if self._ensure():
                active = self._shared.client.count(self.collection, exact=True,
                            count_filter=_projection_filter(version_id, projection_id, ready=True)).count
                if active:
                    raise ValueError("PROJECTION_ALREADY_ACTIVE")
                self.discard_projection(version_id, projection_id)
            self._shared.writers[writer_key] = token
        try:
            written = self._write_batches(prepared, checkpoint=checkpoint, progress=progress,
                                          writer=(writer_key, token))
        finally:
            with self._shared.lock:
                if self._shared.writers.get(writer_key) is token:
                    del self._shared.writers[writer_key]
        return {"block_count": len(block_chunks), "chunk_count": written}

    def activate_version(self, version_id: str, projection_id: str, expected_chunks: int) -> dict:
        """Validate and mark ready only. Commit the database receipt before pruning old projections."""
        version_id = str(UUID(str(version_id)))
        projection_id = _projection_id(projection_id)
        if isinstance(expected_chunks, bool) or not isinstance(expected_chunks, int) or expected_chunks < 0:
            raise ValueError("INVALID_EXPECTED_CHUNKS")
        target = _projection_filter(version_id, projection_id)
        with self._shared.lock:
            if (self.collection, version_id, projection_id) in self._shared.writers:
                raise ValueError("PROJECTION_BUILD_IN_PROGRESS")
            exists = self._ensure()
            count = self._shared.client.count(self.collection, count_filter=target, exact=True).count if exists else 0
            if count != expected_chunks:
                raise ValueError("PROJECTION_CHUNK_COUNT_MISMATCH")
            if exists:
                offset = None
                while True:
                    points, offset = self._shared.client.scroll(self.collection, scroll_filter=target,
                        offset=offset, limit=256, with_payload=True, with_vectors=False)
                    for point in points:
                        self._check_candidate({**(point.payload or {}), "ready": True}, [version_id], [projection_id])
                    if offset is None:
                        break
                self._mutate(self._shared.client.set_payload, self.collection,
                             payload={"ready": True}, points=target, wait=True)
                ready_count = self._shared.client.count(self.collection, exact=True,
                                count_filter=_projection_filter(version_id, projection_id, ready=True)).count
                if ready_count != expected_chunks:
                    raise RuntimeError("PROJECTION_ACTIVATION_INCOMPLETE")
        return {"chunk_count": count}

    def prune_ready_projections(self, version_id: str, keep_projection_ids: list[str]) -> None:
        """After receipt commit, delete other ready projections of this version/collection.

        The caller must reacquire its database version/receipt lock and derive keep IDs
        from the current committed receipt. An empty keep list explicitly prunes all
        ready projections of this version. Staging points are always preserved.
        """
        from qdrant_client import models
        version_id = str(UUID(str(version_id)))
        if keep_projection_ids is None or isinstance(keep_projection_ids, (str, bytes)):
            raise ValueError("INVALID_KEEP_PROJECTION_IDS")
        keep = sorted({_projection_id(p) for p in keep_projection_ids})
        target = models.Filter(must=[_field("version_id", version_id), _field("ready", True)])
        if keep:
            target.must_not = [models.FieldCondition(key="projection_id", match=models.MatchAny(any=keep))]
        with self._shared.lock:
            if self._ensure():
                if self._shared.client.count(self.collection, count_filter=target, exact=True).count:
                    self._mutate(self._shared.client.delete, self.collection,
                                 points_selector=models.FilterSelector(filter=target), wait=True)

    def projection_is_complete(self, version_id: str, projection_id: str, expected_chunks: int) -> bool:
        """Read-only count/readiness/integrity check, never inferred from collection existence.

        Missing or damaged nonempty projections return False; backend failures propagate.
        For an expected zero-chunk receipt, an absent projection is correctly empty; the
        caller's database receipt, not this zero count, establishes that it was activated.
        """
        version_id = str(UUID(str(version_id)))
        projection_id = _projection_id(projection_id)
        if isinstance(expected_chunks, bool) or not isinstance(expected_chunks, int) or expected_chunks < 0:
            raise ValueError("INVALID_EXPECTED_CHUNKS")
        with self._shared.lock:
            if (self.collection, version_id, projection_id) in self._shared.writers:
                return False
            if not self._ensure():
                return expected_chunks == 0
            target = _projection_filter(version_id, projection_id)
            count = self._shared.client.count(self.collection, count_filter=target, exact=True).count
            if count != expected_chunks:
                return False
            ready = self._shared.client.count(self.collection, exact=True,
                count_filter=_projection_filter(version_id, projection_id, ready=True)).count
            if ready != expected_chunks:
                return False
            offset, seen = None, 0
            while True:
                points, offset = self._shared.client.scroll(self.collection, scroll_filter=target, offset=offset,
                    limit=256, with_payload=True, with_vectors=False)
                for point in points:
                    try:
                        self._check_candidate(point.payload or {}, [version_id], [projection_id])
                    except (RuntimeError, UnicodeError):
                        return False
                    seen += 1
                if offset is None:
                    return seen == expected_chunks

    def complete_projection_ids(self, requirements: list[dict]) -> set[str]:
        """Batch exact ready/total counts, scoped to each requested version/projection.

        Small unambiguous sets use two facets. Large sets use flat indexed filters
        and one metadata-only scroll pass, verifying every returned pair locally.
        This avoids quadratic native filtering across thousands of nested OR pairs.
        No source text or vectors are fetched. This is count/readiness validation; use
        projection_is_complete for an individual payload-integrity check.
        """
        from qdrant_client import models
        if not requirements:
            return set()
        required = {}
        by_projection = {}
        for item in requirements:
            version = str(UUID(str(item["version_id"])))
            projection = _projection_id(item["projection_id"])
            count = item["chunk_count"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("INVALID_EXPECTED_CHUNKS")
            key = (version, projection)
            if key in required and required[key] != count:
                raise ValueError("CONFLICTING_PROJECTION_REQUIREMENTS")
            required[key] = count
            by_projection.setdefault(projection, set()).add(version)
        with self._shared.lock:
            busy = {p for v, p in required if (self.collection, v, p) in self._shared.writers}
            if not self._ensure():
                return {p for p, versions in by_projection.items()
                        if p not in busy and all(required[v, p] == 0 for v in versions)}
            if len(required) > 32 or any(len(versions) > 1 for versions in by_projection.values()):
                allowed_versions = {v for v, p in required}
                target = models.Filter(must=[
                    models.FieldCondition(key="version_id",match=models.MatchAny(any=sorted(allowed_versions))),
                    models.FieldCondition(key="projection_id",match=models.MatchAny(any=sorted(by_projection))),
                ])
                totals, ready, offset = Counter(), Counter(), None
                while True:
                    page, offset = self._shared.client.scroll(self.collection, scroll_filter=target, offset=offset,
                        limit=4096, with_payload=["version_id", "projection_id", "ready", "projection_schema",
                                                 "embedding_fingerprint"], with_vectors=False)
                    for point in page:
                        payload = point.payload or {}
                        pair = (payload.get("version_id"), payload.get("projection_id"))
                        if pair[0] not in allowed_versions or pair[1] not in by_projection:
                            raise RuntimeError("VECTOR_PROJECTION_FILTER_VIOLATION")
                        if pair not in required:
                            # Flat filters intentionally admit cross-products;
                            # only an exact requested pair contributes coverage.
                            continue
                        totals[pair] += 1
                        if (payload.get("ready") is True and payload.get("projection_schema") == self.embedding.projection_schema
                                and payload.get("embedding_fingerprint") == self.embedding.fingerprint):
                            ready[pair] += 1
                    if offset is None:
                        break
                return {p for p, versions in by_projection.items() if p not in busy and
                        all(totals[v, p] == ready[v, p] == required[v, p] for v in versions)}
            target = models.Filter(should=[_projection_filter(v, p) for v, p in required])
            totals = self._shared.client.facet(self.collection, key="projection_id", facet_filter=target,
                                               limit=len(by_projection), exact=True)
            ready_filter = models.Filter(must=[target, self._ready_filter(allow_legacy=False)])
            ready = self._shared.client.facet(self.collection, key="projection_id", facet_filter=ready_filter,
                                              limit=len(by_projection), exact=True)
            totals = {hit.value: hit.count for hit in totals.hits}
            ready = {hit.value: hit.count for hit in ready.hits}
            if (set(totals) | set(ready)) - by_projection.keys():
                raise RuntimeError("VECTOR_PROJECTION_FILTER_VIOLATION")
            return {p for (v, p), count in required.items() if p not in busy and
                    totals.get(p, 0) == ready.get(p, 0) == count}

    def discard_projection(self, version_id: str, projection_id: str) -> None:
        """Delete only this version's ready=false projection, never an active projection."""
        from qdrant_client import models
        target = _projection_filter(str(UUID(str(version_id))), _projection_id(projection_id), ready=False)
        with self._shared.lock:
            exists = self._ensure()
            self._shared.writers.pop((self.collection, str(UUID(str(version_id))), projection_id), None)
            if exists:
                self._mutate(self._shared.client.delete, self.collection,
                             points_selector=models.FilterSelector(filter=target), wait=True)

    def upsert(self, records: list[dict]):
        """Compatibility publishing path: deterministic child IDs, immediately ready, block-scoped updates."""
        from qdrant_client import models
        prepared, block_chunks = self._prepare(records, _LEGACY_PROJECTION, ready=True)
        if not block_chunks:
            return
        with self._shared.lock:
            self._write_batches(prepared)
            if self._ensure():
                for record, chunk_count in block_chunks:
                    stale = _projection_filter(record["version_id"], _LEGACY_PROJECTION)
                    stale.must.extend([_field("block_id", record["block_id"]),
                        models.FieldCondition(key="chunk_index", range=models.Range(gte=chunk_count))])
                    self._mutate(self._shared.client.delete, self.collection,
                                 points_selector=models.FilterSelector(filter=stale), wait=True)

    def _ready_filter(self, *, allow_legacy: bool):
        from qdrant_client import models
        current = models.Filter(must=[_field("ready", True), _field("projection_schema", self.embedding.projection_schema),
                                     _field("embedding_fingerprint", self.embedding.fingerprint)])
        if allow_legacy:
            # Legacy bare points are read only by the old version-only API. A projection ID
            # always requires ready=true; a missing ready flag cannot promote a new projection.
            legacy = models.Filter(must=[models.IsEmptyCondition(is_empty=models.PayloadField(key=key))
                                          for key in ("ready", "projection_id", "projection_schema", "chunk_index")])
            return models.Filter(should=[current, legacy])
        return current

    def _read_filter(self, allowed, projections):
        from qdrant_client import models
        readiness = self._ready_filter(allow_legacy=projections is None)
        conditions = [models.FieldCondition(key="version_id", match=models.MatchAny(any=allowed)), readiness]
        if projections is not None:
            conditions.append(models.FieldCondition(key="projection_id", match=models.MatchAny(any=projections)))
        return models.Filter(must=conditions)

    def _check_candidate(self, payload, allowed, projections):
        if payload.get("version_id") not in allowed:
            raise RuntimeError("VECTOR_ACL_FILTER_VIOLATION")
        if projections is not None and payload.get("projection_id") not in projections:
            raise RuntimeError("VECTOR_PROJECTION_FILTER_VIOLATION")
        if projections is None and _legacy_payload(payload):
            return
        if payload.get("ready") is not True:
            raise RuntimeError("VECTOR_READINESS_FILTER_VIOLATION")
        if (payload.get("projection_schema") != self.embedding.projection_schema or
                payload.get("embedding_fingerprint") != self.embedding.fingerprint):
            raise RuntimeError("VECTOR_PROJECTION_SCHEMA_MISMATCH")
        text = payload.get("text")
        start, end = payload.get("chunk_start"), payload.get("chunk_end")
        if (not isinstance(text, str) or type(start) is not int or type(end) is not int or
                start < 0 or end <= start or end - start != len(text) or
                type(payload.get("chunk_index")) is not int or payload["chunk_index"] < 0 or
                payload.get("start") != start or payload.get("end") != end or
                payload.get("chunk_sha256") != text_sha256(text) or
                payload.get("parent_version_id") != payload.get("version_id") or
                payload.get("parent_block_id") != payload.get("block_id") or
                payload.get("parent_content_sha256") != payload.get("content_sha256")):
            raise RuntimeError("VECTOR_CHUNK_PAYLOAD_INVALID")
        if self.embedding.semantic:
            self._check_semantic_spans(payload)

    def search(self, query: str, allowed_version_ids: list[str], limit: int = 20,
               *, allowed_projection_ids=None) -> list[dict]:
        # Empty ACL never means "all", including an explicitly empty projection ACL.
        if (not allowed_version_ids or (allowed_projection_ids is not None and not allowed_projection_ids) or
                not query.strip() or limit <= 0):
            return []
        projections = None if allowed_projection_ids is None else sorted({_projection_id(p) for p in allowed_projection_ids})
        if projections == []:
            return []
        allowed = sorted({str(UUID(str(v))) for v in allowed_version_ids})
        if not allowed:
            return []
        if self._closed:
            raise RuntimeError("VECTOR_INDEX_CLOSED")
        if not self._ready:
            with self._shared.lock:
                if not self._ensure():
                    return []
        # Embedding has its own model lock. Do not hold the vector-store lock
        # while computing it; BM25 discovery can proceed at the same time.
        vector = self._embed_queries([query])[0]
        with self._shared.lock:
            if not self._ensure():
                return []
            unit_mode = getattr(self.settings, "retrieval_strategy", "version_rrf") == "unit_rerank"
            if self.embedding.semantic and not unit_mode:
                points = self._grouped_query_points(vector, allowed, projections, limit)
            else:
                result = self._shared.client.query_points(collection_name=self.collection, query=vector,
                        query_filter=self._read_filter(allowed, projections),
                        limit=min(1000 if unit_mode else 100, limit), with_payload=True, with_vectors=False)
                points = result.points
            return self._vector_hits(points, allowed, projections)

    def _embed_queries(self, queries: list[str]) -> list[list[float]]:
        """Batch complete query slices, pooling only within their original query."""
        with self.embedding.tokenization_scope():
            return self._embed_query_inputs(queries)

    def _embed_query_inputs(self, queries: list[str]) -> list[list[float]]:
        query_capacity = (getattr(self.settings, "embedding_model_max_tokens", self.embedding.max_tokens)
                          if self.embedding.chunk_strategy == "semantic_sections_v3" else self.embedding.max_tokens)
        inputs, spans = [], []
        for query in queries:
            count = (self.embedding.query_token_count if getattr(self.embedding, "is_qwen", False)
                     else self.embedding.token_count)
            fits_query = count(query) <= query_capacity \
                if self.embedding.semantic else len(query.encode("utf-8")) <= self.embedding.chunk_bytes
            start = len(inputs)
            if fits_query:
                inputs.append(query)
                spans.append((start, len(inputs), None))
            else:
                # Preserve the existing lossless disjoint slices, including the
                # final tail, and the exact byte-weighted fsum pooling formula.
                chunks = split_text_for_embedding(query, max_bytes=self.embedding.chunk_bytes)
                inputs.extend(c.text for c in chunks)
                spans.append((start, len(inputs), [len(c.text.encode("utf-8")) for c in chunks]))
        vectors = []
        for start in range(0, len(inputs), self.embedding.batch_size):
            batch = inputs[start:start + self.embedding.batch_size]
            vectors.extend(_normalize_vectors(self.embedding.embed(batch, query=True), len(batch),
                                              self.embedding.dimension))
        output = []
        for start, end, weights in spans:
            if weights is None:
                output.append(vectors[start])
            else:
                total = sum(weights)
                pooled = [math.fsum(v[i] * w / total for v, w in zip(vectors[start:end], weights, strict=True))
                          for i in range(self.embedding.dimension)]
                output.append(_normalize_vectors([pooled], 1, self.embedding.dimension)[0])
        return output

    def _grouped_query_points(self, vector, allowed, projections, limit):
        # Qdrant QueryRequest has no group_by. Keep the native grouped query;
        # a fixed-size ungrouped fetch could silently change source candidates.
        result = self._shared.client.query_points_groups(collection_name=self.collection, query=vector,
            query_filter=self._read_filter(allowed, projections), group_by="version_id", group_size=3,
            limit=min(100, limit), with_payload=True, with_vectors=False)
        return sorted((hit for group in result.groups for hit in group.hits), key=lambda h: -h.score)

    def _vector_hits(self, points, allowed, projections):
        output = []
        for hit in points:
            payload = hit.payload or {}
            self._check_candidate(payload, allowed, projections)
            if isinstance(hit.score, bool) or not isinstance(hit.score, Real) or not math.isfinite(hit.score):
                raise RuntimeError("VECTOR_INVALID_SCORE")
            output.append({**payload, "score": float(hit.score), "retrieval_channel": "vector",
                           "embedding_mode": self.embedding.mode, "candidate_only": True})
        return output

    def search_many(self, queries: list[str], allowed_version_ids: list[str], limit: int = 20,
                    *, allowed_projection_ids=None) -> list[list[dict]]:
        """Equivalent per-query candidates and ACLs, with batched embeddings/IO.

        Empty queries retain their positions. Every request keeps search's own
        candidate limit and readiness/schema/hash checks; no cross-query top-k
        or deduplication is applied. Grouped version retrieval retains Qdrant's
        native grouping, which its point-batch endpoint cannot express.
        """
        from qdrant_client import models
        if not isinstance(queries, (list, tuple)) or any(not isinstance(q, str) for q in queries):
            raise ValueError("INVALID_SEARCH_QUERIES")
        output = [[] for _ in queries]
        if (not queries or not allowed_version_ids or
                (allowed_projection_ids is not None and not allowed_projection_ids)):
            return output
        active = [(i, query) for i, query in enumerate(queries) if query.strip() and not limit <= 0]
        if not active:
            return output
        projections = None if allowed_projection_ids is None else sorted({_projection_id(p) for p in allowed_projection_ids})
        if projections == []:
            return output
        allowed = sorted({str(UUID(str(v))) for v in allowed_version_ids})
        if not allowed:
            return output
        if self._closed:
            raise RuntimeError("VECTOR_INDEX_CLOSED")
        if not self._ready:
            with self._shared.lock:
                if not self._ensure():
                    return output
        vectors = self._embed_queries([query for _, query in active])
        with self._shared.lock:
            if not self._ensure():
                return output
            unit_mode = getattr(self.settings, "retrieval_strategy", "version_rrf") == "unit_rerank"
            if self.embedding.semantic and not unit_mode:
                for (index, _), vector in zip(active, vectors, strict=True):
                    points = self._grouped_query_points(vector, allowed, projections, limit)
                    output[index] = self._vector_hits(points, allowed, projections)
            else:
                for start in range(0, len(vectors), self.embedding.batch_size):
                    batch = vectors[start:start + self.embedding.batch_size]
                    requests = [models.QueryRequest(query=vector, filter=self._read_filter(allowed, projections),
                        limit=min(1000 if unit_mode else 100, limit), with_payload=True, with_vector=False)
                        for vector in batch]
                    results = self._shared.client.query_batch_points(collection_name=self.collection, requests=requests)
                    if not isinstance(results, (list, tuple)) or len(results) != len(batch):
                        raise RuntimeError("VECTOR_QUERY_BATCH_COUNT_MISMATCH")
                    for (index, _), result in zip(active[start:start + len(batch)], results, strict=True):
                        output[index] = self._vector_hits(result.points, allowed, projections)
            return output

    def _lexical_corpus(self, allowed, projections, cache_key):
        """Caller holds vector lock; cache contains postings/IDs, never source bodies."""
        state = self._shared.state
        key = None
        if cache_key is not None:
            try:
                hash(cache_key)
                key = (self._shared.cache_namespace, self.collection, cache_key, tuple(allowed),
                       None if projections is None else tuple(projections))
            except TypeError:
                pass  # Unhashable external keys safely opt out of caching.
        with state.lock:
            now = time.monotonic()
            for old_key, (created, generation, size, _) in list(state.lexical_cache.items()):
                if generation != state.generation or now - created >= _BM25_CACHE_TTL:
                    del state.lexical_cache[old_key]
                    state.cache_bytes -= size
            if key is not None and key in state.lexical_cache:
                state.lexical_cache.move_to_end(key)
                return state.lexical_cache[key][3]
            generation = state.generation
        rows, lengths, postings = [], [], {}
        allowed_set = set(allowed)
        projection_set = None if projections is None else set(projections)
        offset = None
        # Strict projection readers need only stored term statistics. Legacy readers
        # may have bare points without BM25 payloads, so retain their full-text fallback.
        fields = True if projections is None else ["version_id", "block_id", "projection_id", "ready",
                 "projection_schema", "embedding_fingerprint", "chunk_index", "bm25_tf", "bm25_length"]
        while True:
            page, offset = self._shared.client.scroll(self.collection,
                scroll_filter=self._read_filter(allowed, projections), offset=offset,
                limit=4096, with_payload=fields, with_vectors=False)
            for point in page:
                payload = point.payload or {}
                if payload.get("version_id") not in allowed_set:
                    raise RuntimeError("VECTOR_ACL_FILTER_VIOLATION")
                if projection_set is not None and payload.get("projection_id") not in projection_set:
                    raise RuntimeError("VECTOR_PROJECTION_FILTER_VIOLATION")
                if "text" in payload:
                    self._check_candidate(payload, allowed, projections)
                if _legacy_payload(payload):
                    tf = Counter(tokenize(str(payload.get("title", "")) + "\n" + str(payload.get("text", ""))))
                    length = sum(tf.values())
                else:
                    if payload.get("ready") is not True:
                        raise RuntimeError("VECTOR_READINESS_FILTER_VIOLATION")
                    if (payload.get("projection_schema") != self.embedding.projection_schema or
                            payload.get("embedding_fingerprint") != self.embedding.fingerprint):
                        raise RuntimeError("VECTOR_PROJECTION_SCHEMA_MISMATCH")
                    tf, length = payload.get("bm25_tf"), payload.get("bm25_length")
                if (not isinstance(tf, dict) or type(length) is not int or length < 0 or
                        any(not isinstance(k, str) or type(v) is not int or v <= 0 for k, v in tf.items()) or
                        sum(tf.values()) != length):
                    raise RuntimeError("VECTOR_LEXICAL_PAYLOAD_INVALID")
                row = len(rows)
                rows.append((point.id, payload["version_id"], payload.get("block_id", ""),
                             str(payload.get("projection_id", "")), payload.get("chunk_index", 0)))
                lengths.append(length)
                for term, count in tf.items():
                    if term not in postings:
                        postings[term] = array("Q")
                    postings[term].extend((row, count))
            if offset is None:
                break
        mean_len = sum(lengths) / max(1, len(rows)) or 1.0
        corpus = (rows, lengths, postings, mean_len)
        # Conservative deep accounting (including repeated references) bounds retained memory.
        size = sys.getsizeof(rows) + sys.getsizeof(lengths) + sys.getsizeof(postings)
        size += sum(sys.getsizeof(row) + sum(sys.getsizeof(v) for v in row) for row in rows)
        size += sum(sys.getsizeof(v) for v in lengths)
        for term, entries in postings.items():
            size += sys.getsizeof(term) + sys.getsizeof(entries)
        with state.lock:
            if state.generation != generation:
                raise RuntimeError("VECTOR_INDEX_CHANGED_DURING_READ")
            if key is not None and size <= _BM25_CACHE_MAX_BYTES and _BM25_CACHE_MAX_ENTRIES > 0:
                while state.lexical_cache and (len(state.lexical_cache) >= _BM25_CACHE_MAX_ENTRIES or
                                               state.cache_bytes + size > _BM25_CACHE_MAX_BYTES):
                    _, (_, _, old_size, _) = state.lexical_cache.popitem(last=False)
                    state.cache_bytes -= old_size
                state.lexical_cache[key] = (time.monotonic(), generation, size, corpus)
                state.cache_bytes += size
        return corpus

    def lexical_search(self, query: str, allowed_version_ids: list[str], limit: int = 20,
                       *, allowed_projection_ids=None, cache_key=None) -> list[dict]:
        """Exact BM25 (k1=1.2, b=0.75) over the complete authorized projection corpus.

        A hashable external cache_key enables a scope/collection/client-isolated LRU of
        IDs, lengths and postings, bounded to 8 scopes/256 MiB per backend and a 60s TTL.
        Every index write invalidates the shared generation, including failed attempts;
        TTL handles out-of-band edits. Oversize corpora are scored fully but not retained.
        Only the winning points' payloads are fetched and checked on each query.
        """
        if (not allowed_version_ids or (allowed_projection_ids is not None and not allowed_projection_ids) or
                not query.strip() or limit <= 0):
            return []
        projections = None if allowed_projection_ids is None else sorted({_projection_id(p) for p in allowed_projection_ids})
        if projections == []:
            return []
        allowed = sorted({str(UUID(str(v))) for v in allowed_version_ids})
        terms = set(tokenize(query))
        if not allowed or not terms:
            return []
        with self._shared.lock:
            if not self._ensure():
                return []
            rows, lengths, postings, mean_len = self._lexical_corpus(allowed, projections, cache_key)
            scores = Counter()
            for term in sorted(terms):
                matches = postings.get(term, [])
                df = len(matches) // 2
                idf = math.log1p((len(rows) - df + 0.5) / (df + 0.5))
                for position in range(0, len(matches), 2):
                    row, tf = matches[position], matches[position + 1]
                    scores[row] += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * lengths[row] / mean_len))
            ranked = sorted(scores, key=lambda i: (-scores[i], rows[i][1:]))
            unit_mode = getattr(self.settings, "retrieval_strategy", "version_rrf") == "unit_rerank"
            if self.embedding.semantic and not unit_mode:
                per_page, chosen = Counter(), []
                for i in ranked:
                    vid = rows[i][1]
                    if per_page[vid] >= 3 or vid not in per_page and len(per_page) >= min(100, limit):
                        continue
                    chosen.append(i)
                    per_page[vid] += 1
                ranked = chosen
            else:
                ranked = ranked[:min(1000 if unit_mode else 100, limit)]
            if not ranked:
                return []
            wanted = {rows[i][0]: i for i in ranked}
            hits = self._shared.client.retrieve(self.collection, ids=list(wanted),
                                                with_payload=True, with_vectors=False)
            found = {}
            for hit in hits:
                if hit.id not in wanted:
                    raise RuntimeError("VECTOR_POINT_ID_FILTER_VIOLATION")
                payload = hit.payload or {}
                self._check_candidate(payload, allowed, projections)
                row = wanted[hit.id]
                if (payload.get("version_id"), payload.get("block_id", ""),
                        str(payload.get("projection_id", "")), payload.get("chunk_index", 0)) != rows[row][1:]:
                    raise RuntimeError("VECTOR_CHUNK_PAYLOAD_INVALID")
                found[row] = {**payload, "score": scores[row], "retrieval_channel": "lexical",
                              "lexical_method": "bm25", "candidate_only": True}
            if len(found) != len(wanted):
                self._shared.state.invalidate()
            return [found[i] for i in ranked if i in found]

    def delete_versions(self, version_ids: list[str]):
        from qdrant_client import models
        if not version_ids:
            return
        ids = [str(UUID(str(v))) for v in version_ids]
        with self._shared.lock:
            if self._closed:
                raise RuntimeError("VECTOR_INDEX_CLOSED")
            for writer_key in list(self._shared.writers):
                if writer_key[1] in ids:
                    del self._shared.writers[writer_key]
            # Tombstones apply to older embedding generations too, all owned by
            # this app's fkb_<fingerprint> namespace, not just the current model.
            names = [c.name for c in self._shared.client.get_collections().collections
                     if re.fullmatch(r"fkb_[a-f0-9]{24}", c.name)]
            for name in names:
                self._mutate(self._shared.client.delete, name, points_selector=models.FilterSelector(
                    filter=models.Filter(must=[models.FieldCondition(key="version_id", match=models.MatchAny(any=ids))])),
                    wait=True)

    def status(self) -> dict:
        from qdrant_client import models
        result = {"backend": "qdrant", "mode": self.mode, "database_kind": "QdrantLocal" if self.mode == "local" else "QdrantRemote",
                  "embedding_mode": self.embedding.mode, "development_only": self.embedding.mode == "hashing",
                  "embedding_model": self.embedding.model, "embedding_dimensions": self.embedding.dimension,
                  "retrieval_strategy": getattr(self.settings, "retrieval_strategy", "version_rrf"),
                  "reranking": {"mode": getattr(self.settings, "reranker_mode", "disabled"),
                      "model": getattr(self.settings, "reranker_model", ""),
                      "revision": getattr(self.settings, "reranker_revision", ""),
                      "loaded": self._reranker is not None},
                  "semantic_effectiveness": "NOT_EVALUATED", "embedding_fingerprint": self.embedding.fingerprint,
                  "collection": self.collection, "closed": self._closed,
                  "projection_schema": self.embedding.projection_schema, "embedding_chunk_bytes": self.embedding.chunk_bytes,
                  "embedding_chunk_strategy": self.embedding.chunk_strategy,
                  "embedding_max_tokens": self.embedding.max_tokens,
                  "embedding_overlap_tokens": self.embedding.overlap_tokens,
                  "embedding_context_tokens": self.embedding.context_tokens,
                  "embedding_overlap_bytes": self.embedding.overlap_bytes,
                  "embedding_title_bytes": self.embedding.title_bytes,
                  "embedding_batch_size": self.embedding.batch_size, "embedding_threads": self.embedding.threads}
        if self.embedding.mode == "hashing":
            result["embedding_status"] = "READY_DEVELOPMENT_ONLY"
        elif self.embedding.mode == "http":
            result["embedding_status"] = "CONFIGURED_NOT_VERIFIED" if self.embedding.model and getattr(
                self.settings, "embedding_base_url", None) else "NOT_CONFIGURED"
        else:
            cache = getattr(self.settings, "embedding_cache_dir", None)
            model_path = getattr(self.settings, "embedding_model_path", None)
            if model_path:
                result["embedding_status"] = ("EXPLICIT_LOCAL_FILES_NOT_VERIFIED" if Path(model_path).is_dir()
                                              else "LOCAL_MODEL_UNAVAILABLE")
            else:
                result["embedding_status"] = "LOCAL_FILES_NOT_VERIFIED" if cache and Path(cache).is_dir() else "LOCAL_MODEL_UNAVAILABLE"
            package = "transformers" if self.embedding.mode == "transformers" else "fastembed"
            if importlib.util.find_spec(package) is None:
                result["embedding_status"] = "DEPENDENCY_UNAVAILABLE"
        if self._closed:
            return {**result, "available": False, "status": "CLOSED"}
        try:
            with self._shared.lock:
                collections = self._shared.client.get_collections()
                exists = self.collection in {c.name for c in collections.collections}
                count = self._shared.client.count(self.collection, exact=True).count if exists else 0
                ready = self._shared.client.count(self.collection, exact=True,
                    count_filter=self._ready_filter(allow_legacy=True)).count if exists else 0
                staging = self._shared.client.count(self.collection, exact=True,
                    count_filter=models.Filter(must=[_field("ready", False)])).count if exists else 0
            return {**result, "available": True, "status": "READY" if exists else "EMPTY",
                    "collection_exists": exists, "indexed_blocks": count, "indexed_chunks": count,
                    "ready_chunks": ready, "staging_chunks": staging,
                    "note": "真实Qdrant数据库；hashing为开发特征，未验证语义效果。" if self.embedding.mode == "hashing" else "语义模型需独立语料评测。"}
        except Exception:  # noqa: BLE001 - status must sanitize backend errors without exposing endpoints or credentials.
            return {**result, "available": False, "status": "UNAVAILABLE", "error_code": "VECTOR_BACKEND_UNAVAILABLE"}

    def rerank(self, query, texts):
        """Local complete-input relevance scores; no source/authority decisions."""
        if getattr(self.settings, "reranker_mode", "disabled") != "local":
            return None
        if self._closed:
            raise RuntimeError("VECTOR_INDEX_CLOSED")
        with self._reranker_lock:
            if self._reranker is None:
                from .local_encoders import LocalReranker
                self._reranker = LocalReranker(self.settings)
            scores = self._reranker.score(query, texts)
            if len(scores) != len(texts) or any(not math.isfinite(float(s)) for s in scores):
                raise ProviderError("RERANKER_INVALID_RESPONSE")
            return [float(s) for s in scores]

    def rerank_many(self, requests: list[tuple[str, list[str]]]) -> list[list[float] | None]:
        """Batch local scoring, preserving request and candidate positions.

        Disabled reranking returns one None per request, just like repeated
        rerank calls. Invalid/partial model output fails the complete call.
        """
        if not isinstance(requests, (list, tuple)):
            raise ValueError("INVALID_RERANK_REQUESTS")
        if not requests:
            return []
        if getattr(self.settings, "reranker_mode", "disabled") != "local":
            return [None for _ in requests]
        if self._closed:
            raise RuntimeError("VECTOR_INDEX_CLOSED")
        with self._reranker_lock:
            if self._reranker is None:
                from .local_encoders import LocalReranker
                self._reranker = LocalReranker(self.settings)
            scores = self._reranker.score_many(requests)
            try:
                if not isinstance(scores, (list, tuple)) or len(scores) != len(requests):
                    raise ProviderError("RERANKER_INVALID_RESPONSE")
                output = []
                for row, (_, texts) in zip(scores, requests, strict=True):
                    if (isinstance(row, (str, bytes, dict)) or len(row) != len(texts) or
                            any(isinstance(s, bool) or not isinstance(s, Real) or not math.isfinite(float(s))
                                for s in row)):
                        raise ProviderError("RERANKER_INVALID_RESPONSE")
                    output.append([float(s) for s in row])
                return output
            except (ValueError, TypeError, OverflowError) as exc:
                raise ProviderError("RERANKER_INVALID_RESPONSE") from exc

    def close(self):
        with _REGISTRY_LOCK, self._shared.lock:
            if self._closed:
                return
            self._closed = True
            # Release native ONNX/tokenizer objects while Python locks/runtime
            # are alive, not from nondeterministic interpreter finalization.
            with self.embedding._lock:
                if self.embedding.mode == "transformers" and self.embedding._model is not None:
                    self.embedding._model.close()
                self.embedding._model = None
                self.embedding._counting_tokenizer = None
            with self._reranker_lock:
                if self._reranker is not None:
                    self._reranker.close()
                    self._reranker = None
            self._shared.references -= 1
            if self._shared.references == 0:
                self._shared.client.close()
                if self._registry_key:
                    _REGISTRY.pop(self._registry_key, None)


def rank_evidence(query: str, candidates: list[dict], vector_index: VectorIndex | None = None,
                  limit: int = 12) -> list[dict]:
    if not query.strip() or not candidates or limit <= 0:
        return []
    # Only the caller's current eligible set can be returned, including after vector fusion.
    unique = {_key(c): dict(c) for c in candidates}
    records = list(unique.values())
    lexical = lexical_scores(query, records)
    scores: dict[tuple, float] = {}
    channels: dict[tuple, list[str]] = {}
    for rank, index in enumerate(sorted(range(len(records)), key=lambda i: (-lexical[i], _key(records[i]))), 1):
        if lexical[index] > 0:
            key = _key(records[index])
            scores[key] = 1.0 / (60 + rank)
            channels[key] = ["lexical"]
    if vector_index is not None:
        allowed = sorted({c["version_id"] for c in records})
        try:
            hits = vector_index.search(query, allowed, limit=min(100, max(limit * 3, 20)))
        except Exception:  # noqa: BLE001 - lexical fallback is explicitly labelled and stays inside authorized records.
            # Preserve lexical search, but never silently label it as hybrid success.
            for record in unique.values():
                record["retrieval_warnings"] = ["VECTOR_CHANNEL_UNAVAILABLE"]
            hits = []
        seen_vector_keys = set()
        for rank, hit in enumerate(hits, 1):
            key = _key(hit)
            current = unique.get(key)
            if key in seen_vector_keys or current is None or current.get("content_sha256") != hit.get("content_sha256"):
                continue
            # Hash collisions are not semantic evidence; hashing can reorder only lexical hits.
            if vector_index.embedding.mode == "hashing" and key not in scores:
                continue
            if hit.get("score", 0) <= 0:
                continue
            seen_vector_keys.add(key)
            scores[key] = scores.get(key, 0) + 1.0 / (60 + rank)
            channels.setdefault(key, []).append("vector")
    return [{**unique[key], "score": scores[key], "retrieval_channels": channels[key]}
            for key in sorted(scores, key=lambda k: (-scores[k], k))[:min(limit, 100)]]
