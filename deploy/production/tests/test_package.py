"""No database credentials, external model calls, Docker mutations or business data."""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "deploy/production/ops"))
import clamav_scan
import entry


@pytest.fixture
def config(tmp_path, monkeypatch):
    # Restore process environment after each case; never inspect host secrets.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    from cryptography.fernet import Fernet
    document = json.loads((ROOT / "deploy/production/settings.example.json").read_text())
    document["site_url"] = "https://kb.example.invalid"
    document["database"] = {"host": "oracle.example.invalid", "port": 1521, "service_name": "FKB",
                            "username": "FKB_APP", "password": "synthetic$pa@ss:?#\\word"}
    values = document["application"]
    values.update(FKB_ALLOWED_ORIGINS=[document["site_url"]], FKB_OIDC_ISSUER="https://sso.example.invalid",
                  FKB_OIDC_CLIENT_ID="synthetic-client", FKB_OIDC_CLIENT_SECRET="synthetic-only",
                  FKB_DEPLOYMENT_ADMIN_SUBJECTS=["synthetic-admin"], FKB_PROVIDER_MASTER_KEY=Fernet.generate_key().decode(),
                  FKB_CELERY_BROKER_URL="amqp://synthetic:synthetic@broker/fundkb", FKB_QDRANT_API_KEY="synthetic-only",
                  FKB_EMBEDDING_BASE_URL="https://embedding.example.invalid/v1", FKB_EMBEDDING_REVISION="synthetic-pin",
                  FKB_EMBEDDING_ALLOW_DOCUMENT_TRANSFER=True)
    path = tmp_path / "settings.json"
    def write():
        path.write_text(json.dumps(document), encoding="utf-8")
        return path
    return document, write


def test_valid_config_password_round_trip(config):
    from sqlalchemy.engine import make_url
    document, write = config
    _, settings = entry.read_configuration(write())
    assert make_url(settings.database_url).password == document["database"]["password"]
    assert settings.app_env == "production"
    assert settings.retrieval_mode == "hybrid"


@pytest.mark.parametrize("key,value", [
    ("FKB_APP_ENV", "development"), ("FKB_AUTO_CREATE_SCHEMA", True), ("FKB_AUTH_MODE", "demo"),
    ("FKB_COOKIE_SECURE", False), ("FKB_STORAGE_DIR", "/tmp"), ("FKB_JOB_BACKEND", "local"),
    ("FKB_SCAN_BACKEND", "basic"), ("FKB_SCANNER_ADAPTER", "unsafe:scan"),
    ("FKB_EMBEDDING_MODE", "hashing"), ("FKB_EMBEDDING_ALLOW_DOCUMENT_TRANSFER", False),
    ("FKB_EMBEDDING_BASE_URL", "http://untrusted.invalid/v1"),
    ("FKB_EMBEDDING_REVISION", ""), ("FKB_PROVIDER_MASTER_KEY", "bad-key"),
    ("FKB_DEPLOYMENT_ADMIN_SUBJECTS", []), ("FKB_ALLOWED_ORIGINS", ["http://localhost"]),
    ("FKB_EMBEDDING_MODE", "transformers"), ("FKB_RERANKER_MODE", "local"),
    ("FKB_COOKIE_SECURE", "true"), ("FKB_CODEX_BRIDGE_CONFIG_FILE", "/not/portable.json"),
    ("FKB_OIDC_ISSUER", "http://sso.invalid"), ("FKB_UNKNOWN", "bad"),
])
def test_unsafe_or_incomplete_configs_rejected(config, key, value):
    document, write = config
    document["application"][key] = value
    with pytest.raises((RuntimeError, ValueError)):
        entry.read_configuration(write())


def test_placeholder_rejected():
    with pytest.raises(entry.SetupError, match="PLACEHOLDERS"):
        entry.read_configuration(ROOT / "deploy/production/settings.example.json")


