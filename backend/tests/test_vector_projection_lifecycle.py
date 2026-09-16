"""Synthetic projection/ACL/BM25 tests against temporary, persistent QdrantLocal."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from qdrant_client import models

from fund_kb import retrieval
from fund_kb.ai_transport import ProviderError
from fund_kb.retrieval import EmbeddingProvider, VectorIndex


def settings(tmp_path, **changes):
    return SimpleNamespace(
        **(
            {
                "app_env": "development",
                "qdrant_path": tmp_path / "qdrant",
                "qdrant_url": None,
                "embedding_mode": "hashing",
                "embedding_model": "synthetic",
                "embedding_dimensions": 8,
                "embedding_chunk_bytes": 64,
                "embedding_overlap_bytes": 8,
                "embedding_title_bytes": 16,
                "embedding_batch_size": 3,
                "embedding_threads": 2,
            }
            | changes
        )
    )


def record(text="alpha beta", *, version_id=None, resource_id=None, **changes):
    result = {
        "resource_id": resource_id or str(uuid4()),
        "version_id": version_id or str(uuid4()),
        "block_id": str(uuid4()),
        "text": text,
        "title": "",
        "block_type": "paragraph",
        "data": {"text": text},
        "locator": {"source_page": 2},
        "ordinal": 0,
    } | changes
    result["content_sha256"] = hashlib.sha256(result["text"].encode("utf-8")).hexdigest()
    return result


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    allowed_loopback = set()
    original_connect = socket.socket.connect

    def forbidden(*args, **kwargs):
        pytest.fail("No real network or cloud model is permitted")

    def local_test_only(sock, address):
        if address in allowed_loopback:
            return original_connect(sock, address)
        return forbidden()

    monkeypatch.setattr(socket.socket, "connect", local_test_only)
    monkeypatch.setattr(retrieval, "post_json", forbidden)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    return allowed_loopback


@pytest.fixture
def make_index(tmp_path, monkeypatch):
    opened = []

    def make(**changes):
        index = VectorIndex(settings(tmp_path, **changes))
        opened.append(index)
        index.test_embeddings = []

        def synthetic_embeddings(texts, *, query=False):
            index.test_embeddings.append((list(texts), query))
            return [[1.0] + [0.0] * (index.embedding.dimension - 1) for _ in texts]

        monkeypatch.setattr(index.embedding, "embed", synthetic_embeddings)
        return index

    yield make
    for index in opened:
        index.close()


@pytest.fixture(params=["local", "native"])
def isolated_backend(request, make_index, tmp_path, offline_only):
    if request.param == "local":
        yield make_index()
        return
    binary = Path(__file__).resolve().parents[2] / "tools/qdrant/1.19.1/qdrant"
    if not binary.is_file():
        pytest.skip("Qdrant 1.19.1 binary not installed; no download attempted")
    with socket.socket() as http_socket, socket.socket() as grpc_socket:
        http_socket.bind(("127.0.0.1", 0))
        grpc_socket.bind(("127.0.0.1", 0))
        http_port, grpc_port = http_socket.getsockname()[1], grpc_socket.getsockname()[1]
    offline_only.add(("127.0.0.1", http_port))
    url = f"http://127.0.0.1:{http_port}"
    environment = {
        "PATH": "/usr/bin:/bin",
        "QDRANT__SERVICE__HOST": "127.0.0.1",
        "QDRANT__SERVICE__HTTP_PORT": str(http_port),
        "QDRANT__SERVICE__GRPC_PORT": str(grpc_port),
        "QDRANT__SERVICE__ENABLE_STATIC_CONTENT": "false",
        "QDRANT__STORAGE__STORAGE_PATH": str(tmp_path / "native-storage"),
        "QDRANT__STORAGE__SNAPSHOTS_PATH": str(tmp_path / "native-snapshots"),
        "QDRANT__STORAGE__PERFORMANCE__MAX_SEARCH_THREADS": "2",
        "QDRANT__CLUSTER__ENABLED": "false",
        "QDRANT__LOG_LEVEL": "ERROR",
    }
    process = subprocess.Popen(
        [str(binary), "--disable-telemetry"],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    index = None
    try:
        deadline = time.monotonic() + 8
        with httpx.Client(trust_env=False, timeout=0.3) as probe:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"Isolated native Qdrant exited: {process.communicate()[0][-2000:]}")
                try:
                    if probe.get(url + "/readyz").status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.02)
            else:
                pytest.fail("Isolated native Qdrant did not become ready")
        index = make_index(qdrant_url=url)
        assert index._shared.client.info().version == "1.19.1"
        yield index
    finally:
        if index is not None:
            index.close()
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def points(index):
    if not index._shared.client.collection_exists(index.collection):
        return []
    result, offset = [], None
    while True:
        page, offset = index._shared.client.scroll(
            index.collection, limit=256, offset=offset, with_payload=True, with_vectors=False
        )
        result.extend(page)
        if offset is None:
            return result


def projection_points(index, projection_id):
    return [p for p in points(index) if p.payload["projection_id"] == projection_id]


def activate(index, records, projection_id):
    receipt = index.stage_version(records, projection_id)
    index.activate_version(records[0]["version_id"], projection_id, receipt["chunk_count"])
    return receipt


def test_complete_staging_projection_preserves_source_hash_offsets_title_and_batch_budget(make_index):
    index = make_index()
    first = record(
        ("中文🙂 synthetic text。\n| a | b |\n\tcode  \n" * 80) + "最后尾部  ", title="原标题🙂" * 20
    )
    second = record("  \t\n", version_id=first["version_id"], resource_id=first["resource_id"])
    empty = record("", version_id=first["version_id"], resource_id=first["resource_id"])
    original = copy.deepcopy([first, second, empty])
    events = []
    receipt = index.stage_version(
        [first, second, empty],
        "build-a",
        checkpoint=lambda: events.append("check"),
        progress=lambda n: events.append(n),
    )
    stored = projection_points(index, "build-a")
    assert receipt == {"block_count": 3, "chunk_count": len(stored)}
    assert [first, second, empty] == original
    assert receipt["chunk_count"] > 100
    assert index.search("中文", [first["version_id"]]) == []
    assert index.lexical_search("synthetic", [first["version_id"]], allowed_projection_ids=["build-a"]) == []
    for parent in original:
        children = sorted(
            (p.payload for p in stored if p.payload["block_id"] == parent["block_id"]),
            key=lambda p: p["chunk_index"],
        )
        covered, rebuilt = 0, []
        for i, child in enumerate(children):
            start, end = child["chunk_start"], child["chunk_end"]
            assert child["chunk_index"] == i
            assert child["ready"] is False
            assert child["parent_version_id"] == child["version_id"] == parent["version_id"]
            assert child["parent_block_id"] == child["block_id"] == parent["block_id"]
            assert child["parent_content_sha256"] == child["content_sha256"] == parent["content_sha256"]
            assert child["start"] == start and child["end"] == end
            assert start <= covered < end
            assert child["text"] == parent["text"][start:end]
            assert child["chunk_sha256"] == hashlib.sha256(child["text"].encode()).hexdigest()
            assert child["title"] == parent["title"] and child["locator"] == parent["locator"]
            assert child["embedding_fingerprint"] == index.embedding.fingerprint
            assert child["projection_schema"] == retrieval.PROJECTION_SCHEMA
            assert len(parent["text"][start:covered].encode()) <= 8
            rebuilt.append(child["text"][covered - start :])
            covered = end
        assert "".join(rebuilt) == parent["text"] and covered == len(parent["text"])
    passage_batches = [batch for batch, query in index.test_embeddings if not query]
    assert all(0 < len(batch) <= 3 for batch in passage_batches)
    assert all(len(text.encode()) <= 64 for batch in passage_batches for text in batch)
    assert events[::3] == ["check"] * len(passage_batches)
    assert events[2::3] == ["check"] * len(passage_batches)
    assert events[-2] == receipt["chunk_count"]
    status = index.status()
    assert status["ready_chunks"] == 0 and status["staging_chunks"] == len(stored)
    index.activate_version(first["version_id"], "build-a", receipt["chunk_count"])
    hits = index.search("中文", [first["version_id"]], allowed_projection_ids=["build-a"])
    assert hits and all(h["ready"] and h["candidate_only"] for h in hits)


def test_title_prefix_is_byte_bounded_embedding_only(make_index):
    index = make_index(embedding_chunk_bytes=24, embedding_title_bytes=10, embedding_overlap_bytes=3)
    source = record("中文abcdef" * 20, title="标题🙂untouched-suffix")
    index.stage_version([source], "title")
    embedded = [text for batch, query in index.test_embeddings if not query for text in batch]
    assert all(text.startswith("标题\n") and len(text.encode()) <= 24 for text in embedded)
    children = projection_points(index, "title")
    assert all(p.payload["title"] == source["title"] for p in children)
    assert all(p.payload["text"] in source["text"] for p in children)


def test_activation_count_mismatch_leaves_old_projection_and_staging_untouched(make_index):
    index = make_index()
    source = record("old alpha")
    activate(index, [source], "old")
    changed = record("new beta " * 30, version_id=source["version_id"], resource_id=source["resource_id"])
    receipt = index.stage_version([changed], "new")
    with pytest.raises(ValueError, match="CHUNK_COUNT_MISMATCH"):
        index.activate_version(source["version_id"], "new", receipt["chunk_count"] + 1)
    assert all(p.payload["ready"] for p in projection_points(index, "old"))
    assert all(not p.payload["ready"] for p in projection_points(index, "new"))
    assert {h["projection_id"] for h in index.search("alpha", [source["version_id"]])} == {"old"}


def test_activation_preserves_old_and_staging_until_explicit_scoped_prune(make_index):
    current = make_index()
    old_model = make_index(embedding_dimensions=16)
    source, other = record("target alpha"), record("unrelated beta")
    activate(current, [source], "old")
    activate(current, [other], "other")
    activate(old_model, [source], "old-fingerprint")
    current.stage_version([source], "abandoned")
    receipt = current.stage_version([source], "new")
    current.activate_version(source["version_id"], "new", receipt["chunk_count"])
    current.activate_version(source["version_id"], "new", receipt["chunk_count"])
    assert {p.payload["projection_id"] for p in points(current)} == {"old", "new", "other", "abandoned"}
    current.prune_ready_projections(source["version_id"], ["new"])
    current.prune_ready_projections(source["version_id"], ["new"])
    assert {p.payload["projection_id"] for p in points(current)} == {"new", "other", "abandoned"}
    assert projection_points(old_model, "old-fingerprint")
    assert current.search("beta", [other["version_id"]])
    with pytest.raises(ValueError, match="ALREADY_ACTIVE"):
        current.stage_version([source], "new")


def test_cancelled_batch_is_invisible_and_discard_does_not_delete_ready_or_other_versions(make_index):
    index = make_index()
    source, other = record("alpha " * 100), record("beta")
    activate(index, [source], "active")
    index.stage_version([other], "cancelled")
    progress = []
    checks = 0

    def checkpoint():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("synthetic cancellation")

    with pytest.raises(RuntimeError, match="synthetic cancellation"):
        index.stage_version([source], "cancelled", checkpoint=checkpoint, progress=progress.append)
    partial = [
        p for p in projection_points(index, "cancelled") if p.payload["version_id"] == source["version_id"]
    ]
    assert len(partial) == 3 and progress == [3]
    assert all(not p.payload["ready"] for p in partial)
    assert {h["projection_id"] for h in index.search("alpha", [source["version_id"]])} == {"active"}
    index.discard_projection(source["version_id"], "cancelled")
    index.discard_projection(source["version_id"], "active")
    assert projection_points(index, "active")
    assert {p.payload["version_id"] for p in projection_points(index, "cancelled")} == {other["version_id"]}


def test_failed_embedding_batch_can_be_discarded_or_retried_without_stale_tail(make_index, monkeypatch):
    index = make_index()
    source = record("alpha " * 100)
    original_embed = index.embedding.embed
    batches = 0

    def fail_second(texts, **kwargs):
        nonlocal batches
        batches += 1
        if batches == 2:
            raise ProviderError("SYNTHETIC_FAILURE")
        return original_embed(texts, **kwargs)

    monkeypatch.setattr(index.embedding, "embed", fail_second)
    with pytest.raises(ProviderError, match="SYNTHETIC_FAILURE"):
        index.stage_version([source], "retry")
    assert len(projection_points(index, "retry")) == 3
    monkeypatch.setattr(index.embedding, "embed", original_embed)
    shorter = record(
        "alpha",
        version_id=source["version_id"],
        resource_id=source["resource_id"],
        block_id=source["block_id"],
    )
    first = index.stage_version([shorter], "retry")
    ids = [p.id for p in projection_points(index, "retry")]
    assert first == index.stage_version([shorter], "retry") == {"block_count": 1, "chunk_count": 1}
    assert [p.id for p in projection_points(index, "retry")] == ids
    index.activate_version(source["version_id"], "retry", 1)


def test_cancellation_before_first_batch_never_embeds_or_creates_points(make_index, monkeypatch):
    index = make_index()
    monkeypatch.setattr(index.embedding, "embed", lambda *a, **k: pytest.fail("cancelled before embedding"))

    def cancel():
        raise RuntimeError("cancel")

    with pytest.raises(RuntimeError, match="cancel"):
        index.stage_version([record()], "cancel", checkpoint=cancel)
    assert points(index) == []


def test_empty_records_and_empty_block_projection_can_replace_prior_content(make_index):
    index = make_index()
    assert index.stage_version([], "empty") == {"block_count": 0, "chunk_count": 0}
    assert points(index) == [] and index.test_embeddings == []
    source = record()
    activate(index, [source], "old")
    empty = record("", version_id=source["version_id"], resource_id=source["resource_id"])
    receipt = index.stage_version([empty], "empty")
    assert receipt == {"block_count": 1, "chunk_count": 0}
    index.activate_version(source["version_id"], "empty", 0)
    assert projection_points(index, "old")
    assert index.projection_is_complete(source["version_id"], "empty", 0)
    index.prune_ready_projections(source["version_id"], ["empty"])
    assert points(index) == []


@pytest.mark.parametrize("callback_name", ["checkpoint", "progress"])
def test_callbacks_write_sqlite_without_vector_database_lock_inversion(make_index, tmp_path, callback_name):
    index, peer = make_index(), make_index()
    database = tmp_path / "synthetic-receipts.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE progress (value INTEGER)")
    db_locked, callback_started = threading.Event(), threading.Event()

    def concurrent_database_owner():
        with sqlite3.connect(database, timeout=2) as db:
            db.execute("BEGIN IMMEDIATE")
            db_locked.set()
            assert callback_started.wait(5)
            assert peer.status()["available"]  # Database -> vector lock order.
            db.execute("INSERT INTO progress VALUES (99)")

    def callback(*args):
        callback_started.set()
        # This would wait on the DB while holding the vector lock in the old implementation.
        with sqlite3.connect(database, timeout=2) as db:
            db.execute("INSERT INTO progress VALUES (?)", (args[0] if args else 0,))

    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(concurrent_database_owner)
        assert db_locked.wait(5)
        receipt = index.stage_version([record("alpha " * 15)], "callbacks", **{callback_name: callback})
        owner.result(timeout=5)
    assert receipt["chunk_count"] > 0
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM progress").fetchone()[0] >= 2


def test_building_projection_cannot_be_replaced_or_activated_and_prune_preserves_it(make_index):
    index, peer = make_index(), make_index()
    source = record("alpha " * 40)
    activate(index, [source], "old")
    activate(index, [source], "committed")
    entered, resume = threading.Event(), threading.Event()

    def progress(count):
        if count == 3:
            entered.set()
            assert resume.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        build = pool.submit(index.stage_version, [source], "building", progress=progress)
        try:
            assert entered.wait(5)
            assert not peer.projection_is_complete(source["version_id"], "building", 3)
            with pytest.raises(ValueError, match="BUILD_IN_PROGRESS"):
                peer.stage_version([source], "building")
            with pytest.raises(ValueError, match="BUILD_IN_PROGRESS"):
                peer.activate_version(source["version_id"], "building", 3)
            peer.prune_ready_projections(source["version_id"], ["committed"])
            assert len(projection_points(peer, "building")) == 3
            assert not projection_points(peer, "old")
        finally:
            resume.set()
        result = build.result(timeout=5)
    assert len(projection_points(index, "building")) == result["chunk_count"]
    assert all(not p.payload["ready"] for p in projection_points(index, "building"))


@pytest.mark.parametrize("operation", ["discard", "delete_versions"])
def test_cancelled_inflight_embedding_cannot_resurrect_deleted_points(make_index, monkeypatch, operation):
    index, peer = make_index(), make_index()
    source = record()
    entered, resume = threading.Event(), threading.Event()
    original_embed = index.embedding.embed

    def paused_embed(texts, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original_embed(texts, **kwargs)

    monkeypatch.setattr(index.embedding, "embed", paused_embed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        build = pool.submit(index.stage_version, [source], "cancel")
        try:
            assert entered.wait(5)
            if operation == "discard":
                peer.discard_projection(source["version_id"], "cancel")
            else:
                peer.delete_versions([source["version_id"]])
        finally:
            resume.set()
        with pytest.raises(RuntimeError, match="BUILD_CANCELLED"):
            build.result(timeout=5)
    assert points(index) == []
    assert index._shared.writers == {}


def test_activation_then_receipt_rollback_keeps_old_readable_and_recovery_can_prune(make_index, tmp_path):
    index = make_index()
    source = record("old alpha")
    activate(index, [source], "old")
    changed = record("new beta", version_id=source["version_id"], resource_id=source["resource_id"])
    receipt = index.stage_version([changed], "new")
    database = tmp_path / "receipts.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE receipt (projection TEXT)")
        db.execute("INSERT INTO receipt VALUES ('old')")
    with pytest.raises(RuntimeError, match="synthetic commit failure"), sqlite3.connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        index.activate_version(source["version_id"], "new", receipt["chunk_count"])
        db.execute("UPDATE receipt SET projection='new'")
        raise RuntimeError("synthetic commit failure")
    with sqlite3.connect(database) as db:
        old = db.execute("SELECT projection FROM receipt").fetchone()[0]
    assert old == "old"
    assert index.lexical_search("alpha", [source["version_id"]], allowed_projection_ids=[old])
    assert index.projection_is_complete(source["version_id"], "old", 1)
    assert index.projection_is_complete(source["version_id"], "new", 1)
    with sqlite3.connect(database) as db:
        index.activate_version(source["version_id"], "new", receipt["chunk_count"])
        db.execute("UPDATE receipt SET projection='new'")
    # New transaction: read the now-current committed keep set before cleanup.
    with sqlite3.connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        keep = [db.execute("SELECT projection FROM receipt").fetchone()[0]]
        index.prune_ready_projections(source["version_id"], keep)
    assert not projection_points(index, "old")
    assert index.projection_is_complete(source["version_id"], "new", 1)


def test_failed_prune_can_retry_without_touching_kept_or_staging_points(make_index, monkeypatch):
    index = make_index()
    source = record()
    activate(index, [source], "old")
    activate(index, [source], "keep")
    index.stage_version([source], "staging")
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise OSError("synthetic cleanup failure")

        patch.setattr(index._shared.client, "delete", fail)
        with pytest.raises(OSError, match="cleanup failure"):
            index.prune_ready_projections(source["version_id"], ["keep"])
    assert index.projection_is_complete(source["version_id"], "keep", 1)
    index.prune_ready_projections(source["version_id"], ["keep"])
    assert {p.payload["projection_id"] for p in points(index)} == {"keep", "staging"}


def test_completeness_checks_actual_ready_counts_and_hash_without_writing(make_index, monkeypatch):
    index = make_index()
    source = record("alpha " * 40)
    receipt = index.stage_version([source], "p")
    count = receipt["chunk_count"]
    assert not index.projection_is_complete(source["version_id"], "p", count)
    index.activate_version(source["version_id"], "p", count)
    monkeypatch.setattr(index.embedding, "embed", lambda *a, **k: pytest.fail("read only"))
    assert index.projection_is_complete(source["version_id"], "p", count)
    assert not index.projection_is_complete(source["version_id"], "p", count - 1)
    assert not index.projection_is_complete(source["version_id"], "absent", count)
    child = projection_points(index, "p")[0]
    index._shared.client.set_payload(index.collection, points=[child.id], payload={"chunk_sha256": "0" * 64})
    assert not index.projection_is_complete(source["version_id"], "p", count)
    index._shared.client.delete(index.collection, points_selector=[child.id], wait=True)
    assert not index.projection_is_complete(source["version_id"], "p", count)


def test_frozen_generation_point_payload_and_embedding_input_fingerprints(make_index):
    index = make_index()
    source = record(
        "甲乙🙂 alpha beta。\n\n尾部文本 " * 3,
        resource_id="00000000-0000-0000-0000-000000000001",
        version_id="00000000-0000-0000-0000-000000000002",
        block_id="00000000-0000-0000-0000-000000000003",
        title="固定标题🙂 with tail",
    )
    receipt = index.stage_version([source], "frozen-projection")
    assert receipt["chunk_count"] == 4
    assert index.embedding.fingerprint == "0a8234568656de52d171ce5fbea71b359350dc7c45a21e6437321b76a8910e93"
    stored = [
        {"id": p.id, "payload": p.payload}
        for p in sorted(points(index), key=lambda p: p.payload["chunk_index"])
    ]
    digest = hashlib.sha256(json.dumps(stored, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert digest == "555ed5bbabd7abbfede72af2fe52f9e3e2e46a76e55ebd9e12b34e48ecad8ce1"
    inputs = [text for batch, query in index.test_embeddings if not query for text in batch]
    digest = hashlib.sha256(json.dumps(inputs, ensure_ascii=False).encode()).hexdigest()
    assert digest == "ca6a6d15235a34a5708acf1493c463ab877f2d8371ccc98f64fd190d754e4175"


def test_legacy_upsert_keeps_short_payloads_and_removes_only_replaced_block_tail(make_index):
    index = make_index()
    source = record("alpha " * 100)
    other = record("beta", version_id=source["version_id"], resource_id=source["resource_id"])
    index.upsert([source, other])
    index.upsert([source, other])
    assert len(points(index)) > 2
    shorter = record(
        "alpha",
        version_id=source["version_id"],
        resource_id=source["resource_id"],
        block_id=source["block_id"],
    )
    index.upsert([shorter])
    assert len(points(index)) == 2
    hit = next(
        h for h in index.search("alpha", [source["version_id"]]) if h["block_id"] == source["block_id"]
    )
    assert hit["text"] == shorter["text"] and hit["data"] == shorter["data"] and hit["ready"] is True
    index.upsert(
        [
            record(
                "",
                version_id=source["version_id"],
                resource_id=source["resource_id"],
                block_id=source["block_id"],
            )
        ]
    )
    assert {p.payload["block_id"] for p in points(index)} == {other["block_id"]}


def test_delete_versions_preserves_other_versions_and_unowned_collections(make_index):
    index, old = make_index(), make_index(embedding_dimensions=16)
    source, other = record(), record("beta")
    index.upsert([source, other])
    old.upsert([source, other])
    client = index._shared.client
    client.create_collection(
        "not_owned", vectors_config=models.VectorParams(size=8, distance=models.Distance.COSINE)
    )
    client.upsert("not_owned", points=[models.PointStruct(id=1, vector=[1.0] + [0.0] * 7, payload=source)])
    index.delete_versions([source["version_id"]])
    assert {p.payload["version_id"] for p in points(index)} == {other["version_id"]}
    assert {p.payload["version_id"] for p in points(old)} == {other["version_id"]}
    assert client.count("not_owned", exact=True).count == 1


def test_persisted_projection_and_bm25_survive_reopen(make_index):
    first = make_index()
    source = record("alpha beta")
    activate(first, [source], "persisted")
    first.close()
    second = make_index()
    assert second.search("alpha", [source["version_id"]], allowed_projection_ids=["persisted"])
    assert second.lexical_search("alpha", [source["version_id"]], allowed_projection_ids=["persisted"])


@pytest.mark.parametrize("method", ["search", "lexical_search"])
@pytest.mark.parametrize(
    "versions,projections",
    [([], None), ([], ["p"]), (["not-a-uuid"], []), (["not-a-uuid"], set()), (["not-a-uuid"], ())],
)
def test_empty_acl_does_no_backend_work_or_embedding(make_index, monkeypatch, method, versions, projections):
    index = make_index()
    monkeypatch.setattr(index.embedding, "embed", lambda *a, **k: pytest.fail("must not embed"))
    monkeypatch.setattr(index, "_ensure", lambda *a, **k: pytest.fail("must not query"))
    assert getattr(index, method)("alpha", versions, allowed_projection_ids=projections) == []


@pytest.mark.parametrize("method", ["search", "lexical_search"])
def test_empty_collection_and_blank_query_do_not_embed(make_index, monkeypatch, method):
    index = make_index()
    monkeypatch.setattr(index.embedding, "embed", lambda *a, **k: pytest.fail("must not embed"))
    assert getattr(index, method)("alpha", [str(uuid4())]) == []
    assert getattr(index, method)(" \n", [str(uuid4())]) == []
    assert getattr(index, method)("alpha", [str(uuid4())], limit=0) == []


@pytest.mark.parametrize("method", ["search", "lexical_search"])
def test_versions_and_projections_are_intersected_not_unioned(make_index, method):
    index = make_index()
    first, secret = record("alpha beta"), record("alpha secret")
    activate(index, [first], "shared-name")
    activate(index, [secret], "shared-name")
    # Simulate the legitimate activation window before cleanup: two ready projections.
    index.stage_version([first], "other-projection")
    for p in projection_points(index, "other-projection"):
        index._shared.client.set_payload(index.collection, payload={"ready": True}, points=[p.id])
    call = getattr(index, method)
    hits = call("alpha", [first["version_id"]], allowed_projection_ids=["shared-name"])
    assert len(hits) == 1
    assert hits[0]["version_id"] == first["version_id"] and hits[0]["projection_id"] == "shared-name"
    assert call("alpha", [first["version_id"]], allowed_projection_ids=["absent"]) == []


def test_bare_legacy_points_require_version_acl_and_explicit_projection_queries_exclude_them(make_index):
    index = make_index()
    legacy = record()
    index._ensure(create=True)
    index._shared.client.upsert(
        index.collection,
        points=[
            models.PointStruct(id=1, vector=[1.0] + [0.0] * 7, payload=legacy),
            models.PointStruct(
                id=2, vector=[1.0] + [0.0] * 7, payload={**legacy, "projection_id": "missing-ready"}
            ),
            models.PointStruct(id=3, vector=[1.0] + [0.0] * 7, payload={**legacy, "ready": False}),
        ],
    )
    for method in (index.search, index.lexical_search):
        assert len(method("alpha", [legacy["version_id"]])) == 1
        assert method("alpha", [legacy["version_id"]], allowed_projection_ids=["missing-ready"]) == []


@pytest.mark.parametrize("method", ["search", "lexical_search"])
@pytest.mark.parametrize(
    "tamper,code",
    [
        ({"version_id": str(uuid4())}, "ACL_FILTER"),
        ({"projection_id": "secret"}, "PROJECTION_FILTER"),
        ({"ready": False}, "READINESS_FILTER"),
        ({"chunk_sha256": "0" * 64}, "CHUNK_PAYLOAD"),
    ],
)
def test_backend_filter_or_chunk_integrity_violations_fail_closed(
    make_index, monkeypatch, method, tamper, code
):
    index = make_index()
    source = record()
    activate(index, [source], "allowed")
    forged = SimpleNamespace(payload=points(index)[0].payload | tamper, score=1.0)
    if method == "search":
        monkeypatch.setattr(
            index._shared.client, "query_points", lambda **k: SimpleNamespace(points=[forged])
        )
    else:
        monkeypatch.setattr(index._shared.client, "scroll", lambda *a, **k: ([forged], None))
    with pytest.raises(RuntimeError, match=code):
        getattr(index, method)("alpha", [source["version_id"]], allowed_projection_ids=["allowed"])


def test_activation_checks_child_hash_before_ready_or_old_projection_cleanup(make_index):
    index = make_index()
    source = record()
    activate(index, [source], "old")
    receipt = index.stage_version([source], "corrupt")
    child = projection_points(index, "corrupt")[0]
    index._shared.client.set_payload(index.collection, payload={"chunk_sha256": "0" * 64}, points=[child.id])
    with pytest.raises(RuntimeError, match="CHUNK_PAYLOAD_INVALID"):
        index.activate_version(source["version_id"], "corrupt", receipt["chunk_count"])
    assert all(p.payload["ready"] for p in projection_points(index, "old"))
    assert all(not p.payload["ready"] for p in projection_points(index, "corrupt"))


def bm25_reference(query_terms, documents):
    """Independent source-based oracle, never using retrieval's payload/scoring helpers."""
    lengths = [len(words) for words in documents]
    mean_length = sum(lengths) / len(lengths) or 1
    frequencies = [Counter(words) for words in documents]
    scores = []
    for length, frequencies_here in zip(lengths, frequencies, strict=True):
        value = 0.0
        for term in set(query_terms):
            count = frequencies_here[term]
            if count:
                document_frequency = sum(term in bag for bag in frequencies)
                inverse_frequency = math.log((len(documents) + 1) / (document_frequency + 0.5))
                denominator = count + 1.2 * (1 - 0.75 + 0.75 * length / mean_length)
                value += inverse_frequency * count * (1.2 + 1) / denominator
        scores.append(value)
    return scores


