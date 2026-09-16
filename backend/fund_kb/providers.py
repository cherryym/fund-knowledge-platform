"""Encrypted connection records and bounded native protocol adapters.

Only public_snapshot/public_connection may leave the service boundary. Networking
is explicit, outside database transactions, and never occurs while editing settings.
"""
from __future__ import annotations

import ipaddress
import asyncio
import json
import os
import re
import socket
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import update
from sqlalchemy.orm.attributes import set_committed_value

from . import models as m
from .provider_catalog import PROTOCOLS, preset_models, provider_by_id
from .provider_contracts import ThrottledCheck, safe_diagnostic, safe_usage, schema_validator, seconds, structured_mode, timeout_budget
from .services import deployment_admin, fail, now, primitive, space_access

NAMESPACE = "model-connection:"
PUBLIC_KEYS = ("id", "owner_user_id", "space_id", "name", "provider_id", "protocol", "base_url", "enabled",
    "credential_mode", "api_key_env", "allow_document_transfer", "status", "last_error_code", "last_synced_at", "models")
SNAPSHOT_KEYS = ("id", "owner_user_id", "context_space_id", "auth_epoch", "space_id", "name", "provider_id", "kind", "protocol", "base_url", "model_id",
    "brand", "revision", "allow_document_transfer", "credential_mode")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ProviderError(RuntimeError):
    """Safe error code only: never retain provider body, URL, headers or key in exceptions."""
    def __init__(self, code, diagnostic=None):
        self.code = code
        self.diagnostic = safe_diagnostic(diagnostic)
        super().__init__(code)


def _secret(value):
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def validate_env_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"FKB_[A-Z][A-Z0-9_]{1,100}", value) \
        or any(part in value for part in ("MASTER", "DATABASE", "OIDC", "S3_", "RABBITMQ", "SESSION")):
        raise ProviderError("INVALID_CREDENTIAL_REFERENCE")
    return value


def allowed_env_references(settings, user):
    from .codex_bridge_config import model_policy
    values = model_policy(settings).env_refs_by_user.get(user.id, ()) if user and user.active else ()
    return sorted({validate_env_name(value) for value in values})


def authorize_env_reference(settings, user, value):
    value = validate_env_name(value)
    if not user or not user.active or not (
        deployment_admin(user, settings) or value in allowed_env_references(settings, user)
    ):
        fail(403, "CREDENTIAL_REFERENCE_FORBIDDEN", "未获准使用此环境变量引用")
    return value


def validate_base_url(value, kind="direct", local_hosts=()):
    if not isinstance(value, str) or not value or len(value) > 2048 \
        or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise ProviderError("INVALID_PROVIDER_URL")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not host or port == 0 \
            or parsed.scheme not in {"https", "http"}:
            raise ValueError()
        host = host.rstrip(".").encode("idna").decode().lower()
        if not host or "%" in host or "%" in parsed.path or any(x in {".", ".."} for x in parsed.path.split("/")):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ProviderError("INVALID_PROVIDER_URL") from None
    allowed_local = LOCAL_HOSTS | set(local_hosts or ())
    local_allowed = kind == "local" and host in allowed_local
    if parsed.scheme != "https" and not local_allowed:
        raise ProviderError("PROVIDER_TLS_REQUIRED")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and not ip.is_global and not local_allowed:
        raise ProviderError("PROVIDER_ADDRESS_BLOCKED")
    if host in {"localhost", "metadata.google.internal"} and not local_allowed:
        raise ProviderError("PROVIDER_ADDRESS_BLOCKED")
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _master_key(settings, create=False):
    explicit = _secret(getattr(settings, "provider_master_key", None))
    if explicit:
        try:
            return Fernet(str(explicit).encode())
        except (ValueError, TypeError):
            raise ProviderError("INVALID_PROVIDER_MASTER_KEY") from None
    if settings.app_env == "production":
        raise ProviderError("PROVIDER_MASTER_KEY_REQUIRED")
    private = Path(settings.storage_dir) / "private"
    for part in [private, *private.parents]:
        if part.is_symlink():
            raise ProviderError("MASTER_KEY_PATH_UNSAFE")
    path = private / "provider-master.key"
    if private.exists() and (stat.S_IMODE(private.stat().st_mode) & 0o077 or private.stat().st_uid != os.getuid()):
        raise ProviderError("MASTER_KEY_PERMISSIONS")
    if not path.exists() and create:
        private.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                raise ProviderError("MASTER_KEY_PERMISSIONS")
            raw = stream.read(256)
        return Fernet(raw)
    except ProviderError:
        raise
    except (OSError, ValueError):
        raise ProviderError("PROVIDER_MASTER_KEY_UNAVAILABLE") from None


def encrypt_credential(settings, connection_id, space_id, api_key, *, owner_user_id=None):
    if not isinstance(api_key, str) or not 1 <= len(api_key) <= 8192 or re.search(r"[\r\n\x00]", api_key):
        raise ProviderError("INVALID_API_KEY")
    payload = json.dumps({"connection_id": connection_id, "space_id": space_id,
        "owner_user_id": owner_user_id, "api_key": api_key}).encode()
    return _master_key(settings, create=True).encrypt(payload).decode()


def credential_value(config, settings, user=None):
    mode = config.get("credential_mode", "none")
    if mode == "none":
        return None
    if mode == "env":
        value = os.environ.get(authorize_env_reference(settings, user, config.get("api_key_env")))
        if not value:
            raise ProviderError("CREDENTIAL_MISSING")
    elif mode == "encrypted":
        try:
            raw = _master_key(settings).decrypt(config.get("credential_ciphertext", "").encode())
            payload = json.loads(raw)
            if payload.get("connection_id") != config["id"] or payload.get("space_id") != config["space_id"] \
                or payload.get("owner_user_id") != config.get("owner_user_id"):
                raise ValueError()
            value = payload["api_key"]
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise ProviderError("CREDENTIAL_DECRYPT_FAILED") from None
    else:
        raise ProviderError("INVALID_CREDENTIAL_MODE")
    if not isinstance(value, str) or not value or len(value) > 8192 or re.search(r"[\r\n\x00]", value):
        raise ProviderError("INVALID_API_KEY")
    return value


def credential_present(config, settings=None, user=None):
    if config.get("credential_mode") == "chatgpt_oauth":
        return False  # Live OAuth state is separately authority-checked; never infer from config.
    if config.get("credential_mode") == "none":
        return False
    if config.get("credential_mode") == "encrypted":
        return bool(config.get("credential_ciphertext"))
    if settings is None or user is None:
        return False
    name = authorize_env_reference(settings, user, config.get("api_key_env"))
    return bool(os.environ.get(name))


def _connection_credential(config, settings, kind, user=None):
    key = credential_value(config, settings, user)
    if not key and kind not in {"local", "custom"}:
        raise ProviderError("CREDENTIAL_MISSING")
    return key


def require_connection_space(db, user, space_id, settings, manage=False):
    if not user or not user.active:
        fail(401, "AUTH_REQUIRED", "身份未激活")
    return space_access(db, user, space_id)


def load_connection(db, user, connection_id, settings, *, manage=False, include_deleted=False, check_env=True):
    if not user or not user.active:
        fail(401, "AUTH_REQUIRED", "身份未激活")
    policy = db.get(m.RuntimePolicy, connection_id)
    if not policy or policy.name != NAMESPACE + connection_id or not isinstance(policy.config, dict):
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if not policy.config.get("owner_user_id") or policy.config["owner_user_id"] != user.id:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if policy.config.get("deleted_at") and not include_deleted:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if check_env and policy.config.get("credential_mode") == "env":
        authorize_env_reference(settings, user, policy.config.get("api_key_env"))
    return policy


