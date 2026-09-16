"""Offline stability regressions: fake RPC/store, virtual time and bounded threads."""
from __future__ import annotations

import io
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_codex_stream_backpressure import (
    CANARY,
    EngineHarness,
    VirtualClock,
    event,
    terminal_events,
)
from test_codex_stream_backpressure import (
    forbid_real_identity_process_and_network as offline_fences,  # noqa: F401 - autouse safety fixture
)

from fund_kb.codex_stability import (
    LOGGER,
    RPC_BUDGET,
    BoundedAdmission,
    InferenceTrace,
    RequestBudget,
    checked_lock,
)
from fund_kb.codex_text import INSTRUCTION_CONTRACT_SHA256, PROFILE_VERSION, TextStdioTransport
from fund_kb.providers import ProviderError


@pytest.fixture(autouse=True)
def capture_named_logger(caplog):
    LOGGER.addHandler(caplog.handler)
    yield
    LOGGER.removeHandler(caplog.handler)


def records(caplog):
    return [json.loads(row.getMessage().removeprefix("codex_inference "))
            for row in caplog.records if row.name == "fund_kb.codex_stability"]


def call_engine(harness, timeout=2):
    return harness.adapter.complete(harness.snapshot, [{"role": "user", "content": CANARY}],
                                    max_tokens=4096, json_mode=True, timeout=timeout)


def test_business_guard_runs_after_setup_before_user_payload(monkeypatch):
    h = EngineHarness(monkeypatch, terminal_events())
    called = []
    def refuse():
        called.append(True)
        raise ProviderError("EVIDENCE_ACCESS_CHANGED")
    h.snapshot["_before_send_check"] = refuse
    with pytest.raises(ProviderError, match="EVIDENCE_ACCESS_CHANGED"):
        call_engine(h)
    assert called == [True]
    assert "thread/start" in h.trace and "turn/start" not in h.trace
    assert h.closed and h.released


def wait_for_count(admission, count):
    deadline = time.monotonic() + 2
    with admission.condition:
        while len(admission.waiters) != count:
            assert time.monotonic() < deadline, "bounded worker did not reach queue"
            admission.condition.wait(timeout=.005)


def test_fifo_two_workers_do_not_barge_and_each_request_runs_once():
    slot = threading.BoundedSemaphore(1)
    admission = BoundedAdmission(slot)
    slot.acquire()
    order, active, peak = [], 0, 0

    def worker(index):
        nonlocal active, peak
        budget = RequestBudget(2, lambda: None, time.monotonic)
        with admission.take(budget, InferenceTrace(time.monotonic)):
            active += 1
            peak = max(active, peak)
            order.append(index)
            active -= 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, 1)
        wait_for_count(admission, 1)
        second = pool.submit(worker, 2)
        wait_for_count(admission, 2)
        slot.release()
        first.result(timeout=2)
        second.result(timeout=2)
    assert order == [1, 2] and peak == 1 and admission.waiting == 0
    assert slot.acquire(blocking=False)
    slot.release()


def test_full_queue_rejects_without_displacing_waiter():
    slot = threading.BoundedSemaphore(1)
    admission = BoundedAdmission(slot, max_waiting=1)
    slot.acquire()

    def waiter():
        with admission.take(RequestBudget(2, lambda: None, time.monotonic), InferenceTrace(time.monotonic)):
            return "once"

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(waiter)
        wait_for_count(admission, 1)
        try:
            with pytest.raises(ProviderError, match="^CODEX_INFERENCE_BUSY$"):
                waiter()
            assert admission.waiting == 1
        finally:
            slot.release()
        assert pending.result(timeout=2) == "once"
    assert admission.waiting == 0


