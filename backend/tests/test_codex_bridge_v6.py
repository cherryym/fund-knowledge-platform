"""Offline auth-only success/storage tests. No subprocesses, OS keyring or real auth."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from test_api import EDITOR, READER, SPACE
from test_api import api as api  # noqa: PLC0414 - shared offline API fixture

from fund_kb import api_oauth, providers
from fund_kb import models as m
from fund_kb.codex_bridge import CodexBridge, EncryptedAuthStore
from fund_kb.codex_bridge_config import CodexBridgeConfig
from fund_kb.services import uid


class FakeRPC:
    def __init__(self, config, home):
        self.home, self.calls, self.events, self.account = home, [], [], None
        self.closed, self.login_id = False, uid()

    def call(self, method, params=None):
        assert method in {"account/read", "account/login/start", "account/logout", "account/login/cancel", "model/list"}
        self.calls.append((method, params))
        if method == "account/read":
            return {"account": self.account}
        if method == "account/login/start":
            return {"loginId": self.login_id, "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "TEST-1234"}
        if method == "model/list":
            return {"data": [{"id": "synthetic-text", "model": "synthetic-text", "displayName": "Synthetic",
                "inputModalities": ["text"]}], "nextCursor": None}
        if method == "account/logout":
            self.account = None
        return {}

    def notifications(self):
        result, self.events = self.events, []
        return result

    def authenticate(self):
        self.account = {"type": "chatgpt", "email": "must-not-leak@example.invalid", "planType": "pro"}
        # This is synthetic new identity output, never an existing auth file.
        fd = os.open(self.home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b'{"synthetic-token":"NEVER-REAL-CREDENTIAL"}')
        self.events.append({"method": "account/login/completed", "params": {"loginId": self.login_id, "success": True}})

    def close(self):
        self.closed = True


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    import socket
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError("Real process/network forbidden")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    config = CodexBridgeConfig(enabled=True, runtime_root=tmp_path / "runtime", archive_root=tmp_path / "archive")
    store = EncryptedAuthStore(config, Fernet.generate_key())
    manager = CodexBridge(config, store=store, transport_factory=FakeRPC)
    yield manager
    manager.close()


def connection(api):
    response = api.call("POST", "/model-connections", {"space_id": SPACE, "provider_id": "chatgpt-codex",
        "credential_mode": "chatgpt_oauth", "name": "My synthetic ChatGPT"})
    assert response.status_code == 201, response.text
    return response


def finish_job(api, bridge, response):
    assert response.status_code == 202, response.text
    jid = response.json()["id"]
    with api.app.state.session_factory.begin() as db:
        job = db.get(m.Job, jid)
        job.state, job.attempts = "RUNNING", 1
        task = job.payload["task"]
    runner = providers.run_connection_job if task in {"MODEL_SYNC", "MODEL_TEST"} else api_oauth.run_oauth_job
    result = runner(api.app.state.settings, api.app.state.session_factory, jid, 1, lambda *a: None, bridge=bridge)
    with api.app.state.session_factory.begin() as db:
        job = db.get(m.Job, jid)
        providers.authorize_model_job(db, db.get(m.User, job.owner_id), job, api.app.state.settings, action="complete")
        job.state, job.result = "SUCCEEDED", result
    return jid


def test_real_store_fake_rpc_login_pro_sync_and_logout_do_not_create_turns(api, bridge):
    api.app.state.codex_bridge = bridge
    created = connection(api)
    cid, root = created.json()["id"], "/model-connections/" + created.json()["id"]
    assert bridge.sessions == {}  # saving a connection does not start any login
    finish_job(api, bridge, api.call("POST", root + "/oauth/start", {"flow": "device_code", "connection_revision": 1}, '"1"'))
    state = api.call("GET", root + "/oauth")
    challenge = api.call("GET", root + "/oauth/challenge?attempt_id=" + state.json()["attempt_id"])
    assert challenge.json()["user_code"] == "TEST-1234"
    assert "no-store" in challenge.headers["cache-control"]
    session = bridge.sessions[EDITOR, cid]
    session.rpc.authenticate()
    finish_job(api, bridge, api.call("POST", root + "/oauth/refresh", {"connection_revision": 1}, state.headers["etag"]))
    signed = api.call("GET", root + "/oauth")
    assert signed.json()["state"] == "AUTHENTICATED"
    assert signed.json()["account"] == {"type": "chatgpt", "plan_type": "pro"}
    assert signed.json()["capabilities"]["inference"] is False
    assert "must-not-leak" not in signed.text
    archive = next(bridge.store.archive.glob("*.enc"))
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert b"NEVER-REAL-CREDENTIAL" not in archive.read_bytes()
    jid = finish_job(api, bridge, api.call("POST", root + "/sync-models", etag=created.headers["etag"]))
    assert api.call("GET", root).json()["revision"] == 1
    assert api.call("GET", "/jobs/" + jid).status_code == 200
    option = api.call("GET", "/model-options?space_id=" + SPACE).json()["items"][0]
    assert option["configured"] and not option["selectable"]
    assert option["blocked_reason"] == "CODEX_TEXT_ISOLATION_UNVERIFIED"
    for row in session.rpc.calls:
        assert row[0] not in {"thread/start", "turn/start"}
    with api.app.state.session_factory() as db:
        for cls, attr in ((m.Job, "result"), (m.Job, "payload"), (m.IdempotencyRecord, "response"),
            (m.AuditEvent, "details"), (m.RuntimePolicy, "config")):
            for row in db.scalars(select(cls)):
                text = json.dumps(getattr(row, attr))
                assert "TEST-1234" not in text and "NEVER-REAL-CREDENTIAL" not in text
    finish_job(api, bridge, api.call("POST", root + "/oauth/logout", {"connection_revision": 1}, signed.headers["etag"]))
    assert api.call("GET", root + "/oauth").json()["state"] == "SIGNED_OUT"
    assert bridge.sessions == {} and not session.home.exists() and not archive.exists()


def test_attested_text_engine_enables_default_model_probe_without_api_key(api, bridge):
    class TextEngine:
        models = frozenset({"synthetic-text"})
        calls = 0
        def complete(self, snapshot, messages, **kwargs):
            snapshot["_authority_check"]()
            assert "api_key" not in snapshot
            assert kwargs["json_mode"]
            self.calls += 1
            return {"choices":[{"message":{"content":'{"ok":true}'},"finish_reason":"stop"}],"usage":{}}
    bridge.text_engine = TextEngine()
    api.app.state.codex_bridge = bridge
    object.__setattr__(api.app.state.settings, "_codex_bridge", bridge)
    object.__setattr__(api.app.state.settings, "_codex_session_factory", api.app.state.session_factory)
    created = connection(api)
    cid, root = created.json()["id"], "/model-connections/" + created.json()["id"]
    finish_job(api, bridge, api.call("POST", root + "/oauth/start", {"flow":"device_code","connection_revision":1}, '"1"'))
    bridge.sessions[EDITOR,cid].rpc.authenticate()
    state = api.call("GET",root+"/oauth")
    finish_job(api,bridge,api.call("POST",root+"/oauth/refresh",{"connection_revision":1},state.headers["etag"]))
    finish_job(api,bridge,api.call("POST",root+"/sync-models",etag=created.headers["etag"]))
    probe = api.call("POST",root+"/test",{},created.headers["etag"])
    jid = finish_job(api,bridge,probe)
    result=api.call("GET","/jobs/"+jid).json()["result"]
    assert result["probe_ok"] and result["model_id"] == "synthetic-text"
    assert bridge.text_engine.calls == 1
    option=api.call("GET","/model-options?space_id="+SPACE).json()["items"][0]
    assert option["selectable"] and option["blocked_reason"] is None


def test_transfer_consent_patch_keeps_identity_but_revokes_old_connection_snapshot(api, bridge):
    api.app.state.codex_bridge = bridge
    created = connection(api)
    cid = created.json()["id"]
    root = "/model-connections/" + cid
    state_before = api.call("GET",root+"/oauth").json()
    changed = api.call("PATCH",root,{"allow_document_transfer":True},created.headers["etag"])
    assert changed.status_code == 200, changed.text
    assert changed.json()["allow_document_transfer"] is True
    assert changed.json()["revision"] > created.json()["revision"]
    after = api.call("GET",root+"/oauth").json()
    assert after["auth_epoch"] == state_before["auth_epoch"]
    assert after["connection_revision"] == changed.json()["revision"]
    assert after["state"] == state_before["state"]
    assert api.call("PATCH",root,{"allow_document_transfer":False},created.headers["etag"]).status_code == 412
    revoked=api.call("PATCH",root,{"allow_document_transfer":False},changed.headers["etag"])
    assert revoked.status_code == 200 and revoked.json()["allow_document_transfer"] is False


def test_auth_success_notification_without_secure_file_cannot_mark_authenticated(api, bridge):
    api.app.state.codex_bridge = bridge
    created = connection(api)
    cid = created.json()["id"]
    root = f"/model-connections/{cid}/oauth"
    finish_job(api, bridge, api.call("POST", root + "/start", {"flow": "device_code", "connection_revision": 1}, '"1"'))
    session = bridge.sessions[EDITOR, cid]
    session.rpc.account = {"type": "chatgpt", "planType": "pro"}
    session.rpc.events.append({"method": "account/login/completed", "params": {"loginId": session.login_id, "success": True}})
    state = api.call("GET", root)
    with pytest.raises(providers.ProviderError) as caught:
        finish_job(api, bridge, api.call("POST", root + "/refresh", {"connection_revision": 1}, state.headers["etag"]))
    assert caught.value.code == "CODEX_AUTH_NOT_PERSISTED"
    assert api.call("GET", root).json()["state"] == "ERROR"
    assert session.rpc.closed and not session.home.exists()


def test_ciphertext_owner_binding_and_home_modes(bridge):
    owner, cid = EDITOR, uid()
    home = bridge.store.materialize(owner, cid, 7)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "config.toml").stat().st_mode) == 0o600
    rpc = FakeRPC(bridge.config, home)
    rpc.authenticate()
    bridge.store.persist(home)
    bridge.store.release(home)
    correct = bridge.store.materialize(owner, cid, 7, restore=True)
    assert b"NEVER-REAL-CREDENTIAL" in (correct / "auth.json").read_bytes()
    bridge.store.release(correct)
    # Copy only the encrypted synthetic blob, then attempt another owner.
    source = bridge.store.archive / (owner + "." + cid + ".enc")
    target = bridge.store.archive / (READER + "." + cid + ".enc")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(source.read_bytes())
    with pytest.raises(providers.ProviderError) as caught:
        bridge.store.materialize(READER, cid, 7, restore=True)
    assert caught.value.code == "CODEX_AUTH_BINDING_INVALID"


def test_storage_rejects_project_roots_symlinks_and_unregistered_cleanup(tmp_path, bridge):
    with pytest.raises(providers.ProviderError):
        bridge.store.release(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(bridge.store.root, target_is_directory=True)
    with pytest.raises(providers.ProviderError):
        EncryptedAuthStore(replace(bridge.config, runtime_root=link), Fernet.generate_key())
    from pathlib import Path
    with pytest.raises(providers.ProviderError):
        EncryptedAuthStore(replace(bridge.config, runtime_root=Path(__file__).resolve().parents[2] / "auth"), Fernet.generate_key())
