"""V4 large-context probes: the v3 loopback matrix rebound to the v4 contract,
with a synthetic request above the v3 64 KiB semantic cap.

Loads the pinned v3 runner by path and changes only the contract under test, its
caps and the synthetic fixture size. Same OS-confined loopback mock, pinned
binary and capture inspector; no account RPC, identity store, credential or
business input. Writes a NEW data/codex-text-v4-candidate-* directory;
installation is a separate step.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
from fund_kb.codex_text import (
    CONFIGURABLE_PROFILE_VERSION,
    LARGE_CONTEXT_INSTRUCTION_CONTRACT,
    LARGE_CONTEXT_INSTRUCTION_CONTRACT_SHA256,
    LARGE_CONTEXT_PROFILE_VERSION,
    configured_text_request_params,
    contract_budget,
    text_request_sizes,
)
from fund_kb.providers import ProviderError

V3_RUNNER = Path(__file__).with_name("probe-codex-reasoning-v3.py")
PINNED_V3_RUNNER_SHA256 = "54512bf335868d947af828eeb3924942e155f42dd272e578092032f72673269b"
PROBE_SUITE_VERSION = "large-context-v4-loopback-matrix-1"
# Above the v3 semantic cap, within v4's with room for the instruction channels.
LARGE_MESSAGE_UTF8_BYTES = 180000
USER_TAIL = "\nUSER_END_1e7"


def digest(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def large_helpers(helpers):
    """The v2 fixture with indexed synthetic paragraphs before its final user marker, so a dropped or reordered
    middle section fails the inspector's full-text containment check."""
    messages = copy.deepcopy(helpers.PROBE_MESSAGES)
    if not messages[-1]["content"].endswith(USER_TAIL):
        raise ProviderError("V4_FIXTURE_BASE_CHANGED")
    head = messages[-1]["content"][:-len(USER_TAIL)]

    def size(body):
        messages[-1]["content"] = head + body + USER_TAIL
        return len(json.dumps(messages, ensure_ascii=False).encode())

    paragraphs = []
    while True:
        line = f"第{len(paragraphs):05d}段合成正文：用于核对大容量请求完整、按序送达，不含任何业务资料。"
        if size("".join(paragraphs) + line) > LARGE_MESSAGE_UTF8_BYTES:
            break
        paragraphs.append(line)
    remainder = LARGE_MESSAGE_UTF8_BYTES - size("".join(paragraphs))
    size("".join(paragraphs) + "资" * (remainder // 3) + "x" * (remainder % 3))
    thread, turn = configured_text_request_params(messages, "synthetic", "/probe")
    sizes = text_request_sizes(thread, turn)
    semantic_cap, ipc_cap = contract_budget(LARGE_CONTEXT_PROFILE_VERSION)
    if not contract_budget(CONFIGURABLE_PROFILE_VERSION)[0] < sizes["semantic_utf8_bytes"] <= semantic_cap \
            or max(sizes["thread_rpc_bytes"], sizes["turn_rpc_bytes"]) > ipc_cap:
        raise ProviderError("V4_FIXTURE_OUT_OF_RANGE")
    return SimpleNamespace(PROBE_MESSAGES=messages, inspect_capture=helpers.inspect_capture,
                           PROBE_FIXTURE_SHA256=digest(json.dumps(messages, ensure_ascii=False, sort_keys=True)))


def load_runner():
    if digest(V3_RUNNER.read_bytes()) != PINNED_V3_RUNNER_SHA256:
        raise ProviderError("V3_RUNNER_CHANGED")
    spec = importlib.util.spec_from_file_location("fkb_large_context_v4_runner", V3_RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = module.load_helpers
    module.load_helpers = lambda: large_helpers(base())
    module.PROFILE_UNDER_TEST = LARGE_CONTEXT_PROFILE_VERSION
    module.CONTRACT_UNDER_TEST = LARGE_CONTEXT_INSTRUCTION_CONTRACT
    module.CONTRACT_SHA256_UNDER_TEST = LARGE_CONTEXT_INSTRUCTION_CONTRACT_SHA256
    module.BUDGET_CAPS = contract_budget(LARGE_CONTEXT_PROFILE_VERSION)
    module.MIN_SEMANTIC_BYTES = contract_budget(CONFIGURABLE_PROFILE_VERSION)[0]
    module.CANDIDATE_PREFIX = "codex-text-v4-candidate-"
    module.PROBE_SUITE_VERSION = PROBE_SUITE_VERSION
    module.EXTRA_SOURCES = (Path(__file__),)
    return module


def main(argv=None):
    return load_runner().main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