def public_connection(policy, settings=None, user=None):
    config = policy.config
    return {**{key: primitive(config.get(key)) for key in PUBLIC_KEYS}, "revision": policy.revision,
        "credential_present": credential_present(config, settings, user), "models": connection_models(config)}


def connection_models(config):
    # A successful sync supersedes presets, including an explicitly empty model list.
    base = config.get("synced_models", []) if config.get("last_synced_at") else preset_models(config["provider_id"])
    merged = {model["id"]: dict(model) for model in base}
    for item in config.get("custom_models", []):
        merged[item["id"]] = {**item, "source": "custom"}
    return list(merged.values())


def resolve_connection(db, user, space_id, connection_id, model_id, settings, require_transfer=False, expected_revision=None):
    policy = load_connection(db, user, connection_id, settings)
    config = policy.config
    require_connection_space(db, user, space_id, settings)
    if expected_revision is not None and policy.revision != expected_revision:
        raise ProviderError("CONNECTION_REVISION_CHANGED")
    if not config.get("enabled"):
        raise ProviderError("CONNECTION_DISABLED")
    if require_transfer and not config.get("allow_document_transfer"):
        raise ProviderError("DOCUMENT_TRANSFER_NOT_AUTHORIZED")
    if config.get("protocol") == "codex_app_server":
        from .codex_text import text_snapshot
        return text_snapshot(db, user, policy, model_id, settings, space_id)
    models = connection_models(config)
    selected = next((item for item in models if item["id"] == model_id), None)
    if not selected:
        raise ProviderError("MODEL_NOT_CONFIGURED")
    provider = provider_by_id(config["provider_id"])
    if not provider:
        raise ProviderError("UNKNOWN_PROVIDER")
    local_hosts = list(getattr(settings, "provider_local_hosts", []) or [])
    base_url = validate_base_url(config["base_url"], provider["kind"], local_hosts)
    return {"id": policy.id, "owner_user_id": user.id, "space_id": space_id, "context_space_id": space_id,
        "revision": policy.revision, "name": config["name"],
        "provider_id": config["provider_id"], "kind": provider["kind"], "protocol": config["protocol"],
        "base_url": base_url, "model_id": model_id, "brand": selected.get("brand", config["provider_id"]),
        "credential_mode": config["credential_mode"], "api_key": _connection_credential(config, settings, provider["kind"], user),
        "supported_parameters": selected.get("supported_parameters"),
        "allow_document_transfer": bool(config.get("allow_document_transfer")), "local_hosts": local_hosts,
        "max_response_bytes": int(getattr(settings, "provider_max_response_bytes", 8388608)),
        "max_request_bytes": int(getattr(settings, "provider_max_request_bytes", 2097152)),
        "http_timeout": float(getattr(settings, "provider_http_timeout_seconds", 60)),
        **({"read_idle_timeout": settings.provider_read_idle_timeout_seconds}
           if getattr(settings, "provider_read_idle_timeout_seconds", None) is not None else {})}


def public_snapshot(snapshot):
    return {key: primitive(snapshot.get(key)) for key in SNAPSHOT_KEYS}


def _dns_addresses(host, port):
    return list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


def _pinned_url(snapshot, url, addresses=None):
    parsed = urlsplit(url)
    base = validate_base_url(snapshot["base_url"], snapshot["kind"], snapshot.get("local_hosts", []))
    if (parsed.scheme, parsed.netloc) != (urlsplit(base).scheme, urlsplit(base).netloc):
        raise ProviderError("PROVIDER_ORIGIN_CHANGED")
    allowed_local = snapshot["kind"] == "local" and parsed.hostname in (LOCAL_HOSTS | set(snapshot.get("local_hosts", [])))
    if addresses is None:
        addresses = _dns_addresses(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if not addresses:
        raise ProviderError("PROVIDER_DNS_FAILED")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if parsed.hostname in LOCAL_HOSTS and not ip.is_loopback:
            raise ProviderError("PROVIDER_ADDRESS_BLOCKED")
        # Explicit local allowlist never includes metadata/link-local/multicast endpoints.
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or (not ip.is_global and not allowed_local):
            raise ProviderError("PROVIDER_ADDRESS_BLOCKED")
    return httpx.URL(url).copy_with(host=addresses[0]), parsed.netloc, parsed.hostname


def _request_json(snapshot, method, url, payload, headers, timeout, transport=None, *, deadline=None):
    started = time.monotonic()
    deadline = started + timeout if deadline is None else deadline
    first_chunk = None
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise ProviderError("PROVIDER_TIMEOUT")
        return value
    try:
        remaining()
        pinned, host_header, sni = _pinned_url(snapshot, url)
        remaining()
        encoded = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        if encoded and len(encoded) > snapshot.get("max_request_bytes", 2097152):
            raise ProviderError("PROVIDER_REQUEST_TOO_LARGE")
        headers = {**headers, "Host": host_header,
            "Accept": "text/event-stream, application/json" if payload and payload.get("stream") else "application/json", "Content-Type": "application/json",
            "Accept-Encoding": "identity"}
        try:
            idle = seconds(snapshot.get("read_idle_timeout", timeout))
        except ValueError:
            raise ProviderError("INVALID_TIMEOUT") from None
        with (httpx.Client(timeout=httpx.Timeout(remaining(), connect=min(remaining(), 10), read=min(idle, remaining())), trust_env=False,
            follow_redirects=False, transport=transport) as client,
            client.stream(method, pinned, content=encoded, headers=headers,
                extensions={"sni_hostname": sni}) as response):
            if 300 <= response.status_code < 400:
                raise ProviderError("PROVIDER_REDIRECT_BLOCKED")
            if response.status_code >= 400:
                code = {401: "PROVIDER_AUTH_FAILED", 403: "PROVIDER_ACCESS_DENIED", 404: "PROVIDER_MODEL_NOT_FOUND",
                    429: "PROVIDER_RATE_LIMITED"}.get(response.status_code, "PROVIDER_HTTP_ERROR")
                raise ProviderError(code)
            if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise ProviderError("PROVIDER_ENCODING_UNSUPPORTED")
            if int(response.headers.get("Content-Length", "0")) > snapshot.get("max_response_bytes", 8388608):
                raise ProviderError("PROVIDER_RESPONSE_TOO_LARGE")
            raw = bytearray()
            from .provider_stream import ResponseStream, StreamError
            streaming = (response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() == 'text/event-stream')
            if streaming and snapshot.get("protocol") != "responses":
                raise ProviderError("UNSUPPORTED_STREAM_PROTOCOL")
            envelope = bool(payload and any(tool.get("name") == "return_structured_result"
                for tool in payload.get("tools", []) if isinstance(tool, dict)))
            def inspect_event(value):
                key = snapshot.get("api_key")
                if key and key in json.dumps(value, ensure_ascii=False):
                    raise ProviderError("PROVIDER_SECRET_ECHO")
            collector = ResponseStream(snapshot.get('max_response_bytes', 8388608),
                allow_data_envelope=envelope, inspect_event=inspect_event,
                _on_public_text=snapshot.get("_on_public_text"), secrets=(snapshot.get("api_key"),)) if streaming else None
            terminal = None
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > snapshot.get("max_response_bytes", 8388608):
                    raise ProviderError("PROVIDER_RESPONSE_TOO_LARGE")
                raw.extend(chunk)
                remaining()
                if first_chunk is None:
                    first_chunk = round((time.monotonic() - started) * 1000)
                if collector:
                    try:
                        terminal = collector.feed(chunk)
                    except StreamError as exc:
                        raise ProviderError(exc.code) from None
                    if terminal is not None:
                        break
        remaining()
        try:
            if collector:
                try:
                    parsed = terminal if terminal is not None else collector.finish()
                except StreamError as exc:
                    raise ProviderError(exc.code) from None
            else:
                parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("PROVIDER_INVALID_RESPONSE", {"response_phase": "http_json", "response_bytes": len(raw),
                "json_line": exc.lineno, "json_column": exc.colno}) from None
        if not isinstance(parsed, dict):
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        key = snapshot.get("api_key")
        if key and (key in raw.decode("utf-8", errors="replace") or key in json.dumps(parsed, ensure_ascii=False)):
            raise ProviderError("PROVIDER_SECRET_ECHO")
        remaining()
        parsed["_provider_transport"] = {"response_bytes": len(raw), "http_status": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000),
            **({"first_chunk_ms": first_chunk} if first_chunk is not None else {})}
        return parsed
    except ProviderError:
        raise
    except httpx.TimeoutException:
        raise ProviderError("PROVIDER_TIMEOUT") from None
    except httpx.RemoteProtocolError:
        raise ProviderError("PROVIDER_CONNECTION_INTERRUPTED") from None
    except httpx.ReadError:
        raise ProviderError("PROVIDER_READ_FAILED") from None
    except httpx.ConnectError:
        raise ProviderError("PROVIDER_CONNECTION_FAILED") from None
    except (httpx.HTTPError, OSError, ValueError, UnicodeError, RecursionError):
        raise ProviderError("PROVIDER_INVALID_RESPONSE") from None


