"""Production-only launcher: JSON config, redacted checks, explicit migrations.

No implicit host credentials, demo fallback, model request or automatic DDL.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


class SetupError(RuntimeError):
    pass


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SetupError("DUPLICATE_CONFIG_KEY")
        result[key] = value
    return result


def read_configuration(path):
    from cryptography.fernet import Fernet
    from fund_kb.settings import Settings
    from sqlalchemy.engine import URL

    document = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=no_duplicate_keys)
    if document.get("schema_version") != 1 or set(document) != {
        "schema_version", "site_url", "database", "application"
    }:
        raise SetupError("CONFIG_SCHEMA_INVALID")
    if re.search(r"CHANGE_ME|GENERATE_ON_INIT", json.dumps(document)):
        raise SetupError("CONFIG_PLACEHOLDERS_REMAIN")
    site = urlsplit(document["site_url"])
    if (site.scheme != "https" or not site.hostname or site.username or site.password
            or site.query or site.fragment or site.path not in ("", "/")):
        raise SetupError("SITE_HTTPS_ORIGIN_REQUIRED")
    database = document["database"]
    if set(database) != {"host", "port", "service_name", "username", "password"}:
        raise SetupError("DATABASE_CONFIG_INVALID")
    if not all(isinstance(database[k], str) and database[k].strip()
               for k in ("host", "service_name", "username", "password")):
        raise SetupError("DATABASE_FIELDS_REQUIRED")
    if not isinstance(database["port"], int) or not 1 <= database["port"] <= 65535:
        raise SetupError("DATABASE_PORT_INVALID")
    if database["username"].upper() in {"SYS", "SYSTEM"}:
        raise SetupError("DEDICATED_ORACLE_SCHEMA_REQUIRED")
    values = dict(document["application"])
    allowed = {"FKB_" + key.upper() for key in Settings.model_fields}
    if set(values) - allowed or "FKB_DATABASE_URL" in values:
        raise SetupError("UNKNOWN_OR_DUPLICATE_APPLICATION_SETTING")
    required = {
        "FKB_APP_ENV": "production", "FKB_AUTO_CREATE_SCHEMA": False,
        "FKB_AUTH_MODE": "oidc", "FKB_COOKIE_SECURE": True,
        "FKB_STORAGE_DIR": "/app/data", "FKB_STORAGE_BACKEND": "local",
        "FKB_JOB_BACKEND": "celery", "FKB_SCAN_BACKEND": "adapter",
        "FKB_SCANNER_ADAPTER": "clamav_scan:scan",
    }
    for key, expected in required.items():
        if values.get(key) != expected or type(values[key]) is not type(expected):
            raise SetupError("PRODUCTION_INVARIANT_" + key)
    if values.get("FKB_ALLOWED_ORIGINS") != [document["site_url"].rstrip("/")]:
        raise SetupError("SITE_ORIGIN_MISMATCH")
    if not values.get("FKB_DEPLOYMENT_ADMIN_SUBJECTS"):
        raise SetupError("OIDC_ADMIN_SUBJECT_REQUIRED")
    try:
        Fernet(values["FKB_PROVIDER_MASTER_KEY"].encode("ascii"))
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise SetupError("MASTER_KEY_INVALID") from None
    if not values.get("FKB_CELERY_BROKER_URL") or not values.get("FKB_QDRANT_API_KEY"):
        raise SetupError("SERVICE_CREDENTIAL_REQUIRED")
    # Local mode loads an explicitly rebased profile, never a Mac absolute path
    # or an inherited local-development configuration.
    if values.get("FKB_RETRIEVAL_MODE") == "hybrid":
        mode = values.get("FKB_EMBEDDING_MODE")
        if mode == "transformers":
            from fund_kb.retrieval_profile import read_local_profile
            profile = values.get("FKB_RETRIEVAL_PROFILE")
            registry = values.get("FKB_RETRIEVAL_PROFILES_FILE")
            if (not isinstance(profile, str) or not profile.startswith("/app/data/production-profiles/")
                    or registry != "/app/data/production-profiles/registry.json"
                    or ".." in Path(profile).parts):
                raise SetupError("PREPARED_PRODUCTION_PROFILE_REQUIRED")
            loaded = read_local_profile(profile, Path("/app/data"))
            if (loaded["embedding_mode"] != "transformers" or loaded["embedding_device"] != "cpu"
                    or loaded["reranker_device"] != "cpu" or loaded["reranker_mode"] != "local"):
                raise SetupError("EXPLICIT_CPU_FULL_RETRIEVAL_REQUIRED")
            values.update({"FKB_" + k.upper(): str(v) if isinstance(v, Path) else v for k, v in loaded.items()})
        elif mode == "http":
            if not values.get("FKB_EMBEDDING_BASE_URL") or not values.get("FKB_EMBEDDING_REVISION"):
                raise SetupError("PINNED_EMBEDDING_SERVICE_REQUIRED")
            from fund_kb.ai_transport import endpoint
            endpoint(values["FKB_EMBEDDING_BASE_URL"], "embeddings")
            if values.get("FKB_EMBEDDING_ALLOW_DOCUMENT_TRANSFER") is not True:
                raise SetupError("EMBEDDING_TRANSFER_CONSENT_REQUIRED")
        else:
            raise SetupError("PRODUCTION_EMBEDDING_MODE_REQUIRED")
    if values.get("FKB_RERANKER_MODE", "disabled") != "disabled" and values.get("FKB_EMBEDDING_MODE") != "transformers":
        raise SetupError("LOCAL_RERANKER_REQUIRES_SEPARATE_MODEL_IMAGE")
    if values.get("FKB_CODEX_BRIDGE_CONFIG_FILE") or values.get("FKB_CODEX_TEXT_PROFILE_FILE"):
        raise SetupError("CODEX_BRIDGE_REQUIRES_SEPARATE_LINUX_VALIDATION")
    # Start with only these explicit settings; reject inherited FKB_* surprises.
    for key in list(os.environ):
        if key.startswith("FKB_") and key != "FKB_CONFIG_FILE":
            del os.environ[key]
    for key, value in values.items():
        os.environ[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    url = URL.create("oracle+oracledb", username=database["username"], password=database["password"],
                     host=database["host"], port=database["port"], query={"service_name": database["service_name"]})
    os.environ["FKB_DATABASE_URL"] = url.render_as_string(hide_password=False)
    settings = Settings()
    if settings.oidc_issuer and not settings.oidc_issuer.startswith("https://"):
        raise SetupError("OIDC_HTTPS_REQUIRED")
    return document, settings


def oracle_engine(settings):
    from sqlalchemy import create_engine
    return create_engine(settings.database_url, pool_pre_ping=True,
                         connect_args={"tcp_connect_timeout": 10})


def schema_status(settings, *, require_current=False):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect, text
    engine = oracle_engine(settings)
    try:
        with engine.connect() as connection:
            connection.connection.driver_connection.call_timeout = 15000
            connection.execute(text("SELECT 1 FROM DUAL")).scalar_one()
            version = connection.connection.driver_connection.version
            if tuple(int(v) for v in version.split(".")[:2]) < (12, 2):
                raise SetupError("PACKAGE_REQUIRES_ORACLE_12_2_OR_LATER")
            tables = set(inspect(connection).get_table_names())
            if not tables:
                state = "EMPTY"
            elif "alembic_version" not in tables:
                raise SetupError("EXISTING_UNMANAGED_SCHEMA_DO_NOT_OVERWRITE")
            else:
                revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
                head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
                if revision != [head]:
                    raise SetupError("SCHEMA_REVISION_MISMATCH")
                from fund_kb import models  # noqa: F401
                from fund_kb.db import Base
                if set(Base.metadata.tables) - tables:
                    raise SetupError("SCHEMA_TABLES_MISSING")
                state = "CURRENT"
            if require_current and state != "CURRENT":
                raise SetupError("SCHEMA_MIGRATION_REQUIRED")
            return {"status": "PASS", "schema": state, "oracle_version": version}
    finally:
        engine.dispose()


def doctor(document, settings, *, require_current=False):
    import httpx
    from clamav_scan import ping, scan
    from kombu import Connection

    results = []
    checks = [
        ("oracle", lambda: schema_status(settings, require_current=require_current)),
        ("oidc", lambda: check_oidc(settings, httpx)),
        ("qdrant", lambda: check_qdrant(settings, httpx)),
        ("broker", lambda: check_broker(settings, Connection)),
        ("scanner", lambda: check_scanner(settings, ping, scan)),
        ("embedding_endpoint", lambda: check_embedding_dns(settings)),
    ]
    for name, operation in checks:
        try:
            detail = operation() or {"status": "PASS"}
            results.append({"check": name, **detail})
        except Exception as exc:  # noqa: BLE001 - redact infrastructure exceptions at the operator boundary.
            results.append({"check": name, "status": "FAIL", "code": safe_code(exc)})
    print(json.dumps({"checks": results, "model_requests": 0}, ensure_ascii=False))
    return 1 if any(row["status"] == "FAIL" for row in results) else 0


def check_oidc(settings, httpx):
    issuer = settings.oidc_issuer.rstrip("/")
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = client.get(issuer + "/.well-known/openid-configuration")
        response.raise_for_status()
        data = response.json()
    if data.get("issuer", "").rstrip("/") != issuer or any(
        not str(data.get(key, "")).startswith("https://")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri")
    ):
        raise SetupError("OIDC_DISCOVERY_INVALID")
    return {"status": "PASS", "note": "discovery only; human login still required"}


def check_qdrant(settings, httpx):
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.get(settings.qdrant_url.rstrip("/") + "/collections",
                              headers={"api-key": settings.qdrant_api_key})
        response.raise_for_status()
        if not isinstance(response.json().get("result", {}).get("collections"), list):
            raise SetupError("QDRANT_INVALID_RESPONSE")


def check_broker(settings, connection_type):
    with connection_type(settings.celery_broker_url, connect_timeout=10) as connection:
        connection.connect()


def check_scanner(settings, ping, scan):
    ping(settings)
    with tempfile.TemporaryDirectory(prefix="fkb-scanner-check-") as directory:
        path = Path(directory) / "synthetic.txt"
        path.write_text("FundKB synthetic deployment health check", encoding="utf-8")
        if scan(path=path, filename=path.name, settings=settings).get("clean") is not True:
            raise SetupError("SCANNER_SYNTHETIC_CHECK_FAILED")


def check_embedding_dns(settings):
    if settings.retrieval_mode == "wiki":
        return {"status": "SKIP", "note": "explicit wiki-only configuration"}
    if settings.embedding_mode == "transformers":
        from fund_kb.api_retrieval import _local_model_status
        from fund_kb.retrieval_profile import read_local_profile
        from fund_kb.retrieval_registry import ProfileManifest
        registry = ProfileManifest.model_validate(json.loads(settings.retrieval_profiles_file.read_text()))
        checked = []
        for item in registry.profiles:
            if not item.enabled:
                continue
            configured = settings.model_copy(update=read_local_profile(item.profile_file, settings.storage_dir))
            if configured.embedding_device != "cpu" or configured.reranker_device != "cpu":
                raise SetupError("PROFILE_DEVICE_NOT_CPU")
            if not all(_local_model_status(configured, role)[0] for role in ("embedding", "reranker")):
                raise SetupError("LOCAL_MODEL_FILES_INCOMPLETE")
            checked.append(item.id)
        return {"status": "PASS", "profiles": checked, "note": "local file prerequisites only; no inference/quality/latency certification"}
    endpoint = urlsplit(settings.embedding_base_url)
    if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
        raise SetupError("EMBEDDING_ENDPOINT_INVALID")
    with socket.create_connection((endpoint.hostname, endpoint.port or (443 if endpoint.scheme == "https" else 80)),
                                  timeout=10):
        pass
    return {"status": "PASS", "note": "TCP only; model/dimension/quality not evaluated"}


def safe_code(exc):
    if isinstance(exc, SetupError) and re.fullmatch(r"[A-Z0-9_]+", str(exc)):
        return str(exc)
    return type(exc).__name__.upper()  # no DSN, token, issuer response or provider output


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "config"
    try:
        document, settings = read_configuration(os.environ.get("FKB_CONFIG_FILE", "/run/fkb/settings.json"))
        if action == "config":
            print('{"status":"PASS","check":"configuration","model_requests":0}')
            return 0
        if action in {"doctor", "ready"}:
            return doctor(document, settings, require_current=action == "ready")
        if action == "migrate":
            if "--ack-empty-or-backed-up-schema" not in args:
                raise SetupError("MIGRATION_ACK_REQUIRED")
            state = schema_status(settings)
            if state["schema"] == "EMPTY":
                from alembic import command
                from alembic.config import Config
                command.upgrade(Config("alembic.ini"), "head")
            print(json.dumps(schema_status(settings, require_current=True)))
            return 0
        if action == "sql":
            from alembic import command
            from alembic.config import Config
            command.upgrade(Config("alembic.ini"), "head", sql=True)
            return 0
        if action == "worker-health":
            from fund_kb.celery_app import app
            destination = "celery@" + socket.gethostname()
            replies = app.control.ping(destination=[destination], timeout=5)
            return 0 if any(reply.get(destination, {}).get("ok") == "pong" for reply in replies) else 1
        if action == "api":
            schema_status(settings, require_current=True)
            import uvicorn
            # Only Nginx exposes a host port; never expose the API port separately.
            uvicorn.run("fund_kb.main:app", host="0.0.0.0", port=8765, workers=1,
                        proxy_headers=True, forwarded_allow_ips="*")
            return 0
        if action == "worker":
            schema_status(settings, require_current=True)
            from fund_kb.celery_app import app
            app.worker_main(["worker", "--loglevel=WARNING", "--concurrency=2"])
            return 0
        raise SetupError("UNKNOWN_ACTION")
    except Exception as exc:  # noqa: BLE001 - never print DSNs, credentials or provider response bodies.
        print(json.dumps({"status": "FAIL", "code": safe_code(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
