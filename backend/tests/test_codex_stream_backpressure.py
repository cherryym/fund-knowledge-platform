"""Offline stream backpressure and delivery-fence regressions.

Exercise the real parser, bounded queues and text engine with BytesIO and fake
process/store objects. No auth/profile file, subprocess, model or database is
needed. A virtual clock tests guard cadence without timing-dependent DB mocks.
"""
from __future__ import annotations

import io
import json
import queue
import socket
import threading
import time
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from fund_kb import codex_bridge as bridge_module
from fund_kb import codex_text as text_module
from fund_kb.codex_bridge import AuthStdioTransport
from fund_kb.codex_bridge_config import CodexBridgeConfig
from fund_kb.codex_stability import BoundedAdmission
from fund_kb.codex_text import CodexTextEngine, TextStdioTransport
from fund_kb.providers import ProviderError

DELTA_COUNT = 1200
CANARY = "SYNTHETIC_PRIVATE_DETAIL_NEVER_EXPOSE"
SAFE_FAILURES = (
    "CODEX_RPC_CLOSED", "CODEX_RPC_TOO_LARGE", "CODEX_RPC_INVALID_MESSAGE",
    "CODEX_RPC_UNSOLICITED_REQUEST", "CODEX_RPC_BACKPRESSURE",
    "CODEX_RPC_IO_ERROR", "CODEX_RPC_UNAVAILABLE",
)