@pytest.mark.parametrize("code", ["CANCELLED", "CONNECTION_REVISION_CHANGED", "EVIDENCE_ACCESS_CHANGED"])
def test_waiter_guard_failure_leaves_no_identity_or_stale_ticket(monkeypatch, code):
    harness = EngineHarness(monkeypatch, terminal_events())
    harness.adapter.slot.acquire()
    cancelled = threading.Event()

    def guard():
        if cancelled.is_set():
            raise ProviderError(code)

    harness.snapshot["_authority_check"] = guard
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(call_engine, harness)
        wait_for_count(harness.adapter.admission, 1)
        cancelled.set()
        with pytest.raises(ProviderError, match=f"^{code}$"):
            pending.result(timeout=1)
    assert not harness.materialized and harness.factory_calls == 0
    assert harness.adapter.admission.waiting == 0
    assert not harness.adapter.slot.acquire(blocking=False), "waiter released another request's slot"
    harness.adapter.slot.release()
    harness.snapshot["_authority_check"] = harness.guard
    call_engine(harness)
    assert harness.factory_calls == 1


@pytest.mark.parametrize("timeout,queue_cap,expected", [(2, 30, 2), (150, 30, 30), (600, 30, 30)])
def test_queue_wait_uses_smaller_budget_without_launching(monkeypatch, caplog, timeout, queue_cap, expected):
    caplog.set_level("INFO", logger="fund_kb.codex_stability")
    harness = EngineHarness(monkeypatch, terminal_events())
    admission = harness.adapter.admission
    admission.max_wait_seconds = queue_cap
    harness.adapter.slot.acquire()

    def wait(timeout):
        harness.clock.value += timeout

    monkeypatch.setattr(admission.condition, "wait", wait)
    with pytest.raises(ProviderError, match="^CODEX_INFERENCE_QUEUE_TIMEOUT$"):
        call_engine(harness, timeout=timeout)
    assert harness.clock.value - 100 == pytest.approx(expected)
    assert not harness.materialized and harness.factory_calls == 0 and admission.waiting == 0
    assert records(caplog)[-1]["error_phase"] == "queue_wait"
    harness.adapter.slot.release()


def test_queue_time_is_not_added_to_generation_allowance(monkeypatch):
    harness = EngineHarness(monkeypatch, [])
    harness.adapter.slot.acquire()

    def wait(timeout):
        harness.clock.value += .75
        harness.adapter.slot.release()

    monkeypatch.setattr(harness.adapter.admission.condition, "wait", wait)
    with pytest.raises(ProviderError, match="^CODEX_INFERENCE_TIMEOUT$"):
        call_engine(harness, timeout=1)
    assert harness.clock.value - 100 == pytest.approx(1)
    assert harness.factory_calls == 1 and harness.closed and harness.released
    harness.assert_slot_released()


@pytest.mark.parametrize("stage,next_method", [
    ("factory", "account/read"), ("account/read", "thread/start"), ("thread/start", "turn/start"),
])
def test_expired_setup_cannot_send_next_rpc_or_generate(monkeypatch, stage, next_method):
    harness = EngineHarness(monkeypatch, terminal_events())
    original_factory, original_call = harness.factory, harness.call

    def factory(config, home):
        result = original_factory(config, home)
        if stage == "factory":
            harness.clock.value += 2
        return result

    def rpc_call(method, params):
        result = original_call(method, params)
        if stage == method:
            harness.clock.value += 2
        return result

    harness.adapter.transport_factory, harness.call = factory, rpc_call
    with pytest.raises(ProviderError, match="^CODEX_INFERENCE_TIMEOUT$"):
        call_engine(harness)
    assert next_method not in harness.trace
    assert harness.closed and harness.released
    harness.assert_slot_released()
    assert RPC_BUDGET.get() is None


@pytest.mark.parametrize("timeout", [.05, 60, 150, 999])
def test_generation_preserves_caller_budget_and_180_second_cap(monkeypatch, timeout):
    harness = EngineHarness(monkeypatch, [])
    with pytest.raises(ProviderError, match="^CODEX_INFERENCE_TIMEOUT$"):
        call_engine(harness, timeout=timeout)
    assert harness.clock.value - 100 == pytest.approx(min(timeout, 180))
    assert harness.trace.count("turn/start") == 1


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "150"])
def test_invalid_timeout_is_rejected_before_identity(monkeypatch, timeout):
    harness = EngineHarness(monkeypatch, terminal_events())
    with pytest.raises(ProviderError, match="^INVALID_TIMEOUT$"):
        call_engine(harness, timeout=timeout)
    assert not harness.materialized
    harness.assert_slot_released()


