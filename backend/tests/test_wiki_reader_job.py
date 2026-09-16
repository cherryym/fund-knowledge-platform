"""Real Wiki worker, synthetic DB and model callbacks; no external calls."""
import copy
import re

import pytest

from test_reference_security_review import env as base_env, queued_run
from test_reference_review import make_version
from fund_kb import models as m, providers, services as svc
from fund_kb.jobs import JobDispatcher, JobError
from fund_kb.ingestion import text_sha256
from fund_kb.wiki_reader import split_text, read_requests


@pytest.fixture
def env(base_env):
    base_env.settings = base_env.settings.model_copy(update={"answer_engine": "wiki_reader"})
    return base_env


def prepare(env, monkeypatch, responder):
    selection = {"connection_id": svc.uid(), "model_id": "synthetic-wiki-reader"}
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=selection["connection_id"], name=providers.NAMESPACE + selection["connection_id"],
            updated_by=env.owner, config={"owner_user_id": env.owner, "enabled": True, "allow_document_transfer": True}))
    snapshot = {"id": selection["connection_id"], "model_id": selection["model_id"], "base_url": "https://model.invalid/v1",
        "protocol": "openai", "provider_id": "custom", "brand": "custom", "revision": 1, "owner_user_id": env.owner,
        "max_request_bytes": 1048576}
    monkeypatch.setattr(providers, "resolve_connection", lambda *a, **kw: copy.deepcopy(snapshot))
    calls = []
    def complete(connection, messages, **kwargs):
        assert kwargs["timeout"] is None and kwargs["json_mode"] is False and kwargs["output_schema"] is None
        assert callable(connection["_cancel_check"])
        calls.append(messages[1]["content"])
        result = responder(calls, connection)
        return {"choices": [{"finish_reason": "stop", "message": {"content": result}}]}
    monkeypatch.setattr(providers, "complete", complete)
    rid, jid = queued_run(env, selection=selection, question="FOF产品买入基金如何估值")
    with env.db.begin() as db:
        run = db.get(m.ConsultationRun, rid)
        run.request = {**run.request, "reasoning_strategy": "model_first", "require_model": True}
    return rid, jid, calls


def execute(env, jid):
    dispatcher = JobDispatcher(env.settings, env.db, None)
    try:
        dispatcher._answer(jid, 1)
    finally:
        dispatcher.close()


def test_full_catalog_full_100_paragraph_page_and_markdown_without_json_gate(env, monkeypatch):
    vid = make_version(env, "document")
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, vid)
        version.title = "FOF估值实务全文"
        for number in range(1, 101):
            text = f"第{number}段完整内容：需核对被投基金净值的可得性和适用场景。"
            db.add(m.ContentBlock(version_id=vid, block_id=svc.uid(), ordinal=number, block_type="paragraph",
                data={"text": text}, search_text=text, content_sha256=text_sha256(text), locator={"label": f"第{number}段"}))
    def respond(calls, _):
        if len(calls) == 1:
            assert "FOF估值实务全文" not in calls[0]
            return "先核对被投基金类型，再查阅完整估值知识。"
        if len(calls) == 2:
            assert "FOF估值实务全文" in calls[-1]
            return "READ " + re.search(r"(W\d+) \| 来源文档 \| FOF估值实务全文", calls[-1])[1]
        assert "第1段完整内容" in calls[-1] and "第100段完整内容" in calls[-1]
        ids = re.findall(r"\[(E\d+)\]", calls[-1])
        return f"## FOF估值处理\n\n需区分不同基金类型与净值时点。[{ids[-1]}]\n\nC < R 是比较式，不是脚本。CAS 22是编号。"
    rid, jid, calls = prepare(env, monkeypatch, respond)
    execute(env, jid)
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "COMPLETED" and len(calls) == 3
        assert run.response["format"] == "wiki_markdown" and run.response["grounding_status"] == "SOURCE_LINKED"
        assert run.response["claims"] == [] and "C < R" in run.response["narrative_markdown"]
        assert run.model_snapshot["wiki_reading"]["loaded_blocks"] == 101
        assert run.model_snapshot["last_request"]["limits"]["total_seconds"] is None
        assert run.response["review_status"] == "REQUIRES_EXPERT"
        assert all(row.get("evidence_id") for row in run.evidence_snapshot)
        frozen = {row["evidence_id"]: row for row in run.evidence_snapshot}
        for citation in run.response["citations"]:
            assert frozen[citation["id"]]["block_id"] == citation["block_id"]
            assert frozen[citation["id"]]["content_sha256"] == citation["content_sha256"]