def test_bm25_matches_independent_full_authorized_corpus_without_phrase_bonuses(make_index, monkeypatch):
    monkeypatch.setattr(retrieval, "tokenize", str.split)
    index = make_index()
    docs = [record("alpha alpha beta"), record("alpha gamma"), record("delta delta delta delta")]
    secret = record("alpha beta " * 10)
    index.upsert([*docs, secret])
    allowed = [r["version_id"] for r in docs]
    hits = index.lexical_search("alpha beta", allowed, cache_key="same-external-key")
    scores = bm25_reference(["alpha", "beta"], [r["text"].split() for r in docs])
    expected = {r["block_id"]: score for r, score in zip(docs, scores, strict=True) if score > 0}
    assert {h["block_id"]: h["score"] for h in hits} == pytest.approx(expected)
    assert all(h["lexical_method"] == "bm25" and h["candidate_only"] for h in hits)
    assert index.lexical_search("absent", allowed) == []
    assert (
        index.lexical_search("beta", [secret["version_id"]], cache_key="same-external-key")[0]["version_id"]
        == secret["version_id"]
    )


def test_bm25_scans_every_authorized_page_before_applying_result_limit(make_index, monkeypatch):
    monkeypatch.setattr(retrieval, "tokenize", str.split)
    index = make_index(embedding_batch_size=32)
    docs = [record("neutral " * (i % 4 + 1)) for i in range(300)] + [record("needle needle neutral")]
    index.upsert(docs)
    allowed = [r["version_id"] for r in docs]
    hit = index.lexical_search("needle", allowed, limit=1)[0]
    expected = bm25_reference(["needle"], [r["text"].split() for r in docs])[-1]
    assert hit["score"] == pytest.approx(expected)
    assert hit["block_id"] == docs[-1]["block_id"]


