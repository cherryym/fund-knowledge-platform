"""Cold runtime recovery uses the exact authorized project binding, once only.

All storage/RPC here is synthetic; never read user archives or start a process.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_codex_stream_backpressure import EngineHarness, terminal_events
from test_codex_stream_backpressure import (
    forbid_real_identity_process_and_network as offline_fences,  # noqa: F401
)

from fund_kb.codex_bridge import EncryptedAuthStore
from fund_kb.providers import ProviderError


def cold(monkeypatch, *, existing=None, restore_error=None, revoke=False):
    h = EngineHarness(monkeypatch, terminal_events())
    key = (h.snapshot['owner_user_id'], h.snapshot['id'])
    h.sessions = {} if existing is None else {key: existing}
    h.store = Mock(spec=EncryptedAuthStore, wraps=h)
    h.refresh_calls = []
    def session(owner, cid, epoch, revision):
        s = h.sessions.get((owner, cid))
        if not s or s.epoch != epoch or s.revision != revision:
            raise ProviderError('CODEX_LOGIN_STALE')
        return s
    def refresh(owner, cid, epoch, revision):
        h.refresh_calls.append((owner, cid, epoch, revision))
        if restore_error: raise ProviderError(restore_error)
        h.sessions[owner, cid] = SimpleNamespace(epoch=epoch, revision=revision, authenticated=True)
        if revoke: h.revoked = True
        return {'state':'AUTHENTICATED'}
    h._session, h.refresh = session, refresh
    return h


def run(h):
    return h.adapter.complete(h.snapshot, [{'role':'user','content':'synthetic request'}],
        max_tokens=1000, json_mode=False, timeout=2)


def test_cold_authorized_request_restores_exact_binding_once_before_text_process(monkeypatch):
    h = cold(monkeypatch)
    run(h)
    assert h.refresh_calls == [('synthetic-owner','synthetic-connection',1,1)]
    assert h.factory_calls == 1 and h.trace.count('turn/start') == 1


@pytest.mark.parametrize('epoch,revision', [(2,1),(1,2)])
def test_present_mismatched_identity_never_auto_replaced(monkeypatch, epoch, revision):
    h = cold(monkeypatch, existing=SimpleNamespace(epoch=epoch, revision=revision, authenticated=True))
    with pytest.raises(ProviderError, match='CODEX_LOGIN_STALE'): run(h)
    assert not h.refresh_calls and not h.materialized and h.factory_calls == 0


def test_warm_session_does_not_refresh_or_regenerate(monkeypatch):
    h = cold(monkeypatch, existing=SimpleNamespace(epoch=1, revision=1, authenticated=True))
    run(h)
    assert not h.refresh_calls and h.factory_calls == 1


@pytest.mark.parametrize('code',['CODEX_AUTH_ARCHIVE_UNAVAILABLE','CODEX_AUTH_BINDING_INVALID','CODEX_RPC_FAILED'])
def test_restore_failure_is_not_retried_and_no_text_is_sent(monkeypatch, code):
    h = cold(monkeypatch, restore_error=code)
    with pytest.raises(ProviderError, match=code): run(h)
    assert len(h.refresh_calls) == 1 and not h.materialized and h.factory_calls == 0


def test_revocation_after_restore_prevents_text_materialization(monkeypatch):
    h = cold(monkeypatch, revoke=True)
    with pytest.raises(ProviderError, match='CONNECTION_REVISION_CHANGED'): run(h)
    assert len(h.refresh_calls) == 1 and not h.materialized and h.factory_calls == 0


def test_revocation_before_restore_does_not_touch_archive(monkeypatch):
    h = cold(monkeypatch); h.revoked = True
    with pytest.raises(ProviderError, match='CONNECTION_REVISION_CHANGED'): run(h)
    assert not h.refresh_calls and not h.materialized


def test_unencrypted_or_implicit_store_is_not_a_recovery_source(monkeypatch):
    h = cold(monkeypatch); h.store = h
    with pytest.raises(ProviderError, match='CODEX_CREDENTIAL_STORE_UNAVAILABLE'): run(h)
    assert not h.refresh_calls and not h.materialized