@pytest.mark.parametrize("text", ["{" + '"broken": 未闭合，但正文仍可阅读。', "## 不带引用的一般分析\n请核对具体业务条件。"])
def test_plain_or_malformed_json_prose_is_delivered_but_never_claims_verified_evidence(env, monkeypatch, text):
    def responder(calls, _):
        if len(calls) == 1: return "待核对业务类型。"
        if len(calls) == 2: return "READ " + re.findall(r"W\d+", calls[-1])[0]
        return text
    rid, jid, _ = prepare(env, monkeypatch, responder)
    execute(env, jid)
    with env.db() as db:
        value = db.get(m.ConsultationRun, rid).response
        assert value["narrative_markdown"] == text and value["grounding_status"] == "UNVERIFIED"
        assert value["citations"] == [] and value["quality_warnings"]


def test_late_read_revocation_blocks_delivery_not_just_the_button(env, monkeypatch):
    def responder(calls, _):
        if len(calls) == 1: return "待核对业务条件。"
        if len(calls) == 2: return "READ " + " ".join(re.findall(r"W\d+", calls[-1]))
        with env.db.begin() as db:
            resource = db.get(m.Resource, db.get(m.ResourceVersion, env.source).resource_id)
            resource.suspended = True
        return "## 模型正文[E1]"
    rid, jid, _ = prepare(env, monkeypatch, responder)
    with pytest.raises((JobError, svc.APIError)):
        execute(env, jid)
    with env.db() as db:
        assert db.get(m.ConsultationRun, rid).response is None


def test_additional_read_retains_previously_read_full_page(env, monkeypatch):
    extra = make_version(env, "document")
    with env.db.begin() as db:
        db.get(m.ResourceVersion, extra).title = "额外FOF核算全文"
    ids = []
    def responder(calls, _):
        if len(calls) == 1: return "核对全文后综合。"
        if len(calls) == 2:
            ids.extend(re.findall(r"(W\d+) \|", calls[-1]))
            return "READ " + ids[0]
        if len(calls) == 3:
            assert "# " + ids[0] in calls[-1]
            return "READ " + ids[-1]
        assert "# " + ids[0] in calls[-1] and "# " + ids[-1] in calls[-1]
        return "## 综合说明\n根据两份完整正文核对。[E1]"
    rid, jid, calls = prepare(env, monkeypatch, responder)
    execute(env, jid)
    with env.db() as db:
        assert db.get(m.ConsultationRun, rid).state == "COMPLETED"
        assert len(calls) == 4


def test_catalog_has_no_200_300_or_1000_page_cutoff():
    from uuid import uuid4
    from fund_kb.wiki_reader import pages_from_records, index_lines
    records = [{"resource_id":str(uuid4()),"version_id":str(uuid4()),"block_id":str(uuid4()),
        "title":f"完整知识{i:04d}","text":f"完整正文{i}","kind":"knowledge"} for i in range(1109)]
    pages = pages_from_records(records)
    assert len(pages) == 1109 and len(index_lines(pages)) == 1109
    assert "完整知识1108" in "\n".join(index_lines(pages))


@pytest.mark.parametrize("size", [4, 20, 71, 65536])
def test_transport_packets_are_lossless_not_source_truncation(size):
    value = "甲乙丙🙂\n" * 10001
    pieces = list(split_text(value, size))
    assert "".join(pieces) == value
    assert all(len(piece.encode()) <= size for piece in pieces)


def test_read_command_can_only_address_the_registered_catalog():
    pages = {"W1": {}, "W1008": {}}
    assert read_requests("READ W1 W1008 W999999; rm -rf /", pages) == ["W1", "W1008"]
    assert read_requests("普通最终答案", pages) == []