def test_bm25_uses_persisted_frequencies_and_finds_late_text_and_untrimmed_title(make_index, monkeypatch):
    monkeypatch.setattr(retrieval, "tokenize", str.split)
    index = make_index(embedding_chunk_bytes=448, embedding_batch_size=32)
    source = record("neutral " * 4000 + "needle", title="heading " * 100 + "title_tail")
    activate(index, [source], "long")
    seen = []

    def query_only(text):
        seen.append(text)
        assert text in {"needle", "title_tail"}, "must not tokenize stored source at query time"
        return text.split()

    monkeypatch.setattr(retrieval, "tokenize", query_only)
    hits = index.lexical_search("needle", [source["version_id"]], allowed_projection_ids=["long"])
    assert hits and hits[0]["chunk_end"] == len(source["text"])
    assert hits[0]["content_sha256"] == source["content_sha256"]
    assert index.lexical_search("title_tail", [source["version_id"]], allowed_projection_ids=["long"])
    assert seen == ["needle", "title_tail"]


def test_bm25_statistics_change_after_writes_activation_discard_and_delete_without_cache_leaks(
    make_index, monkeypatch
):
    monkeypatch.setattr(retrieval, "tokenize", str.split)
    index = make_index()
    first, second = record("alpha"), record("beta")
    activate(index, [first], "first")
    allowed = [first["version_id"], second["version_id"]]
    score_one = index.lexical_search("alpha", allowed, cache_key="reused")[0]["score"]
    staged = index.stage_version([second], "second")
    assert index.lexical_search("alpha", allowed, cache_key="reused")[0]["score"] == score_one
    index.discard_projection(second["version_id"], "second")
    assert index.lexical_search("beta", allowed, cache_key="reused") == []
    index.stage_version([second], "second")
    index.activate_version(second["version_id"], "second", staged["chunk_count"])
    assert index.lexical_search("alpha", allowed, cache_key="reused")[0]["score"] > score_one
    # A projection-restricted caller's corpus must not include the other ready projection.
    assert (
        index.lexical_search("alpha", allowed, allowed_projection_ids=["first"], cache_key="reused")[0][
            "score"
        ]
        == score_one
    )
    index.delete_versions([second["version_id"]])
    assert index.lexical_search("alpha", allowed, cache_key="reused")[0]["score"] == score_one