def test_late_terminal_event_is_not_delivered(monkeypatch):
    harness = EngineHarness(monkeypatch, terminal_events(), step=1)
    with pytest.raises(ProviderError, match="^CODEX_INFERENCE_TIMEOUT$"):
        call_engine(harness)
    assert harness.closed and harness.released


def test_archive_failure_does_not_mask_model_timeout(monkeypatch, caplog):
    caplog.set_level("INFO", logger="fund_kb.codex_stability")
    harness = EngineHarness(monkeypatch, [])

    def persist(home):
        raise ProviderError("CODEX_AUTH_ARCHIVE_UNAVAILABLE")

    harness.persist = persist
    with pytest.raises(ProviderError, match="^CODEX_INFERENCE_TIMEOUT$"):
        call_engine(harness)
    row = records(caplog)[-1]
    assert row["error_phase"] == "generation"
    assert row["cleanup_codes"] == ["CODEX_AUTH_ARCHIVE_UNAVAILABLE"]
    assert harness.closed and harness.released
    harness.assert_slot_released()


def test_success_requires_persist_and_final_guard_and_stops_writer_first(monkeypatch):
    harness = EngineHarness(monkeypatch, terminal_events())
    call_engine(harness)
    assert harness.trace.index("close") < harness.trace.index("persist") < harness.trace.index("release")
    assert harness.trace[-1] == "guard"


@pytest.mark.parametrize("failure", ["persist", "release"])
def test_cleanup_failure_prevents_success_but_releases_slot(monkeypatch, failure):
    harness = EngineHarness(monkeypatch, terminal_events())

    def fail(home):
        raise ProviderError("CODEX_STORAGE_PATH_UNSAFE")

    setattr(harness, failure, fail)
    with pytest.raises(ProviderError, match="^CODEX_STORAGE_PATH_UNSAFE$"):
        call_engine(harness)
    assert harness.closed
    harness.assert_slot_released()


def test_unconfirmed_close_retains_home_and_quarantines_admission(monkeypatch):
    harness = EngineHarness(monkeypatch, terminal_events())

    def close():
        raise ProviderError("CODEX_RPC_UNAVAILABLE")

    harness.close = close
    with pytest.raises(ProviderError, match="^CODEX_RPC_UNAVAILABLE$"):
        call_engine(harness)
    assert not harness.persisted and not harness.released
    assert harness.adapter.admission.quarantined
    with pytest.raises(ProviderError, match="^CODEX_RPC_UNAVAILABLE$"):
        call_engine(harness)
    assert harness.factory_calls == 1


def test_archive_restore_failure_does_not_spawn_or_retry(monkeypatch):
    harness = EngineHarness(monkeypatch, terminal_events())

    def materialize(*args, **kwargs):
        raise ProviderError("CODEX_AUTH_ARCHIVE_UNAVAILABLE")

    harness.materialize = materialize
    with pytest.raises(ProviderError, match="^CODEX_AUTH_ARCHIVE_UNAVAILABLE$"):
        call_engine(harness)
    assert harness.factory_calls == 0
    harness.assert_slot_released()


def test_completed_messages_are_bounded_even_without_deltas(monkeypatch):
    events = [event("item/completed", item={"type": "agentMessage", "id": str(i), "text": "x" * 40000})
              for i in range(3)]
    harness = EngineHarness(monkeypatch, events)
    with pytest.raises(ProviderError, match="^PROVIDER_RESPONSE_TOO_LARGE$"):
        call_engine(harness)
    assert len(harness.consumed) == 2 and harness.closed and harness.released


