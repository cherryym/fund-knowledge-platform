"""Local preparation coordination, no model weights, network, or private data."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from fund_kb.model_warmup import ModelWarmup, ModelWarmupError, start_default_warmup


def settings(**extra):
    return SimpleNamespace(**{"retrieval_warmup_mode":"auto", "embedding_mode":"transformers",
        "reranker_mode":"local", **extra})


def test_status_never_starts_loading_and_default_start_is_background_single_flight():
    started, release = threading.Event(), threading.Event()
    calls = []
    def probe(phase):
        calls.append(1); phase("loading_embedding"); started.set(); release.wait(3); phase("loading_reranker")
    manager = ModelWarmup(settings(),probe)
    assert manager.snapshot()["state"] == "NOT_LOADED" and not calls
    start_default_warmup(settings(),SimpleNamespace(model_runtime=manager))
    assert started.wait(2)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            pending = [pool.submit(manager.ensure_ready) for _ in range(8)]
            assert manager.start()["state"] == "LOADING" and calls == [1]
            release.set()
            results = [future.result(3) for future in pending]
        assert all(r["state"] == "READY" and r["self_tested"] for r in results)
        assert manager.snapshot()["attempts"] == 1 and len(calls) == 1
        manager.start(retry=True)
        assert len(calls) == 1, "already-ready models remain resident"
    finally:
        release.set(); manager.close()


def test_failed_preparation_is_sanitized_and_requires_explicit_retry():
    calls = []
    def probe(phase):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("DO_NOT_EXPOSE_PRIVATE_TOKEN_OR_PATH")
    manager = ModelWarmup(settings(),probe)
    try:
        for _ in range(3):
            with pytest.raises(ModelWarmupError,match="MODEL_WARMUP_FAILED"):
                manager.ensure_ready()
        assert len(calls) == 1 and manager.snapshot()["state"] == "FAILED"
        assert "DO_NOT_EXPOSE" not in str(manager.snapshot())
        manager.start(retry=True)
        assert manager.ensure_ready()["state"] == "READY" and len(calls) == 2
    finally:
        manager.close()


def test_cancelling_one_waiter_does_not_cancel_shared_preparation():
    started, release = threading.Event(), threading.Event()
    def probe(phase): started.set(); release.wait(3)
    manager=ModelWarmup(settings(),probe)
    manager.start(); assert started.wait(2)
    def cancel(): raise RuntimeError("CONSULTATION_CANCELLED")
    try:
        with pytest.raises(RuntimeError,match="CONSULTATION_CANCELLED"):
            manager.ensure_ready(cancel)
        assert manager.snapshot()["state"] == "LOADING"
        release.set()
        assert manager.ensure_ready()["state"] == "READY"
    finally:
        release.set(); manager.close()


@pytest.mark.parametrize("extra", [{"embedding_mode":"http"},{"embedding_mode":"hashing"},
    {"embedding_mode":"fastembed"},{"reranker_mode":"disabled"},{"retrieval_warmup_mode":"disabled"}])
def test_unsupported_or_disabled_plan_never_calls_probe(extra):
    manager=ModelWarmup(settings(**extra),lambda phase:pytest.fail("must not perform IO or inference"))
    assert manager.start()["state"] == manager.ensure_ready()["state"] == "NOT_APPLICABLE"
    manager.close()


def test_on_demand_is_not_started_by_startup_but_query_can_prepare_it():
    calls=[]
    cfg=settings(retrieval_warmup_mode="on_demand")
    manager=ModelWarmup(cfg,lambda phase:calls.append(1))
    try:
        start_default_warmup(cfg,SimpleNamespace(model_runtime=manager))
        assert not calls and manager.snapshot()["state"] == "NOT_LOADED"
        assert manager.ensure_ready()["state"] == "READY" and calls == [1]
    finally: manager.close()


def test_close_waits_for_native_probe_and_does_not_mark_closing_models_ready():
    started,release=threading.Event(),threading.Event()
    def probe(phase): started.set(); release.wait(3); phase("self_test_complete")
    manager=ModelWarmup(settings(),probe); manager.start(); assert started.wait(2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        closing=pool.submit(manager.close)
        release.set(); closing.result(3)
    assert manager.snapshot()["state"] == "CLOSED" and manager.snapshot()["self_tested"] is False
    with pytest.raises(ModelWarmupError,match="MODEL_WARMUP_CLOSED"): manager.ensure_ready()


@pytest.mark.parametrize("error,code", [(ValueError("LOCAL_MODEL_HASH_MISMATCH:private/file"),"LOCAL_MODEL_HASH_MISMATCH"),
    (ModuleNotFoundError("private/path"),"LOCAL_DEPENDENCY_UNAVAILABLE"),
    (RuntimeError("MPS out of memory private/trace"),"LOCAL_MEMORY_UNAVAILABLE")])
def test_public_error_categories_do_not_include_private_exception_text(error,code):
    def probe(phase): raise error
    manager=ModelWarmup(settings(),probe)
    try:
        with pytest.raises(ModelWarmupError,match=code): manager.ensure_ready()
        assert manager.snapshot()["error_code"] == code and "private" not in str(manager.snapshot())
    finally: manager.close()
