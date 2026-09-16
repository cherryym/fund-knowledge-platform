"""Bounded identifier-only route reuse. Never an answer or authorization cache."""
from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict

_LOCK = threading.RLock()
_CACHE = OrderedDict()
_MAX_ENTRIES = 64
_MAX_BYTES = 4 * 1024 * 1024
_TTL = 300.0


def clear_query_path_cache():
    with _LOCK:
        _CACHE.clear()


def get_query_path(key):
    with _LOCK:
        found = _CACHE.get(key)
        if not found:
            return None
        created, text = found
        if time.monotonic() - created > _TTL:
            del _CACHE[key]
            return None
        _CACHE.move_to_end(key)
        return json.loads(text)


def put_query_path(key, plan):
    # The caller key MUST bind user, space, scope/context, catalog signature,
    # primary-source policy and embedding/prompt/router versions. Even a hit
    # goes through current source/hash/ACL reading before model input.
    value = {k: plan[k] for k in ("requested", "anchors", "reasons", "used_edges", "deferred_pages", "warnings", "stats") if k in plan}
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    size = len(text.encode())
    if size > _MAX_BYTES:
        return False
    with _LOCK:
        _CACHE[key] = (time.monotonic(), text)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _MAX_ENTRIES or sum(len(v[1].encode()) for v in _CACHE.values()) > _MAX_BYTES:
            _CACHE.popitem(last=False)
    return True
