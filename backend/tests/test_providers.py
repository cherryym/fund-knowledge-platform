"""Offline provider integration/security tests. All credentials and HTTP responses are synthetic."""
from __future__ import annotations

import json
import stat

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from test_api import (
    EDITOR,
    OUTSIDER,
    READER,
    SPACE,
)
from test_api import api as api  # noqa: PLC0414 - intentional pytest fixture re-export

from fund_kb import api_models, provider_catalog, providers
from fund_kb import models as m
from fund_kb.services import uid

SYNTHETIC_KEY = "synthetic-provider-secret-for-offline-tests"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(providers, "_dns_addresses", lambda host, port: ["127.0.0.1"] if host in providers.LOCAL_HOSTS else ["93.184.216.34"])
    def deny_real_request(*args, **kwargs):
        raise AssertionError("Real provider traffic is forbidden in this test suite")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_real_request)


def connection_body(provider_id="openai", **overrides):
    return {"space_id": SPACE, "name": "离线测试连接", "provider_id": provider_id,
        "credential_mode": "encrypted", "api_key": SYNTHETIC_KEY, "enabled": True,
        "allow_document_transfer": False, **overrides}


def create_connection(api, **overrides):
    response = api.call("POST", "/model-connections", connection_body(**overrides))
    assert response.status_code == 201, response.text
    return response


def snapshot(protocol="responses", provider_id="openai", **extra):
    p = provider_catalog.provider_by_id(provider_id)
    return {"id": uid(), "space_id": SPACE, "name": "synthetic", "revision": 1,
        "provider_id": provider_id, "kind": p["kind"], "protocol": protocol, "base_url": p["base_url"],
        "model_id": "gpt-6-astra", "brand": provider_id, "api_key": SYNTHETIC_KEY,
        "credential_mode": "encrypted", "allow_document_transfer": True, **extra}