@pytest.mark.parametrize("invalid", [-1, True, 1.5])
def test_corrupt_term_frequency_payload_fails_closed(make_index, invalid):
    index = make_index()
    source = record()
    index.upsert([source])
    child = points(index)[0]
    index._shared.client.set_payload(
        index.collection, points=[child.id], payload={"bm25_tf": {"alpha": invalid}, "bm25_length": invalid}
    )
    with pytest.raises(RuntimeError, match="LEXICAL_PAYLOAD_INVALID"):
        index.lexical_search("alpha", [source["version_id"]])


def test_bad_parent_hash_duplicate_blocks_and_mixed_versions_fail_before_embedding(make_index, monkeypatch):
    index = make_index()
    source = record()
    bad = source | {"content_sha256": "f" * 64}
    monkeypatch.setattr(index.embedding, "embed", lambda *a, **k: pytest.fail("validation must come first"))
    for records, code in [
        ([bad], "CONTENT_HASH_MISMATCH"),
        ([source, source], "DUPLICATE_PROJECTION_BLOCK"),
        ([source, record()], "MUST_SHARE_VERSION"),
    ]:
        with pytest.raises(ValueError, match=code):
            index.stage_version(records, "invalid")
    assert points(index) == []


@pytest.mark.parametrize("value", [-1, True, 1.5, "1"])
def test_invalid_expected_count_cannot_activate(make_index, value):
    index = make_index()
    with pytest.raises(ValueError, match="INVALID_EXPECTED_CHUNKS"):
        index.activate_version(str(uuid4()), "p", value)


