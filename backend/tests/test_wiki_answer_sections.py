"""Scoped-source answer regressions: synthetic memory DB and captured model stubs.

Run only this module while the scoped reader is being integrated. These are
behavioral/TDD tests; do not change production code to accommodate the fixtures.
The receipt contract is wiki_reading.loaded_blocks/scoped_source_pages/
full_source_pages plus model_snapshot.reading_progress. No section-helper API
or wording of the complete prompt is assumed.
"""
import copy
import json
import re
import socket
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_reference_review import make_version
from test_wiki_reader_job import base_env, execute, prepare  # noqa: F401
from test_wiki_reader_job import env as reader_env  # noqa: F401

from fund_kb import ai_transport, codex_bridge, codex_text, hybrid_retrieval, providers, retrieval
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.jobs import JobDispatcher, JobError


@pytest.fixture
def env(reader_env, monkeypatch):  # noqa: F811 - imported pytest fixture dependency
    """Reuse Settings.model_construct and memory SQLite; forbid real adapters."""
    def forbidden(*args, **kwargs):
        pytest.fail("Scoped answer regressions must not access network, credentials, or real models")

    for target, name in (
        (socket, "getaddrinfo"), (socket.socket, "connect_ex"),
        (subprocess, "Popen"), (providers, "complete"),
        (providers, "_network"), (providers, "_master_key"),
        (codex_bridge, "_read_private"), (codex_text, "_read_private"),
        (ai_transport, "post_json"), (retrieval.EmbeddingProvider, "embed"),
        (retrieval.VectorIndex, "__init__"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    assert str(reader_env.db.kw["bind"].url) == "sqlite:///:memory:"
    return reader_env


def _block(vid, ordinal, text, *, level=None, table=None):
    kind = "table" if table is not None else "heading" if level else "paragraph"
    data = table if table is not None else {"text": text, **({"level": level} if level else {})}
    text = block_text({"block_type": kind, "data": data})
    return m.ContentBlock(
        version_id=vid, block_id=svc.uid(), ordinal=ordinal, block_type=kind,
        data=data, search_text=text, content_sha256=text_sha256(text),
        locator={"page": ordinal // 10 + 1, "label": f"合成原文第{ordinal + 1}块"},
    )


@pytest.fixture
def manual(env):
    """One exact CITES anchor in a large V2; V1 and both source hashes stay frozen.

    The target has two parent headings, a first/last paragraph and a whole
    table. Other chapters and the next sibling section contain canaries.
    Expected blocks are enumerated here, independently of source_sections.
    """
    old_id = make_version(env, "document")
    wiki_id = make_version(env, "knowledge")
    source_id = svc.uid()
    with env.db.begin() as db:
        old = db.get(m.ResourceVersion, old_id)
        old.title = "合成手册旧版"
        old.content_sha256 = svc.check_frozen_hash(db, old)
        old.state = "IN_REVIEW"
        db.flush()
        current = m.ResourceVersion(
            id=source_id, resource_id=old.resource_id, version_no=2, base_version_id=old.id,
            author_id=env.owner, title="合成FOF会计手册", knowledge_type="source",
            origin="COPY", source_blob_id=old.source_blob_id,
            source_verified=False, legal_status="UNKNOWN",
        )
        db.add(current)
        db.flush()
        blocks = []

        def add(text="", **kwargs):
            row = _block(source_id, len(blocks), text, **kwargs)
            blocks.append(row)
            return row

        root = add("合成会计实务手册总标题", level=1)
        add("第一章 行政档案", level=2)
        for number in range(48):
            add(f"UNRELATED_CANARY_BEFORE_{number:02d} " + "办公用品登记、档案编号与库房盘点记录。" * 24)
        parent = add("第二章 FOF会计核算", level=2)
        section = add("第一节 被投基金估值", level=3)
        first = add("SECTION_START：本节适用范围为持有其他基金份额，先核对净值时点。")
        anchor = add("CITED_ANCHOR：净值暂缺时核对被投基金类型、价格来源和估值条件。")
        middle = add("SECTION_MIDDLE：相关条件和例外须结合上下文连续阅读，不以命中句替代本节。")
        table = add(table={"columns": ["情形", "核对事项"], "rows": [
            ["TABLE_FIRST_ROW", "净值可得性"], ["TABLE_LAST_ROW", "价格来源与适用日期"],
        ]})
        last = add("SECTION_END：本节结束前还应核对例外和所需补充资料，不据此声称已核验。")
        add("第二节 行政用品盘点", level=3)
        for number in range(48):
            add(f"UNRELATED_CANARY_AFTER_{number:02d} " + "与被投基金估值无关的行政物资登记记录。" * 24)
        db.add_all(blocks)
        db.flush()
        current.content_sha256 = svc.check_frozen_hash(db, current)

        wiki = db.get(m.ResourceVersion, wiki_id)
        wiki.title = "FOF净值暂缺知识页"
        wiki_block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == wiki_id))
        text = "WIKI_COMPLETE：被投基金净值暂缺时，应阅读会计手册对应章节并核对适用条件。"
        wiki_block.data, wiki_block.search_text, wiki_block.content_sha256 = (
            {"text": text}, text, text_sha256(text)
        )
        db.add(m.EvidenceLink(
            id=svc.uid(), from_version_id=wiki_id, from_block_id=wiki_block.block_id,
            to_version_id=source_id, to_block_id=anchor.block_id, purpose="FACT",
        ))
        resource = db.get(m.Resource, current.resource_id)
        blob = db.get(m.Blob, current.source_blob_id)
        db.add(m.RuntimePolicy(
            id=svc.uid(), name=f"wiki-provenance:{wiki.resource_id}", updated_by=env.owner,
            config={"resource_id": wiki.resource_id, "source_mode": "unverified_draft",
                "source_version_ids": [source_id], "reference_snapshot": [],
                "source_snapshot": [{"resource_id": resource.id, "version_id": source_id,
                    "content_sha256": current.content_sha256, "access_epoch": resource.access_epoch,
                    "source_blob_sha256": blob.sha256}]},
        ))
        related = [root, parent, section, first, anchor, middle, table, last]
        value = SimpleNamespace(
            source=source_id, old=old_id, resource=resource.id, wiki=wiki_id,
            source_title=current.title, wiki_title=wiki.title, wiki_block=wiki_block.block_id,
            expected_ids={row.block_id for row in related},
            section_ids={row.block_id for row in [section, first, anchor, middle, table, last]},
            expected_texts=[row.search_text for row in related],
            all_ids={row.block_id for row in blocks}, all_texts=[row.search_text for row in blocks],
            unrelated_block=blocks[2].block_id,
        )
    assert sum(len(text.encode()) for text in value.all_texts) > 2 * 65536
    return value


def _source_snapshot(env, manual):
    """Compare all source columns and recomputed hashes, including historical V1."""
    with env.db() as db:
        versions = list(db.scalars(select(m.ResourceVersion).where(
            m.ResourceVersion.resource_id == manual.resource).order_by(m.ResourceVersion.version_no)))
        rows = [db.get(m.Resource, manual.resource), *versions]
        rows.extend(db.scalars(select(m.ContentBlock).where(
            m.ContentBlock.version_id.in_([v.id for v in versions]))
            .order_by(m.ContentBlock.version_id, m.ContentBlock.ordinal)))
        rows.extend(db.scalars(select(m.Blob).where(
            m.Blob.id.in_({v.source_blob_id for v in versions})).order_by(m.Blob.id)))
        return {
            "rows": [copy.deepcopy({column.name: getattr(row, column.name)
                for column in row.__table__.columns}) for row in rows],
            "recomputed_hashes": {v.id: svc.check_frozen_hash(db, v) for v in versions},
        }


def _page_id(text, title):
    match = re.search(r"(W\d+) \| (?:知识页|来源文档) \| " + re.escape(title) + r" \|", text)
    assert match, f"Expected authorized catalog entry: {title}"
    return match[1]


def _markdown(frame):
    evidence = re.findall(r"\[(E\d+)\]", frame["text"])
    return "## 合成处理说明\n请核对净值时点、适用条件与来源。" + (f"[{evidence[0]}]" if evidence else "")


def _prepare(env, monkeypatch, responder, *, capacity=65536, before_send=None):
    """Wrap the existing model stub with request-size and late-authority checks."""
    frames = []
    rid, jid, calls = prepare(env, monkeypatch, lambda calls, connection: responder(frames[-1]))
    resolve, complete = providers.resolve_connection, providers.complete

    def connection(*args, **kwargs):
        return {**resolve(*args, **kwargs), "max_request_bytes": capacity}

    def capture(connection, messages, **kwargs):
        with env.db() as db:
            snapshot = copy.deepcopy(db.get(m.ConsultationRun, rid).model_snapshot)
        # This fixture uses the existing synthetic OpenAI-protocol connection.
        # Size the actual JSON envelope, including escaping and system text.
        payload = {"model": connection["model_id"], "messages": messages,
            "stream": False, "max_tokens": kwargs["max_tokens"]}
        frame = {"phase": snapshot["last_request"]["phase"], "text": messages[1]["content"],
            "wiki_reading": snapshot.get("wiki_reading", {}),
            "reading_progress": snapshot.get("reading_progress", {}),
            "serialized_bytes": len(json.dumps(payload, ensure_ascii=False).encode())}
        if before_send:
            before_send(frame)
        connection["_before_send_check"]()
        assert frame["serialized_bytes"] <= capacity, "Serialized model request exceeds the fixture capacity"
        frames.append(frame)
        return complete(connection, messages, **kwargs)

    monkeypatch.setattr(providers, "resolve_connection", connection)
    monkeypatch.setattr(providers, "complete", capture)
    return rid, jid, calls, frames


def _select_wiki(manual):
    def responder(frame):
        if frame["phase"] == "planning":
            return "先读净值暂缺知识页，再核对其精确引用的手册章节。"
        if frame["phase"] == "wiki_index":
            return "READ " + _page_id(frame["text"], manual.wiki_title)
        return _markdown(frame)
    return responder


def test_cites_one_block_reads_complete_section_only_and_preserves_source_versions(env, monkeypatch, manual):
    before = _source_snapshot(env, manual)
    select_wiki = _select_wiki(manual)

    def responder(frame):
        if frame["phase"] in {"wiki_notes", "synthesis"}:
            assert "UNRELATED_CANARY" not in frame["text"], "CITES must not expand unrelated source chapters"
            assert "WIKI_COMPLETE" in frame["text"]
            for text in manual.expected_texts:
                assert text in frame["text"], "Keep section boundaries, both parent headings, and the complete table"
        return select_wiki(frame)

    rid, jid, _, frames = _prepare(env, monkeypatch, responder)
    try:
        execute(env, jid)
    finally:
        assert _source_snapshot(env, manual) == before, "Reading must not rewrite source text, hashes, or V1"
    assert [frame["phase"] for frame in frames] == ["planning", "wiki_index", "synthesis"]
    assert all("UNRELATED_CANARY" not in frame["text"] for frame in frames)
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "COMPLETED"
        reading = run.model_snapshot["wiki_reading"]
        assert reading["loaded_blocks"] == len(run.evidence_snapshot)
        assert reading["scoped_source_pages"] == 1 and reading["full_source_pages"] == 0
        assert reading.get("full_text_loaded") is not True, "A section receipt must not claim the whole source was read"
        loaded = {row["block_id"] for row in run.evidence_snapshot if row["version_id"] == manual.source}
        # Parents must appear in the prompt; either anchored evidence rows or
        # section metadata may carry those headings. Never admit a sibling.
        assert manual.section_ids <= loaded <= manual.expected_ids
        assert {(row["version_id"], row["block_id"]) for row in run.evidence_snapshot
            if row["version_id"] != manual.source} == {(manual.wiki, manual.wiki_block)}
        assert all(row["version_id"] != manual.old for row in run.evidence_snapshot)


def test_search_expressions_merge_and_deduplicate_candidates_before_one_selection(env, monkeypatch):
    titles = ["合成候选甲", "合成候选乙", "合成候选丙"]
    versions = [make_version(env, "knowledge") for _ in titles]
    with env.db.begin() as db:
        for number, (vid, title) in enumerate(zip(versions, titles, strict=True)):
            db.get(m.ResourceVersion, vid).title = title
            row = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == vid))
            text = f"DISCOVERY_BODY_{number}：完整知识正文，候选目录不得提前发送。"
            row.data, row.search_text, row.content_sha256 = {"text": text}, text, text_sha256(text)
    queries = ["FOF产品买入基金如何估值", "净值暂缺替代表述", "特殊估值例外"]
    routes = {queries[0]: versions[:1], queries[1]: versions[:2], queries[2]: versions[1:]}
    searched = []

    def search(db, user, space_id, query, *, pages, **kwargs):
        searched.append(query)
        by_version = {page["version_id"]: page for page in pages.values()}
        hits = [{"page_id": by_version[vid]["id"], "resource_id": by_version[vid]["resource_id"],
            "version_id": vid, "title": by_version[vid]["title"], "kind": "knowledge",
            "score": 1 / 61, "channels": ["bm25", "vector"], "matched_block_ids": []}
            for vid in routes[query]]
        return {"query": query, "scope": "reference", "mode": "hybrid", "hits": hits,
            "catalog_pages": len(pages), "indexed_catalog_pages": len(pages),
            "total_candidates": len(hits), "returned": len(hits), "warnings": [],
            "timing_ms": 0.0, "evidence_preview": False}

    def responder(frame):
        if frame["phase"] == "planning":
            return "核对净值时点。\nSEARCH 净值暂缺替代表述\nSEARCH 特殊估值例外\nSEARCH 净值暂缺替代表述"
        if frame["phase"] == "wiki_index":
            assert set(searched) == set(queries) and len(searched) == len(queries), (
                "Recall all unique SEARCH expressions before asking the model to select once"
            )
            assert "DISCOVERY_BODY_" not in frame["text"]
            assert all(frame["text"].count(title) == 1 for title in titles), "Deduplicate candidate pages across searches"
            return "READ " + " ".join(_page_id(frame["text"], title) for title in titles)
        assert all(f"DISCOVERY_BODY_{number}" in frame["text"] for number in range(3))
        return _markdown(frame)

    env.settings = env.settings.model_copy(update={"retrieval_mode": "hybrid"})
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    rid, jid, _, frames = _prepare(env, monkeypatch, responder)
    # Non-null vector sentinel enables orchestration; search is stubbed above.
    dispatcher = JobDispatcher(env.settings, env.db, object())
    try:
        dispatcher._answer(jid, 1)
    finally:
        dispatcher.close()
    assert [frame["phase"] for frame in frames] == ["planning", "wiki_index", "synthesis"]
    with env.db() as db:
        assert db.get(m.ConsultationRun, rid).state == "COMPLETED"