def response_fixture(protocol, content='{"ok":true}'):
    if protocol == "responses":
        return {"id": "response-test", "status": "completed", "output": [{"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": content}]}],
            "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}}
    if protocol == "anthropic":
        return {"content": [{"type": "thinking", "thinking": "not public"}, {"type": "text", "text": content}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 4}}
    if protocol == "gemini":
        return {"candidates": [{"content": {"parts": [{"text": "not public", "thought": True}, {"text": content}]},
            "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4, "totalTokenCount": 7}}
    if protocol == "ollama":
        return {"message": {"role": "assistant", "content": content, "thinking": "not public"},
            "done": True, "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 4}
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}


def test_extension_shape_and_current_official_catalog(api):
    assert len(api_models.HANDLERS) == 10
    result = api.call("GET", "/model-providers")
    assert result.status_code == 200
    assert result.json()["live_verified"] is False
    catalog = {p["id"]: p for p in result.json()["items"]}
    assert set(catalog) == {"openai", "anthropic", "google", "deepseek", "qwen", "moonshot", "zhipu", "doubao",
        "minimax", "mistral", "xai", "openrouter", "siliconflow", "ollama", "lmstudio", "local-openai", "custom", "chatgpt-codex"}
    assert catalog["openai"]["models"][0]["id"] == "gpt-6-astra"
    assert {x["id"] for x in catalog["anthropic"]["models"]} >= {"claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"}
    assert all(x["availability"] == "UNKNOWN" for p in catalog.values() for x in p["models"])
    assert catalog["ollama"]["models"] == catalog["lmstudio"]["models"] == []


def test_encrypted_credentials_never_leave_api_or_audit_and_key_is_private(api):
    created = create_connection(api)
    public = created.json()
    assert public["credential_present"] is True and public["status"] == "UNVERIFIED"
    assert public["allow_document_transfer"] is False
    private = api.app.state.settings.storage_dir / "private" / "provider-master.key"
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    with api.app.state.session_factory() as db:
        policy = db.get(m.RuntimePolicy, public["id"])
        ciphertext = policy.config["credential_ciphertext"]
        assert SYNTHETIC_KEY not in json.dumps(policy.config)
        assert providers.credential_value(policy.config, api.app.state.settings) == SYNTHETIC_KEY
        rows = db.scalars(select(m.AuditEvent)).all()
        assert all(SYNTHETIC_KEY not in json.dumps(row.details) for row in rows)
        idem = db.scalars(select(m.IdempotencyRecord)).all()
        assert all(SYNTHETIC_KEY not in json.dumps(row.response) for row in idem)
    for path in (f"/model-connections/{public['id']}", f"/model-connections?space_id={SPACE}", f"/model-options?space_id={SPACE}"):
        result = api.call("GET", path)
        assert result.status_code == 200
        assert SYNTHETIC_KEY not in result.text and ciphertext not in result.text
        assert "credential_ciphertext" not in result.text


def test_config_writes_do_not_perform_network_and_etag_and_permissions_hold(api):
    created = create_connection(api)
    id = created.json()["id"]
    assert api.call("PATCH", f"/model-connections/{id}", {"name": "改名"}).status_code == 428
    changed = api.call("PATCH", f"/model-connections/{id}", {"name": "改名"}, created.headers["etag"])
    assert changed.status_code == 200 and changed.json()["credential_present"]
    assert api.call("PATCH", f"/model-connections/{id}", {"name": "过期"}, created.headers["etag"]).status_code == 412
    api.login(READER)
    assert api.call("GET", f"/model-connections/{id}").status_code == 404
    assert api.call("PATCH", f"/model-connections/{id}", {"name": "越权"}, changed.headers["etag"]).status_code == 404
    assert api.call("POST", f"/model-connections/{id}/test", {}, etag=changed.headers["etag"]).status_code == 404
    api.login(OUTSIDER)
    assert api.call("GET", f"/model-connections/{id}").status_code == 404
    assert api.call("GET", f"/model-options?space_id={SPACE}").status_code == 404


def test_snapshot_transfer_gate_revision_and_public_allowlist(api):
    created = create_connection(api)
    id = created.json()["id"]
    with api.app.state.session_factory() as db:
        user = db.get(m.User, EDITOR)
        with pytest.raises(providers.ProviderError, match="DOCUMENT_TRANSFER_NOT_AUTHORIZED"):
            providers.resolve_connection(db, user, SPACE, id, "gpt-6-astra", api.app.state.settings, require_transfer=True)
        with pytest.raises(providers.ProviderError, match="CONNECTION_REVISION_CHANGED"):
            providers.resolve_connection(db, user, SPACE, id, "gpt-6-astra", api.app.state.settings, expected_revision=999)
        private = providers.resolve_connection(db, user, SPACE, id, "gpt-6-astra", api.app.state.settings)
        assert private["api_key"] == SYNTHETIC_KEY
        public = providers.public_snapshot(private)
        assert set(public) == set(providers.SNAPSHOT_KEYS)
        assert "api_key" not in public and SYNTHETIC_KEY not in json.dumps(public)


def test_environment_references_are_explicit_and_master_key_cannot_be_forwarded(api, monkeypatch):
    monkeypatch.setenv("FKB_SYNTHETIC_API_KEY", SYNTHETIC_KEY)
    body = connection_body(credential_mode="env", api_key_env="FKB_SYNTHETIC_API_KEY")
    body.pop("api_key")
    result = api.call("POST", "/model-connections", body)
    assert result.status_code == 201 and result.json()["credential_present"] is True
    with api.app.state.session_factory() as db:
        private = providers.resolve_connection(db, db.get(m.User, EDITOR), SPACE, result.json()["id"], "gpt-6-astra", api.app.state.settings)
        assert private["api_key"] == SYNTHETIC_KEY
    for reference in ("OPENAI_API_KEY", "FKB_PROVIDER_MASTER_KEY", "FKB_DATABASE_URL", "FKB_OIDC_CLIENT_SECRET"):
        assert api.call("POST", "/model-connections", {**body, "api_key_env": reference}).status_code == 422
    monkeypatch.delenv("FKB_SYNTHETIC_API_KEY")
    assert api.call("GET", f"/model-connections/{result.json()['id']}").json()["credential_present"] is False


def test_master_key_required_in_production_and_ciphertext_bound_to_connection(api):
    settings = api.app.state.settings.model_copy(update={"app_env": "production", "provider_master_key": None})
    with pytest.raises(providers.ProviderError, match="PROVIDER_MASTER_KEY_REQUIRED"):
        providers.encrypt_credential(settings, uid(), SPACE, SYNTHETIC_KEY)
    settings = settings.model_copy(update={"provider_master_key": Fernet.generate_key().decode()})
    id = uid()
    ciphertext = providers.encrypt_credential(settings, id, SPACE, SYNTHETIC_KEY)
    config = {"id": id, "space_id": SPACE, "credential_mode": "encrypted", "credential_ciphertext": ciphertext}
    assert providers.credential_value(config, settings) == SYNTHETIC_KEY
    with pytest.raises(providers.ProviderError, match="CREDENTIAL_DECRYPT_FAILED"):
        providers.credential_value({**config, "id": uid()}, settings)


def test_delete_erases_credential_and_retains_audit_record(api):
    created = create_connection(api)
    id = created.json()["id"]
    deleted = api.call("DELETE", f"/model-connections/{id}", etag=created.headers["etag"])
    assert deleted.status_code == 204
    assert api.call("GET", f"/model-connections/{id}").status_code == 404
    with api.app.state.session_factory() as db:
        policy = db.get(m.RuntimePolicy, id)
        assert policy.config["credential_ciphertext"] is None and policy.config["api_key_env"] is None
        assert policy.config["enabled"] is False and policy.config["deleted_at"]
        assert db.scalars(select(m.AuditEvent).where(m.AuditEvent.object_id == id)).all()


@pytest.mark.parametrize("url", ["https://user:secret@example.com/v1", "https://example.com/v1?key=secret", "https://example.com/v1#secret",
    "http://example.com/v1", "https://127.0.0.1/v1", "https://169.254.169.254/latest", "https://10.0.0.1/v1",
    "file:///tmp/test", "https://example.com\\@127.0.0.1/v1", "https://example.com/%2e%2e/private"])
def test_unsafe_urls_are_rejected_without_network(url):
    with pytest.raises(providers.ProviderError):
        providers.validate_base_url(url)


@pytest.mark.parametrize("protocol,provider_id,model_id,path", [
    ("responses", "openai", "gpt-6-astra", "/v1/responses"),
    ("openai", "openai", "gpt-6-astra", "/v1/chat/completions"),
    ("anthropic", "anthropic", "claude-opus-5", "/v1/messages"),
    ("gemini", "google", "gemini-3.8-flash", "/v1beta/models/gemini-3.8-flash:generateContent"),
    ("ollama", "ollama", "local-model:latest", "/api/chat"),
])
def test_native_protocol_request_and_response_are_correct(protocol, provider_id, model_id, path):
    sent = []
    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=response_fixture(protocol))
    result = providers.complete(snapshot(protocol, provider_id, model_id=model_id),
        [{"role": "system", "content": "Bounded context"}, {"role": "user", "content": "Probe"}],
        max_tokens=512, transport=httpx.MockTransport(respond))
    assert result["choices"][0]["message"]["content"] == '{"ok":true}'
    assert result["choices"][0]["finish_reason"] == "stop" and result["usage"]["total_tokens"] == 7
    assert sent[0].url.path == path
    payload = json.loads(sent[0].content)
    assert not set(payload) & {"temperature", "top_p", "top_logprobs", "logprobs"}
    if protocol == "responses":
        assert payload["reasoning"] == {"effort": "low"} and payload["max_output_tokens"] == 512 and payload["store"] is False
    elif protocol == "openai":
        assert payload["max_completion_tokens"] == 512 and "max_tokens" not in payload
    elif protocol == "anthropic":
        assert sent[0].headers["x-api-key"] == SYNTHETIC_KEY and sent[0].headers["anthropic-version"] == "2023-06-01"
        assert all(m["role"] != "system" for m in payload["messages"]) and "Bounded context" in payload["system"]
        assert "response_format" not in payload
    elif protocol == "gemini":
        assert sent[0].headers["x-goog-api-key"] == SYNTHETIC_KEY
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
    else:
        assert payload["stream"] is False and payload["format"] == "json" and payload["options"]["num_predict"] == 512


@pytest.mark.parametrize("protocol,response", [
    ("openai", {"choices": [{"message": {"tool_calls": [{"id": "call"}], "content": "looks successful"}, "finish_reason": "tool_calls"}]}),
    ("responses", {"output": [{"type": "function_call", "name": "write_database"}]}),
    ("anthropic", {"content": [{"type": "tool_use", "name": "write_database"}], "stop_reason": "tool_use"}),
    ("gemini", {"candidates": [{"content": {"parts": [{"functionCall": {"name": "write_database"}}]}, "finishReason": "STOP"}]}),
    ("ollama", {"message": {"content": "", "tool_calls": [{"function": {"name": "write_database"}}]}, "done": True}),
])
def test_native_tool_calls_are_never_silently_text_success(protocol, response):
    with pytest.raises(providers.ProviderError, match="UNSUPPORTED_TOOL_CALL"):
        providers.normalize_response(protocol, response, "synthetic")


def test_redirect_size_timeout_and_error_bodies_do_not_leak_keys():
    for response, code in [(httpx.Response(302, headers={"Location": "https://untrusted.invalid"}), "PROVIDER_REDIRECT_BLOCKED"),
        (httpx.Response(401, text=SYNTHETIC_KEY), "PROVIDER_AUTH_FAILED"),
        (httpx.Response(200, content=b"x" * 100), "PROVIDER_RESPONSE_TOO_LARGE")]:
        with pytest.raises(providers.ProviderError) as caught:
            providers.complete(snapshot(max_response_bytes=50), [{"role": "user", "content": "Probe"}], transport=httpx.MockTransport(lambda request, result=response: result))
        assert caught.value.code == code and SYNTHETIC_KEY not in str(caught.value)
    def timeout(request):
        raise httpx.ReadTimeout(SYNTHETIC_KEY)
    with pytest.raises(providers.ProviderError, match="PROVIDER_TIMEOUT") as caught:
        providers.complete(snapshot(), [{"role": "user", "content": "Probe"}], transport=httpx.MockTransport(timeout))
    assert SYNTHETIC_KEY not in str(caught.value)


def test_response_echo_of_credential_is_blocked():
    with pytest.raises(providers.ProviderError, match="PROVIDER_SECRET_ECHO"):
        providers.complete(snapshot(), [{"role": "user", "content": "Probe"}], transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response_fixture("responses", SYNTHETIC_KEY))))