async def _resolve_cancellable(host, port):
    """Killable DNS setup, not a forever-blocked getaddrinfo executor thread.

    The isolated child sees only hostname/port, never headers, payload or keys.
    Connection setup stays bounded; response/model waiting has no deadline.
    """
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    code = ("import socket,sys,json; "
            "print(json.dumps(list(dict.fromkeys(x[4][0] for x in "
            "socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)))))")
    process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-S", "-c", code, host, str(port),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    try:
        async with asyncio.timeout(10):
            raw = await process.stdout.read(65537)
            if len(raw) > 65536:
                raise ProviderError("PROVIDER_DNS_FAILED")
            await process.wait()
        if process.returncode:
            raise ProviderError("PROVIDER_DNS_FAILED")
        values = json.loads(raw)
        if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
            raise ProviderError("PROVIDER_DNS_FAILED")
        return values
    except TimeoutError:
        raise ProviderError("PROVIDER_TIMEOUT") from None
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


def _request_json_cancellable(snapshot, method, url, payload, headers, transport=None):
    """Own and join the async HTTP task. Cancellation closes HTTP even before headers.

    No total/read/write/pool deadline; the caller's lightweight guard is polled
    around once per second on the caller thread. Never leave a paid HTTP task
    in an abandoned future. AsyncClient is existing httpx, not a new SDK.
    """
    check = snapshot.get("_cancel_check")
    if not callable(check):
        raise ProviderError("CANCELLATION_CHECK_REQUIRED")
    check()
    parsed_url = urlsplit(url)
    base = validate_base_url(snapshot["base_url"], snapshot["kind"], snapshot.get("local_hosts", []))
    if (parsed_url.scheme, parsed_url.netloc) != (urlsplit(base).scheme, urlsplit(base).netloc):
        raise ProviderError("PROVIDER_ORIGIN_CHANGED")
    encoded = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    if encoded and len(encoded) > snapshot.get("max_request_bytes", 2097152):
        raise ProviderError("PROVIDER_REQUEST_TOO_LARGE")
    started = time.monotonic()
    ready, done, state = threading.Event(), threading.Event(), {}

    async def request():
        state.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
        ready.set()
        addresses = await _resolve_cancellable(parsed_url.hostname, parsed_url.port or (443 if parsed_url.scheme == "https" else 80))
        pinned, host_header, sni = _pinned_url(snapshot, url, addresses)
        wire_headers = {**headers, "Host": host_header, "Accept-Encoding": "identity", "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json" if payload and payload.get("stream") else "application/json"}
        raw, first_chunk, terminal, collector = bytearray(), None, None, None
        from .provider_stream import ResponseStream
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10), trust_env=False,
                                     follow_redirects=False, transport=transport) as client:
            async with client.stream(method, pinned, content=encoded, headers=wire_headers,
                                     extensions={"sni_hostname": sni}) as response:
                if 300 <= response.status_code < 400:
                    raise ProviderError("PROVIDER_REDIRECT_BLOCKED")
                if response.status_code >= 400:
                    raise ProviderError({401: "PROVIDER_AUTH_FAILED", 403: "PROVIDER_ACCESS_DENIED",
                        404: "PROVIDER_MODEL_NOT_FOUND", 429: "PROVIDER_RATE_LIMITED"}.get(response.status_code, "PROVIDER_HTTP_ERROR"))
                if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    raise ProviderError("PROVIDER_ENCODING_UNSUPPORTED")
                maximum = snapshot.get("max_response_bytes", 8388608)
                if int(response.headers.get("Content-Length", "0")) > maximum:
                    raise ProviderError("PROVIDER_RESPONSE_TOO_LARGE")
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "text/event-stream":
                    if snapshot.get("protocol") != "responses":
                        raise ProviderError("UNSUPPORTED_STREAM_PROTOCOL")
                    def inspect_event(value):
                        key = snapshot.get("api_key")
                        if key and key in json.dumps(value, ensure_ascii=False):
                            raise ProviderError("PROVIDER_SECRET_ECHO")
                    envelope = bool(payload and any(isinstance(tool, dict) and tool.get("name") == "return_structured_result"
                        for tool in payload.get("tools", [])))
                    collector = ResponseStream(maximum, allow_data_envelope=envelope, inspect_event=inspect_event,
                        _on_public_text=snapshot.get("_on_public_text"), secrets=(snapshot.get("api_key"),))
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > maximum:
                        raise ProviderError("PROVIDER_RESPONSE_TOO_LARGE")
                    raw.extend(chunk)
                    if first_chunk is None:
                        first_chunk = round((time.monotonic() - started) * 1000)
                    if collector:
                        terminal = collector.feed(chunk)
                        if terminal is not None:
                            break
        parsed = (terminal if terminal is not None else collector.finish()) if collector else json.loads(raw)
        if not isinstance(parsed, dict):
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        key = snapshot.get("api_key")
        if key and (key in raw.decode("utf-8", errors="replace") or key in json.dumps(parsed, ensure_ascii=False)):
            raise ProviderError("PROVIDER_SECRET_ECHO")
        parsed["_provider_transport"] = {"response_bytes": len(raw), "http_status": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000),
            **({"first_chunk_ms": first_chunk} if first_chunk is not None else {})}
        return parsed

    def work():
        try:
            state["result"] = asyncio.run(request())
        except BaseException as exc:
            state["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=work, name="fkb-cancellable-http")
    worker.start()
    try:
        while not done.wait(1):
            check()
    except BaseException:
        # Registered before DNS/HTTP starts. Cancel on its owning event loop,
        # then join after AsyncClient and the DNS child have both been closed.
        while not ready.wait(.1):
            if done.is_set():
                break
        if not done.is_set():
            try:
                state["loop"].call_soon_threadsafe(state["task"].cancel)
            except RuntimeError:
                pass  # The loop finished concurrently; join below still owns cleanup.
        worker.join()
        raise
    worker.join()
    check()
    error = state.get("error")
    if error is not None:
        from .provider_stream import StreamError
        if isinstance(error, (ProviderError, StreamError)):
            raise ProviderError(error.code, getattr(error, "diagnostic", None)) from None
        if isinstance(error, asyncio.CancelledError):
            raise ProviderError("CANCELLED") from None
        if isinstance(error, httpx.TimeoutException):
            raise ProviderError("PROVIDER_TIMEOUT") from None
        if isinstance(error, (httpx.RemoteProtocolError, httpx.ReadError)):
            raise ProviderError("PROVIDER_CONNECTION_INTERRUPTED") from None
        if isinstance(error, (httpx.ConnectError, OSError)):
            raise ProviderError("PROVIDER_CONNECTION_FAILED") from None
        raise ProviderError("PROVIDER_INVALID_RESPONSE") from None
    return state["result"]


def _network(snapshot, method, operation, payload=None, timeout=60, transport=None):
    url = snapshot["base_url"].rstrip("/") + "/" + operation.lstrip("/")
    key, protocol = snapshot.get("api_key"), snapshot["protocol"]
    headers = {}
    if protocol == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif protocol == "gemini":
        if key:
            headers["x-goog-api-key"] = key
    elif key:
        headers["Authorization"] = "Bearer " + key
    if timeout is None:
        return _request_json_cancellable(snapshot, method, url, payload, headers, transport)
    try:
        budget = timeout_budget(timeout, snapshot.get("http_timeout"))
        if "read_idle_timeout" in snapshot:
            seconds(snapshot["read_idle_timeout"])
    except ValueError:
        raise ProviderError("INVALID_TIMEOUT") from None
    deadline = min(time.monotonic() + budget, snapshot.get("_call_deadline", float("inf")))
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fkb-provider")
    future = executor.submit(_request_json, snapshot, method, url, payload, headers, budget, transport, deadline=deadline)
    try:
        return future.result(timeout=max(0, deadline - time.monotonic()))
    except FutureTimeoutError:
        future.cancel()
        raise ProviderError("PROVIDER_TIMEOUT") from None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _messages(messages, json_mode):
    if not isinstance(messages, list) or not messages or len(messages) > 100:
        raise ProviderError("INVALID_MESSAGES")
    output = []
    for message in messages:
        if not isinstance(message, dict) or set(message) - {"role", "content"} \
            or message.get("role") not in {"system", "developer", "user", "assistant"} or not isinstance(message.get("content"), str):
            raise ProviderError("UNSUPPORTED_MESSAGE_CONTENT")
        output.append(dict(message))
    if json_mode:
        output.insert(0, {"role": "system", "content": "Return a valid JSON object only. No markdown fences or tool calls."})
    return output


def complete(snapshot, messages, max_tokens=4096, json_mode=True, timeout=60, *, transport=None, reasoning_effort=None, output_schema=None, read_idle_timeout=None):
    from .answer_preview import revoke_callback
    callback = snapshot.get("_on_public_text")
    # Opt-in public text is only supported by these real streaming protocols.
    # Existing JSON/schema and all other protocol callers retain full delivery.
    if json_mode or output_schema is not None or snapshot.get("protocol") not in {"responses", "codex_app_server"}:
        snapshot = {key: value for key, value in snapshot.items() if key != "_on_public_text"}
    try:
        result = _complete(snapshot, messages, max_tokens, json_mode, timeout, transport=transport,
            reasoning_effort=reasoning_effort, output_schema=output_schema, read_idle_timeout=read_idle_timeout)
        if result["choices"][0].get("finish_reason") != "stop":
            revoke_callback(callback)
        return result
    except BaseException:
        revoke_callback(callback)
        raise


def _complete(snapshot, messages, max_tokens=4096, json_mode=True, timeout=60, *, transport=None, reasoning_effort=None, output_schema=None, read_idle_timeout=None):
    """One bounded attempt; every protocol enforces the caller's schema locally."""
    # Reject unattested Codex execution before inspecting messages or invoking
    # cancellation/schema callbacks. A supplied transport cannot replace isolation.
    if snapshot.get("protocol") == "codex_app_server" and (
            snapshot.get("_codex_engine") is None or transport is not None):
        raise ProviderError("CODEX_TEXT_ISOLATION_UNVERIFIED")
    try:
        if timeout is not None:
            seconds(timeout)
        if timeout is not None and read_idle_timeout is not None:
            seconds(read_idle_timeout)
    except ValueError:
        raise ProviderError("INVALID_TIMEOUT") from None
    try:
        total = timeout_budget(timeout, snapshot.get("http_timeout") if snapshot.get("protocol") != "codex_app_server" else None)
    except ValueError:
        raise ProviderError("INVALID_TIMEOUT") from None
    deadline = time.monotonic() + total if total is not None else None
    snapshot = {**snapshot, "_call_deadline": deadline}
    cancel = None
    if timeout is None:
        callback = snapshot.get("_cancel_check")
        if not callable(callback):
            raise ProviderError("CANCELLATION_CHECK_REQUIRED")
        def checkpoint():
            if callback() is False:
                raise ProviderError("CANCELLED")
        cancel = ThrottledCheck(checkpoint, time.monotonic)
        cancel.force()
        snapshot["_cancel_check"] = cancel
    validator = None
    if output_schema is not None:
        try:
            output_schema, validator = schema_validator(output_schema)
        except Exception:
            raise ProviderError("INVALID_OUTPUT_SCHEMA") from None
        json_mode = True
    mode = structured_mode(snapshot, output_schema)
    if read_idle_timeout is not None:
        snapshot = {**snapshot, "read_idle_timeout": read_idle_timeout}
    try:
        normalized = _complete_transport(snapshot, messages, max_tokens, json_mode, timeout,
            transport=transport, reasoning_effort=reasoning_effort, output_schema=output_schema)
        normalized = _validate_completion(normalized, json_mode, validator)
        if deadline is not None and time.monotonic() >= deadline:
            raise ProviderError("PROVIDER_TIMEOUT")
        if cancel is not None:
            cancel.force()
        normalized.setdefault("provider_meta", {}).update(structured_mode=mode)
        # Compatibility receipt for jobs; this describes adapter parameters,
        # never the provider's private reasoning or a model-name guess in jobs.
        normalized.setdefault("transport_meta", {}).update(structured_strategy=mode)
        return normalized
    except ProviderError as exc:
        exc.diagnostic = safe_diagnostic({**exc.diagnostic, "structured_mode": mode})
        raise


def _complete_transport(snapshot, messages, max_tokens=4096, json_mode=True, timeout=60, *, transport=None, reasoning_effort=None, output_schema=None):
    """Text-only normalized completion; protocol-specific tools never become an accidental success."""
    if snapshot.get("protocol") != "codex_app_server" and reasoning_effort is not None and (type(reasoning_effort) is not str or reasoning_effort not in {"none", "minimal", "low", "medium", "high"}
            or snapshot.get("protocol") != "responses"):
        raise ProviderError("UNSUPPORTED_REASONING_OPTION")
    structured = output_schema is not None
    mode = structured_mode(snapshot, output_schema)
    envelope = mode == "data_envelope"
    messages = _messages(messages, json_mode and not envelope)
    if structured and not envelope:
        messages.append({"role": "system", "content": "Return one JSON object matching this output schema. "
            "Return result values, never the schema itself. Do not use tools or Markdown. "
            "All evidence and authorization requirements remain mandatory.\n"
            + json.dumps(output_schema, ensure_ascii=False, allow_nan=False)})
    if envelope:
        result_fields = ', '.join(output_schema.get('required', []))
        messages.append({"role": "system", "content":
            "Transport instruction: return exactly one return_structured_result function call. "
            "This sole function is a data-only return envelope, not an executable tool or action. "
            "Its arguments must contain the complete output object matching its parameter schema. "
            "Task instructions prohibiting tools mean no files, search, network, code or other operations; "
            "they do not prohibit this return envelope. Do not emit prose or Markdown outside it. "
            "All task evidence, authorization and truthfulness requirements remain mandatory. "
            "Return result VALUES, not a JSON Schema or a wrapper containing properties. "
            "Required top-level argument fields: " + result_fields + ". "
            + ("summary MUST be a plain string, NOT an object or array. claims MUST be an array of {text,evidence_ids}; "
               "analysis MUST be {interpretation,checks,branches}. checks items are {title,reason,evidence_ids}; "
               "branches items are {condition,action,evidence_ids}. Use the supplied E identifiers, not invented ones."
               if {'summary','claims','analysis'} <= set(output_schema.get('properties', {})) else '')})
    if type(max_tokens) is not int or not 1 <= max_tokens <= 131072:
        raise ProviderError("INVALID_TOKEN_LIMIT")
    protocol, model = snapshot["protocol"], snapshot["model_id"]
    if protocol not in PROTOCOLS:
        raise ProviderError("UNSUPPORTED_PROTOCOL")
    if protocol == "codex_app_server":
        engine = snapshot.get("_codex_engine")
        if engine is None or transport is not None:
            raise ProviderError("CODEX_TEXT_ISOLATION_UNVERIFIED")
        # The verified engine resolves provider defaults and per-call effort.
        # Keep its actual parameter receipt, including legacy-profile pinning
        # and unknown effective reasoning when no override was sent.
        options = {} if reasoning_effort is None else {"reasoning_effort": reasoning_effort}
        return engine.complete(snapshot, messages, max_tokens=max_tokens, json_mode=json_mode,
                               timeout=timeout, **options)
    payload = {"model": model}
    if protocol == "responses":
        operation = "responses"
        payload.update(input=messages, max_output_tokens=max_tokens, store=False,
            stream=timeout is None or callable(snapshot.get("_on_public_text")))
        if json_mode and snapshot["provider_id"] != "minimax":
            payload["text"] = {"format": {"type": "json_object"}}
        if mode == "native_schema":
            payload["text"] = {"format": {"type": "json_schema", "name": "result", "strict": True, "schema": output_schema}}
        if envelope:
            payload.update(tools=[{"type": "function", "name": "return_structured_result",
                "description": "Return the requested JSON result as data. This has no execution capability.",
                "parameters": output_schema}], tool_choice="auto", stream=True)
        # Do not send temperature/top_p/top_logprobs; Astra does not support them.
        if timeout is not None and model.startswith(("gpt-6", "gpt-5.6")):
            payload["reasoning"] = {"effort": "low"}
        if timeout is not None and structured and snapshot["provider_id"] == "minimax" and model.startswith("MiniMax-M3"):
            payload["reasoning"] = {"effort": "low"}
        if reasoning_effort is not None:
            payload["reasoning"] = {"effort": reasoning_effort}
        elif timeout is None and snapshot["provider_id"] == "minimax" and model.startswith("MiniMax-M3"):
            # MiniMax documents omission as disabled. A non-none value enables
            # Adaptive Thinking; medium is not a depth/time cap on this API.
            payload["reasoning"] = {"effort": "medium"}
    elif protocol == "openai":
        operation = "chat/completions"
        payload.update(messages=messages, stream=False)
        token_field = "max_completion_tokens" if (snapshot["provider_id"] == "openai" and model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))) or (snapshot["provider_id"] == "minimax" and model.startswith("MiniMax-M3")) else "max_tokens"
        payload[token_field] = max_tokens
        if timeout is not None and snapshot["provider_id"] == "openai" and model.startswith(("gpt-6", "gpt-5.6")):
            payload["reasoning_effort"] = "low"
        if snapshot["provider_id"] == "minimax":
            payload["reasoning_split"] = True
        if timeout is not None and snapshot["provider_id"] == "qwen" and model in {x["id"] for x in preset_models("qwen")}:
            payload["enable_thinking"] = False
        supported = snapshot.get("supported_parameters")
        native_json = snapshot["provider_id"] not in {"minimax", "custom", "local-openai"} and (
            snapshot["kind"] != "gateway" or (isinstance(supported, list) and "response_format" in supported))
        if json_mode and native_json:
            payload["response_format"] = {"type": "json_object"}
        if mode == "native_schema":
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "result", "strict": True, "schema": output_schema}}
    elif protocol == "anthropic":
        operation = "messages"
        payload.update(max_tokens=max_tokens, messages=[x for x in messages if x["role"] in {"user", "assistant"}])
        payload["system"] = "\n\n".join(x["content"] for x in messages if x["role"] in {"system", "developer"})
        if mode == "native_schema":
            payload["output_config"] = {"format": {"type": "json_schema", "schema": output_schema}}
        # Anthropic has no OpenAI json_object flag; JSON instruction + caller schema validation.
    elif protocol == "gemini":
        operation = "models/" + quote(model.removeprefix("models/"), safe="") + ":generateContent"
        payload = {"contents": [{"role": "model" if x["role"] == "assistant" else "user", "parts": [{"text": x["content"]}]}
            for x in messages if x["role"] in {"user", "assistant"}],
            "systemInstruction": {"parts": [{"text": "\n\n".join(x["content"] for x in messages if x["role"] in {"system", "developer"})}]},
            "generationConfig": {"maxOutputTokens": max_tokens}}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
    else:
        operation = "api/chat"
        payload.update(messages=[{**x, "role": "system" if x["role"] == "developer" else x["role"]} for x in messages],
            stream=False, options={"num_predict": max_tokens})
        if json_mode:
            payload["format"] = "json"
        if mode == "native_schema":
            payload["format"] = output_schema
    response = _network(snapshot, "POST", operation, payload, timeout, transport)
    if envelope:
        output = response.get("output", []) if isinstance(response, dict) else []
        if not isinstance(output, list):
            raise ProviderError('PROVIDER_INVALID_RESPONSE', {'response_phase':'normalization'})
        calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"]
        if calls:
            if len(calls) != 1 or calls[0].get("name") != "return_structured_result" or not isinstance(calls[0].get("arguments"), str):
                raise ProviderError("UNSUPPORTED_TOOL_CALL")
            if calls[0].get('status') not in {None, 'completed'}:
                raise ProviderError('PROVIDER_RESPONSE_INCOMPLETE')
            if any(not isinstance(item, dict) or item.get("type") not in {"reasoning", "function_call"} for item in output):
                raise ProviderError("UNSUPPORTED_RESPONSE_CONTENT")
            # There is deliberately no tool dispatcher/executor. Only parse the
            # one known function's arguments, then run unchanged output/evidence validation.
            response = {**response, "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": calls[0]["arguments"]}]}]}
    result = normalize_response(protocol, response, model)
    reasoning = payload.get("reasoning") or {}
    requested = True if reasoning or payload.get("reasoning_effort") else (
        False if payload.get("enable_thinking") is False else None)
    result["transport_meta"] = {"reasoning_requested": requested}
    return result


