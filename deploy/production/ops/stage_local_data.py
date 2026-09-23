"""Copy an already verified, sanitized transfer into an EMPTY application volume.

No Oracle import, model calls, credential restoration or original-file changes.
Container operator action only; never point --destination at a live data volume.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
from prepare_local_profiles import prepare


def check_sanitized_source(source):
    for name in ("private", "oauth", "identities"):
        if (source / name).exists():
            raise ValueError("CREDENTIAL_DIRECTORY_IN_SOURCE")
    for path in source.rglob("*"):
        if path.is_symlink() or path.name in {"auth.json", "archive.key", "provider-master.key", "qdrant-api.key", ".env"}:
            raise ValueError("CREDENTIAL_OR_SYMLINK_IN_SOURCE")
    database = source / "fund_kb.sqlite3"
    if Path(str(database) + "-wal").exists():
        raise ValueError("SOURCE_MUST_BE_FROZEN_TRANSFER")
    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("SOURCE_DATABASE_INVALID")
        for name, raw in db.execute("SELECT name,config FROM runtime_policies"):
            config = json.loads(raw)
            if name.startswith("model-connection:") and (config.get("enabled") is not False
                    or config.get("credential_ciphertext") or config.get("api_key_env")):
                raise ValueError("SOURCE_MODEL_CREDENTIAL_NOT_CLEARED")
            if name.startswith("model-oauth:") and (config.get("state") != "SIGNED_OUT" or config.get("account")):
                raise ValueError("SOURCE_OAUTH_NOT_CLEARED")
            if name.startswith("agent-access:") and (not config.get("revoked_at") or config.get("token_sha256")):
                raise ValueError("SOURCE_AGENT_ACCESS_NOT_CLEARED")
        if db.execute("SELECT count(*) FROM login_sessions WHERE token_hash IS NOT NULL OR revoked_at IS NULL").fetchone()[0]:
            raise ValueError("SOURCE_SESSIONS_NOT_CLEARED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", default="/app/data", type=Path)
    parser.add_argument("--ack-empty-volume", action="store_true")
    args = parser.parse_args()
    if not args.ack_empty_volume:
        raise ValueError("EMPTY_VOLUME_ACK_REQUIRED")
    source, target = args.source.resolve(), args.destination.resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("SOURCE_DESTINATION_OVERLAP")
    if target != Path("/app/data") or not target.is_dir() or any(target.iterdir()):
        raise ValueError("REQUIRES_EMPTY_APPLICATION_DATA_VOLUME")
    if not (source / "fund_kb.sqlite3").is_file() or not (source / "retrieval-profiles.json").is_file():
        raise ValueError("SANITIZED_TRANSFER_DATA_REQUIRED")
    check_sanitized_source(source)
    shutil.copytree(source, target, dirs_exist_ok=True)
    receipt = prepare(target)
    if os.geteuid() == 0:
        for path in target.rglob("*"):
            os.chown(path, 10001, 10001)
        os.chown(target, 10001, 10001)
    print(json.dumps({**receipt, "oracle_imported": False, "auth_restored": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