@pytest.mark.parametrize("value", [None, "", " \n", False])
def test_invalid_projection_id_cannot_write(make_index, value):
    index = make_index()
    with pytest.raises(ValueError, match="INVALID_PROJECTION_ID"):
        index.stage_version([record()], value)


@pytest.mark.parametrize(
    "vectors,code",
    [
        ([], "COUNT_MISMATCH"),
        ([[1.0] * 7], "DIMENSION_MISMATCH"),
        ([[True] + [0.0] * 7], "INVALID_VECTOR"),
        ([[float("nan")] + [0.0] * 7], "INVALID_VECTOR"),
        ([[float("inf")] + [0.0] * 7], "INVALID_VECTOR"),
        ([[0.0] * 8], "EMPTY_TEXT"),
        ([None], "INVALID_VECTOR"),
        ([["1"] + [0.0] * 7], "INVALID_VECTOR"),
    ],
)
def test_invalid_embedding_outputs_never_create_ready_points(make_index, monkeypatch, vectors, code):
    index = make_index()
    monkeypatch.setattr(index.embedding, "embed", lambda *a, **k: vectors)
    with pytest.raises(ProviderError, match=code):
        index.stage_version([record()], "invalid-vectors")
    assert points(index) == []


def test_existing_wrong_dimension_collection_is_rejected(make_index):
    index = make_index()
    index._shared.client.create_collection(
        index.collection, vectors_config=models.VectorParams(size=16, distance=models.Distance.COSINE)
    )
    with pytest.raises(RuntimeError, match="COLLECTION_CONFIG_MISMATCH"):
        index.stage_version([record()], "mismatch")


@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_chunk_bytes", 96),
        ("embedding_overlap_bytes", 4),
        ("embedding_title_bytes", 8),
        ("embedding_dimensions", 16),
        ("embedding_revision", "revision-2"),
    ],
)
def test_projection_affecting_settings_change_fingerprint(tmp_path, field, value):
    assert (
        EmbeddingProvider(settings(tmp_path)).fingerprint
        != EmbeddingProvider(settings(tmp_path, **{field: value})).fingerprint
    )


def test_projection_schema_changes_fingerprint_and_runtime_tuning_does_not(tmp_path, monkeypatch):
    base = EmbeddingProvider(settings(tmp_path)).fingerprint
    assert (
        base
        == EmbeddingProvider(settings(tmp_path, embedding_batch_size=32, embedding_threads=4)).fingerprint
    )
    monkeypatch.setattr(retrieval, "PROJECTION_SCHEMA", "synthetic-next-schema")
    assert base != EmbeddingProvider(settings(tmp_path)).fingerprint


def test_getattr_configuration_defaults_do_not_require_new_settings_fields(tmp_path):
    provider = EmbeddingProvider(SimpleNamespace(app_env="development"))
    assert (
        provider.chunk_bytes,
        provider.overlap_bytes,
        provider.title_bytes,
        provider.batch_size,
        provider.threads,
    ) == (448, 64, 96, 32, 4)


@pytest.mark.parametrize(
    "changes",
    [
        {"embedding_chunk_bytes": 0},
        {"embedding_overlap_bytes": -1},
        {"embedding_title_bytes": 64},
        {"embedding_overlap_bytes": 48},
        {"embedding_batch_size": 0},
        {"embedding_threads": True},
        {"embedding_chunk_bytes": 64.0},
    ],
)
def test_invalid_chunk_or_runtime_settings_are_explicit_errors(tmp_path, changes):
    with pytest.raises(ValueError, match="INVALID_EMBEDDING"):
        EmbeddingProvider(settings(tmp_path, **changes))


def fake_fastembed(monkeypatch, *, dimension=512, overflow=False):
    observed = {"constructors": [], "operations": []}

    class LocalModel:
        def __init__(self, **kwargs):
            observed["constructors"].append(kwargs)
            self.model = SimpleNamespace(
                tokenizer=SimpleNamespace(
                    encode_batch=lambda texts: [
                        SimpleNamespace(overflowing=["overflow"] if overflow else []) for _ in texts
                    ]
                )
            )

        def passage_embed(self, texts, **kwargs):
            observed["operations"].append(("passage", list(texts), kwargs))
            return [[3.0, 4.0] + [0.0] * (dimension - 2) for _ in texts]

        def query_embed(self, texts, **kwargs):
            observed["operations"].append(("query", list(texts), kwargs))
            return [[1.0] + [0.0] * (dimension - 1) for _ in texts]

    monkeypatch.setitem(
        sys.modules,
        "fastembed",
        SimpleNamespace(TextEmbedding=LocalModel, __spec__=ModuleSpec("fastembed", loader=None)),
    )
    return observed


