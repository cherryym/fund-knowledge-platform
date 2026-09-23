"""Synthetic transfer tests. No local business database/credentials are used."""
import importlib.util
import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("private_transfer", ROOT / "scripts/private-transfer.py")
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "source.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE content_blocks (id TEXT PRIMARY KEY, text TEXT NOT NULL);
          CREATE TABLE resources (id TEXT PRIMARY KEY, deleted_at TEXT);
          CREATE TABLE runtime_policies (id TEXT PRIMARY KEY, name TEXT NOT NULL, config TEXT NOT NULL);
          CREATE TABLE login_sessions (id TEXT PRIMARY KEY, token_hash TEXT, csrf_token TEXT NOT NULL, revoked_at TEXT);
          CREATE TABLE consultation_runs (id TEXT PRIMARY KEY, response TEXT);
        """)
        db.execute("INSERT INTO content_blocks VALUES (?,?)", ("block", "原文完整保留。引用字段不能删减。"))
        db.execute("INSERT INTO resources VALUES (?,?)", ("recycled", "2026-09-01"))
        db.execute("INSERT INTO runtime_policies VALUES (?,?,?)", ("connection", "model-connection:connection", json.dumps({
            "id": "connection", "name": "Synthetic model", "credential_ciphertext": "synthetic-sensitive-cipher",
            "api_key_env": "SYNTHETIC_ONLY_REF", "enabled": True, "status": "READY", "models": [{"id": "synthetic"}]})))
        db.execute("INSERT INTO runtime_policies VALUES (?,?,?)", ("oauth", "model-oauth:connection", json.dumps({
            "state": "AUTHENTICATED", "auth_epoch": 9, "account": {"email": "synthetic@example.invalid"}, "connection_id": "connection"})))
        db.execute("INSERT INTO runtime_policies VALUES (?,?,?)", ("taxonomy", "wiki-taxonomy:space", '{"folders":["估值","核算"]}'))
        db.execute("INSERT INTO login_sessions VALUES (?,?,?,?)", ("session", "synthetic-token-hash", "synthetic-csrf", None))
        db.execute("INSERT INTO consultation_runs VALUES (?,?)", ("history", '{"answer":"保持历史答案","api_key":"synthetic-credential"}'))
    return path


def test_snapshot_never_modifies_source_and_preserves_business(database, tmp_path):
    before = transfer.file_hash(database)
    target = tmp_path / "clean.sqlite3"
    report = transfer.snapshot_database(database, target, export_directory=tmp_path / "logical")
    assert transfer.file_hash(database) == before
    assert report["tables_before"]["content_blocks"] == report["tables_after"]["content_blocks"]
    assert report["tables_before"]["resources"] == report["tables_after"]["resources"]
    assert all(report["tables_before"][key]["rows"] == after["rows"] for key, after in report["tables_after"].items())
    raw = target.read_bytes()
    for excluded in (b"synthetic-sensitive-cipher", b"synthetic-token-hash", b"synthetic-csrf", b"synthetic-credential"):
        assert excluded not in raw
    with sqlite3.connect(target) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        connection = json.loads(db.execute("select config from runtime_policies where id='connection'").fetchone()[0])
        assert connection["enabled"] is False and connection["models"] == [{"id": "synthetic"}]
        assert connection["credential_ciphertext"] is None
        oauth = json.loads(db.execute("select config from runtime_policies where id='oauth'").fetchone()[0])
        assert oauth["state"] == "SIGNED_OUT" and oauth["account"] is None
        assert db.execute("select revoked_at from login_sessions").fetchone()[0]
        assert db.execute("select deleted_at from resources").fetchone()[0] == "2026-09-01"
    assert len(list((tmp_path / "logical").glob("*.jsonl"))) == 5


@pytest.mark.parametrize("path", ["data/private/provider-master.key", "data/private/qdrant-api.key",
                                   "data/codex-text-v3-candidate-x/runs/id/auth.json", ".env", "auth.json"])
def test_credentials_excluded_by_path(path):
    assert transfer.credentials_reason(Path(path))


def test_oauth_library_source_is_not_login_state():
    assert transfer.credentials_reason(Path("backend/.venv/site-packages/provider/oauth/client.py")) is None
    assert transfer.credentials_reason(Path("frontend/node_modules/library/private/implementation.js")) is None


def test_structured_redaction_does_not_strip_business_tokens():
    changes = Counter()
    data = {"api_key": "synthetic", "FKB_QDRANT_API_KEY": "synthetic", "max_output_tokens": 8000,
            "content": "债券处理全文", "authorship": "human", "credential_mode": "encrypted"}
    clean = transfer.scrub(data, changes)
    assert clean["api_key"] is None and clean["FKB_QDRANT_API_KEY"] is None
    assert clean["max_output_tokens"] == 8000 and clean["content"] == data["content"]
    assert clean["credential_mode"] == "encrypted" and len(changes) == 2


def test_authorization_audit_note_is_retained_but_http_bearer_is_removed():
    changes = Counter()
    value = {"authorization": "2026-09-23 用户要求打包，保留审计记录", "headers": {"Authorization": "Bearer SYNTHETIC_ONLY"}}
    cleaned = transfer.scrub(value, changes)
    assert cleaned["authorization"] == value["authorization"]
    assert cleaned["headers"]["Authorization"] is None


def test_json_schema_and_yaml_keys_are_not_oauth_state():
    changes = Counter()
    data = {"state": {"type": "string"}, True: {"description": "schema"}, "password": {"type": "string"}}
    assert transfer.scrub(data, changes) == data
    assert not changes


def test_model_download_cache_is_exactly_reconstructable(tmp_path):
    root = tmp_path / "data/universal-models/qwen/revision"
    partdir = root / ".parts/model.safetensors"
    partdir.mkdir(parents=True)
    canonical = root / "model.safetensors"
    canonical.write_bytes(b"0123456789")
    part = partdir / "000000000002-000000000007.part"
    part.write_bytes(b"23456")
    derived = transfer.derived_model_file(part, tmp_path)
    assert derived["action"] == "DERIVED_EXACT_RANGE" and derived["length"] == 5
    part.write_bytes(b"99999")
    assert transfer.derived_model_file(part, tmp_path) is None


def test_snapshot_refuses_overwrite(database, tmp_path):
    target = tmp_path / "existing.sqlite3"
    target.write_bytes(b"existing")
    with pytest.raises(ValueError, match="SNAPSHOT_EXISTS"):
        transfer.snapshot_database(database, target)
    assert target.read_bytes() == b"existing"