def test_dns_rebinding_is_blocked_and_validated_ip_preserves_host_sni(monkeypatch):
    monkeypatch.setattr(providers, "_dns_addresses", lambda host, port: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(providers.ProviderError, match="PROVIDER_ADDRESS_BLOCKED"):
        providers.complete(snapshot(), [{"role": "user", "content": "Probe"}])
    calls = []
    def resolve(host, port):
        calls.append(host)
        return ["93.184.216.34"] if len(calls) == 1 else ["127.0.0.1"]
    monkeypatch.setattr(providers, "_dns_addresses", resolve)
    def respond(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "api.openai.com"
        assert request.extensions["sni_hostname"] == "api.openai.com"
        return httpx.Response(200, json=response_fixture("responses"))
    providers.complete(snapshot(), [{"role": "user", "content": "Probe"}], transport=httpx.MockTransport(respond))
    assert calls == ["api.openai.com"]


def test_sync_and_probe_jobs_commit_before_network_and_recheck_revision(api, monkeypatch):
    created = create_connection(api)
    id = created.json()["id"]
    queued = api.call("POST", f"/model-connections/{id}/sync-models", etag=created.headers["etag"])
    assert queued.status_code == 202
    job_id = queued.json()["id"]
    with api.app.state.session_factory.begin() as db:
        job = db.get(m.Job, job_id)
        assert job.kind == "COMPILE" and job.payload["task"] == "MODEL_SYNC"
        assert "api_key" not in json.dumps(job.payload)
        assert db.scalars(select(m.Outbox).where(m.Outbox.aggregate_id == job_id)).first()
        job.state, job.attempts = "RUNNING", 1
    def network(snapshot, *args, **kwargs):
        # A separate write transaction succeeds while the provider request is running.
        with api.app.state.session_factory.begin() as db:
            assert db.get(m.RuntimePolicy, id)
            db.add(m.AuditEvent(id=uid(), actor_id=EDITOR, action="synthetic.concurrent", object_type="test", outcome="SUCCESS", trace_id=uid(), details={}))
        return {"data": [{"id": "gpt-6-account-test", "name": "Synced model"}]}
    monkeypatch.setattr(providers, "_network", network)
    result = providers.run_connection_job(api.app.state.settings, api.app.state.session_factory, job_id, 1, lambda stage, details: None)
    assert result["status"] == "SYNCED" and result["model_count"] == 1
    public = api.call("GET", f"/model-connections/{id}").json()
    assert public["models"][0]["id"] == "gpt-6-account-test" and public["models"][0]["source"] == "synced"
    assert public["revision"] == created.json()["revision"]
    queued = api.call("POST", f"/model-connections/{id}/test", {"model_id": "gpt-6-account-test"}, etag=created.headers["etag"])
    with api.app.state.session_factory.begin() as db:
        job = db.get(m.Job, queued.json()["id"])
        job.state, job.attempts = "RUNNING", 1
    def revoke(snapshot, *args, **kwargs):
        with api.app.state.session_factory.begin() as db:
            policy = db.get(m.RuntimePolicy, id)
            policy.config = {**policy.config, "enabled": False}
        return response_fixture("responses")
    monkeypatch.setattr(providers, "_network", revoke)
    from fund_kb.services import APIError
    with pytest.raises(APIError) as changed_error:
        providers.run_connection_job(api.app.state.settings, api.app.state.session_factory, queued.json()["id"], 1, lambda stage, details: None)
    assert changed_error.value.code == "CONNECTION_REVISION_CHANGED"


@pytest.mark.parametrize("provider_id,protocol,rows,expected", [
    ("openai", "responses", [{"id": name} for name in ("gpt-6-astra", "gpt-5.6-sol", "text-embedding-3-large", "gpt-image-2",
        "gpt-audio", "gpt-realtime", "whisper-1", "sora-2", "omni-moderation-latest", "tts-1")], ["gpt-6-astra", "gpt-5.6-sol"]),
    ("mistral", "openai", [{"id": "mistral-medium-3-5", "capabilities": {"completion_chat": True}},
        {"id": "mistral-embed", "capabilities": {"completion_chat": False}}, {"id": "unknown-no-capabilities"}], ["mistral-medium-3-5"]),
    ("google", "gemini", [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-3.1-flash-image", "supportedGenerationMethods": ["generateContent"]}], ["gemini-3.8-flash"]),
    ("openrouter", "openai", [{"id": "openai/gpt-6-astra", "architecture": {"output_modalities": ["text"]}},
        {"id": "vendor/painter", "architecture": {"output_modalities": ["image"]}},
        {"id": "vendor/multi-output", "architecture": {"output_modalities": ["text", "image"]}}], ["openai/gpt-6-astra"]),
    ("ollama", "ollama", [{"name": "qwen3:8b"}, {"name": "nomic-embed-text:latest"}], ["qwen3:8b"]),
])
def test_model_sync_excludes_non_chat_models(provider_id, protocol, rows, expected):
    key = "models" if protocol in {"gemini", "ollama"} else "data"
    models = providers._list_models(snapshot(protocol, provider_id), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={key: rows})))
    assert [x["id"] for x in models] == expected


@pytest.mark.parametrize("protocol,provider_id", [("gemini", "google"), ("anthropic", "anthropic")])
def test_sync_native_pagination_is_bounded_and_no_tokens_become_urls(protocol, provider_id):
    requests = []
    def respond(request):
        requests.append(request)
        if protocol == "gemini":
            return httpx.Response(200, json={"models": [{"name": "models/gemini-synthetic-" + str(len(requests)),
                "supportedGenerationMethods": ["generateContent"]}], **({"nextPageToken": "next+opaque"} if len(requests) == 1 else {})})
        return httpx.Response(200, json={"data": [{"id": "claude-synthetic-" + str(len(requests))}],
            "has_more": len(requests) == 1, "last_id": "claude-synthetic-1"})
    metadata = {}
    models = providers._list_models(snapshot(protocol, provider_id), transport=httpx.MockTransport(respond), metadata=metadata)
    assert len(models) == 2 and len(requests) == 2 and metadata["truncated"] is False
    assert all(SYNTHETIC_KEY not in str(request.url) for request in requests)
    assert ("pageToken=" if protocol == "gemini" else "after_id=") in str(requests[1].url)


def test_sync_reports_truncation_and_rejects_cursor_cycles():
    metadata = {}
    models = providers._list_models(snapshot(), limit=1, metadata=metadata, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "gpt-first"}, {"id": "gpt-second"}]})))
    assert len(models) == 1 and metadata["truncated"] is True
    response = {"data": [{"id": "claude-first"}], "has_more": True, "last_id": "loop"}
    with pytest.raises(providers.ProviderError, match="PROVIDER_INVALID_PAGINATION"):
        providers._list_models(snapshot("anthropic", "anthropic"), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))