def _validate_completion(normalized, json_mode, validator=None):
    if json_mode and normalized["choices"][0]["finish_reason"] == "stop":
        original = normalized["choices"][0]["message"]["content"]
        # Local rejection must not erase proof of a completed provider response.
        # Count the original normalized text, but never retain it in diagnostics.
        receipt = safe_diagnostic({"outcome": "completed", "finish_reason": "stop",
            "usage": normalized.get("usage", {}), "response_chars": len(original),
            **safe_diagnostic(normalized.get("provider_meta"))})
        content = original.strip().removeprefix("\ufeff").strip()
        # Some text-only providers wrap a correct JSON object in a single
        # Markdown fence. Remove only that transport wrapper, never extract a
        # fragment from prose, repair values, delete reasoning, or add fields.
        fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", content, re.IGNORECASE)
        if fenced:
            content = fenced[1].strip()
        def strict_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicateKeys")
                result[key] = value
            return result
        def reject_constant(_):
            raise ValueError("nonFinite")
        try:
            decoded = json.loads(content, object_pairs_hook=strict_pairs, parse_constant=reject_constant)
            if not isinstance(decoded, dict):
                raise TypeError()
        except (ValueError, TypeError) as exc:
            shape = "fenced" if fenced else "object" if content.startswith("{") else "array" if content.startswith("[") else "other"
            raise ProviderError("PROVIDER_INVALID_JSON_OBJECT", {**receipt, "output_shape": shape,
                **({"json_line": exc.lineno, "json_column": exc.colno} if isinstance(exc, json.JSONDecodeError) else {})}) from None
        normalized["choices"][0]["message"]["content"] = content
        normalized["output_format"] = {"json_object": True, "wrapper_removed": content != original.strip()}
        if validator is not None:
            try:
                error = next(validator.iter_errors(decoded), None)
            except Exception:
                raise ProviderError("PROVIDER_OUTPUT_SCHEMA_INVALID", {
                    **receipt, "response_phase": "schema"}) from None
            if error is not None:
                raise ProviderError("PROVIDER_OUTPUT_SCHEMA_INVALID", {
                    **receipt, "response_phase": "schema"}) from None
    return normalized