def test_explicit_local_model_path_works_without_hf_cache_and_stays_private(tmp_path, monkeypatch):
    model_dir = tmp_path / "synthetic-pinned-model"
    model_dir.mkdir()
    observed = fake_fastembed(monkeypatch)
    config = settings(
        tmp_path,
        embedding_mode="fastembed",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_dimensions=512,
        embedding_model_path=model_dir,
        embedding_cache_dir=None,
        embedding_allow_downloads=True,
        embedding_batch_size=32,
        embedding_threads=4,
    )
    provider = EmbeddingProvider(config)
    assert provider.embed(["中文🙂", "English"])[0][:2] == pytest.approx([0.6, 0.8])
    assert provider.embed(["查询"], query=True)[0][0] == 1
    assert len(observed["constructors"]) == 1
    constructor = observed["constructors"][0]
    assert constructor["specific_model_path"] == str(model_dir) and constructor["cache_dir"] is None
    assert constructor["local_files_only"] is True and constructor["threads"] == 4
    assert [op[0] for op in observed["operations"]] == ["passage", "query"]
    assert all(op[2] == {"batch_size": 32, "parallel": None} for op in observed["operations"])
    index = VectorIndex(config)
    try:
        status = index.status()
        assert status["embedding_status"] == "EXPLICIT_LOCAL_FILES_NOT_VERIFIED"
        assert str(model_dir) not in json.dumps(status)
        assert "embedding_model_path" not in status
    finally:
        index.close()


def test_fastembed_cache_only_configuration_remains_compatible(tmp_path, monkeypatch):
    observed = fake_fastembed(monkeypatch, dimension=8)
    provider = EmbeddingProvider(settings(tmp_path, embedding_mode="fastembed", embedding_cache_dir=tmp_path))
    provider.embed(["alpha"])
    assert observed["constructors"][0]["specific_model_path"] is None
    assert observed["constructors"][0]["cache_dir"] == str(tmp_path)
    assert observed["constructors"][0]["local_files_only"] is True


def test_missing_explicit_model_path_does_not_fall_back_to_cache_or_download(tmp_path, monkeypatch):
    observed = fake_fastembed(monkeypatch, dimension=8)
    provider = EmbeddingProvider(
        settings(
            tmp_path,
            embedding_mode="fastembed",
            embedding_cache_dir=tmp_path,
            embedding_model_path=tmp_path / "missing",
            embedding_allow_downloads=True,
        )
    )
    with pytest.raises(ProviderError, match="LOCAL_MODEL_UNAVAILABLE"):
        provider.embed(["alpha"])
    assert observed["constructors"] == []


def test_fastembed_rejects_oversized_input_before_model_loading(tmp_path, monkeypatch):
    observed = fake_fastembed(monkeypatch, dimension=8)
    provider = EmbeddingProvider(settings(tmp_path, embedding_mode="fastembed", embedding_cache_dir=tmp_path))
    with pytest.raises(ProviderError, match="INPUT_TOO_LONG"):
        provider.embed(
            ["中" * 22]
        )  # 66 UTF-8 bytes > configured 64, even though there are only 22 characters.
    assert observed["constructors"] == []


def test_fastembed_detects_tokenizer_truncation_before_inference(tmp_path, monkeypatch):
    observed = fake_fastembed(monkeypatch, dimension=8, overflow=True)
    provider = EmbeddingProvider(settings(tmp_path, embedding_mode="fastembed", embedding_cache_dir=tmp_path))
    with pytest.raises(ProviderError, match="INPUT_TRUNCATED"):
        provider.embed(["synthetic overflow"])
    assert observed["operations"] == []


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"embedding_chunk_bytes": 512}, "UNSAFE_LOCAL_INPUT_BUDGET"),
        (
            {"embedding_model": "BAAI/bge-small-zh-v1.5", "embedding_dimensions": 384},
            "MODEL_DIMENSION_MISMATCH",
        ),
    ],
)
def test_fastembed_512_token_budget_and_chinese_model_dimension_fail_closed(tmp_path, changes, code):
    with pytest.raises(ValueError, match=code):
        EmbeddingProvider(settings(tmp_path, embedding_mode="fastembed", **changes))


@pytest.mark.parametrize("magnitude", [1e308, 1e-300])
def test_output_normalization_handles_finite_extreme_magnitudes_without_overflow(
    tmp_path, monkeypatch, magnitude
):
    provider = EmbeddingProvider(
        settings(tmp_path, embedding_mode="http", embedding_base_url="https://example.invalid/v1")
    )
    monkeypatch.setattr(
        retrieval, "post_json", lambda *a, **k: {"data": [{"index": 0, "embedding": [magnitude] * 8}]}
    )
    vector = provider.embed(["synthetic"])[0]
    assert vector == pytest.approx([1 / math.sqrt(8)] * 8)


def test_http_request_contract_and_credential_forwarding_remain_unchanged(tmp_path, monkeypatch):
    sentinel = object()  # Synthetic identity, never a real credential.
    observed = []

    def respond(*args):
        observed.append(args)
        return {"data": [{"index": 0, "embedding": [1.0] + [0.0] * 7}]}

    monkeypatch.setattr(retrieval, "post_json", respond)
    provider = EmbeddingProvider(
        settings(
            tmp_path,
            embedding_mode="http",
            embedding_model="synthetic-http",
            embedding_base_url="https://example.invalid/v1",
            embedding_api_key=sentinel,
            embedding_timeout_seconds=7,
        )
    )
    text = "synthetic " * 100
    provider.embed([text])
    assert observed == [
        (
            "https://example.invalid/v1",
            "embeddings",
            {"model": "synthetic-http", "input": [text]},
            sentinel,
            7,
        )
    ]


def count_reads(monkeypatch, index):
    calls = []
    for method in ("scroll", "facet", "count", "retrieve"):
        original = getattr(index._shared.client, method)

        def observe(*args, _method=method, _original=original, **kwargs):
            calls.append((_method, kwargs))
            return _original(*args, **kwargs)

        monkeypatch.setattr(index._shared.client, method, observe)
    return calls


def test_batch_completeness_large_requirement_list_is_pair_scoped_and_batched(isolated_backend, monkeypatch):
    index = isolated_backend
    good, missing = record("alpha"), record("not indexed")
    activate(index, [good], "good")
    if index.mode == "remote":
        schema = index._shared.client.get_collection(index.collection).payload_schema
        assert {
            key: schema[key].data_type
            for key in ("version_id", "projection_id", "projection_schema", "embedding_fingerprint", "ready")
        } == {
            "version_id": models.PayloadSchemaType.KEYWORD,
            "projection_id": models.PayloadSchemaType.KEYWORD,
            "projection_schema": models.PayloadSchemaType.KEYWORD,
            "embedding_fingerprint": models.PayloadSchemaType.KEYWORD,
            "ready": models.PayloadSchemaType.BOOL,
        }
    # Both projection and version are allowed separately, but this precise pair is not.
    activate(index, [good], "cross-pair")
    pending = record("pending")
    index.stage_version([pending], "pending")
    requirements = [
        {"version_id": good["version_id"], "projection_id": "good", "chunk_count": 1},
        {"version_id": missing["version_id"], "projection_id": "cross-pair", "chunk_count": 1},
        {"version_id": pending["version_id"], "projection_id": "pending", "chunk_count": 1},
    ]
    requirements += [
        {"version_id": str(uuid4()), "projection_id": f"absent-{i}", "chunk_count": 1} for i in range(1010)
    ]
    calls = count_reads(monkeypatch, index)
    assert index.complete_projection_ids(requirements) == {"good"}
    # With only four points, metadata/facet reads must be batched, not 1,013 separate counts.
    assert len(calls) < 10
    for method, kwargs in calls:
        if method == "scroll":
            assert set(kwargs["with_payload"]) <= {
                "version_id",
                "projection_id",
                "ready",
                "projection_schema",
                "embedding_fingerprint",
            }
            assert kwargs["with_vectors"] is False