def test_minimax_native_reasoning_split_and_gateway_parameter_capabilities():
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=response_fixture("openai"))
    providers.complete(snapshot("openai", "minimax", model_id="MiniMax-M3"), [{"role": "user", "content": "Probe"}],
        transport=httpx.MockTransport(respond))
    assert captured[-1]["reasoning_split"] is True and "max_completion_tokens" in captured[-1]
    assert "response_format" not in captured[-1]
    for supported, expected in ((None, False), (["max_tokens"], False), (["response_format"], True)):
        providers.complete(snapshot("openai", "openrouter", model_id="openai/gpt-6-astra", supported_parameters=supported),
            [{"role": "user", "content": "Probe"}], transport=httpx.MockTransport(respond))
        assert ("response_format" in captured[-1]) is expected


def test_minimax_responses_reasoning_is_explicit_and_private_output_never_normalized():
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        body = response_fixture("responses")
        body["output"][0]["content"] = [{"type": "reasoning_text", "text": "private-synthetic-marker"}]
        body["usage"]["output_tokens_details"] = {"reasoning_tokens": 2}
        return httpx.Response(200, json=body)
    connection = snapshot("responses", "minimax", model_id="MiniMax-M3")
    result = providers.complete(connection, [{"role": "user", "content": "Synthetic probe"}],
        reasoning_effort="low", max_tokens=4096, transport=httpx.MockTransport(respond))
    assert captured[0]["reasoning"] == {"effort": "low"}
    assert captured[0]["max_output_tokens"] == 4096
    assert "text" not in captured[0], "MiniMax only documents text format, not native JSON schema/object mode"
    assert "tools" not in captured[0] and captured[0]["store"] is False
    assert result["usage"]["reasoning_tokens"] == 2
    assert "private-synthetic-marker" not in json.dumps(result)
    providers.complete(connection, [{"role": "user", "content": "Synthetic probe"}],
        transport=httpx.MockTransport(respond))
    assert "reasoning" not in captured[1], "existing non-answer tasks must not silently enable extra reasoning"