def normalize_response(protocol, response, model):
    usage, finish, parts = {}, "stop", []
    meta = safe_diagnostic(response.get("_provider_transport", {})) if isinstance(response, dict) else {}
    try:
        if not isinstance(response, dict):
            raise ValueError()
        if protocol == "openai":
            choices = response.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError()
            choice = choices[0]
            message = choice["message"]
            if message.get("tool_calls") or message.get("function_call") or choice.get("finish_reason") in {"tool_calls", "function_call"}:
                raise ProviderError("UNSUPPORTED_TOOL_CALL")
            if message.get("refusal"):
                raise ProviderError("PROVIDER_REFUSAL")
            parts = [message["content"]] if message.get("content") is not None else []
            if isinstance(message["content"], list):
                parts = []
                for item in message["content"]:
                    if not isinstance(item, dict) or item.get("type") != "text":
                        raise ProviderError("UNSUPPORTED_RESPONSE_CONTENT")
                    parts.append(item["text"])
            finish = choice.get("finish_reason")
            if finish == "content_filter":
                raise ProviderError("PROVIDER_REFUSAL")
            if finish not in {"stop", "length"}:
                raise ProviderError("PROVIDER_RESPONSE_INCOMPLETE")
            usage = response.get("usage", {})
            if isinstance(usage, dict) and isinstance(usage.get("completion_tokens_details"), dict):
                usage = {**usage, "reasoning_tokens": usage["completion_tokens_details"].get("reasoning_tokens")}
        elif protocol == "responses":
            output = response.get("output", [])
            if isinstance(output, list) and any(isinstance(item, dict) and item.get("type") not in {"reasoning", "message"}
                                                for item in output):
                raise ProviderError("UNSUPPORTED_TOOL_CALL")
            status = response.get("status")
            meta["response_status"] = status if status in {"completed", "incomplete", "failed"} else "unknown"
            if status == "incomplete":
                detail = response.get("incomplete_details") or {}
                reason = detail.get("reason") if isinstance(detail, dict) else None
                meta["incomplete_reason"] = reason if reason in {"max_output_tokens", "content_filter"} else "unknown"
                if reason == "content_filter":
                    raise ProviderError("PROVIDER_REFUSAL")
                finish = "length" if reason in {None, "max_output_tokens"} else "incomplete"
            elif status != "completed":
                raise ProviderError("PROVIDER_RESPONSE_FAILED")
            for item in response.get("output", []):
                if item.get("type") in {"reasoning"}:
                    continue
                if item.get("type") != "message":
                    raise ProviderError("UNSUPPORTED_TOOL_CALL")
                if item.get("status") not in {None, "completed"} and not (
                        status == "incomplete" and item.get("status") == "incomplete"):
                    raise ProviderError("PROVIDER_RESPONSE_INCOMPLETE")
                for content in item.get("content", []):
                    if content.get("type") == "refusal":
                        raise ProviderError("PROVIDER_REFUSAL")
                    if content.get("type") != "output_text":
                        raise ProviderError("UNSUPPORTED_RESPONSE_CONTENT")
                    parts.append(content["text"])
            raw = response.get("usage") or {}
            usage = {"prompt_tokens": raw.get("input_tokens"), "completion_tokens": raw.get("output_tokens"), "total_tokens": raw.get("total_tokens")}
            if isinstance(raw.get("output_tokens_details"), dict):
                usage["reasoning_tokens"] = raw["output_tokens_details"].get("reasoning_tokens")
        elif protocol == "anthropic":
            for item in response.get("content", []):
                if item.get("type") in {"thinking", "redacted_thinking"}:
                    continue
                if item.get("type") != "text":
                    raise ProviderError("UNSUPPORTED_TOOL_CALL")
                parts.append(item["text"])
            if response.get("stop_reason") in {"tool_use", "pause_turn"}:
                raise ProviderError("UNSUPPORTED_TOOL_CALL")
            if response.get("stop_reason") == "refusal":
                raise ProviderError("PROVIDER_REFUSAL")
            if response.get("stop_reason") not in {"end_turn", "stop_sequence", "max_tokens", "model_context_window_exceeded"}:
                raise ProviderError("PROVIDER_RESPONSE_INCOMPLETE")
            finish = "length" if response["stop_reason"] in {"max_tokens", "model_context_window_exceeded"} else "stop"
            raw = response.get("usage") or {}
            usage = {"prompt_tokens": raw.get("input_tokens"), "completion_tokens": raw.get("output_tokens")}
        elif protocol == "gemini":
            candidates = response.get("candidates", [])
            if len(candidates) != 1:
                if response.get("promptFeedback", {}).get("blockReason"):
                    raise ProviderError("PROVIDER_REFUSAL")
                raise ProviderError("PROVIDER_EMPTY_OUTPUT")
            candidate = candidates[0]
            if candidate.get("finishReason") in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}:
                raise ProviderError("PROVIDER_REFUSAL")
            if candidate.get("finishReason") not in {"STOP", "MAX_TOKENS"}:
                raise ProviderError("PROVIDER_RESPONSE_INCOMPLETE")
            for item in candidate.get("content", {}).get("parts", []):
                if item.get("thought"):
                    continue
                if "functionCall" in item:
                    raise ProviderError("UNSUPPORTED_TOOL_CALL")
                if "text" not in item:
                    raise ProviderError("UNSUPPORTED_RESPONSE_CONTENT")
                parts.append(item["text"])
            finish = "length" if candidate.get("finishReason") == "MAX_TOKENS" else "stop"
            raw = response.get("usageMetadata") or {}
            usage = {"prompt_tokens": raw.get("promptTokenCount"), "completion_tokens": raw.get("candidatesTokenCount"),
                     "total_tokens": raw.get("totalTokenCount"), "reasoning_tokens": raw.get("thoughtsTokenCount")}
        elif protocol == "ollama":
            message = response.get("message", {})
            if message.get("tool_calls"):
                raise ProviderError("UNSUPPORTED_TOOL_CALL")
            if response.get("done") is not True:
                raise ProviderError("PROVIDER_RESPONSE_INCOMPLETE")
            parts = [message["content"]]
            finish = response.get("done_reason", "stop")
            if finish not in {"stop", "length"}:
                raise ProviderError("PROVIDER_RESPONSE_INCOMPLETE")
            usage = {"prompt_tokens": response.get("prompt_eval_count"), "completion_tokens": response.get("eval_count")}
        else:
            raise ProviderError("UNSUPPORTED_PROTOCOL")
        if not parts and finish in {"length", "incomplete"}:
            parts = [""]  # Preserve the provider's truncation receipt, never deliver it as an answer.
        if not all(isinstance(x, str) for x in parts):
            raise ValueError()
        if not parts or (finish == "stop" and not "".join(parts).strip()):
            raise ProviderError("PROVIDER_EMPTY_OUTPUT")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise TypeError()
        clean_usage = safe_usage(usage)
        if "prompt_tokens" in clean_usage and "completion_tokens" in clean_usage:
            clean_usage.setdefault("total_tokens", clean_usage["prompt_tokens"] + clean_usage["completion_tokens"])
        meta.update(outcome="completed" if finish == "stop" else "incomplete", finish_reason=finish, usage=clean_usage)
        return {"id": str(response.get("id", ""))[:256], "model": str(response.get("model", model))[:512],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(parts)}, "finish_reason": finish}],
            "usage": clean_usage, "provider_meta": safe_diagnostic(meta)}
    except ProviderError as exc:
        outcome = {"PROVIDER_REFUSAL": "refusal", "PROVIDER_EMPTY_OUTPUT": "empty",
                   "PROVIDER_RESPONSE_INCOMPLETE": "incomplete", "PROVIDER_RESPONSE_FAILED": "failed"}.get(exc.code, "protocol_error")
        exc.diagnostic = safe_diagnostic({**meta, "outcome": outcome, "usage": safe_usage(usage), **exc.diagnostic})
        raise
    except (KeyError, ValueError, TypeError, AttributeError):
        output = response.get('output', []) if isinstance(response, dict) else []
        output = output if isinstance(output, list) else []
        kinds = [item.get('type') for item in output if isinstance(item, dict)]
        raw_usage = response.get('usage', {}) if isinstance(response, dict) else {}
        detail = {**meta, 'outcome': 'protocol_error', 'response_phase':'normalization', 'message_items':kinds.count('message'),
            'reasoning_items':kinds.count('reasoning'), 'function_items':kinds.count('function_call'),
            'response_status':response.get('status','unknown') if isinstance(response,dict) else 'unknown'}
        if isinstance(raw_usage, dict):
            detail['completion_tokens'] = raw_usage.get('output_tokens', raw_usage.get('completion_tokens'))
        raise ProviderError("PROVIDER_INVALID_RESPONSE", detail) from None


