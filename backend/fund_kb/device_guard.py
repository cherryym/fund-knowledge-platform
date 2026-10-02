"""One process-wide lock for Metal (MPS) work of local models.

PyTorch's MPS backend is not safe for concurrent command submission from several
threads: an embedding batch (indexing) running while a reranking batch (retrieval)
ran aborted the API process with an IOGPUMetalCommandBuffer assertion. Every local
model therefore serializes its MPS forward passes, weight transfers and cache
releases here. CPU execution is not serialized by this lock.
"""
from __future__ import annotations

import threading
from contextlib import nullcontext

_MPS_LOCK = threading.RLock()


def gpu_section(device):
    """Context manager for GPU work on ``device`` ("mps" serializes; anything else is a no-op)."""
    return _MPS_LOCK if device == "mps" else nullcontext()
