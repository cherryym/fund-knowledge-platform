"""Independent boundary regressions: temporary WAL DB, synthetic vector/model IO."""
from __future__ import annotations

import copy
import socket
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import event, insert, select, update

from fund_kb import models as m
from fund_kb import providers, retrieval
from fund_kb import services as svc
from fund_kb.db import Base, build_engine, make_session_factory
from fund_kb.hybrid_retrieval import _candidate_blocks
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.universal_retrieval import search_many_catalog
from fund_kb.vector_indexing import receipt_name
from fund_kb.wiki_catalog import build_catalog


@pytest.fixture(autouse=True)
def forbid_network_and_real_models(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("BOUNDARY_TEST_MUST_NOT_USE_NETWORK_OR_REAL_MODELS")

    for target, name in ((socket.socket, "connect"), (socket, "create_connection"),
            (socket, "getaddrinfo"), (providers, "complete"),
            (retrieval.EmbeddingProvider, "embed"), (retrieval.VectorIndex, "__init__")):
        monkeypatch.setattr(target, name, forbidden)


@pytest.fixture
def wal_env(tmp_path):
    path = tmp_path / "batch-boundaries.sqlite"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer_engine = build_engine("sqlite:///" + str(path))
    reader_engine = build_engine("sqlite:///" + str(path), read_only=True)
    try:
        Base.metadata.create_all(writer_engine)
        writer = make_session_factory(writer_engine)
        reader = make_session_factory(reader_engine, read_only=True)
        actor, space = svc.uid(), svc.uid()
        with writer.begin() as db:
            db.add(m.Space(id=space, name="合成边界空间"))
            db.add(m.User(id=actor, external_subject="synthetic:" + actor, display_name="合成测试用户"))
            db.flush()
            db.add_all(m.SpaceMember(space_id=space, user_id=actor, role=role)
                for role in ("reader", "editor", "reviewer", "publisher"))
        with reader() as db:
            connection = db.connection()
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA read_uncommitted").scalar_one() == 0
        assert reader_engine.pool is not writer_engine.pool
        yield SimpleNamespace(writer=writer, reader=reader, writer_engine=writer_engine,
            reader_engine=reader_engine, actor=actor, space=space)
    finally:
        reader_engine.dispose()
        writer_engine.dispose()


def _source(env, title, texts):
    rid, vid, blob = svc.uid(), svc.uid(), svc.uid()
    records = []
    with env.writer.begin() as db:
        db.add(m.Resource(id=rid, space_id=env.space, kind="document", name=title, owner_id=env.actor))
        db.add(m.Blob(id=blob, space_id=env.space, object_key="synthetic/" + blob,
            sha256=text_sha256(title), size_bytes=len(title.encode()), mime_type="text/plain", scan_state="CLEAN"))
        db.flush()
        version = m.ResourceVersion(id=vid, resource_id=rid, version_no=1, author_id=env.actor,
            title=title, origin="HUMAN", source_blob_id=blob, knowledge_type="source")
        db.add(version)
        db.flush()
        for ordinal, text in enumerate(texts):
            row = {"version_id": vid, "block_id": svc.uid(), "ordinal": ordinal, "block_type": "paragraph",
                "data": {"text": text}, "search_text": text, "content_sha256": text_sha256(text), "locator": {}}
            db.add(m.ContentBlock(**row))
            records.append({**row, "resource_id": rid})
        db.flush()
        version.content_sha256 = svc.check_frozen_hash(db, version)
        version.state = "IN_REVIEW"
    return records


def _hit(row):
    text = row["search_text"]
    return {key: row[key] for key in ("resource_id", "version_id", "block_id", "ordinal", "content_sha256")} | {
        "text": text, "source_kind": "document", "block_ids": [row["block_id"]], "score": 1.0,
        "section_path": [], "projection_id": "projection-" + row["version_id"],
        "source_spans": [{"block_id": row["block_id"], "ordinal": row["ordinal"],
            "content_sha256": row["content_sha256"], "start": 0, "end": len(text),
            "text_start": 0, "text_end": len(text)}]}


class _Vector:
    def __init__(self, hits):
        self.embedding = SimpleNamespace(mode="fastembed", fingerprint="synthetic-boundary-vector")
        self.settings = SimpleNamespace(retrieval_strategy="unit_rerank", retrieval_unit_candidates=80,
            reranker_mode="local", reranker_model="synthetic-pair-scorer", reranker_revision="synthetic-v1")
        self.hits = hits
        self.complete_ids = {hit["projection_id"] for hit in hits}
        self.receipts, self.dense_queries, self.pairs = [], [], []
        self.on_rerank = lambda: None

    def complete_projection_ids(self, rows):
        self.receipts.append(copy.deepcopy(rows))
        return {row["projection_id"] for row in rows if row["projection_id"] in self.complete_ids}

    def search(self, query, versions, *, limit, allowed_projection_ids):
        return copy.deepcopy([hit for hit in self.hits if hit["version_id"] in versions
            and hit["projection_id"] in allowed_projection_ids][:limit])

    def search_many(self, queries, versions, *, limit, allowed_projection_ids):
        self.dense_queries.append(list(queries))
        return [self.search(query, versions, limit=limit, allowed_projection_ids=allowed_projection_ids)
            for query in queries]

    def lexical_search(self, query, versions, *, limit, allowed_projection_ids, cache_key):
        return self.search(query, versions, limit=limit, allowed_projection_ids=allowed_projection_ids)

    def rerank(self, query, texts):
        # Also instrument the old fallback, so a routing regression performs
        # the same committed state change instead of failing in a mock method.
        return self.rerank_many([(query, texts)])[0]

    def rerank_many(self, requests):
        self.pairs.append(copy.deepcopy(requests))
        scores = [[float(len(query) + len(text)) for text in texts] for query, texts in requests]
        self.on_rerank()
        return scores


def _indexed_sources(env):
    affected = _source(env, "合成待变更来源", [f"完整事项{i}及其条件和例外。" for i in range(3)])
    control = _source(env, "合成保留来源", ["保留来源的完整正文。"])
    vector = _Vector([_hit(row) for row in affected + control])
    with env.writer.begin() as db:
        pages = build_catalog(db, env.actor, env.space, scope="reference")
        for page in pages.values():
            vid = page["version_id"]
            db.add(m.RuntimePolicy(id=svc.uid(), name=receipt_name(vector.embedding.fingerprint, vid),
                updated_by=env.actor, config={"fingerprint": vector.embedding.fingerprint, "version_id": vid,
                    "state": "READY", "projection_id": "projection-" + vid,
                    "metadata_signature": page["_metadata_signature"]}))
    return affected, control, vector, pages


@pytest.mark.parametrize("queries", [["事项0"], ["事项0", " 事项0 ", "\t事项0\n"]],
    ids=["single-query", "normalized-duplicates"])
def test_universal_single_query_revocation_uses_fresh_cross_connection_boundaries(wal_env, queries):
    env = wal_env
    affected, control, vector, pages = _indexed_sources(env)
    frozen_pages = copy.deepcopy(pages)
    open_sessions, read_connections, observations = [], [], []

    @contextmanager
    def factory():
        with env.reader() as db:
            read_connections.append(db.connection().connection.driver_connection)
            open_sessions.append(db)
            try:
                yield db
            finally:
                open_sessions.remove(db)

    def revoke():
        observation = {"held_transactions": sum(bool(db.in_transaction()) for db in open_sessions)}
        with env.writer.begin() as db:
            observation["writer_connection"] = db.connection().connection.driver_connection
            db.execute(update(m.Resource).where(m.Resource.id == affected[0]["resource_id"]).values(suspended=True))
        observation["committed"] = True
        observations.append(observation)

    vector.on_rerank = revoke
    result = search_many_catalog(None, env.actor, env.space, queries, pages=pages, vector=vector,
        session_factory=factory)
    assert len(observations) == 1 and observations[0]["committed"]
    assert observations[0]["held_transactions"] == 0
    assert all(connection is not observations[0]["writer_connection"] for connection in read_connections)
    with env.reader() as db:
        assert db.get(m.Resource, affected[0]["resource_id"]).suspended is True
    assert {u["version_id"] for u in result["units"]} == {control[0]["version_id"]}
    assert {h["resource_id"] for h in result["hits"]} == {control[0]["resource_id"]}
    assert {bid for u in result["units"] for bid in u["block_ids"]} == {control[0]["block_id"]}
    assert vector.dense_queries == [["事项0"]]
    assert len(vector.pairs) == 1 and len(vector.pairs[0]) == 1
    assert len(vector.pairs[0][0][1]) == len(affected) + len(control)
    assert result["batch_execution"]["authority_rounds"] == 3
    assert result["batch_execution"]["query_count"] == 1
    assert result["searches"][0]["reranking"]["mode"] == "local_cross_encoder"
    assert pages == frozen_pages


def test_projection_replacement_during_rerank_cannot_authorize_old_candidates(wal_env):
    env = wal_env
    affected, control, vector, pages = _indexed_sources(env)
    frozen_pages = copy.deepcopy(pages)
    vid = affected[0]["version_id"]
    old_projection, replacement = "projection-" + vid, "replacement-" + vid
    swapped = []

    def replace():
        with env.writer.begin() as db:
            receipt = db.scalar(select(m.RuntimePolicy).where(
                m.RuntimePolicy.name == receipt_name(vector.embedding.fingerprint, vid)))
            receipt.config = {**receipt.config, "projection_id": replacement}
        vector.complete_ids.remove(old_projection)
        vector.complete_ids.add(replacement)
        swapped.append(True)

    vector.on_rerank = replace
    result = search_many_catalog(None, env.actor, env.space, ["事项0", "事项1"],
        pages=pages, vector=vector, session_factory=env.reader)
    assert swapped == [True]
    assert len(vector.receipts) == 3
    assert [next(row["projection_id"] for row in rows if row["version_id"] == vid)
            for rows in vector.receipts] == [old_projection, old_projection, replacement]
    assert replacement in vector.complete_ids and old_projection not in vector.complete_ids
    with env.reader() as db:
        current = {page["version_id"]: page for page in build_catalog(db, env.actor, env.space).values()}
        previous = next(page for page in pages.values() if page["version_id"] == vid)
        assert current[vid]["_metadata_signature"] == previous["_metadata_signature"]
        receipt = db.scalar(select(m.RuntimePolicy).where(
            m.RuntimePolicy.name == receipt_name(vector.embedding.fingerprint, vid)))
        assert receipt.config["state"] == "READY" and receipt.config["projection_id"] == replacement
    assert {u["version_id"] for u in result["units"]} == {control[0]["version_id"]}
    assert len(result["units"]) == 2  # The unaffected candidate remains paired with each query.
    assert {u["matched_queries"][0] for u in result["units"]} == {"事项0", "事项1"}
    assert {h["resource_id"] for h in result["hits"]} == {control[0]["resource_id"]}
    assert "VECTOR_PROJECTION_CHANGED_DURING_READ" in result["warnings"]
    assert all(search["reranking"]["mode"] == "local_cross_encoder" for search in result["searches"])
    assert len(vector.pairs[0]) == 2 and all(len(texts) == 4 for _, texts in vector.pairs[0])
    assert pages == frozen_pages


def test_1001_version_candidate_union_preserves_all_blocks_and_unique_headings(wal_env):
    env = wal_env
    resources, versions, records, hits = [], [], [], []
    expected, expected_headings = {}, {}
    first_heading_id = first_heading_title = None
    for number in range(1001):
        rid, vid = str(UUID(int=1_000_000 + number)), str(UUID(int=2_000_000 + number))
        title = f"边界标题 {number:04d}"
        resources.append({"id": rid, "space_id": env.space, "kind": "document", "name": title, "owner_id": env.actor})
        versions.append({"id": vid, "resource_id": rid, "version_no": 1, "author_id": env.actor,
            "title": title, "origin": "HUMAN"})
        expected_headings[vid] = [(0, title)]
        # The first version also crosses the per-version IN/bind budget. Its
        # first heading is selected by ID in one SQL batch and by label later.
        texts = [title] + [f"合成正文 {number}-{i}，全文与尾部。" for i in range(800 if number == 0 else 1)]
        for ordinal, text in enumerate(texts):
            bid = str(UUID(int=10_000_000 + number * 1000 + ordinal))
            kind = "heading" if ordinal == 0 else "paragraph"
            data = {"text": text, **({"level": 1} if ordinal == 0 else {})}
            assert block_text({"block_type": kind, "data": data}) == text
            row = {"version_id": vid, "block_id": bid, "ordinal": ordinal, "block_type": kind,
                "data": data, "search_text": text, "content_sha256": text_sha256(text), "locator": {}}
            records.append(row)
            expected[(vid, bid)] = {**row, "resource_id": rid, "version_no": 1}
            hits.append({"version_id": vid, "block_id": bid, "section_path": ["边界标题", title]})
            if number == ordinal == 0:
                first_heading_id, first_heading_title = bid, title
    with env.writer.begin() as db:
        db.execute(insert(m.Resource), resources)
        db.execute(insert(m.ResourceVersion), versions)
        db.execute(insert(m.ContentBlock), records)
    # Repeated channel/query hits must not multiply blocks or heading entries.
    union = hits + list(reversed(hits)) + hits[:20]
    frozen_union, statements = copy.deepcopy(union), []

    def observe(_connection, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and "content_blocks" in statement:
            statements.append(tuple(parameters))

    event.listen(env.reader_engine, "before_cursor_execute", observe)
    try:
        with env.reader() as db:
            blocks, headings, checked = _candidate_blocks(db, union)
    finally:
        event.remove(env.reader_engine, "before_cursor_execute", observe)
    assert len(expected) == 2801 and len(expected_headings) == 1001
    assert blocks == expected
    assert dict(headings) == expected_headings
    assert checked == len(expected)
    assert union == frozen_union
    assert len(statements) > 1 and all(len(parameters) < 800 for parameters in statements)
    assert all(len(set(parameters) & expected_headings.keys()) <= 128 for parameters in statements)
    id_batches = {i for i, parameters in enumerate(statements) if first_heading_id in parameters}
    label_batches = {i for i, parameters in enumerate(statements) if first_heading_title in parameters}
    assert id_batches and label_batches and id_batches.isdisjoint(label_batches)