def _text_model(snapshot, row, model_id):
    non_text = r"(?:embed|rerank|\btts\b|whisper|image|sora|realtime|moderation|transcrib|audio|video|speech|babbage|davinci)"
    if re.search(non_text, model_id.lower()):
        return False
    if snapshot["protocol"] == "gemini":
        return "generateContent" in row.get("supportedGenerationMethods", [])
    if snapshot["provider_id"] == "mistral":
        caps = row.get("capabilities")
        return isinstance(caps, dict) and caps.get("completion_chat") is True
    architecture = row.get("architecture")
    modalities = architecture.get("output_modalities") if isinstance(architecture, dict) else None
    if isinstance(modalities, list) and ("text" not in modalities or any(x != "text" for x in modalities)):
        return False
    if row.get("type") in {"embedding", "embeddings", "image", "audio", "video", "rerank"}:
        return False
    capabilities = row.get("capabilities")
    if isinstance(capabilities, list) and "completion" not in capabilities and "chat" not in capabilities:
        return False
    if snapshot["provider_id"] == "openai":
        return model_id.startswith(("gpt-", "chatgpt-", "chat-latest", "o1", "o3", "o4", "codex-", "ft:gpt-"))
    return True


def _list_models(snapshot, limit=1000, *, transport=None, metadata=None):
    protocol = snapshot["protocol"]
    limit = min(1000, max(1, int(limit)))
    operation = "api/tags" if protocol == "ollama" else "models"
    if snapshot["provider_id"] == "siliconflow":
        operation = "models?type=text"
    models, seen, cursors = [], set(), set()
    truncated, page, started = False, 0, time.monotonic()
    while operation:
        budget = float(snapshot.get("http_timeout", 60)) - (time.monotonic() - started)
        if budget <= 0:
            raise ProviderError("PROVIDER_TIMEOUT")
        response = _network(snapshot, "GET", operation, timeout=budget, transport=transport)
        rows = response.get("models" if protocol in {"gemini", "ollama"} else "data")
        if not isinstance(rows, list):
            raise ProviderError("PROVIDER_INVALID_MODEL_LIST")
        page += 1
        next_cursor = response.get("nextPageToken") if protocol == "gemini" else response.get("last_id") if protocol == "anthropic" and response.get("has_more") else None
        if next_cursor and (not isinstance(next_cursor, str) or len(next_cursor) > 2048 or next_cursor in cursors):
            raise ProviderError("PROVIDER_INVALID_PAGINATION")
        remaining = limit - len(models)
        converted = _model_rows(snapshot, rows, seen)
        models.extend(converted[:remaining])
        if len(converted) > remaining or (next_cursor and (len(models) >= limit or page >= 10)):
            truncated = True
            break
        if next_cursor:
            cursors.add(next_cursor)
            key = "pageToken" if protocol == "gemini" else "after_id"
            operation = "models?" + key + "=" + quote(next_cursor, safe="")
        else:
            operation = None
    if metadata is not None:
        metadata.update(truncated=truncated, pages=page)
    return models