@pytest.fixture(autouse=True)
def forbid_real_identity_process_and_network(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("forbidden")
        raise AssertionError("Offline stream tests must not access auth, subprocesses or network")

    monkeypatch.setattr(bridge_module, "_read_private", forbidden)
    monkeypatch.setattr(text_module, "_read_private", forbidden)
    monkeypatch.setattr(bridge_module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(bridge_module.subprocess, "run", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert not attempts


def event(method, **params):
    return {"method": method, "params": {"threadId": "thread-test", "turnId": "turn-test", **params}}


def deltas(count=DELTA_COUNT):
    return [event("item/agentMessage/delta", itemId="message-test", delta=f"{i},") for i in range(count)]


def terminal_events(text='{"ok":true}'):
    return [
        event("item/completed", item={"type": "agentMessage", "id": "message-test", "text": text}),
        event("turn/completed", turn={"id": "turn-test", "status": "completed"}),
    ]


def encoded(events):
    return b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in events)


class RecordedBytesIO(io.BytesIO):
    """Keep synthetic writes available for assertions after production close()."""
    def close(self):
        if not self.closed:
            self.closed_bytes = self.getvalue()
        super().close()

    def written(self):
        return self.closed_bytes if self.closed else self.getvalue()


class FakeProcess:
    def __init__(self, raw=b""):
        self.stdin = RecordedBytesIO()
        self.stdout = io.BytesIO(raw)
        self.returncode = None
        self.terminated = self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        assert timeout is not None and timeout <= 2
        return self.returncode


class ObservedQueue(queue.Queue):
    """Measure occupancy inside Queue's lock, rather than sampling and missing peaks."""
    def __init__(self, maxsize):
        super().__init__(maxsize=maxsize)
        self.high_water = 0
        self.full_put_started = threading.Event()
        self.put_options = []

    def _put(self, item):
        super()._put(item)
        self.high_water = max(self.high_water, self._qsize())

    def put(self, item, block=True, timeout=None):
        self.put_options.append((block, timeout))
        if self.full():
            self.full_put_started.set()
        return super().put(item, block=block, timeout=timeout)


@pytest.fixture
def make_transport():
    instances = []

    def make(raw=b"", *, capacity=4, timeout=.2, max_bytes=4096, kind=TextStdioTransport):
        # Bypass only process/auth startup. Parser, enqueue, notifications, call
        # failure handling and close below are the actual production methods.
        rpc = object.__new__(kind)
        rpc.config = CodexBridgeConfig(rpc_timeout_seconds=timeout, max_rpc_bytes=max_bytes)
        rpc.events, rpc.responses = ObservedQueue(capacity), queue.Queue(maxsize=128)
        rpc.process = FakeProcess(raw)
        rpc.lock, rpc.counter = threading.Lock(), 0
        rpc.failed, rpc.failure_code = False, None
        rpc.reader = None
        instances.append(rpc)
        return rpc

    yield make
    for rpc in instances:
        rpc.close()
        if rpc.reader is not None:
            rpc.reader.join(timeout=1)
            assert not rpc.reader.is_alive(), "A blocked reader survived transport shutdown"


def start_reader(rpc):
    errors = []

    def read():
        try:
            rpc._read()
        except Exception as exc:  # noqa: BLE001 - asserted by the joining test thread
            errors.append(exc)

    rpc.reader = threading.Thread(target=read, name="test-offline-codex-reader", daemon=True)
    rpc.reader.start()
    return errors


def assert_stopped(rpc, errors, *, timeout=1):
    rpc.reader.join(timeout=timeout)
    assert not rpc.reader.is_alive(), "Event enqueue did not respect its bounded wait"
    assert not errors, errors
    assert rpc.failed and rpc.process.terminated
    assert rpc.process.stdin.closed and rpc.process.stdout.closed


@pytest.mark.parametrize("kind", [AuthStdioTransport, TextStdioTransport])
def test_constructor_preserves_capacity_and_only_text_opts_out_literal_delta(monkeypatch, kind):
    class FakeExecutable:
        def is_absolute(self):
            return True

        def is_symlink(self):
            return False

        def is_file(self):
            return True

        def __str__(self):
            return "/synthetic/codex"

    process = FakeProcess()
    calls = []

    def capture_call(self, method, params=None):
        calls.append((method, params))
        return {}

    monkeypatch.setattr(bridge_module, "Path", lambda value: FakeExecutable())
    monkeypatch.setattr(bridge_module, "_private_directory", lambda path, **kwargs: path)
    monkeypatch.setattr(bridge_module.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=bridge_module.VERIFIED_VERSION.encode()))
    monkeypatch.setattr(bridge_module.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(AuthStdioTransport, "_read", lambda self: None)
    monkeypatch.setattr(AuthStdioTransport, "call", capture_call)
    monkeypatch.setattr(AuthStdioTransport, "_write", lambda *a, **k: None)
    rpc = kind(CodexBridgeConfig(executable=Path("/synthetic/codex")), Path("/synthetic-home"))
    try:
        assert rpc.events.maxsize == rpc.responses.maxsize == 128
        assert rpc.failure_code is None
        assert len(calls) == 1 and calls[0][0] == "initialize"
        expected = {"experimentalApi": False}
        if kind is TextStdioTransport:
            # Exact equality excludes wildcards, safety/lifecycle events and RPCs.
            expected["optOutNotificationMethods"] = ["item/agentMessage/delta"]
            assert {"item/started", "item/completed", "turn/completed", "error"} <= rpc.EVENT_METHODS
        assert calls[0][1]["capabilities"] == expected
    finally:
        rpc.close()
        rpc.reader.join(timeout=1)


@pytest.mark.parametrize("capacity", [1, 4, 128])
def test_fast_delta_burst_and_terminal_events_arrive_in_order_with_slow_consumer(make_transport, capacity):
    expected = [*deltas(), *terminal_events()]
    rpc = make_transport(encoded(expected), capacity=capacity, timeout=1)
    errors = start_reader(rpc)
    # Start draining only after the producer has actually reached backpressure.
    assert rpc.events.full_put_started.wait(timeout=1)
    received = []
    deadline = time.monotonic() + 5
    while len(received) < len(expected) and time.monotonic() < deadline:
        try:
            received.append(rpc.events.get(timeout=.1))
        except queue.Empty:
            if not rpc.reader.is_alive():
                break
        time.sleep(.0005)
    assert_stopped(rpc, errors)
    assert received == expected, "A delta or terminal event was dropped, duplicated or reordered"
    assert rpc.events.empty()
    assert rpc.events.high_water == capacity and rpc.events.maxsize == capacity
    assert rpc.failure_code == "CODEX_RPC_CLOSED", "Normal EOF must not become queue overflow"
    assert all(block and timeout is not None and 0 < timeout <= .05
               for block, timeout in rpc.events.put_options)


@pytest.mark.parametrize("timeout", [.06, .15])
def test_no_consumer_times_out_without_enlarging_or_overwriting_queue(make_transport, timeout):
    expected = deltas(3)
    rpc = make_transport(encoded(expected), capacity=1, timeout=timeout)
    started = time.monotonic()
    errors = start_reader(rpc)
    assert_stopped(rpc, errors, timeout=timeout + .5)
    elapsed = time.monotonic() - started
    assert timeout * .8 <= elapsed < timeout + .5
    assert rpc.failure_code == "CODEX_RPC_BACKPRESSURE"
    assert rpc.events.maxsize == rpc.events.high_water == 1
    assert rpc.notifications() == expected[:1]
    assert rpc.responses.get_nowait() == {"transport_closed": True}


@pytest.mark.parametrize("stop", ["close", "failed_flag"])
def test_shutdown_interrupts_blocked_enqueue_before_full_rpc_timeout(make_transport, stop):
    expected = deltas(3)
    rpc = make_transport(encoded(expected), capacity=1, timeout=3)
    errors = start_reader(rpc)
    assert rpc.events.full_put_started.wait(timeout=1)
    started = time.monotonic()
    if stop == "close":
        rpc.close()
    else:
        rpc.failed = True
    assert_stopped(rpc, errors, timeout=.5)
    assert time.monotonic() - started < .5
    assert rpc.failure_code == "CODEX_RPC_CLOSED"
    assert rpc.notifications() == expected[:1]


def test_failed_text_transport_never_accepts_another_event(make_transport):
    rpc = make_transport()
    rpc.failed = True
    with pytest.raises(ProviderError, match="^CODEX_RPC_CLOSED$"):
        rpc._enqueue_event(deltas(1)[0])
    assert rpc.events.empty()


def test_auth_enqueue_retains_nonblocking_overflow_behavior(make_transport):
    rpc = make_transport(capacity=1, timeout=3, kind=AuthStdioTransport)
    notification = {"method": "account/updated", "params": {}}
    rpc._enqueue_event(notification)
    started = time.monotonic()
    with pytest.raises(queue.Full):
        rpc._enqueue_event(notification)
    assert time.monotonic() - started < .2
    assert rpc.events.put_options == [(False, None), (False, None)]
    assert rpc.notifications() == [notification]


@pytest.mark.parametrize("kind", [AuthStdioTransport, TextStdioTransport])
@pytest.mark.parametrize("method", ["item/commandExecution/requestApproval", "item/tool/call", "exec_command",
                                   "item/agentMessage/delta"])
def test_server_initiated_rpc_is_rejected_even_when_its_method_looks_like_an_event(make_transport, kind, method):
    request = {"id": 42, "method": method, "params": {"command": CANARY}}
    rpc = make_transport(encoded([request, *deltas(1)]), kind=kind)
    errors = start_reader(rpc)
    assert_stopped(rpc, errors)
    assert rpc.failure_code == "CODEX_RPC_UNSOLICITED_REQUEST"
    assert rpc.events.empty()
    reply = json.loads(rpc.process.stdin.written())
    assert reply == {"id": 42, "error": {"code": -32601, "message": "Disabled"}}
    assert CANARY not in str(reply) and CANARY not in rpc.failure_code


@pytest.mark.parametrize("method", ["command/exec", "fs/readFile", "thread/shellCommand", "process/spawn"])
def test_text_transport_does_not_expand_outgoing_rpc_allowlist(make_transport, method):
    rpc = make_transport()
    with pytest.raises(ProviderError, match="^CODEX_RPC_FORBIDDEN$"):
        rpc.call(method, {"value": CANARY})
    assert rpc.process.stdin.written() == b""


@pytest.mark.parametrize("kind", [AuthStdioTransport, TextStdioTransport])
def test_oversized_inbound_message_fails_before_event_admission(make_transport, kind):
    notification = {"method": "account/updated" if kind is AuthStdioTransport else "item/agentMessage/delta",
                    "params": {"delta": CANARY * 50}}
    rpc = make_transport(encoded([notification]), max_bytes=256, kind=kind)
    errors = start_reader(rpc)
    assert_stopped(rpc, errors)
    assert rpc.failure_code == "CODEX_RPC_TOO_LARGE" and rpc.events.empty()
    assert rpc.process.stdin.written() == b""


def test_valid_message_exactly_at_byte_limit_is_accepted(make_transport):
    notification = deltas(1)[0]
    raw = encoded([notification])
    rpc = make_transport(raw, max_bytes=len(raw))
    errors = start_reader(rpc)
    assert_stopped(rpc, errors)
    assert rpc.notifications() == [notification]
    assert rpc.failure_code == "CODEX_RPC_CLOSED"


def test_oversized_outgoing_rpc_never_reaches_process_stdin(make_transport):
    rpc = make_transport(max_bytes=128)
    with pytest.raises(ProviderError, match="^CODEX_RPC_TOO_LARGE$"):
        rpc._write({"method": "turn/start", "params": {"text": "x" * 129}})
    assert rpc.process.stdin.written() == b""


@pytest.mark.parametrize("raw", [b"[]\n", b"null\n", b"not-json\n", b'"' + CANARY.encode() + b'"\n'])
def test_malformed_message_has_only_a_safe_failure_code(make_transport, raw):
    rpc = make_transport(raw)
    errors = start_reader(rpc)
    assert_stopped(rpc, errors)
    assert rpc.failure_code == "CODEX_RPC_INVALID_MESSAGE"
    assert rpc.events.empty() and CANARY not in rpc.failure_code


@pytest.mark.parametrize("raised,expected", [
    (ProviderError("CODEX_RPC_BACKPRESSURE"), "CODEX_RPC_BACKPRESSURE"),
    (ProviderError("CODEX_RPC_CLOSED"), "CODEX_RPC_CLOSED"),
    (ProviderError("CODEX_RPC_TOO_LARGE"), "CODEX_RPC_TOO_LARGE"),
    (ProviderError(CANARY), "CODEX_RPC_UNAVAILABLE"),
    (OSError(CANARY), "CODEX_RPC_IO_ERROR"),
])
def test_enqueue_failures_are_classified_without_exposing_exception_details(make_transport, raised, expected):
    rpc = make_transport(encoded(deltas(1)))

    def fail(value):
        raise raised

    rpc._enqueue_event = fail
    errors = start_reader(rpc)
    assert_stopped(rpc, errors)
    assert rpc.failure_code == expected
    assert rpc.responses.get_nowait() == {"transport_closed": True}
    with pytest.raises(ProviderError) as caught:
        rpc.call("account/read", {})
    assert caught.value.code == expected and CANARY not in str(caught.value)


class VirtualClock:
    def __init__(self):
        self.value = 100.0

    def monotonic(self):
        return self.value


@pytest.mark.parametrize("configured_timeout", [.12, 2.0, 15.0])
def test_enqueue_wait_budget_is_capped_at_two_seconds_with_50ms_polling(make_transport, monkeypatch, configured_timeout):
    rpc = make_transport(capacity=1, timeout=configured_timeout)
    rpc.events.put_nowait(deltas(1)[0])
    clock = VirtualClock()
    waits = []

    def full_queue_wait(value, *, timeout):
        waits.append(timeout)
        clock.value += timeout
        raise queue.Full

    monkeypatch.setattr(text_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(rpc.events, "put", full_queue_wait)
    with pytest.raises(ProviderError, match="^CODEX_RPC_BACKPRESSURE$"):
        rpc._enqueue_event(deltas(2)[1])
    assert sum(waits) == pytest.approx(min(2.0, configured_timeout))
    assert all(0 < wait <= .05 for wait in waits)
    assert rpc.events.maxsize == rpc.events.qsize() == 1
    assert rpc.events.get_nowait() == deltas(1)[0]


class EngineHarness:
    """Run CodexTextEngine.complete while replacing only external boundaries."""
    def __init__(self, monkeypatch, events, *, step=.00025, revoke_at=None, failure_code=None):
        self.clock = VirtualClock()
        self.trace, self.guards, self.consumed = [], [], []
        self.step, self.revoke_at, self.revoked = step, revoke_at, False
        self.pending = iter(events)
        self.lock_count = 0
        self.persisted = self.released = self.closed = self.materialized = False
        self.factory_calls = 0
        self.failed = failure_code is not None
        if failure_code is not None:
            self.failure_code = failure_code
        self.home = Path("/synthetic-offline-text-home")
        self.config = CodexBridgeConfig()
        self.events = self
        self.store = self
        self.adapter = object.__new__(CodexTextEngine)
        self.adapter.bridge = self
        self.adapter.transport_factory = self.factory
        self.adapter.models = frozenset({"synthetic"})
        self.adapter.catalog = b'{"models":[]}'
        self.adapter.slot = threading.BoundedSemaphore(1)
        self.adapter.admission = BoundedAdmission(self.adapter.slot)
        self.snapshot = {"model_id": "synthetic", "owner_user_id": "synthetic-owner", "id": "synthetic-connection",
                         "auth_epoch": 1, "revision": 1, "_authority_check": self.guard}
        monkeypatch.setattr(text_module, "time", SimpleNamespace(monotonic=self.clock.monotonic))
        monkeypatch.setattr(text_module, "_write_private", self.write_catalog)

    def guard(self):
        self.guards.append((self.clock.value, len(self.consumed)))
        self.trace.append("guard")
        if self.revoke_at == "initial" or (self.revoke_at == "stream" and self.clock.value >= 100.12):
            self.revoked = True
        if self.revoked:
            raise ProviderError("CONNECTION_REVISION_CHANGED")

    def _lock(self, owner, cid):
        self.lock_count += 1
        self.trace.append("lock")
        if self.revoke_at == "persist_lock" and self.lock_count == 2:
            self.revoked = True
        return threading.RLock()

    def _session(self, owner, cid, epoch, revision):
        return SimpleNamespace(authenticated=True)

    def materialize(self, *args, **kwargs):
        self.materialized = True
        self.trace.append("materialize")
        return self.home

    def write_catalog(self, path, data):
        assert path == self.home / "models.json" and data == self.adapter.catalog
        self.trace.append("catalog")

    def persist(self, home):
        assert home == self.home
        self.trace.append("persist")
        self.persisted = True

    def release(self, home):
        assert home == self.home
        self.trace.append("release")
        self.released = True

    def factory(self, config, home):
        assert config == self.config and home == self.home
        self.factory_calls += 1
        self.trace.append("factory")
        return self

    def call(self, method, params):
        self.trace.append(method)
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        if method == "thread/start":
            assert params["ephemeral"] and params["approvalPolicy"] == "never"
            assert params["sandbox"] == "read-only"
            return {"thread": {"id": "thread-test"}}
        if method == "turn/start":
            assert params["approvalPolicy"] == "never"
            assert params["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
            return {"turn": {"id": "turn-test"}}
        raise AssertionError(method)

    def get(self, timeout):
        try:
            value = next(self.pending)
        except StopIteration:
            self.clock.value += timeout
            raise queue.Empty from None
        self.clock.value += self.step
        self.consumed.append(value)
        self.trace.append(value["method"])
        if self.revoke_at == "terminal" and value["method"] == "turn/completed":
            self.revoked = True
        return value

    def close(self):
        self.trace.append("close")
        self.closed = True

    def invoke(self, *, max_tokens=4096):
        return self.adapter.complete(self.snapshot, [{"role": "user", "content": "synthetic"}],
                                     max_tokens=max_tokens, json_mode=True, timeout=2)

    def assert_slot_released(self):
        assert self.adapter.slot.acquire(blocking=False)
        self.adapter.slot.release()


def test_engine_high_frequency_deltas_throttle_guards_but_force_final_and_persistence_checks(monkeypatch):
    expected = [*deltas(), *terminal_events()]
    harness = EngineHarness(monkeypatch, expected)
    result = harness.invoke()
    assert result["choices"][0]["message"]["content"] == '{"ok":true}'
    assert result["choices"][0]["finish_reason"] == "stop"
    assert harness.consumed == expected
    # Only guards between consumed events are throttled. Startup, final delivery
    # and persistence guards are deliberately allowed at the same timestamp.
    during = [stamp for stamp, count in harness.guards if 0 < count < len(expected)]
    assert 2 <= len(during) <= 4
    assert all(later - earlier >= .1 - 1e-8 for earlier, later in pairwise(during))
    assert len(harness.guards) <= 20, "Guard/database work must not scale per delta"
    assert harness.trace[0] == "guard"
    assert harness.trace.index("guard") < harness.trace.index("materialize")
    terminal = harness.trace.index("turn/completed")
    persist = harness.trace.index("persist")
    assert "guard" in harness.trace[terminal + 1:persist]
    assert harness.trace[persist - 1] == "guard"
    assert harness.persisted and harness.closed and harness.released
    harness.assert_slot_released()


@pytest.mark.parametrize("revoke_at", ["initial", "stream", "terminal", "persist_lock"])
def test_throttling_never_bypasses_revocation_or_persists_revoked_identity(monkeypatch, revoke_at):
    # Terminal revocation occurs inside one throttle interval, so only a forced
    # final check can catch it. Stream revocation requires periodic revalidation.
    step = .00025 if revoke_at == "stream" else .000001
    harness = EngineHarness(monkeypatch, [*deltas(), *terminal_events()], step=step, revoke_at=revoke_at)
    with pytest.raises(ProviderError, match="^CONNECTION_REVISION_CHANGED$"):
        harness.invoke()
    assert not harness.persisted
    if revoke_at == "initial":
        assert not harness.materialized and harness.factory_calls == 0
    else:
        assert harness.closed and harness.released
    if revoke_at == "stream":
        assert 0 < len(harness.consumed) < DELTA_COUNT
    if revoke_at == "terminal":
        assert harness.clock.value - 100 < .1
    harness.assert_slot_released()


@pytest.mark.parametrize("failure_code", [*SAFE_FAILURES, None])
def test_engine_propagates_safe_transport_failure_and_supports_legacy_transport(monkeypatch, failure_code):
    harness = EngineHarness(monkeypatch, [], failure_code=failure_code)
    harness.failed = True
    with pytest.raises(ProviderError) as caught:
        harness.invoke()
    assert caught.value.code == (failure_code or "CODEX_RPC_UNAVAILABLE")
    assert harness.closed and harness.released
    harness.assert_slot_released()


@pytest.mark.parametrize("kind", ["commandExecution", "fileChange", "mcpToolCall", "webSearch", "imageView"])
def test_engine_still_rejects_nontext_items_after_delta_burst(monkeypatch, kind):
    events = [*deltas(), event("item/started", item={"id": "forbidden", "type": kind}), *terminal_events()]
    harness = EngineHarness(monkeypatch, events)
    with pytest.raises(ProviderError, match="^UNSUPPORTED_TOOL_CALL$"):
        harness.invoke()
    assert harness.closed and harness.released
    assert all(item["method"] != "turn/completed" for item in harness.consumed)
    harness.assert_slot_released()


@pytest.mark.parametrize("fault,code", [
    ("output_limit", "PROVIDER_RESPONSE_TOO_LARGE"),
    ("turn_mismatch", "CODEX_TURN_MISMATCH"),
    ("incomplete_turn", "PROVIDER_RESPONSE_INCOMPLETE"),
    ("invalid_json", "PROVIDER_INVALID_JSON"),
])
def test_final_delivery_gates_are_unchanged_after_backpressure_optimization(monkeypatch, fault, code):
    events = [*deltas(), *terminal_events()]
    if fault == "turn_mismatch":
        events[-1]["params"]["turnId"] = "other-turn"
    elif fault == "incomplete_turn":
        events[-1]["params"]["turn"]["status"] = "failed"
    elif fault == "invalid_json":
        events[-2]["params"]["item"]["text"] = "invalid JSON"
    harness = EngineHarness(monkeypatch, events)
    with pytest.raises(ProviderError, match=f"^{code}$"):
        harness.invoke(max_tokens=1 if fault == "output_limit" else 4096)
    assert harness.closed and harness.released
    harness.assert_slot_released()