def test_batch_completeness_reused_projection_ids_and_invalid_generations_are_not_promoted(make_index):
    index = make_index()
    first, second, corrupt = record(), record(), record()
    activate(index, [first], "reused")
    activate(index, [second], "reused")
    activate(index, [corrupt], "corrupt")
    bad = projection_points(index, "corrupt")[0]
    index._shared.client.set_payload(
        index.collection, points=[bad.id], payload={"embedding_fingerprint": "wrong"}
    )
    required = [
        {"version_id": first["version_id"], "projection_id": "reused", "chunk_count": 1},
        {"version_id": second["version_id"], "projection_id": "reused", "chunk_count": 2},
        {"version_id": corrupt["version_id"], "projection_id": "corrupt", "chunk_count": 1},
    ]
    assert index.complete_projection_ids(required) == set()
    required[1]["chunk_count"] = 1
    assert index.complete_projection_ids(required) == {"reused"}


def test_batch_completeness_empty_and_missing_points_do_not_trust_collection_existence(
    make_index, monkeypatch
):
    index = make_index()
    with monkeypatch.context() as patch:
        patch.setattr(index, "_ensure", lambda *a, **k: pytest.fail("empty requirements must not query"))
        assert index.complete_projection_ids([]) == set()
    source = record()
    activate(index, [source], "ready")
    request = {"version_id": source["version_id"], "projection_id": "ready", "chunk_count": 1}
    assert index.complete_projection_ids([request]) == {"ready"}
    index._shared.client.delete(index.collection, points_selector=[points(index)[0].id], wait=True)
    assert index.complete_projection_ids([request]) == set()
    assert not index.projection_is_complete(source["version_id"], "ready", 1)


@pytest.mark.parametrize("count", [-1, True, 1.5])
def test_completeness_invalid_counts_raise_without_promoting(make_index, count):
    index = make_index()
    version = str(uuid4())
    with pytest.raises(ValueError, match="INVALID_EXPECTED_CHUNKS"):
        index.complete_projection_ids([{"version_id": version, "projection_id": "p", "chunk_count": count}])
    with pytest.raises(ValueError, match="INVALID_EXPECTED_CHUNKS"):
        index.projection_is_complete(version, "p", count)


def test_cached_bm25_reuses_scope_without_caching_bodies_and_isolates_acl_user_and_projection(
    isolated_backend, monkeypatch
):
    monkeypatch.setattr(retrieval, "tokenize", str.split)
    index = isolated_backend
    first, second, secret = record("alpha alpha beta"), record("alpha gamma"), record("alpha secret")
    for source, projection in ((first, "first"), (second, "second"), (secret, "secret")):
        activate(index, [source], projection)
    allowed = [first["version_id"], second["version_id"]]
    calls = count_reads(monkeypatch, index)

    def search(user="user-a", versions=allowed, projections=("first", "second")):
        return index.lexical_search("alpha", versions, allowed_projection_ids=projections, cache_key=user)

    initial = search()
    scans = len([c for c in calls if c[0] == "scroll"])
    assert scans > 0
    expected = bm25_reference(["alpha"], [first["text"].split(), second["text"].split()])
    assert {h["block_id"]: h["score"] for h in initial} == pytest.approx(
        {first["block_id"]: expected[0], second["block_id"]: expected[1]}
    )
    initial[0]["text"] = "caller mutation"
    initial[0]["bm25_tf"]["alpha"] = 999
    repeated = search(versions=list(reversed(allowed)), projections=("second", "first"))
    assert len([c for c in calls if c[0] == "scroll"]) == scans
    assert all(h["text"] != "caller mutation" and h["bm25_tf"]["alpha"] != 999 for h in repeated)
    assert secret["version_id"] not in {h["version_id"] for h in repeated}
    search(user="user-b")
    assert len([c for c in calls if c[0] == "scroll"]) > scans
    restricted = search(versions=[first["version_id"]])
    assert len(restricted) == 1 and restricted[0]["score"] == pytest.approx(math.log(4 / 3) * 4.4 / 3.2)
    restricted_projection = search(projections=("first",))
    assert restricted_projection[0]["score"] == pytest.approx(restricted[0]["score"])
    for method, kwargs in calls:
        if method == "scroll":
            assert kwargs["limit"] >= 4096
            assert isinstance(kwargs["with_payload"], list)
            assert not {"text", "data", "locator", "title"}.intersection(kwargs["with_payload"])


@pytest.mark.parametrize("operation", ["stage", "activate", "discard", "prune", "upsert", "delete_versions"])
def test_cached_scope_invalidates_after_every_shared_index_mutation(make_index, monkeypatch, operation):
    index, writer = make_index(), make_index()
    source, other = record("alpha"), record("beta")
    activate(index, [source], "allowed")
    activate(writer, [other], "old")
    staged = writer.stage_version([other], "pending")
    calls = count_reads(monkeypatch, index)

    def search():
        return index.lexical_search(
            "alpha", [source["version_id"]], allowed_projection_ids=["allowed"], cache_key="same-user"
        )

    assert search()
    count = len([c for c in calls if c[0] == "scroll"])
    if operation == "stage":
        writer.stage_version([other], "new-staging")
    elif operation == "activate":
        writer.activate_version(other["version_id"], "pending", staged["chunk_count"])
    elif operation == "discard":
        writer.discard_projection(other["version_id"], "pending")
    elif operation == "prune":
        writer.prune_ready_projections(other["version_id"], [])
    elif operation == "upsert":
        writer.upsert([other])
    else:
        writer.delete_versions([other["version_id"]])
    # Count only this follow-up query, excluding reads required by the mutation itself.
    scans_before = len([c for c in calls if c[0] == "scroll"])
    assert scans_before >= count
    assert search()
    assert len([c for c in calls if c[0] == "scroll"]) > scans_before


def test_native_clients_share_invalidation_even_when_their_cache_namespaces_are_separate(
    isolated_backend, make_index, monkeypatch
):
    reader = isolated_backend
    changes = {"qdrant_url": reader.settings.qdrant_url} if reader.mode == "remote" else {}
    writer = make_index(**changes)
    source = record("alpha")
    activate(reader, [source], "allowed")
    calls = count_reads(monkeypatch, reader)
    kwargs = {"allowed_projection_ids": ["allowed"], "cache_key": "same-user"}
    reader.lexical_search("alpha", [source["version_id"]], **kwargs)
    writer.upsert([record("outside scope")])
    before = len([c for c in calls if c[0] == "scroll"])
    reader.lexical_search("alpha", [source["version_id"]], **kwargs)
    assert len([c for c in calls if c[0] == "scroll"]) > before


def test_bm25_cache_ttl_reloads_out_of_band_term_changes(make_index, monkeypatch):
    monkeypatch.setattr(retrieval, "tokenize", str.split)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(retrieval, "time", SimpleNamespace(monotonic=lambda: clock.now))
    index = make_index()
    source = record("alpha")
    activate(index, [source], "p")
    kwargs = {"allowed_projection_ids": ["p"], "cache_key": "user"}
    assert index.lexical_search("alpha", [source["version_id"]], **kwargs)
    child = points(index)[0]
    sha = hashlib.sha256(b"beta").hexdigest()
    index._shared.client.set_payload(
        index.collection,
        points=[child.id],
        payload={
            "text": "beta",
            "content_sha256": sha,
            "parent_content_sha256": sha,
            "chunk_sha256": sha,
            "chunk_end": 4,
            "end": 4,
            "bm25_tf": {"beta": 1},
            "bm25_length": 1,
        },
    )
    assert index.lexical_search("beta", [source["version_id"]], **kwargs) == []
    clock.now += 59
    assert index.lexical_search("beta", [source["version_id"]], **kwargs) == []
    clock.now += 2  # The explicitly adopted 60-second TTL must now have expired.
    assert index.lexical_search("beta", [source["version_id"]], **kwargs)[0]["text"] == "beta"


