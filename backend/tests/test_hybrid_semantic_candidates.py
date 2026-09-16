"""Synthetic in-memory discovery/reading checks; no models, services or real DB."""
from __future__ import annotations

import copy
import json
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update

from fund_kb import hybrid_retrieval as hybrid
from fund_kb import models as m
from fund_kb import providers, retrieval
from fund_kb import services as svc
from fund_kb.db import Base, build_engine, make_session_factory
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.vector_indexing import receipt_name
from fund_kb.wiki_catalog import build_catalog
from fund_kb.wiki_section_reader import read_scoped_pages


@pytest.fixture
def env(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Semantic candidate tests must not call networks, models or a real vector store")

    for target, name in ((socket.socket, "connect"), (socket, "create_connection"),
            (socket, "getaddrinfo"), (providers, "complete"),
            (retrieval.EmbeddingProvider, "embed"), (retrieval.VectorIndex, "__init__")):
        monkeypatch.setattr(target, name, forbidden)
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    actor, space = svc.uid(), svc.uid()
    with factory.begin() as db:
        db.add(m.Space(id=space, name="合成检索空间"))
        db.add(m.User(id=actor, external_subject=f"synthetic:{actor}", display_name="合成编辑者"))
        db.flush()
        db.add_all(m.SpaceMember(space_id=space, user_id=actor, role=role)
            for role in ("reader", "editor", "reviewer", "publisher"))
    try:
        yield SimpleNamespace(db=factory, engine=engine, owner=actor, space=space)
    finally:
        engine.dispose()


def source(env, *, title="合成估值标准", kind="document", resource_id=None, texts=None):
    texts = texts or [
        ("heading", {"text": "第二章 估值处理", "level": 1}),
        ("heading", {"text": "第十二条 回售条款", "level": 2}),
        ("paragraph", {"text": "前文🙂：回售登记日至实际收款期间应核对估值全价及信用风险。后文。"}),
        ("paragraph", {"text": "登记截止日后，未行使回售权利的债券应核对长待偿期价格。"}),
        ("paragraph", {"text": "SECTION_END：上述处理须结合适用范围和例外完整阅读。"}),
        ("heading", {"text": "第十三条 其他事项", "level": 2}),
        ("paragraph", {"text": "UNREAD_TAIL_CANARY：无关行政文书仅供归档。"}),
    ]
    rid, vid = resource_id or svc.uid(), svc.uid()
    records = []
    with env.db.begin() as db:
        if not db.get(m.Resource, rid):
            db.add(m.Resource(id=rid, space_id=env.space, kind=kind, name=title, owner_id=env.owner))
            db.flush()
        version_no = len(list(db.scalars(select(m.ResourceVersion.id).where(
            m.ResourceVersion.resource_id == rid)))) + 1
        blob_id = svc.uid() if kind == "document" else None
        if blob_id:
            db.add(m.Blob(id=blob_id, space_id=env.space, object_key=f"synthetic/{blob_id}.txt",
                sha256=text_sha256(title), size_bytes=len(title.encode()), mime_type="text/plain", scan_state="CLEAN"))
            db.flush()
        version = m.ResourceVersion(id=vid, resource_id=rid, version_no=version_no, author_id=env.owner,
            title=title, origin="HUMAN", source_blob_id=blob_id, source_verified=False,
            knowledge_type="source" if kind == "document" else "rule", legal_status="UNKNOWN")
        db.add(version)
        db.flush()
        for ordinal, (block_type, data) in enumerate(texts):
            text = block_text({"block_type": block_type, "data": data})
            row = {"version_id": vid, "block_id": svc.uid(), "ordinal": ordinal,
                "block_type": block_type, "data": data, "locator": {"label": f"原文第{ordinal + 1}块"},
                "search_text": text, "content_sha256": text_sha256(text)}
            db.add(m.ContentBlock(**row))
            records.append({**row, "resource_id": rid, "title": title, "source_kind": kind, "text": text})
        db.flush()
        version.content_sha256 = svc.check_frozen_hash(db, version)
        version.state = "IN_REVIEW"
    return records


def semantic_hit(records, indices=(2, 3), *, slices=None, score=0.9):
    chosen = [records[index] for index in indices]
    text, spans = "", []
    for index, row in enumerate(chosen):
        start, end = slices[index] if slices else (0, len(row["text"]))
        if index:
            text += "\n"
        left = len(text)
        text += row["text"][start:end]
        spans.append({"block_id": row["block_id"], "ordinal": row["ordinal"],
            "content_sha256": row["content_sha256"], "start": start, "end": end,
            "text_start": left, "text_end": len(text)})
    anchor = chosen[0]
    return {"version_id": anchor["version_id"], "resource_id": anchor["resource_id"],
        "block_id": anchor["block_id"], "content_sha256": anchor["content_sha256"],
        "parent_version_id": anchor["version_id"], "parent_block_id": anchor["block_id"],
        "parent_content_sha256": anchor["content_sha256"], "ordinal": anchor["ordinal"],
        "source_kind": anchor["source_kind"], "text": text, "source_spans": spans,
        "block_ids": list(dict.fromkeys(row["block_id"] for row in chosen)),
        "section_path": ["第二章 估值处理", "第十二条 回售条款"],
        "section_title": "第十二条 回售条款", "section_id": "index-only-section-identity",
        "chunk_sha256": text_sha256(text), "score": score,
        "projection_id": "projection-" + anchor["version_id"]}


def legacy_hit(row, start=3, end=20):
    text = row["text"][start:end]
    return {key: row[key] for key in ("version_id", "resource_id", "block_id", "content_sha256", "ordinal")} | {
        "parent_version_id": row["version_id"], "parent_block_id": row["block_id"],
        "parent_content_sha256": row["content_sha256"], "text": text,
        "chunk_start": start, "chunk_end": end, "start": start, "end": end,
        "chunk_sha256": text_sha256(text), "score": 0.8, "projection_id": "projection-" + row["version_id"]}


class SyntheticIndex:
    """Deliberately returns unfiltered payloads to exercise the caller's guard."""

    def __init__(self, lexical, dense):
        self.embedding = SimpleNamespace(mode="fastembed", fingerprint="synthetic-semantic-candidates")
        self.settings = SimpleNamespace(embedding_allow_document_transfer=False)
        self.lexical, self.dense, self.limits = lexical, dense, []

    def complete_projection_ids(self, receipts):
        return {row["projection_id"] for row in receipts}

    def lexical_search(self, query, versions, *, limit, **kwargs):
        self.limits.append(limit)
        return copy.deepcopy(self.lexical)

    def search(self, query, versions, *, limit, **kwargs):
        self.limits.append(limit)
        return copy.deepcopy(self.dense)


def indexed(env, hits, dense=None):
    vector = SyntheticIndex(hits, hits if dense is None else dense)
    with env.db.begin() as db:
        pages = build_catalog(db, env.owner, env.space, scope="reference")
        for page in pages.values():
            vid = page["version_id"]
            db.add(m.RuntimePolicy(id=svc.uid(), name=receipt_name(vector.embedding.fingerprint, vid),
                updated_by=env.owner, config={"fingerprint": vector.embedding.fingerprint,
                    "version_id": vid, "state": "READY", "projection_id": "projection-" + vid,
                    "metadata_signature": page["_metadata_signature"]}))
    return vector, pages


def discover(env, vector, pages=None, *, query="selectorneedle"):
    with env.db() as db:
        return hybrid.search_catalog(db, env.owner, env.space, query, vector=vector, pages=pages)


def test_multi_block_hit_keeps_all_real_anchors_and_exact_offsets(env):
    records = source(env)
    hit = semantic_hit(records, slices=[(3, len(records[2]["text"]) - 3), (0, 15)])
    other = semantic_hit(records, indices=(3, 4))
    vector, pages = indexed(env, [hit], [hit, other])
    before = copy.deepcopy(pages)
    result = discover(env, vector, pages)
    assert result["returned"] == 1 and result["evidence_preview"] is False
    candidate = result["hits"][0]
    assert set(candidate["matched_block_ids"]) == {records[i]["block_id"] for i in (2, 3, 4)}
    assert candidate["score"] == pytest.approx(2 / 61)
    excerpt = candidate["candidate_snippets"][0]
    assert excerpt["text"] == hit["text"]
    assert excerpt["source_spans"] == hit["source_spans"]
    assert excerpt["block_ids"] == hit["block_ids"]
    assert excerpt["version_id"] == records[0]["version_id"] and excerpt["version_no"] == 1
    assert excerpt["section_path"] == hit["section_path"] and excerpt["full_text_verified"] is False
    assert excerpt["verification"] == "CURRENT_DB_BLOCK_SLICES" and excerpt["is_answer_evidence"] is False
    assert result["candidate_preview_stats"] == {"verified_snippets": 1, "source_blocks_checked": 5}
    assert "evidence_id" not in json.dumps(result)
    assert pages == before and all(not page["body_loaded"] for page in pages.values())
    assert vector.limits == [72, 72]
    with env.db() as db:
        assert db.scalar(select(m.RunEvidence).limit(1)) is None
        version = db.get(m.ResourceVersion, records[0]["version_id"])
        assert version.content_sha256 == svc.check_frozen_hash(db, version)


def test_selector_gets_actual_semantic_unit_with_identity_and_non_evidence_notice(env):
    records = source(env)
    hit = semantic_hit(records)
    vector, pages = indexed(env, [hit])
    result = discover(env, vector, pages)
    context = hybrid.candidate_context(result, pages)
    for value in (records[2]["text"], records[3]["text"], "映射2个原始块，后续READ由服务端定位",
            records[0]["version_id"], "来源文档", "version=V1", "section_path=第二章 估值处理 / 第十二条 回售条款",
            "非已核验全文", "不是正式证据", "read_scoped_pages", "当前ACL、版本", "READ_SECTION/READ_FULL"):
        assert value in context
    assert "UNREAD_TAIL_CANARY" not in context and "SECTION_END" not in context
    assert "index-only-section-identity" not in context
    assert records[0]["resource_id"] not in context
    assert all(row["block_id"] not in context for row in records)


def test_actual_semantic_splitter_spans_and_complete_units_are_accepted(env):
    from fund_kb.semantic_embedding import build_semantic_units

    records = source(env, texts=[
        ("heading", {"text": "第一章 合成范围", "level": 1}),
        ("heading", {"text": "第一节 合成处理", "level": 2}),
        ("paragraph", {"text": "适用条件：" + "核对原始资料和处理条件。" * 12}),
        ("table", {"columns": ["情形", "处理"], "rows": [
            [f"第{number}行", "核对价格来源、适用日期和例外条件。"] for number in range(18)]}),
        ("paragraph", {"text": "结束前仍须核对完整适用范围。"}),
    ])
    # A deterministic offline counter checks the splitter/selector interface;
    # this does not claim to reproduce the production embedding tokenizer.
    units = build_semantic_units(records, token_count=len, max_tokens=160, overlap_tokens=24, context_tokens=24)
    by_id = {row["block_id"]: index for index, row in enumerate(records)}
    hits = []
    for unit in units:
        hit = semantic_hit(records, indices=(by_id[unit["block_ids"][0]],))
        hit.update({key: unit[key] for key in ("text", "block_ids", "source_spans", "section_path",
            "section_title", "section_id")})
        hit["chunk_sha256"] = text_sha256(unit["text"])
        hits.append(hit)
        assert len(unit["embedding_text"]) <= 160
    assert len(units) > 3
    vector, pages = indexed(env, hits)
    result = discover(env, vector, pages)
    assert "RETRIEVAL_HIT_REJECTED" not in result["warnings"]
    assert set(result["hits"][0]["matched_block_ids"]) == set(by_id)
    assert result["hits"][0]["candidate_snippets"][0]["text"] == units[0]["text"]


def test_legacy_chunk_and_id_only_payload_compatibility(env):
    records = source(env)
    hit = legacy_hit(records[2])
    id_only = {key: value for key, value in legacy_hit(records[3]).items()
        if key in {"version_id", "projection_id", "block_id", "score"}}
    vector, pages = indexed(env, [hit, id_only])
    candidate = discover(env, vector, pages)["hits"][0]
    assert set(candidate["matched_block_ids"]) == {records[2]["block_id"], records[3]["block_id"]}
    assert [item["text"] for item in candidate["candidate_snippets"]] == [records[2]["text"][3:20]]
    assert candidate["candidate_snippets"][0]["source_spans"][0]["start"] == 3


@pytest.mark.parametrize("damage", ["missing_block", "malformed_block", "foreign_block", "hash", "ordinal",
    "start", "end", "text_start", "text_end", "boolean_offset", "text", "prefix", "gap", "suffix",
    "block_ids", "anchor", "resource", "parent_version", "parent_hash", "chunk_hash",
    "empty_spans", "nonlist_spans", "malformed_span"])
def test_bad_semantic_span_rejects_entire_hit_without_fabricated_evidence(env, damage):
    records = source(env)
    foreign = source(env, title="外部版本内容") if damage == "foreign_block" else None
    hit = semantic_hit(records)
    second = hit["source_spans"][1]
    if damage == "missing_block":
        second["block_id"] = svc.uid()
        hit["block_ids"][1] = second["block_id"]
    elif damage == "malformed_block":
        second["block_id"] = "not-a-source-block"
    elif damage == "foreign_block":
        second["block_id"] = foreign[3]["block_id"]
        hit["block_ids"][1] = second["block_id"]
    elif damage == "hash":
        second["content_sha256"] = "0" * 64
    elif damage == "ordinal":
        second["ordinal"] += 1
    elif damage in {"start", "end", "text_start", "text_end"}:
        second[damage] += 1
    elif damage == "boolean_offset":
        second["start"] = False
    elif damage == "text":
        hit["text"] = "伪" + hit["text"][1:]
    elif damage == "prefix":
        hit["text"] = "TAMPER_PREFIX" + hit["text"]
        for span in hit["source_spans"]:
            span["text_start"] += len("TAMPER_PREFIX")
            span["text_end"] += len("TAMPER_PREFIX")
    elif damage == "gap":
        hit["text"] = hit["text"].replace("\n", "TAMPER_GAP")
        second["text_start"] += len("TAMPER_GAP") - 1
        second["text_end"] += len("TAMPER_GAP") - 1
    elif damage == "suffix":
        hit["text"] += "TAMPER_SUFFIX"
    elif damage == "block_ids":
        hit["block_ids"].append(records[6]["block_id"])
    elif damage == "anchor":
        hit["block_id"] = records[6]["block_id"]
    elif damage == "resource":
        hit["resource_id"] = svc.uid()
    elif damage == "parent_version":
        hit["parent_version_id"] = svc.uid()
    elif damage == "parent_hash":
        hit["parent_content_sha256"] = "0" * 64
    elif damage == "empty_spans":
        hit["source_spans"] = []
    elif damage == "nonlist_spans":
        hit["source_spans"] = {"block_id": records[2]["block_id"]}
    elif damage == "malformed_span":
        hit["source_spans"].append(None)
    hit["chunk_sha256"] = "0" * 64 if damage == "chunk_hash" else text_sha256(hit["text"])
    vector, pages = indexed(env, [hit])
    result = discover(env, vector, pages)
    assert result["hits"] == []
    assert "RETRIEVAL_HIT_REJECTED" in result["warnings"]
    assert "TAMPER" not in hybrid.candidate_context(result, pages)


@pytest.mark.parametrize("damage", ["parent_hash", "chunk_start", "chunk_end", "text", "missing_hash"])
def test_bad_legacy_chunk_cannot_be_promoted_to_excerpt(env, damage):
    records = source(env)
    hit = legacy_hit(records[2])
    if damage == "parent_hash":
        hit["parent_content_sha256"] = "0" * 64
    elif damage in {"chunk_start", "chunk_end"}:
        hit[damage] += 1
        hit[damage.removeprefix("chunk_")] += 1
    elif damage == "text":
        hit["text"] = "伪" + hit["text"][1:]
        hit["chunk_sha256"] = text_sha256(hit["text"])
    else:
        hit.pop("content_sha256")
        hit.pop("parent_content_sha256")
    vector, pages = indexed(env, [hit])
    assert discover(env, vector, pages)["hits"] == []


@pytest.mark.parametrize("damage", ["data", "search_text", "hash", "rehash"])
def test_current_db_tamper_rejects_payload_even_with_old_ready_receipt(env, damage):
    records = source(env)
    hit = semantic_hit(records)
    vector, pages = indexed(env, [hit])
    with env.db.begin() as db:
        row = db.get(m.ContentBlock, (records[3]["version_id"], records[3]["block_id"]))
        if damage in {"data", "rehash"}:
            row.data = {"text": "DB_TAMPER_CANARY：伪造正文。"}
        if damage in {"search_text", "rehash"}:
            row.search_text = "DB_TAMPER_CANARY：伪造正文。"
        if damage == "hash":
            row.content_sha256 = "0" * 64
        if damage == "rehash":
            row.content_sha256 = text_sha256(row.search_text)
    result = discover(env, vector, pages)
    assert result["hits"] == []
    assert result["candidate_preview_stats"] == {"verified_snippets": 0, "source_blocks_checked": 4}
    assert "DB_TAMPER_CANARY" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("change", ["restricted", "suspended", "deleted", "new_version", "version_hash",
    "projection", "source_scan"])
def test_cached_catalog_cannot_leak_revoked_or_changed_sources(env, change):
    records = source(env, title="候选元数据泄露标记")
    hit = semantic_hit(records)
    vector, pages = indexed(env, [hit])
    if change == "new_version":
        source(env, resource_id=records[0]["resource_id"], title="替换后的版本")
    else:
        with env.db.begin() as db:
            resource = db.get(m.Resource, records[0]["resource_id"])
            version = db.get(m.ResourceVersion, records[0]["version_id"])
            if change in {"restricted", "suspended"}:
                setattr(resource, change, True)
                resource.access_epoch += 1
            elif change == "deleted":
                resource.deleted_at = svc.now()
            elif change == "version_hash":
                version.content_sha256 = "0" * 64
            elif change == "source_scan":
                db.get(m.Blob, version.source_blob_id).scan_state = "QUARANTINED"
            else:
                row = db.scalar(select(m.RuntimePolicy).where(
                    m.RuntimePolicy.name == receipt_name(vector.embedding.fingerprint, version.id)))
                row.config = {**row.config, "projection_id": "replacement-projection"}
    result = discover(env, vector, pages, query="候选元数据泄露标记" if change != "projection" else "selectorneedle")
    assert result["hits"] == []
    assert "回售登记日至" not in json.dumps(result, ensure_ascii=False)


def test_revocation_during_index_search_ignores_session_cached_resource(env, monkeypatch):
    records = source(env)
    hit = semantic_hit(records)
    vector, pages = indexed(env, [hit])

    def revoke(*args, **kwargs):
        # This fixture has ONE in-memory connection. Direct SQL on its existing
        # transaction simulates revocation without refreshing the held ORM row;
        # a second Session would only fail to BEGIN, never perform revocation.
        db.connection().execute(update(m.Resource).where(m.Resource.id == records[0]["resource_id"])
            .values(suspended=True))
        return [hit]

    monkeypatch.setattr(vector, "lexical_search", revoke)
    with env.db() as db:
        held_resource = db.get(m.Resource, records[0]["resource_id"])
        assert not held_resource.suspended
        result = hybrid.search_catalog(db, env.owner, env.space, "selectorneedle", vector=vector, pages=pages)
    assert result["hits"] == []


def test_untrusted_section_labels_and_extra_payload_body_are_not_echoed(env):
    records = source(env)
    hit = semantic_hit(records)
    hit.update(section_path=["HIDDEN_SECTION_CANARY"], section_title="HIDDEN_TITLE_CANARY",
        title="PAYLOAD_TITLE_CANARY", data={"text": "PAYLOAD_DATA_CANARY"})
    vector, pages = indexed(env, [hit])
    result = discover(env, vector, pages)
    assert result["hits"][0]["candidate_snippets"][0]["section_path"] == []
    context = hybrid.candidate_context(result, pages)
    assert records[2]["text"] in context and records[0]["title"] in context
    assert "CANARY" not in context


def test_both_channels_use_one_batch_for_all_hit_blocks(env):
    records = source(env)
    hits = [semantic_hit(records, indices=(2, 3)), semantic_hit(records, indices=(3, 4))]
    vector, pages = indexed(env, hits * 6)
    reads = []

    def count_reads(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT content_blocks.version_id, content_blocks.block_id, content_blocks.ordinal"):
            reads.append(statement)

    event.listen(env.engine, "before_cursor_execute", count_reads)
    try:
        result = discover(env, vector, pages)
    finally:
        event.remove(env.engine, "before_cursor_execute", count_reads)
    assert result["returned"] == 1 and len(reads) == 1
    assert len(result["hits"][0]["matched_block_ids"]) == 3
    assert result["candidate_preview_stats"] == {"verified_snippets": 1, "source_blocks_checked": 5}


def test_vector_none_is_still_metadata_only_without_body_reads(env):
    source(env, title="唯一候选目录", texts=[("paragraph", {"text": "METADATA_ONLY_BODY_CANARY"})])
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(env.engine, "before_cursor_execute", capture)
    try:
        with env.db() as db:
            result = hybrid.search_catalog(db, env.owner, env.space, "唯一候选目录", vector=None)
    finally:
        event.remove(env.engine, "before_cursor_execute", capture)
    assert result["returned"] == 1 and result["evidence_preview"] is False
    assert result["hits"][0]["candidate_snippets"] == []
    assert result["candidate_preview_stats"] == {"verified_snippets": 0, "source_blocks_checked": 0}
    assert "METADATA_ONLY_BODY_CANARY" not in json.dumps(result)
    assert not any("content_blocks.search_text" in statement for statement in statements)


def test_candidates_stay_at_24_pages_with_rrf_order_and_full_hit_units(env):
    hits, versions = [], []
    for number in range(30):
        records = source(env, title=f"平行资料{number:02d}", texts=[("paragraph", {"text": f"实际片段{number}。"})])
        hit = semantic_hit(records, indices=(0,))
        hit["section_path"] = []
        hits.extend([hit] * 3)
        versions.append(records[0]["version_id"])
    vector, pages = indexed(env, hits)
    result = discover(env, vector, pages)
    assert result["returned"] == 24 and result["total_candidates"] == 30
    assert result["candidate_preview_stats"] == {"verified_snippets": 24, "source_blocks_checked": 30}
    assert [hit["version_id"] for hit in result["hits"]] == versions[:24]
    for rank, candidate in enumerate(result["hits"], 1):
        assert candidate["score"] == pytest.approx(2 / (60 + rank))
        assert len(candidate["candidate_snippets"]) == 1
        assert candidate["candidate_snippets"][0]["text"] == f"实际片段{rank - 1}。"


@pytest.mark.parametrize("late_change", [None, "acl", "tamper"])
def test_formal_evidence_still_requires_current_scoped_db_read(env, late_change):
    records = source(env)
    vector, pages = indexed(env, [semantic_hit(records)])
    candidate = discover(env, vector, pages)["hits"][0]
    assert all(not page["records"] for page in pages.values())
    if late_change:
        with env.db.begin() as db:
            if late_change == "acl":
                db.get(m.Resource, records[0]["resource_id"]).suspended = True
            else:
                db.get(m.ContentBlock, (records[4]["version_id"], records[4]["block_id"])).data = {
                    "text": "非命中块被篡改，完整来源校验仍必须失败。"}
    pid = candidate["page_id"]
    with env.db() as db:
        fresh, unavailable, _, _ = read_scoped_pages(db, env.owner, env.space, pages, [pid],
            anchors={pid: candidate["matched_block_ids"]})
    if late_change:
        assert not fresh and unavailable == [pid]
    else:
        assert not unavailable
        assert {records[i]["block_id"] for i in (2, 3, 4)} <= {row["block_id"] for row in fresh}
        assert records[6]["block_id"] not in {row["block_id"] for row in fresh}
        assert all(row["evidence_id"].startswith("E") for row in fresh)
        original = {row["block_id"]: row["content_sha256"] for row in records}
        assert all(row["content_sha256"] == original[row["block_id"]] for row in fresh)