def test_explicit_read_full_reads_every_source_block_and_marks_full_source(env, monkeypatch, manual):
    before = _source_snapshot(env, manual)

    def responder(frame):
        if frame["phase"] == "planning":
            return "本次明确要求通读整份合成手册。"
        if frame["phase"] == "wiki_index":
            return "READ_FULL " + _page_id(frame["text"], manual.source_title)
        for text in manual.all_texts:
            assert text in frame["text"], "Explicit READ_FULL must retain every source block"
        return _markdown(frame)

    rid, jid, _, frames = _prepare(env, monkeypatch, responder, capacity=1048576)
    execute(env, jid)
    assert _source_snapshot(env, manual) == before
    assert [frame["phase"] for frame in frames] == ["planning", "wiki_index", "synthesis"]
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "COMPLETED"
        reading = run.model_snapshot["wiki_reading"]
        assert reading["full_source_pages"] == 1 and reading["scoped_source_pages"] == 0
        assert reading["loaded_blocks"] == len(manual.all_ids)
        assert {row["block_id"] for row in run.evidence_snapshot
            if row["version_id"] == manual.source} == manual.all_ids
        assert all(row["version_id"] != manual.old for row in run.evidence_snapshot)


def test_ordinary_wiki_keeps_paragraph_100_and_fits_one_real_capacity_batch(env, monkeypatch):
    vid = make_version(env, "knowledge")
    title = "普通FOF知识页完整百段"
    texts = [f"WIKI_PARAGRAPH_{number:03d} 第{number}段完整内容："
        + "核对被投基金净值的业务时点、价格来源与适用条件。" * 3
        + '字段原样保留："净值"、路径\\记录。' for number in range(1, 101)]
    with env.db.begin() as db:
        db.get(m.ResourceVersion, vid).title = title
        first = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == vid))
        first.data, first.search_text, first.content_sha256 = {"text": texts[0]}, texts[0], text_sha256(texts[0])
        db.add_all(_block(vid, number, text) for number, text in enumerate(texts[1:], 1))

    def responder(frame):
        if frame["phase"] == "planning":
            return "按完整知识页核对。"
        if frame["phase"] == "wiki_index":
            return "READ " + _page_id(frame["text"], title)
        assert frame["phase"] == "synthesis", "Material that fits one actual request must not incur extra note calls"
        assert 24000 < frame["serialized_bytes"] < 65536
        for text in texts:
            assert text in frame["text"], "READ must preserve all 100 Wiki paragraphs, including escaped text"
        return _markdown(frame)

    rid, jid, _, frames = _prepare(env, monkeypatch, responder)
    execute(env, jid)
    assert [frame["phase"] for frame in frames] == ["planning", "wiki_index", "synthesis"]
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "COMPLETED"
        assert run.model_snapshot["wiki_reading"]["loaded_blocks"] == 100
        progress = run.model_snapshot["reading_progress"]
        assert progress["total_batches"] == progress["completed_batches"] == progress["current_batch"] == 1
        assert isinstance(progress["stage"], str) and progress["stage"]