def test_trace_measures_stages_and_does_not_log_input_output_or_identity(monkeypatch, caplog):
    caplog.set_level("INFO", logger="fund_kb.codex_stability")
    events = [event("error", willRetry=True, message=CANARY), *terminal_events(json.dumps({"secret": CANARY}))]
    harness = EngineHarness(monkeypatch, events, step=.02)
    call_engine(harness)
    row, = records(caplog)
    assert row["outcome"] == "COMPLETED" and row["server_retry_events"] == 1
    assert row["events"] == 3 and row["phase_ms"]["generation"] == pytest.approx(60)
    assert row["first_event_ms"] == pytest.approx(20) and row["first_output_ms"] == pytest.approx(40)
    assert sum(row["phase_ms"].values()) == pytest.approx(row["total_ms"])
    for forbidden in (CANARY, "synthetic-owner", "synthetic-connection", "thread-test", "turn-test"):
        assert forbidden not in caplog.text
    assert harness.trace.count("turn/start") == 1, "server willRetry must not trigger adapter resend"


def test_arbitrary_exception_and_code_are_sanitized_in_trace(monkeypatch, caplog):
    caplog.set_level("INFO", logger="fund_kb.codex_stability")
    harness = EngineHarness(monkeypatch, terminal_events())

    def guard():
        raise ProviderError(CANARY)

    harness.snapshot["_authority_check"] = guard
    with pytest.raises(ProviderError):
        call_engine(harness)
    assert CANARY not in caplog.text and records(caplog)[-1]["error_code"] == "INTERNAL_ERROR"


@pytest.mark.parametrize("rpc_timeout,total,code", [(.25, 2, "CODEX_RPC_TIMEOUT"), (15, .25, "CODEX_INFERENCE_TIMEOUT")])
def test_real_text_rpc_wait_obeys_both_deadlines_and_sends_once(rpc_timeout, total, code):
    clock = VirtualClock()
    writes, waits, guards = [], [], []
    budget = RequestBudget(total, lambda: guards.append(clock.value), clock.monotonic)
    rpc = object.__new__(TextStdioTransport)
    rpc.config = SimpleNamespace(rpc_timeout_seconds=rpc_timeout)
    rpc.lock, rpc.counter, rpc.failed, rpc.failure_code = threading.Lock(), 0, False, None
    rpc._write = writes.append

    def get(timeout):
        waits.append(timeout)
        clock.value += timeout
        raise queue.Empty

    rpc.responses = SimpleNamespace(get=get)
    token = RPC_BUDGET.set(budget)
    try:
        with pytest.raises(ProviderError, match=f"^{code}$"):
            rpc.call("turn/start", {"input": CANARY})
    finally:
        RPC_BUDGET.reset(token)
    assert len(writes) == 1 and clock.value - 100 == pytest.approx(min(total, rpc_timeout))
    assert all(0 < wait <= .1 for wait in waits) and len(guards) >= len(waits)
    assert rpc.lock.acquire(blocking=False)
    rpc.lock.release()


def test_rpc_wait_checks_cancellation_before_another_wait():
    clock = VirtualClock()
    cancelled, writes = False, []

    def guard():
        if cancelled:
            raise ProviderError("CANCELLED")

    def get(timeout):
        nonlocal cancelled
        cancelled = True
        clock.value += timeout
        raise queue.Empty

    rpc = object.__new__(TextStdioTransport)
    rpc.config = SimpleNamespace(rpc_timeout_seconds=15)
    rpc.lock, rpc.counter, rpc.failed, rpc.failure_code = threading.Lock(), 0, False, None
    rpc._write, rpc.responses = writes.append, SimpleNamespace(get=get)
    token = RPC_BUDGET.set(RequestBudget(150, guard, clock.monotonic))
    try:
        with pytest.raises(ProviderError, match="^CANCELLED$"):
            rpc.call("account/read", {"refreshToken": False})
    finally:
        RPC_BUDGET.reset(token)
    assert clock.value - 100 == pytest.approx(.1) and len(writes) == 1