def _model_rows(snapshot, rows, seen):
    protocol = snapshot["protocol"]
    models = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderError("PROVIDER_INVALID_MODEL_LIST")
        model_id = row.get("name" if protocol in {"gemini", "ollama"} else "id")
        if protocol == "gemini" and isinstance(model_id, str):
            model_id = model_id.removeprefix("models/")
        if not isinstance(model_id, str) or not model_id or len(model_id) > 512 or re.search(r"[\x00-\x20]", model_id):
            continue
        if model_id in seen or not _text_model(snapshot, row, model_id):
            continue
        seen.add(model_id)
        brand = model_id.split("/", 1)[0] if snapshot["kind"] == "gateway" and "/" in model_id else snapshot["provider_id"]
        item = {"id": model_id, "name": str(row.get("displayName") or row.get("display_name") or row.get("name") or model_id)[:300],
            "brand": brand, "source": "synced"}
        supported = row.get("supported_parameters")
        if isinstance(supported, list):
            item["supported_parameters"] = [x for x in supported if isinstance(x, str) and x in {"response_format", "max_tokens", "max_completion_tokens", "reasoning"}]
        context = row.get("context_length") or row.get("inputTokenLimit") or row.get("max_input_tokens")
        if type(context) is int and context > 0:
            item["context_length"] = context
        models.append(item)
    return models


def authorize_model_snapshot(db, user, snapshot, settings):
    policy = load_connection(db, user, snapshot.get("id"), settings)
    if snapshot.get("owner_user_id") != user.id:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if policy.revision != snapshot.get("revision") or not policy.config.get("enabled"):
        fail(409, "CONNECTION_REVISION_CHANGED", "模型连接已更新或停用")
    require_connection_space(db, user, snapshot.get("context_space_id") or snapshot.get("space_id"), settings)
    if snapshot.get("allow_document_transfer") and not policy.config.get("allow_document_transfer"):
        fail(409, "DOCUMENT_TRANSFER_NOT_AUTHORIZED", "资料传输授权已撤回")
    if policy.config.get("protocol") == "codex_app_server":
        from .api_oauth import state_policy
        state = state_policy(db, policy)
        if state.config.get("state") != "AUTHENTICATED" \
                or state.config.get("auth_epoch") != snapshot.get("auth_epoch") \
                or state.config.get("connection_revision") != policy.revision:
            fail(409, "CODEX_LOGIN_STALE", "订阅登录已失效或退出")