def test_source_acl_revoked_at_before_send_prevents_body_transfer(env, monkeypatch, manual):
    revoked = []

    def revoke(frame):
        if frame["phase"] in {"wiki_notes", "synthesis"} and not revoked:
            assert frame["wiki_reading"]["loaded_blocks"] > 0
            with env.db.begin() as db:
                db.get(m.Resource, manual.resource).restricted = True
            revoked.append(True)

    rid, jid, _, frames = _prepare(env, monkeypatch, _select_wiki(manual), before_send=revoke)
    with pytest.raises((JobError, svc.APIError)):
        execute(env, jid)
    assert revoked == [True]
    assert [frame["phase"] for frame in frames] == ["planning", "wiki_index"], "Revoked source text reached the model stub"
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.response is None and run.state != "COMPLETED"


def test_unread_source_section_hash_change_during_generation_rejects_delivery(env, monkeypatch, manual):
    changed = []
    select_wiki = _select_wiki(manual)

    def responder(frame):
        if frame["phase"] in {"wiki_notes", "synthesis"} and not changed:
            with env.db.begin() as db:
                source = db.get(m.ResourceVersion, manual.source)
                before = source.content_sha256
                block = db.get(m.ContentBlock, (manual.source, manual.unrelated_block))
                text = block.search_text + "合成并发修订：未读取章节也改变整份来源Hash。"
                block.data, block.search_text, block.content_sha256 = {"text": text}, text, text_sha256(text)
                source.content_sha256 = svc.check_frozen_hash(db, source)
                assert source.content_sha256 != before
            changed.append(True)
        return select_wiki(frame)

    rid, jid, _, frames = _prepare(env, monkeypatch, responder)
    with pytest.raises((JobError, svc.APIError)):
        execute(env, jid)
    assert changed == [True] and any(frame["phase"] in {"wiki_notes", "synthesis"} for frame in frames)
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.response is None and run.state != "COMPLETED", "Source hash drift must invalidate the answer"