def test_json_mode_rejects_non_json_and_preserves_non_json_mode():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response_fixture("responses", "not-json")))
    with pytest.raises(providers.ProviderError, match="PROVIDER_INVALID_JSON_OBJECT"):
        providers.complete(snapshot(), [{"role": "user", "content": "Probe"}], transport=transport)
    result = providers.complete(snapshot(), [{"role": "user", "content": "Probe"}], json_mode=False, transport=transport)
    assert result["choices"][0]["message"]["content"] == "not-json"


@pytest.mark.parametrize('content', ['```json\n{"ok":true}\n```', '\ufeff{"ok":true}', '```\n{"ok":true}\n```'])
def test_json_wrapper_only_normalization_preserves_object(content):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response_fixture('responses', content)))
    result = providers.complete(snapshot(), [{'role':'user','content':'Synthetic'}], transport=transport)
    assert json.loads(result['choices'][0]['message']['content']) == {'ok':True}
    assert result['output_format']['wrapper_removed'] is True


@pytest.mark.parametrize('content', ['说明\n```json\n{"ok":true}\n```', '```json\n{"ok":true}\n```\n备注',
    '{"ok":true}\n{"other":true}', '{"ok":true,"ok":false}', '{"x":NaN}', '[{"ok":true}]', '<think>private</think>{"ok":true}'])