def authorize_model_job(db, user, job, settings, *, action="read"):
    if not job or not user or not user.active or job.owner_id != user.id \
        or job.payload.get("owner_user_id") != user.id:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    policy = load_connection(db, user, job.payload.get("connection_id"), settings,
        include_deleted=action == "read" or job.payload.get("task") == "CODEX_LOGOUT", check_env=False)
    stale = policy.revision != job.payload.get("connection_revision") or (
        bool(policy.config.get("deleted_at")) and job.payload.get("task") != "CODEX_LOGOUT"
    ) or (
        not policy.config.get("enabled") and job.payload.get("task") != "CODEX_LOGOUT"
    )
    space_id = job.payload.get("context_space_id")
    if space_id:
        require_connection_space(db, user, space_id, settings)
    reason = "CONNECTION_REVISION_CHANGED" if stale else None
    if policy.config.get("credential_mode") == "env":
        from .services import APIError
        try:
            authorize_env_reference(settings, user, policy.config.get("api_key_env"))
        except (ProviderError, APIError):
            reason = "CREDENTIAL_REFERENCE_FORBIDDEN"
    if policy.config.get("protocol") == "codex_app_server":
        from .api_oauth import authorize_oauth_job
        from .services import APIError
        try:
            authorize_oauth_job(db, user, job, settings)
        except APIError as exc:
            reason = exc.code
    if reason:
        if action == "read" and job.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            # Transient response projection; do not overwrite durable historical evidence.
            set_committed_value(job, "result", {"connection_id": policy.id, "state": job.state,
                "details_unavailable_reason": reason})
        else:
            fail(409, reason, "连接或身份授权已变化，请重新发起任务")
    return policy


def update_connection_status(db, policy, fields, *, owner_id):
    allowed = {"status", "last_error_code", "last_synced_at", "models", "synced_models"}
    if set(fields) - allowed:
        raise ProviderError("INVALID_CONNECTION_STATUS_UPDATE")
    changed = {**policy.config, **fields, "status_revision": policy.config.get("status_revision", 0) + 1}
    # RuntimePolicy has SQLAlchemy version_id_col: assigning .config would bump
    # the authorization revision. Use CAS without changing that revision.
    result = db.execute(update(m.RuntimePolicy).where(m.RuntimePolicy.id == policy.id,
        m.RuntimePolicy.revision == policy.revision).values(config=changed, updated_by=owner_id)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise ProviderError("CONNECTION_REVISION_CHANGED")
    db.expire(policy)
    return changed


def run_connection_job(settings, session_factory, job_id, attempt, checkpoint, *, bridge=None):
    """The dispatcher owns leases and job state; this function only returns safe results."""
    checkpoint("MODEL_CONNECTION_AUTHORIZATION", {})
    with session_factory() as db:
        candidate = db.get(m.Job, job_id)
        candidate_connection = db.get(m.RuntimePolicy, (candidate.payload or {}).get("connection_id")) if candidate else None
        is_codex = bool(candidate_connection and candidate_connection.config.get("protocol") == "codex_app_server")
    if is_codex:
        from .api_oauth import run_codex_model_sync
        # Delegate after the routing transaction has closed.
        return run_codex_model_sync(settings, session_factory, job_id, attempt, checkpoint, bridge=bridge)
    with session_factory() as db:
        job = db.get(m.Job, job_id)
        if not job or job.kind != "COMPILE" or job.payload.get("task") not in {"MODEL_SYNC", "MODEL_TEST"}:
            raise ProviderError("INVALID_MODEL_JOB")
        if job.cancel_requested or job.attempts != attempt or job.state != "RUNNING":
            raise ProviderError("MODEL_JOB_CANCELLED_OR_STALE")
        user = db.get(m.User, job.owner_id)
        policy = authorize_model_job(db, user, job, settings, action="execute")
        if policy.revision != job.payload["connection_revision"]:
            raise ProviderError("CONNECTION_REVISION_CHANGED")
        config = dict(policy.config)
        model_id = job.payload.get("model_id")
        choices = connection_models(config)
        if not model_id and choices:
            model_id = choices[0]["id"]
        if job.payload["task"] == "MODEL_TEST" and not model_id:
            raise ProviderError("MODEL_NOT_CONFIGURED")
        selected = next((row for row in choices if row["id"] == model_id), None)
        if model_id and not selected:
            raise ProviderError("MODEL_NOT_CONFIGURED")
        provider = provider_by_id(config["provider_id"])
        local_hosts = list(getattr(settings, "provider_local_hosts", []) or [])
        snapshot = {**{k: config.get(k) for k in SNAPSHOT_KEYS}, "revision": policy.revision,
            "space_id": None, "context_space_id": None, "model_id": model_id,
            "base_url": validate_base_url(config["base_url"], provider["kind"], local_hosts),
            "kind": provider["kind"], "api_key": _connection_credential(config, settings, provider["kind"], user),
            "supported_parameters": (selected or {}).get("supported_parameters"),
            "local_hosts": local_hosts, "max_request_bytes": getattr(settings, "provider_max_request_bytes", 2097152),
            "max_response_bytes": getattr(settings, "provider_max_response_bytes", 8388608),
            "http_timeout": getattr(settings, "provider_http_timeout_seconds", 60)}
        task, expected, owner_id = job.payload["task"], policy.revision, user.id
    # Neither DNS nor HTTP nor model inference holds a database transaction.
    checkpoint("MODEL_SYNC" if task == "MODEL_SYNC" else "MODEL_TEST", {"connection_id": snapshot["id"]})
    error_code, synced, sync_metadata = None, None, {}
    try:
        if task == "MODEL_SYNC":
            synced = _list_models(snapshot, limit=int(getattr(settings, "provider_max_models", 1000)), metadata=sync_metadata)
        else:
            response = complete(snapshot, [{"role": "user", "content": 'Return exactly {"ok":true}.'}], max_tokens=1024)
            choice = response["choices"][0]
            if choice["finish_reason"] != "stop" or json.loads(choice["message"]["content"]) != {"ok": True}:
                raise ProviderError("MODEL_PROBE_INVALID_RESPONSE")
    except ProviderError as exc:
        error_code = exc.code
    except (ValueError, KeyError, TypeError):
        error_code = "MODEL_PROBE_INVALID_RESPONSE"
    checkpoint("MODEL_CONNECTION_RECHECK", {"connection_id": snapshot["id"]})
    with session_factory.begin() as db:
        user = db.get(m.User, owner_id)
        job = db.get(m.Job, job_id)
        policy = authorize_model_job(db, user, job, settings, action="complete")
        if policy.revision != expected or not policy.config.get("enabled"):
            raise ProviderError("CONNECTION_REVISION_CHANGED")
        if job.cancel_requested or job.attempts != attempt or job.state != "RUNNING":
            raise ProviderError("MODEL_JOB_CANCELLED_OR_STALE")
        changed = dict(policy.config)
        changed["last_error_code"] = error_code
        changed["status"] = "ERROR" if error_code else "PARTIAL_SYNC" if sync_metadata.get("truncated") else "SYNCED" if task == "MODEL_SYNC" else "TESTED"
        if synced is not None:
            changed["synced_models"], changed["last_synced_at"] = synced, primitive(now())
            changed["models"] = connection_models(changed)
        update_connection_status(db, policy, {k: changed[k] for k in (
            "status", "last_error_code", "last_synced_at", "models", "synced_models") if k in changed}, owner_id=owner_id)
        result = {"connection_id": policy.id, "status": changed["status"], "model_id": model_id,
            "model_count": len(changed["models"]), "last_error_code": error_code,
            "model": public_snapshot(snapshot), **sync_metadata}
    if error_code:
        raise ProviderError(error_code)
    return result