def test_duplicate_secret_key_rejected(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(entry.SetupError, match="DUPLICATE"):
        entry.read_configuration(path)


@pytest.mark.parametrize("user", ["SYS", "SYSTEM", "sys"])
def test_system_database_user_rejected(config, user):
    document, write = config
    document["database"]["username"] = user
    with pytest.raises(entry.SetupError, match="DEDICATED_ORACLE"):
        entry.read_configuration(write())


def test_redacted_cli_failure(config, monkeypatch, capsys):
    document, write = config
    monkeypatch.setenv("FKB_CONFIG_FILE", str(write()))
    monkeypatch.setattr(entry, "schema_status", lambda *a, **k: (_ for _ in ()).throw(
        ValueError("secret-value-and-dsn-that-must-not-appear")))
    assert entry.main(["api"]) == 1
    output = capsys.readouterr().out
    assert "secret-value" not in output
    assert document["database"]["password"] not in output
    assert json.loads(output)["code"] == "VALUEERROR"


def test_migration_requires_ack_without_db_access(config, monkeypatch, capsys):
    _, write = config
    monkeypatch.setenv("FKB_CONFIG_FILE", str(write()))
    monkeypatch.setattr(entry, "schema_status", lambda *a, **k: pytest.fail("unexpected database access"))
    assert entry.main(["migrate"]) == 1
    assert "MIGRATION_ACK_REQUIRED" in capsys.readouterr().out


def test_config_check_makes_no_network_calls(config, monkeypatch):
    _, write = config
    monkeypatch.setenv("FKB_CONFIG_FILE", str(write()))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network call"))
    assert entry.main(["config"]) == 0


def scan_server(result, tmp_path, *, payload=b"synthetic\n" * 9000):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    received = bytearray()
    errors = []
    def serve():
        def exact(conn, size):
            data = bytearray()
            while len(data) < size:
                block = conn.recv(size - len(data))
                if not block:
                    raise RuntimeError("client disconnected")
                data.extend(block)
            return bytes(data)
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(3)
                assert exact(connection, 10) == b"zINSTREAM\0"
                while size := struct.unpack("!I", exact(connection, 4))[0]:
                    received.extend(exact(connection, size))
                for part in result:
                    connection.sendall(part)
        except (OSError, RuntimeError, AssertionError) as exc:
            errors.append(exc)
        finally:
            listener.close()
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    path = tmp_path / "synthetic.txt"
    path.write_bytes(payload)
    settings = SimpleNamespace(clamav_host="127.0.0.1", clamav_port=listener.getsockname()[1], max_file_bytes=200000)
    return worker, errors, received, payload, lambda: clamav_scan.scan(path=path, filename=path.name, settings=settings)


@pytest.mark.parametrize("response,expected", [
    ([b"stream: O", b"K\0"], True), ([b"stream: synthetic-test FOUND\0"], False),
])
def test_stream_complete_no_file_path_disclosure(tmp_path, response, expected):
    worker, errors, received, payload, operation = scan_server(response, tmp_path)
    assert operation()["clean"] is expected
    worker.join(4)
    assert not worker.is_alive() and not errors
    assert bytes(received) == payload


@pytest.mark.parametrize("response", [[b"stream: ERROR\0"], [b"unexpected OK\0"], [b"stream: OK"], [b"x" * 9000]])
def test_scanner_never_accepts_partial_or_error_response(tmp_path, response):
    worker, _errors, _received, _payload, operation = scan_server(response, tmp_path)
    with pytest.raises(RuntimeError):
        operation()
    worker.join(4)
    assert not worker.is_alive()


def test_scanner_size_guard_no_network(tmp_path, monkeypatch):
    path = tmp_path / "oversize.txt"
    path.write_bytes(b"12345")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    with pytest.raises(RuntimeError, match="TOO_LARGE"):
        clamav_scan.scan(path=path, filename=path.name, settings=SimpleNamespace(max_file_bytes=4))


def test_compose_production_boundaries():
    data = yaml.safe_load((ROOT / "deploy/production/compose.yaml").read_text())
    services = data["services"]
    assert set(services) == {"api", "worker", "web", "broker", "qdrant", "clamav"}
    for name, spec in services.items():
        assert spec["platform"] == "linux/amd64"
        if name != "web":
            assert "ports" not in spec
    assert services["api"]["read_only"] is True
    assert services["api"]["tmpfs"] == ["/tmp:rw,nosuid,nodev,size=512m"]
    assert services["worker"]["image"] == services["api"]["image"]
    assert services["worker"]["volumes"] == services["api"]["volumes"]
    assert data["networks"]["services"]["internal"] is True
    for name in ("qdrant", "broker", "clamav"):
        assert "@sha256:" in services[name]["image"]


def test_nginx_no_api_exposure_and_large_uploads():
    text = (ROOT / "deploy/production/nginx.conf").read_text()
    assert "listen 8443 ssl;" in text
    assert "client_max_body_size 101m;" in text
    assert "proxy_set_header X-Forwarded-Proto https;" in text
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in text


def test_powershell_no_automatic_destructive_commands():
    text = (ROOT / "deploy/production/Deploy.ps1").read_text()
    for forbidden in ("Invoke-Expression", "Set-ExecutionPolicy", "down','-v", "system prune", "volume rm"):
        assert forbidden not in text
    assert "-AcknowledgeDatabaseChange" in text
    assert "[IO.FileMode]::CreateNew" in text
    assert "config','--quiet" in text


def test_oracle_ddl_compiles_all_metadata_tables_offline():
    import io
    import re

    from alembic import command
    from alembic.config import Config
    from fund_kb import models  # noqa: F401
    from fund_kb.db import Base
    output = io.StringIO()
    config = Config(str(ROOT / "backend/alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    config.attributes["database_url"] = "oracle+oracledb://synthetic:synthetic@invalid/?service_name=synthetic"
    command.upgrade(config, "head", sql=True)
    ddl = output.getvalue()
    created = set(re.findall(r"CREATE TABLE ([a-z_]+)", ddl))
    assert created == set(Base.metadata.tables) | {"alembic_version"}
    assert "CREATE INDEX" in ddl and "FOREIGN KEY" in ddl
    assert "DROP TABLE" not in ddl