def test_json_wrapper_does_not_repair_extract_or_hide_invalid_content(content):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response_fixture('responses', content)))
    with pytest.raises(providers.ProviderError, match='PROVIDER_INVALID_JSON_OBJECT') as caught:
        providers.complete(snapshot(), [{'role':'user','content':'Synthetic'}], transport=transport)
    assert content not in json.dumps(caught.value.diagnostic)
    assert 'private' not in json.dumps(caught.value.diagnostic)


def test_minimax_structured_return_is_data_only_not_an_executable_tool():
    schema = {'type':'object', 'required':['ok'], 'properties':{'ok':{'type':'boolean'}}, 'additionalProperties':False}
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        body = response_fixture('responses')
        body['output'] = [{'type':'reasoning', 'content':[{'type':'reasoning_text','text':'not-public'}]},
            {'type':'function_call', 'name':'return_structured_result', 'arguments':'{"ok":true}'}]
        return httpx.Response(200,json=body)
    result = providers.complete(snapshot('responses','minimax',model_id='MiniMax-M3'),
        [{'role':'user','content':'Synthetic'}], output_schema=schema, transport=httpx.MockTransport(respond))
    assert sent[0]['tools'] == [{'type':'function','name':'return_structured_result',
        'description':'Return the requested JSON result as data. This has no execution capability.','parameters':schema}]
    assert json.loads(result['choices'][0]['message']['content']) == {'ok':True}
    assert 'not-public' not in json.dumps(result)


@pytest.mark.parametrize('output', [
    [{'type':'function_call','name':'read_file','arguments':'{}'}],
    [{'type':'function_call','name':'return_structured_result','arguments':'{}'}]*2,
    [{'type':'function_call','name':'return_structured_result','arguments':{} }],
    [{'type':'function_call','name':'return_structured_result','arguments':'{}'},{'type':'web_search_call'}],
    [{'type':'function_call','name':'return_structured_result','arguments':'{}','status':'in_progress'}],
    [{'type':'function_call','name':'return_structured_result','arguments':'{}','status':'incomplete'}],
])
def test_structured_return_still_rejects_any_other_or_multiple_tool_calls(output):
    body = {**response_fixture('responses'), 'output':output}
    with pytest.raises(providers.ProviderError):
        providers.complete(snapshot('responses','minimax',model_id='MiniMax-M3'), [{'role':'user','content':'Synthetic'}],
            output_schema={'type':'object'}, transport=httpx.MockTransport(lambda r:httpx.Response(200,json=body)))


def test_reasoning_only_truncation_is_not_misreported_as_invalid_response_or_answer():
    body = {'status':'incomplete', 'output':[{'type':'reasoning','summary':[]}],
        'incomplete_details':{'reason':'max_output_tokens'}, 'usage':{'input_tokens':10,'output_tokens':100}}
    result = providers.normalize_response('responses', body, 'MiniMax-M3')
    assert result['choices'][0]['finish_reason'] == 'length'
    assert result['choices'][0]['message']['content'] == ''
    assert result['usage']['completion_tokens'] == 100