def test_identity_lock_wait_is_bounded_without_acquiring():
    clock = VirtualClock()
    waits = []

    def acquire(timeout):
        waits.append(timeout)
        clock.value += timeout
        return False

    lock = SimpleNamespace(acquire=acquire, release=lambda: pytest.fail("unowned lock release"))
    with pytest.raises(ProviderError, match="^CODEX_INFERENCE_TIMEOUT$"), checked_lock(
        lock, RequestBudget(.25, lambda: None, clock.monotonic)
    ):
        pytest.fail("lock not acquired")
    assert sum(waits) == pytest.approx(.25) and all(wait <= .1 for wait in waits)


def test_dedicated_safe_logger_emits_info_with_root_warning(monkeypatch):
    handler = next(h for h in LOGGER.handlers if h.formatter and "asctime" in h.formatter._fmt)
    sink = io.StringIO()
    monkeypatch.setattr(handler, "stream", sink)
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    assert LOGGER.level == logging.INFO and not LOGGER.propagate
    assert handler.level == logging.INFO
    InferenceTrace(time.monotonic).emit(None)
    assert '"outcome":"COMPLETED"' in sink.getvalue()
    assert "fund_kb.codex_stability codex_inference" in sink.getvalue()


def test_logging_failure_never_changes_completion(monkeypatch):
    harness = EngineHarness(monkeypatch, terminal_events())

    def broken(*args, **kwargs):
        raise OSError(CANARY)

    monkeypatch.setattr(LOGGER, "info", broken)
    assert call_engine(harness)["choices"][0]["finish_reason"] == "stop"


def test_profile_v2_hash_and_real_text_rpc_wire_sequence_are_unchanged(monkeypatch):
    # Public non-secret contract digest from docs/answer-source-routing-validation.md.
    assert PROFILE_VERSION == 2
    assert INSTRUCTION_CONTRACT_SHA256 == "20f65b5af86e1768a4e22b3587c8606e68efbe76b1c03d8cd3d3ddd95ecf0d1a"
    harness = EngineHarness(monkeypatch, terminal_events())
    writes = []
    rpc = object.__new__(TextStdioTransport)
    rpc.config, rpc.lock, rpc.counter = harness.config, threading.Lock(), 0
    rpc.failed, rpc.failure_code, rpc.events = False, None, harness
    rpc.responses = queue.Queue()
    rpc.close = harness.close

    def write(value):
        writes.append(value)
        if "id" in value:
            result = {} if value["method"] == "initialize" else harness.call(value["method"], value["params"])
            rpc.responses.put({"id": value["id"], "result": result})

    rpc._write = write

    def factory(config, home):
        rpc.call("initialize", {"clientInfo": {"name": "fund_knowledge_platform"}})
        rpc._write({"method": "initialized", "params": {}})
        return rpc

    harness.adapter.transport_factory = factory
    call_engine(harness)
    assert [row["method"] for row in writes] == ["initialize", "initialized", "account/read", "thread/start", "turn/start"]
    assert [row["id"] for row in writes if "id" in row] == [1, 2, 3, 4]
    assert writes[-1]["params"]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert RPC_BUDGET.get() is None


@pytest.mark.parametrize("response,code", [
    ({"id": 2, "result": {}}, "CODEX_RPC_FAILED"),
    ({"id": 1, "error": {"message": CANARY}}, "CODEX_RPC_FAILED"),
    ({"transport_closed": True}, "CODEX_RPC_UNAVAILABLE"),
])
def test_text_rpc_rejects_bad_response_without_resend(response, code):
    rpc = object.__new__(TextStdioTransport)
    rpc.config = SimpleNamespace(rpc_timeout_seconds=15)
    rpc.lock, rpc.counter, rpc.failed, rpc.failure_code = threading.Lock(), 0, False, None
    rpc.responses = queue.Queue()
    rpc.responses.put(response)
    writes = []
    rpc._write = writes.append
    token = RPC_BUDGET.set(RequestBudget(150, lambda: None, time.monotonic))
    try:
        with pytest.raises(ProviderError, match=f"^{code}$"):
            rpc.call("turn/start", {"input": CANARY})
    finally:
        RPC_BUDGET.reset(token)
    assert len(writes) == 1
