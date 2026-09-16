"""Lossless context packing against the adapter's real serialized request budget."""
from __future__ import annotations

import json
import re

from .providers import ProviderError


def fits_context(system, body, connection, *, default_capacity=1048576):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": body}]
    capacity = int(connection.get("max_request_bytes", default_capacity))
    if connection.get("protocol") == "codex_app_server":
        from .codex_text import check_text_request_budget, text_request_params
        if len(json.dumps(messages, ensure_ascii=False).encode()) > capacity:
            return False
        thread, turn = text_request_params(messages, connection.get("model_id", ""), "")
        try:
            check_text_request_budget(thread, turn, capacity)
            return True
        except ProviderError as exc:
            if exc.code not in {"PROVIDER_REQUEST_TOO_LARGE", "CODEX_RPC_TOO_LARGE"}:
                raise
            return False
    # HTTP adapters add protocol fields around messages. Keep a small explicit
    # envelope allowance, not the old unexplained division by three.
    return len(json.dumps({"messages": messages, "model": connection.get("model_id", "")},
                          ensure_ascii=False).encode()) + 1024 <= capacity


def pack_text(text, prefix, suffix, fits):
    """Return every character, in order. Prefer line boundaries; never discard tails."""
    if not fits(prefix + suffix):
        raise ProviderError("PROVIDER_CONTEXT_CAPACITY_REQUIRED")
    if not text:
        return [""]
    chunks, offset = [], 0
    while offset < len(text):
        if fits(prefix + text[offset:] + suffix):
            chunks.append(text[offset:])
            break
        low, high = 0, len(text) - offset
        while low < high:
            size = (low + high + 1) // 2
            if fits(prefix + text[offset:offset + size] + suffix):
                low = size
            else:
                high = size - 1
        if low == 0:
            raise ProviderError("PROVIDER_CONTEXT_CAPACITY_REQUIRED")
        boundary = text.rfind("\n", offset, offset + low)
        end = boundary + 1 if boundary > offset else offset + low
        # A standalone citation label belongs to the following body, not the
        # preceding packet. Shift the cut back without losing/reordering bytes.
        # If even label + first body character cannot fit, fail explicitly;
        # never manufacture a label-only packet or loop at the same offset.
        last_line = text.rfind("\n", offset, max(offset, end - 1)) + 1
        last_line = max(offset, last_line)
        if re.fullmatch(r"\[E\d+\][^\n]*\n", text[last_line:end]):
            if last_line > offset:
                end = last_line
            elif boundary >= offset and low > end - offset:
                end = offset + low
            else:
                raise ProviderError("PROVIDER_CONTEXT_CAPACITY_REQUIRED")
        chunks.append(text[offset:end])
        offset = end
    return chunks