def test_native_structured_responses_accepts_bounded_sse_completed_envelope():
    schema = {'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']}
    def respond(request):
        assert json.loads(request.content)['stream'] is True
        body = {**response_fixture('responses'), 'output':[
            {'type':'function_call','name':'return_structured_result','arguments':'{"ok":true}','status':'completed'}]}
        data = 'data: '+json.dumps({'type':'response.completed','response':body})+'\n\n'
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},content=data.encode())
    result = providers.complete(snapshot('responses','minimax',model_id='MiniMax-M3'),[{'role':'user','content':'Synthetic'}],
        output_schema=schema,transport=httpx.MockTransport(respond))
    assert json.loads(result['choices'][0]['message']['content']) == {'ok':True}


def test_successful_probe_uses_no_document_content_and_does_not_grant_transfer(api, monkeypatch):
    created = create_connection(api)
    queued = api.call("POST", f"/model-connections/{created.json()['id']}/test", {"model_id": "gpt-6-astra"}, etag=created.headers["etag"])
    job_id = queued.json()["id"]
    with api.app.state.session_factory.begin() as db:
        job = db.get(m.Job, job_id)
        job.state, job.attempts = "RUNNING", 1
    sent = []
    def complete(private, messages, **kwargs):
        sent.extend(messages)
        return response_fixture("openai")
    monkeypatch.setattr(providers, "complete", complete)
    result = providers.run_connection_job(api.app.state.settings, api.app.state.session_factory, job_id, 1, lambda stage, details: None)
    assert result["status"] == "TESTED"
    assert sent == [{"role": "user", "content": 'Return exactly {"ok":true}.'}]
    refreshed = api.call("GET", f"/model-connections/{created.json()['id']}").json()
    assert refreshed["allow_document_transfer"] is False and refreshed["status"] == "TESTED"
    assert refreshed["revision"] == created.json()["revision"]
    assert SYNTHETIC_KEY not in json.dumps(result)


def test_encoded_credential_echo_and_compressed_bomb_are_blocked():
    data = json.dumps(response_fixture("responses", SYNTHETIC_KEY))
    encoded = data.replace(SYNTHETIC_KEY, "".join(f"\\u{ord(c):04x}" for c in SYNTHETIC_KEY))
    with pytest.raises(providers.ProviderError, match="PROVIDER_SECRET_ECHO"):
        providers.complete(snapshot(), [{"role": "user", "content": "Probe"}], transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=encoded.encode())))
    import gzip
    compressed = gzip.compress(b"x" * 10000)
    with pytest.raises(providers.ProviderError, match="PROVIDER_ENCODING_UNSUPPORTED"):
        providers.complete(snapshot(), [{"role": "user", "content": "Probe"}], transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(compressed), headers={"Content-Encoding": "gzip"})))


def test_localhost_must_stay_loopback_and_request_bytes_are_bounded(monkeypatch):
    monkeypatch.setattr(providers, "_dns_addresses", lambda host, port: ["93.184.216.34"])
    with pytest.raises(providers.ProviderError, match="PROVIDER_ADDRESS_BLOCKED"):
        providers.complete(snapshot("ollama", "ollama", base_url="http://localhost:11434"), [{"role": "user", "content": "Probe"}])
    with pytest.raises(providers.ProviderError, match="PROVIDER_REQUEST_TOO_LARGE"):
        providers.complete(snapshot(max_request_bytes=10), [{"role": "user", "content": "A" * 50}])


@pytest.mark.parametrize("protocol", ["openai", "responses", "anthropic", "gemini"])
def test_missing_terminal_status_cannot_be_normalized_as_success(protocol):
    response = response_fixture(protocol)
    if protocol == "openai":
        response["choices"][0].pop("finish_reason")
    elif protocol == "responses":
        response.pop("status")
    elif protocol == "anthropic":
        response.pop("stop_reason")
    else:
        response["candidates"][0].pop("finishReason")
    with pytest.raises(providers.ProviderError):
        providers.normalize_response(protocol, response, "synthetic")


def test_master_key_permissions_and_symlink_are_rejected(api, tmp_path):
    created = create_connection(api)
    key = api.app.state.settings.storage_dir / "private" / "provider-master.key"
    key.chmod(0o640)
    with api.app.state.session_factory() as db:
        config = db.get(m.RuntimePolicy, created.json()["id"]).config
        with pytest.raises(providers.ProviderError, match="MASTER_KEY_PERMISSIONS"):
            providers.credential_value(config, api.app.state.settings)
    key.chmod(0o600)
    target = tmp_path / "separate-owned-test-directory"
    target.mkdir()
    linked = tmp_path / "symlink-key-storage"
    linked.symlink_to(target, target_is_directory=True)
    settings = api.app.state.settings.model_copy(update={"storage_dir": linked})
    with pytest.raises(providers.ProviderError, match="MASTER_KEY_PATH_UNSAFE"):
        providers.encrypt_credential(settings, uid(), SPACE, SYNTHETIC_KEY)


