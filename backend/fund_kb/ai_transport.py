"""Explicitly configured, bounded institutional HTTP transport; no environment credentials."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from urllib.parse import urlsplit

import httpx

from .provider_contracts import seconds, timeout_budget


class ProviderError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)  # Provider response bodies, URLs and keys never become error messages.


def endpoint(base_url: str | None, operation: str) -> str:
    if not base_url:
        raise ProviderError("PROVIDER_NOT_CONFIGURED")
    url = urlsplit(str(base_url))
    if (url.username or url.password or url.query or url.fragment or not url.hostname
            or url.scheme not in {"http", "https"}):
        raise ProviderError("INVALID_PROVIDER_URL")
    if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ProviderError("PROVIDER_TLS_REQUIRED")
    base = str(base_url).rstrip("/")
    return base if base.endswith("/" + operation) else base + "/" + operation


def _post_request(url, payload, api_key, budget, read_idle_timeout=None, deadline=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        key = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else str(api_key)
        headers["Authorization"] = "Bearer " + key
    started = time.monotonic()
    deadline = started + budget if deadline is None else deadline
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise ProviderError("PROVIDER_TIMEOUT")
        return value
    try:
        # Explicitly disable environment proxy/netrc credential discovery and redirects.
        with (httpx.Client(timeout=httpx.Timeout(remaining(), connect=min(10.0, remaining()),
                           read=min(read_idle_timeout or budget, budget), pool=min(5.0, remaining())),
                           follow_redirects=False, trust_env=False) as client,
              client.stream("POST", url, json=payload, headers=headers) as response):
            if response.status_code != 200:
                raise ProviderError("PROVIDER_HTTP_ERROR")
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise ProviderError("PROVIDER_RESPONSE_TOO_LARGE")
                if time.monotonic() - started > budget:
                    raise ProviderError("PROVIDER_TIMEOUT")
                remaining()
        remaining()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ProviderError("PROVIDER_INVALID_JSON")
        remaining()
        return result
    except ProviderError:
        raise
    except httpx.TimeoutException:
        raise ProviderError("PROVIDER_TIMEOUT") from None
    except (httpx.HTTPError, ValueError, UnicodeError):
        raise ProviderError("PROVIDER_INVALID_RESPONSE") from None


def post_json(base_url: str, operation: str, payload: dict, api_key=None, timeout: float = 30.0,
              *, read_idle_timeout=None, deployment_timeout=None, cancel_check=None) -> dict:
    """Finite callers retain wall/socket budgets; None requires cancellable waiting.

    A timed-out HTTP request cannot be recalled from the provider. Its result is
    discarded; the worker socket remains bounded and closes via _post_request.
    """
    url = endpoint(base_url, operation)
    if timeout is None:
        from . import providers
        from .provider_contracts import ThrottledCheck
        if not callable(cancel_check):
            raise ProviderError("CANCELLATION_CHECK_REQUIRED")
        def checkpoint():
            if cancel_check() is False:
                raise ProviderError("CANCELLED")
        guard = ThrottledCheck(checkpoint, time.monotonic)
        guard.force()
        key = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else api_key
        legacy = {"base_url": base_url,
                  "kind": "local" if urlsplit(url).hostname in providers.LOCAL_HOSTS else "custom",
                  "protocol": "openai", "api_key": key,
                  "max_request_bytes": 2 * 1024 * 1024, "max_response_bytes": 2 * 1024 * 1024,
                  "_cancel_check": guard}
        try:
            result = providers._request_json_cancellable(legacy, "POST", url, payload,
                {"Authorization": "Bearer " + str(key)} if key else {})
        except providers.ProviderError as exc:
            raise ProviderError(exc.code) from None
        guard.force()
        result.pop("_provider_transport", None)
        return result
    try:
        budget = timeout_budget(timeout, deployment_timeout)
        if read_idle_timeout is not None:
            seconds(read_idle_timeout)
    except ValueError:
        raise ProviderError("INVALID_TIMEOUT") from None
    deadline = time.monotonic() + budget
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fundkb-http")
    future = executor.submit(_post_request, url, payload, api_key, budget, read_idle_timeout, deadline)
    try:
        return future.result(timeout=max(0, deadline - time.monotonic()))
    except FutureTimeoutError:
        future.cancel()
        raise ProviderError("PROVIDER_TIMEOUT") from None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
