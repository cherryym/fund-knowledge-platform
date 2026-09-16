"""Explicit, environment-prefixed configuration. No implicit credential reuse."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .db import normalize_database_url

APPLICATION_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FKB_", extra="ignore", env_file=None,
                                      populate_by_name=True, hide_input_in_errors=True)

    def __init__(self, **values):
        # BaseSettings' source normalization otherwise discards the second alias
        # before model validators can detect a contradictory constructor value.
        self.reject_conflicting_aliases(values)
        super().__init__(**values)

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = Field(default="", repr=False)
    storage_dir: Path = APPLICATION_ROOT / "data"
    qdrant_path: Path = APPLICATION_ROOT / "data" / "qdrant"
    api_prefix: str = "/api/v1"
    job_backend: Literal["local", "celery"] = Field(default="local", validation_alias=AliasChoices(
        "FKB_JOB_BACKEND", "FKB_JOB_MODE", "job_backend", "job_mode"))
    job_workers: int = Field(default=2, ge=1, le=32)
    job_lease_seconds: int = Field(default=120, ge=5)
    job_recovery_interval_seconds: float = Field(default=5, gt=0)
    celery_broker_url: str | None = Field(default=None, repr=False, validation_alias=AliasChoices(
        "FKB_CELERY_BROKER_URL", "FKB_RABBITMQ_URL", "celery_broker_url", "rabbitmq_url"))
    scan_backend: Literal["basic", "clamav", "adapter"] = Field(default="basic", validation_alias=AliasChoices(
        "FKB_SCAN_BACKEND", "FKB_SCAN_MODE", "scan_backend", "scan_mode"))
    clamav_command: str = "clamscan"
    scanner_adapter: str | None = None
    clamav_host: str = "127.0.0.1"
    clamav_port: int = Field(default=3310, ge=1, le=65535)
    storage_backend: Literal["local", "s3"] = "local"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    s3_prefix: str = "fund-kb"
    s3_access_key: str | None = Field(default=None, repr=False, validation_alias=AliasChoices(
        "FKB_S3_ACCESS_KEY", "FKB_S3_ACCESS_KEY_ID", "s3_access_key", "s3_access_key_id"))
    s3_secret_key: str | None = Field(default=None, repr=False, validation_alias=AliasChoices(
        "FKB_S3_SECRET_KEY", "FKB_S3_SECRET_ACCESS_KEY", "s3_secret_key", "s3_secret_access_key"))
    s3_session_token: str | None = Field(default=None, repr=False)
    s3_credential_mode: Literal["explicit", "workload"] = "explicit"
    s3_access_key_ref: str | None = None
    s3_secret_key_ref: str | None = None
    qdrant_url: str | None = None
    qdrant_api_key: str | None = Field(default=None, repr=False)
    retrieval_mode: Literal["wiki", "hybrid"] = "wiki"
    retrieval_profile: Path | None = None
    retrieval_profiles_file: Path | None = None
    provider_master_key: str | None = Field(default=None, repr=False)
    # Explicit opt-in to this application's dedicated OAuth host manifest.
    # No host Codex credentials or implicit env-file discovery.
    codex_bridge_config_file: Path | None = None
    codex_text_profile_file: Path | None = None
    provider_local_hosts: list[str] = Field(default_factory=list)
    # Deployment ceiling, not a second hidden 60-second generation deadline.
    provider_http_timeout_seconds: float = Field(default=300, gt=0, le=600)
    provider_read_idle_timeout_seconds: float = Field(default=180, gt=0, le=600)
    provider_max_request_bytes: int = Field(default=1048576, ge=1024, le=4194304)
    provider_max_response_bytes: int = Field(default=4194304, ge=1024, le=16777216)
    provider_max_models: int = Field(default=1000, ge=1, le=5000)
    embedding_mode: Literal["hashing", "fastembed", "http", "transformers"] = "hashing"
    embedding_model: str = "development-hashing-v1"
    embedding_base_url: str | None = None
    embedding_api_key: str | None = Field(default=None, repr=False)
    embedding_dimensions: int = Field(default=384, ge=8, le=65536)
    embedding_cache_dir: Path | None = None
    embedding_model_path: Path | None = None
    embedding_allow_downloads: bool = False
    embedding_revision: str | None = None
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
    embedding_allow_document_transfer: bool = False
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
    hybrid_candidate_limit: int = Field(default=24, ge=1, le=100)
    hybrid_index_auto_sync: bool = True
    llm_provider: Literal["evidence", "http"] = "evidence"
    llm_base_url: str | None = None
    llm_api_key: str | None = Field(default=None, repr=False)
    llm_model: str | None = None
    # Keyed by evaluation_id. API must compare the complete provider/model/
    # prompt/vector tuple; presence of an arbitrary string is not an approval.
    # Empty by default: no professional quality evaluation is fabricated here.
    approved_model_policies: dict[str, dict] = Field(default_factory=dict)
    model_timeout_seconds: float = Field(default=60, gt=0, le=600)
    answer_planning_timeout_seconds: float = Field(default=90, gt=0, le=600)
    answer_model_timeout_seconds: float = Field(default=180, gt=0, le=600)
    answer_planning_max_output_tokens: int = Field(default=4096, ge=256, le=131072)
    answer_max_output_tokens: int = Field(default=8192, ge=256, le=131072)
    answer_engine: Literal["wiki_reader", "structured"] = "wiki_reader"
    wiki_query_strategy: Literal["interactive", "adaptive", "universal"] = "interactive"
    wiki_query_target_seconds: float = Field(default=20, gt=0, le=600)
    # No application wall-clock/read deadline for interactive Wiki reasoning.
    # Provider context/output capacities still exist; cancellation stays active.
    wiki_answer_max_output_tokens: int = Field(default=16384, ge=256, le=131072)
    wiki_semantic_timeout_seconds: float = Field(default=150, gt=0, le=180)
    auth_mode: Literal["demo", "oidc"] = "demo"
    deployment_admin_subjects: list[str] = Field(default_factory=list)
    cookie_secure: bool = False
    session_ttl_seconds: int = Field(default=28800, ge=60, le=604800)
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = Field(default=None, repr=False)
    allowed_origins: list[str] = ["http://127.0.0.1:5178", "http://localhost:5178"]
    max_file_bytes: int = Field(default=104857600, ge=1, le=104857600)
    auto_create_schema: bool = True

    @property
    def job_mode(self) -> str:
        return self.job_backend

    @property
    def rabbitmq_url(self) -> str:
        return self.celery_broker_url or "amqp://guest:guest@127.0.0.1:5672//"

    @property
    def scan_mode(self) -> str:
        return self.scan_backend

    @property
    def s3_access_key_id(self) -> str | None:
        return self.s3_access_key

    @property
    def s3_secret_access_key(self) -> str | None:
        return self.s3_secret_key

    @model_validator(mode="before")
    @classmethod
    def reject_conflicting_aliases(cls, data):
        if isinstance(data, dict):
            for names in (("job_backend", "job_mode"), ("scan_backend", "scan_mode"),
                          ("celery_broker_url", "rabbitmq_url"), ("s3_access_key", "s3_access_key_id"),
                          ("s3_secret_key", "s3_secret_access_key")):
                values = [data[key] for name in names for key in (name, f"FKB_{name.upper()}") if key in data]
                if values and any(value != values[0] for value in values[1:]):
                    raise ValueError(f"Conflicting configuration aliases for {names[0]}")
        return data

    @model_validator(mode="after")
    def validate_deployment(self):
        if not self.storage_dir.is_absolute():
            self.storage_dir = APPLICATION_ROOT / self.storage_dir
        if self.retrieval_profile is not None:
            from .retrieval_profile import read_local_profile
            profile = read_local_profile(self.retrieval_profile, self.storage_dir)
            for key, value in profile.items():
                if key not in self.model_fields_set:
                    object.__setattr__(self, key, value)
        if self.embedding_title_bytes + self.embedding_overlap_bytes + 4 >= self.embedding_chunk_bytes:
            raise ValueError("Embedding title and overlap must leave room for complete text characters")
        if self.embedding_context_tokens + self.embedding_overlap_tokens + 8 >= self.embedding_max_tokens:
            raise ValueError("Embedding semantic context and overlap must leave room for source text")
        if self.embedding_max_tokens > self.embedding_model_max_tokens:
            raise ValueError("Embedding input exceeds the declared model capacity")
        if self.reranker_mode == "local" and not all((self.reranker_model, self.reranker_model_path, self.reranker_revision)):
            raise ValueError("Local reranking requires a prepared model, path and revision")
        explicit = set(self.model_fields_set)
        if "database_url" not in explicit:
            from sqlalchemy.engine import URL
            self.database_url = URL.create("sqlite", database=str(self.storage_dir / "fund_kb.sqlite3")).render_as_string()
        if "qdrant_path" not in explicit:
            self.qdrant_path = self.storage_dir / "qdrant"
        elif not self.qdrant_path.is_absolute():
            self.qdrant_path = APPLICATION_ROOT / self.qdrant_path
        if not self.api_prefix.startswith("/") or self.api_prefix.endswith("/"):
            raise ValueError("api_prefix must start with / and have no trailing slash")
        if self.storage_backend == "s3":
            if not self.s3_bucket:
                raise ValueError("S3 requires an explicit bucket")
            if self.s3_credential_mode == "explicit" and not (self.s3_access_key and self.s3_secret_key):
                raise ValueError("S3 explicit mode requires this application's access and secret key; no shared profile fallback")
            if self.s3_credential_mode == "workload" and any((self.s3_access_key, self.s3_secret_key, self.s3_session_token)):
                raise ValueError("Choose either explicit S3 credentials or workload identity")
        url = normalize_database_url(self.database_url)
        if self.app_env == "production":
            required = {"database_url", "storage_dir", "allowed_origins", "auth_mode",
                        "cookie_secure", "auto_create_schema", "retrieval_mode"}
            if self.retrieval_mode == "hybrid":
                required |= {"qdrant_url", "embedding_mode", "embedding_model"}
            if required - explicit:
                raise ValueError("Production requires explicit deployment settings: " +
                                 ", ".join(sorted(required - explicit)))
            if url.get_backend_name() == "sqlite":
                raise ValueError("SQLite is a development/test backend only")
            if self.auto_create_schema:
                raise ValueError("Production schema changes require Alembic, not auto_create_schema")
            if self.auth_mode != "oidc" or not all((self.oidc_issuer, self.oidc_client_id, self.oidc_client_secret)):
                raise ValueError("Production requires explicitly configured OIDC")
            if not self.cookie_secure:
                raise ValueError("Production session cookies must be secure")
            if self.retrieval_mode == "hybrid" and self.embedding_mode == "hashing":
                raise ValueError("Development hashing is not production semantic retrieval")
            if self.scan_backend == "basic":
                raise ValueError("Production requires a real scanner; basic only provides development checks")
            if (self.retrieval_mode == "hybrid" and not self.qdrant_url) or not self.allowed_origins or any(
                not origin.startswith("https://") or "*" in origin for origin in self.allowed_origins
            ):
                raise ValueError("Production requires explicit HTTPS origins; hybrid mode also requires remote Qdrant")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