def test_cloud_without_credentials_stays_unconfigured_but_local_none_is_configured(api):
    cloud = connection_body(credential_mode="none")
    cloud.pop("api_key")
    saved = api.call("POST", "/model-connections", cloud)
    assert saved.status_code == 201
    options = api.call("GET", f"/model-options?space_id={SPACE}").json()["items"]
    assert options and all(x["configured"] is False for x in options)
    with (api.app.state.session_factory() as db,
        pytest.raises(providers.ProviderError, match="CREDENTIAL_MISSING")):
        providers.resolve_connection(db, db.get(m.User, EDITOR), SPACE, saved.json()["id"], "gpt-6-astra", api.app.state.settings)
    local = {**cloud, "provider_id": "ollama", "custom_models": [{"id": "qwen3:8b", "name": "Local model", "brand": "qwen"}]}
    assert api.call("POST", "/model-connections", local).status_code == 201
    options = api.call("GET", f"/model-options?space_id={SPACE}").json()["items"]
    assert next(x for x in options if x["provider_id"] == "ollama")["configured"] is True


def test_dns_wall_timeout_prevents_a_late_request_after_caller_returns(monkeypatch):
    import threading
    import time

    released, finished = threading.Event(), threading.Event()
    requests = []
    def dns(host, port):
        released.wait(1)
        finished.set()
        return ["93.184.216.34"]
    monkeypatch.setattr(providers, "_dns_addresses", dns)
    started = time.monotonic()
    try:
        with pytest.raises(providers.ProviderError, match="PROVIDER_TIMEOUT"):
            providers.complete(snapshot(), [{"role": "user", "content": "Probe"}], timeout=0.03,
                transport=httpx.MockTransport(lambda request: requests.append(request)))
        assert time.monotonic() - started < 0.5
    finally:
        released.set()
    assert finished.wait(1)
    assert requests == []


def test_proxy_environment_is_not_used_for_provider_credentials(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-proxy.invalid:1")
    monkeypatch.setenv("ALL_PROXY", "http://synthetic-proxy.invalid:1")
    sent = []
    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=response_fixture("responses"))
    result = providers.complete(snapshot(), [{"role": "user", "content": "Probe"}], transport=httpx.MockTransport(respond))
    assert result["choices"][0]["finish_reason"] == "stop"
    assert len(sent) == 1 and sent[0].headers["Host"] == "api.openai.com"


def test_generic_local_openai_configures_without_masquerading_as_lmstudio(api):
    catalog = {x["id"]: x for x in api.call("GET", "/model-providers").json()["items"]}
    local = catalog["local-openai"]
    assert local["name"] == "本地兼容服务" and local["kind"] == "local" and local["protocol"] == "openai"
    assert local["base_url"] == "http://127.0.0.1:8000/v1" and local["icon"] == "local"
    assert local["models"] == []
    created = api.call("POST", "/model-connections", {"space_id": SPACE, "name": "vLLM 测试", "provider_id": "local-openai",
        "credential_mode": "none", "allow_document_transfer": True,
        "custom_models": [{"id": "local-text-model", "name": "本地模型", "brand": "local"}]})
    assert created.status_code == 201, created.text
    options = api.call("GET", f"/model-options?space_id={SPACE}").json()["items"]
    assert len(options) == 1 and options[0]["provider_id"] == "local-openai" and options[0]["configured"] is True
    with api.app.state.session_factory() as db:
        private = providers.resolve_connection(db, db.get(m.User, EDITOR), SPACE, created.json()["id"], "local-text-model",
            api.app.state.settings, require_transfer=True)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=response_fixture("openai"))
    providers.complete(private, [{"role": "user", "content": "Probe"}], transport=httpx.MockTransport(respond))
    assert requests[0].url.path == "/v1/chat/completions" and requests[0].headers["Host"] == "127.0.0.1:8000"
    assert "Authorization" not in requests[0].headers


def test_idempotent_create_does_not_resurface_deleted_connection(api):
    key = uid()
    body = connection_body()
    created = api.call("POST", "/model-connections", body, key=key)
    assert created.status_code == 201
    same = api.call("POST", "/model-connections", body, key=key)
    assert same.status_code == 201 and same.json() == created.json()
    assert api.call("DELETE", f"/model-connections/{created.json()['id']}", etag=created.headers["etag"]).status_code == 204
    assert api.call("POST", "/model-connections", body, key=key).status_code == 404