def test_bm25_cache_is_lru_bounded_and_oversize_opt_out_does_not_truncate(make_index, monkeypatch):
    monkeypatch.setattr(retrieval, "_BM25_CACHE_MAX_ENTRIES", 2)
    index = make_index()
    source = record("alpha")
    activate(index, [source], "p")
    calls = count_reads(monkeypatch, index)

    def search(key):
        return index.lexical_search(
            "alpha", [source["version_id"]], allowed_projection_ids=["p"], cache_key=key
        )

    for key in ("user-1", "user-2", "user-1", "user-3"):
        assert search(key)
    before = len([c for c in calls if c[0] == "scroll"])
    assert search("user-1")
    assert len([c for c in calls if c[0] == "scroll"]) == before
    assert search("user-2")
    assert len([c for c in calls if c[0] == "scroll"]) > before
    monkeypatch.setattr(retrieval, "_BM25_CACHE_MAX_BYTES", 1)
    first = search("oversize")
    before = len([c for c in calls if c[0] == "scroll"])
    assert search("oversize") == first
    assert len([c for c in calls if c[0] == "scroll"]) > before


@pytest.mark.parametrize("unicode_prefix", [False, True])
def test_long_query_keeps_tail_and_matches_independent_byte_weighted_pooling(
    isolated_backend, monkeypatch, unicode_prefix
):
    index = isolated_backend
    budget = index.embedding.chunk_bytes
    if unicode_prefix:
        one_chunk = "中" * ((budget - 1) // 3)
        one_chunk += "a" * (budget - len(one_chunk.encode()))
    else:
        one_chunk = "a" * budget
    prefix_bytes = len(one_chunk.encode()) * 8
    query = one_chunk * 8 + "needle"
    assert len(query.encode()) > 448
    expected = [float(prefix_bytes), 6.0] + [0.0] * (index.embedding.dimension - 2)
    norm = math.sqrt(sum(v * v for v in expected))
    expected = [v / norm for v in expected]
    target, prefix = record("tail-sensitive-target"), record("prefix-only")
    seen = []

    def embed(texts, *, query=False):
        if query:
            assert len(texts) <= index.embedding.batch_size
            assert all(len(t.encode()) <= budget for t in texts)
            seen.extend(texts)
            return [
                [0.0, 1.0] + [0.0] * (index.embedding.dimension - 2)
                if "needle" in t
                else [1.0] + [0.0] * (index.embedding.dimension - 1)
                for t in texts
            ]
        return [
            expected if text == target["text"] else [1.0] + [0.0] * (index.embedding.dimension - 1)
            for text in texts
        ]

    monkeypatch.setattr(index.embedding, "embed", embed)
    index.upsert([target, prefix])
    original_query = index._shared.client.query_points
    query_vectors = []

    def capture(**kwargs):
        query_vectors.append(kwargs["query"])
        return original_query(**kwargs)

    monkeypatch.setattr(index._shared.client, "query_points", capture)
    fingerprint = index.embedding.fingerprint
    hits = index.search(query, [target["version_id"], prefix["version_id"]])
    assert "".join(seen) == query and seen[-1] == "needle"
    assert query_vectors[-1] == pytest.approx(expected)
    assert hits[0]["block_id"] == target["block_id"]
    seen.clear()
    hits = index.search("short", [target["version_id"], prefix["version_id"]])
    assert seen == ["short"] and hits[0]["block_id"] == prefix["block_id"]
    assert index.embedding.fingerprint == fingerprint


def test_old_cancelled_writer_cannot_remove_a_replacement_writers_reservation(make_index, monkeypatch):
    old, replacement = make_index(), make_index()
    source = record("alpha " * 40)
    old_entered, old_resume = threading.Event(), threading.Event()
    new_entered, new_resume = threading.Event(), threading.Event()
    old_embed = old.embedding.embed

    def pause_old(texts, **kwargs):
        old_entered.set()
        assert old_resume.wait(5)
        return old_embed(texts, **kwargs)

    def pause_new(count):
        if count == 3:
            new_entered.set()
            assert new_resume.wait(5)

    monkeypatch.setattr(old.embedding, "embed", pause_old)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(old.stage_version, [source], "same-id")
        try:
            assert old_entered.wait(5)
            replacement.discard_projection(source["version_id"], "same-id")
            second = pool.submit(replacement.stage_version, [source], "same-id", progress=pause_new)
            assert new_entered.wait(5)
            old_resume.set()
            with pytest.raises(RuntimeError, match="BUILD_CANCELLED"):
                first.result(timeout=5)
            with pytest.raises(ValueError, match="BUILD_IN_PROGRESS"):
                replacement.activate_version(source["version_id"], "same-id", 3)
        finally:
            old_resume.set()
            new_resume.set()
        result = second.result(timeout=5)
    replacement.activate_version(source["version_id"], "same-id", result["chunk_count"])
    assert replacement.projection_is_complete(source["version_id"], "same-id", result["chunk_count"])


def test_uncertain_batch_write_invalidates_cache_and_releases_writer_for_retry(make_index, monkeypatch):
    index = make_index()
    source = record("alpha")
    activate(index, [source], "allowed")
    kwargs = {"allowed_projection_ids": ["allowed"], "cache_key": "reader"}
    calls = count_reads(monkeypatch, index)
    index.lexical_search("alpha", [source["version_id"]], **kwargs)
    original_upsert = index._shared.client.upsert
    other = record("beta")
    with monkeypatch.context() as patch:

        def uncertain(*args, **values):
            original_upsert(*args, **values)
            raise OSError("synthetic acknowledgement lost")

        patch.setattr(index._shared.client, "upsert", uncertain)
        with pytest.raises(OSError, match="acknowledgement lost"):
            index.stage_version([other], "retry")
    before = len([c for c in calls if c[0] == "scroll"])
    assert index.lexical_search("alpha", [source["version_id"]], **kwargs)
    assert len([c for c in calls if c[0] == "scroll"]) > before
    result = index.stage_version([other], "retry")
    assert result["chunk_count"] == 1
    assert not index.projection_is_complete(other["version_id"], "retry", 1)
    index.activate_version(other["version_id"], "retry", 1)
    assert index.projection_is_complete(other["version_id"], "retry", 1)


def test_prepare_collection_repairs_existing_indexes_without_changing_points(isolated_backend, monkeypatch):
    index = isolated_backend
    client = index._shared.client
    client.create_collection(
        index.collection,
        vectors_config=models.VectorParams(size=index.embedding.dimension, distance=models.Distance.COSINE),
    )
    source = record("synthetic legacy point")
    client.upsert(
        index.collection,
        points=[
            models.PointStruct(id=1, vector=[1.0] + [0.0] * (index.embedding.dimension - 1), payload=source)
        ],
        wait=True,
    )
    before = client.retrieve(index.collection, ids=[1], with_payload=True, with_vectors=True)
    index.prepare_collection()
    if index.mode == "remote":
        schema = client.get_collection(index.collection).payload_schema
        expected = {
            name: models.PayloadSchemaType.KEYWORD
            for name in ("version_id", "projection_id", "projection_schema", "embedding_fingerprint")
        }
        expected["ready"] = models.PayloadSchemaType.BOOL
        assert {name: schema[name].data_type for name in expected} == expected
    monkeypatch.setattr(client, "create_payload_index", lambda *a, **k: pytest.fail("indexes already exist"))
    index.prepare_collection()
    after = client.retrieve(index.collection, ids=[1], with_payload=True, with_vectors=True)
    assert after == before


def test_dense_embedding_does_not_block_concurrent_bm25(make_index, monkeypatch):
    index = make_index()
    source = record("alpha")
    activate(index, [source], "p")
    entered, resume = threading.Event(), threading.Event()
    original = index.embedding.embed

    def paused_query(texts, *, query=False):
        if query:
            entered.set()
            assert resume.wait(5)
        return original(texts, query=query)

    monkeypatch.setattr(index.embedding, "embed", paused_query)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dense = pool.submit(index.search, "alpha", [source["version_id"]], allowed_projection_ids=["p"])
        try:
            assert entered.wait(5)
            lexical = pool.submit(
                index.lexical_search,
                "alpha",
                [source["version_id"]],
                allowed_projection_ids=["p"],
                cache_key="reader",
            )
            assert lexical.result(timeout=2)[0]["block_id"] == source["block_id"]
        finally:
            resume.set()
        assert dense.result(timeout=5)[0]["block_id"] == source["block_id"]
