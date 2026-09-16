"""Provision a NEW application-owned auth store, after real signed-out preflight.

No imports, login, model turns, credentials in stdout, or overwrites are allowed.
Run only after the user explicitly requests this application's OAuth setup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from cryptography.fernet import Fernet

from fund_kb.codex_bridge import (
    VERIFIED_VERSION,
    AuthStdioTransport,
    EncryptedAuthStore,
    _private_directory,
    _write_private,
)
from fund_kb.codex_bridge_config import CodexBridgeConfig
from fund_kb.providers import ProviderError


def provision(root: Path, executable: Path, expected_sha256: str):
    if root.exists() or root.is_symlink():
        raise ProviderError("CODEX_SETUP_TARGET_EXISTS")
    if not executable.is_absolute() or executable.is_symlink() \
            or hashlib.sha256(executable.read_bytes()).hexdigest() != expected_sha256:
        raise ProviderError("CODEX_VERSION_UNSUPPORTED")
    _private_directory(root, create=True)
    key = Fernet.generate_key()
    config = CodexBridgeConfig(enabled=True, runtime_root=root / "runtimes", archive_root=root / "archives",
        executable=executable, executable_sha256=expected_sha256, expected_version=VERIFIED_VERSION,
        auth_runtime_verified=True, browser_callback_reachable=True)
    store = EncryptedAuthStore(config, key)
    runtime = store.materialize(str(uuid4()), str(uuid4()), 0)
    rpc = None
    try:
        rpc = AuthStdioTransport(config, runtime)
        if rpc.call("account/read", {"refreshToken": False}).get("account") is not None:
            raise ProviderError("CODEX_FRESH_IDENTITY_NOT_EMPTY")
    finally:
        if rpc:
            rpc.close()
        store.release(runtime)
    _write_private(root / "archive.key", key)
    data = {"enabled": True, "runtime_root": str(config.runtime_root), "archive_root": str(config.archive_root),
        "executable": str(executable), "expected_version": VERIFIED_VERSION,
        "executable_sha256": expected_sha256, "auth_runtime_verified": True,
        "browser_callback_reachable": True, "master_key_file": str(root / "archive.key")}
    _write_private(root / "host.json", json.dumps(data, indent=2).encode())
    return {"auth_preflight": "PASS", "fresh_identity_signed_out": True,
        "host_config": str(root / "host.json"), "key_created": True, "login_started": False,
        "inference": "BLOCKED_PENDING_SEPARATE_ISOLATION_VALIDATION"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(provision(args.root, args.executable, args.sha256), ensure_ascii=False))
    except (ProviderError, OSError, ValueError) as error:
        print(json.dumps({"status": "FAILED", "code": getattr(error, "code", "CODEX_SETUP_FAILED")}))
        raise SystemExit(1) from None
