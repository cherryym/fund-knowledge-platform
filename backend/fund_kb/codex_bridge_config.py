"""Explicit host injection only. Importing this module never reads environment/credentials."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ModelUserPolicy:
    env_refs_by_user: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def configure_model_policy(settings, *, env_refs_by_user=None):
    policy = ModelUserPolicy({str(k): tuple(v) for k, v in (env_refs_by_user or {}).items()})
    object.__setattr__(settings, "_model_user_policy", policy)
    return policy


def model_policy(settings):
    return getattr(settings, "_model_user_policy", ModelUserPolicy(
        getattr(settings, "provider_env_refs_by_user", {}) or {}))


@dataclass(frozen=True)
class CodexBridgeConfig:
    enabled: bool = False
    runtime_root: Path | None = None
    archive_root: Path | None = None
    forbidden_roots: tuple[Path, ...] = ()
    executable: Path | None = None
    executable_sha256: str = ""
    expected_version: str = ""
    # Explicit deployment attestation for the auth-only process environment;
    # never grants inference and cannot replace text isolation verification.
    auth_runtime_verified: bool = False
    browser_callback_reachable: bool = False
    login_ttl_seconds: int = 600
    rpc_timeout_seconds: float = 15
    max_rpc_bytes: int = 262144
    max_models: int = 500


def bridge_for(ctx):
    return getattr(ctx.request.app.state, "codex_bridge", None)
