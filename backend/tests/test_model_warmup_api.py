"""Authorized HTTP control and query waiting, using synthetic local probes."""
import threading
from types import SimpleNamespace

import pytest

from fund_kb.model_warmup import ModelWarmup
from test_dual_retrieval_api import applications, env, synthetic_backend  # noqa: F401


def attach(env,monkeypatch,probe,ident="qwen3-4b"):
    manager=ModelWarmup(SimpleNamespace(retrieval_warmup_mode="auto",embedding_mode="transformers",reranker_mode="local"),probe)
    monkeypatch.setattr(env.registry.resolve(ident).vector,"model_runtime",manager)
    return manager


def get_state(env):
    response=env.call("GET",f"/retrieval/model-runtime?space_id={env.space}&profile_id=qwen3-4b")
    assert response.status_code==200,response.text
    assert response.headers["cache-control"]=="private, no-store"
    return response.json()


def test_status_get_is_read_only_and_explicit_editor_prepare_coalesces(env,monkeypatch):
    started,release=threading.Event(),threading.Event(); calls=[]
    def probe(phase): calls.append(1); started.set(); release.wait(3)
    manager=attach(env,monkeypatch,probe)
    try:
        assert get_state(env)["model_runtime"]["state"]=="NOT_LOADED" and not calls
        body={"space_id":env.space,"retrieval_selection":{"profile_id":"qwen3-4b"}}
        first=env.call("POST","/retrieval/model-warmup",body,key="same-prepare-key")
        assert first.status_code==202,first.text
        assert started.wait(2)
        again=env.call("POST","/retrieval/model-warmup",body,key="same-prepare-key")
        assert again.status_code==202 and len(calls)==1
        assert get_state(env)["model_runtime"]["state"]=="LOADING"
        release.set(); manager.ensure_ready()
        assert get_state(env)["model_runtime"]["state"]=="READY"
    finally: release.set(); manager.close()


def test_failed_model_cannot_fall_through_to_search_or_generation(env,monkeypatch):
    calls=[]
    def probe(phase): calls.append(1); raise ValueError("LOCAL_MODEL_HASH_MISMATCH:private")
    manager=attach(env,monkeypatch,probe)
    try:
        response=env.call("POST","/retrieval/search",{"space_id":env.space,"query":"合成问题","retrieval_selection":{"profile_id":"qwen3-4b"}})
        assert response.status_code==409 and response.json()["code"]=="LOCAL_MODEL_HASH_MISMATCH"
        assert "private" not in response.text and calls==[1]
        assert get_state(env)["model_runtime"]["state"]=="FAILED"
        response=env.call("POST","/retrieval/search",{"space_id":env.space,"query":"另一个合成问题"})
        assert response.status_code==409 and calls==[1]
    finally: manager.close()


def test_reader_cannot_force_reload_and_other_space_cannot_read_runtime(env,monkeypatch):
    calls=[]; manager=attach(env,monkeypatch,lambda phase:calls.append(1))
    try:
        env.login(env.reader)
        assert get_state(env)["can_prepare"] is False
        reply=env.call("POST","/retrieval/model-warmup",{"space_id":env.space,"retry":True})
        assert reply.status_code==403 and not calls
        from fund_kb.services import uid
        reply=env.call("GET",f"/retrieval/model-runtime?space_id={uid()}")
        assert reply.status_code in {403,404} and not calls
    finally: manager.close()


def test_browser_cannot_supply_model_paths_or_unsupported_external_warmup(env):
    reply=env.call("POST","/retrieval/model-warmup",{"space_id":env.space,"model_path":"/arbitrary"})
    assert reply.status_code==422
    reply=env.call("POST","/retrieval/model-warmup",{"space_id":env.space})
    assert reply.status_code==409 and reply.json()["code"]=="LOCAL_WARMUP_NOT_ENABLED"
