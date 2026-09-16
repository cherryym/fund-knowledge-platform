"""Explicit auth-only host wiring; never imports another application's identity."""
from __future__ import annotations

import json
from pathlib import Path

from .codex_bridge import CodexBridge, EncryptedAuthStore, _private_directory, _read_private
from .codex_bridge_config import CodexBridgeConfig
from .providers import ProviderError

FIELDS = {"enabled", "runtime_root", "archive_root", "executable", "executable_sha256",
    "expected_version", "auth_runtime_verified", "browser_callback_reachable", "master_key_file"}


def configured_bridge(settings):
    manifest = settings.codex_bridge_config_file
    if manifest is None:
        return None
    try:
        manifest = Path(manifest)
        _private_directory(manifest.parent)
        data = json.loads(_read_private(manifest, 16384))
        if not isinstance(data, dict) or set(data) != FIELDS:
            raise ValueError()
        for name in ("enabled", "auth_runtime_verified", "browser_callback_reachable"):
            if not isinstance(data[name], bool):
                raise TypeError()
        for name in FIELDS - {"enabled", "auth_runtime_verified", "browser_callback_reachable"}:
            if not isinstance(data[name], str) or not data[name]:
                raise ValueError()
        if data["enabled"] is not True:
            return None
        key_path = Path(data.pop("master_key_file"))
        if key_path != manifest.parent / "archive.key":
            raise ValueError()
        for name in ("runtime_root", "archive_root", "executable"):
            data[name] = Path(data[name])
        if data["runtime_root"] != manifest.parent / "runtimes" \
                or data["archive_root"] != manifest.parent / "archives":
            raise ValueError()
        config = CodexBridgeConfig(**data, forbidden_roots=(settings.storage_dir,))
        # Version/runtime gates precede any key read or directory creation.
        probe = CodexBridge(config, store=object())
        if probe.unavailable_reason:
            raise ProviderError(probe.unavailable_reason)
        store = EncryptedAuthStore(config, _read_private(key_path, 128).strip())
        bridge = CodexBridge(config, store=store)
        if settings.codex_text_profile_file is not None:
            from .codex_text import CodexTextEngine
            if settings.codex_text_profile_file.parent != manifest.parent:
                raise ProviderError("CODEX_TEXT_PROFILE_UNVERIFIED")
            bridge.text_engine = CodexTextEngine(bridge, settings.codex_text_profile_file)
        return bridge
    except ProviderError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise ProviderError("CODEX_HOST_CONFIG_INVALID") from None
