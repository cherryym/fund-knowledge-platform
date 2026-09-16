"""Offline regression coverage for resolved OAuth answer callbacks; no credentials or DB."""
from __future__ import annotations

import copy
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from fund_kb import ai
from fund_kb.ai_transport import ProviderError
from fund_kb.ingestion import block_text, text_sha256

QUESTION = "基金合同如何核对"
MODEL = "synthetic-codex-model"
SUMMARY = "先核对基金合同版本，再记录业务日期核对结果。"
REFERENCE_NOTICE = "资料辅助答疑：包含未核验/未发布资料，仅供参考，不代表现行制度或正式业务结论。"


class CallbackSettings(SimpleNamespace):
    @property
    def llm_api_key(self):
        raise AssertionError("OAuth callback must not read an API key")


def settings(**overrides):
    # Mirror the worker's settings derived from an already-resolved OAuth snapshot.
    snapshot = {"protocol": "codex_app_server", "base_url": "", "model_id": MODEL}
    return CallbackSettings(**({
        "llm_provider": "http", "llm_base_url": snapshot["base_url"], "llm_model": snapshot["model_id"],
    } | overrides))


def source_record(**overrides):
    data = {
        "action": "核对基金合同版本与业务日期", "owner_role": "运营复核岗",
        "output": "基金合同核对记录", "verification": "核对记录与基金合同一致",
    }
    text = block_text({"block_type": "step", "data": data})
    return {
        "resource_id": str(uuid4()), "version_id": str(uuid4()), "block_id": str(uuid4()),
        "title": "合成基金合同核对SOP", "text": text, "data": data, "block_type": "step",
        "knowledge_type": "sop", "source_verified": True, "ordinal": 0, "version_step_ordinals": [0],
        "required_facts": [], "applicability": {}, "locator": {"label": "合成核对步骤"},
        "content_sha256": text_sha256(text), **overrides,
    }


def completion_response(payload):
    request = json.loads(payload["messages"][1]["content"])
    candidate = request["output_skeleton"]
    candidate.update(summary=SUMMARY, review_status="EXPERT_REVIEWED",
                     run_id=str(uuid4()), limitations=["合成回调结果，尚需专业复核。"])
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(candidate, ensure_ascii=False),
    }}]}


@pytest.fixture(autouse=True)
def forbid_external_calls(monkeypatch):
    transport = Mock(side_effect=AssertionError("Unexpected direct HTTP request"))
    monkeypatch.setattr(ai, "post_json", transport)
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("Unexpected network")))
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("Unexpected network")))
    yield transport
    transport.assert_not_called()


@pytest.mark.parametrize("mode", ["answer", "auto", "solution"])
def test_oauth_empty_base_url_uses_callback_and_real_grounded_validation(mode, monkeypatch):
    source = source_record()
    original = copy.deepcopy(source)
    run_id = str(uuid4())
    callback = Mock(side_effect=completion_response)
    validator = Mock(wraps=ai.validate_answer)
    monkeypatch.setattr(ai, "validate_answer", validator)

    answer = ai.generate_answer(QUESTION, mode, {}, [source], settings(), run_id,
                                completion_client=callback)

    callback.assert_called_once()
    payload = callback.call_args.args[0]
    request = json.loads(payload["messages"][1]["content"])
    assert payload["model"] == MODEL
    assert payload["stream"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert "tools" not in payload
    assert request["question"] == QUESTION and request["context"] == {}
    assert [record["block_id"] for record in request["evidence"]] == [source["block_id"]]
    assert request["evidence"][0]["content_sha256"] == source["content_sha256"]
    validator.assert_called_once_with(answer, [source], mode="grounded", context={}, answer_scope="formal")
    assert answer["status"] == "ANSWERED" and answer["summary"] == SUMMARY
    assert answer["mode"] == ("answer" if mode == "auto" else mode)
    assert answer["run_id"] == run_id
    assert answer["generated_at"] == request["output_skeleton"]["generated_at"]
    assert answer["review_status"] == "REQUIRES_EXPERT"
    assert not any("MODEL_NOT_CONFIGURED" in item for item in answer["limitations"])
    assert source == original


@pytest.mark.parametrize("base_url,model", [
    ("", ""), (None, None), ("", MODEL), (None, MODEL),
    ("https://example.invalid/v1", ""), ("https://example.invalid/v1", None),
])
def test_http_without_callback_still_requires_url_and_model(base_url, model):
    source = source_record()
    answer = ai.generate_answer(QUESTION, "answer", {}, [source],
                                settings(llm_base_url=base_url, llm_model=model), str(uuid4()))

    assert any("MODEL_NOT_CONFIGURED" in item for item in answer["limitations"])
    assert answer["summary"] != SUMMARY
    assert answer["claims"][0]["text"] == source["text"]
    ai.validate_answer(answer, [source])


@pytest.mark.parametrize("model", ["", None])
def test_callback_still_requires_model(model):
    callback = Mock(side_effect=AssertionError("Unconfigured model must not be called"))
    answer = ai.generate_answer(QUESTION, "answer", {}, [source_record()], settings(llm_model=model),
                                str(uuid4()), completion_client=callback)

    callback.assert_not_called()
    assert any("MODEL_NOT_CONFIGURED" in item for item in answer["limitations"])


@pytest.mark.parametrize("mode", ["answer", "solution"])
@pytest.mark.parametrize("case", ["empty", "bad_hash", "unverified", "inapplicable"])
def test_callback_is_not_called_without_usable_evidence(mode, case):
    evidence = {
        "empty": [],
        "bad_hash": [source_record(content_sha256="0" * 64)],
        "unverified": [source_record(knowledge_type="source", source_verified=False)],
        "inapplicable": [source_record(valid_to="2026-09-01")],
    }[case]
    context = {"business_date": "2026-09-08"}
    callback = Mock(side_effect=AssertionError("No usable evidence must not call a model"))

    answer = ai.generate_answer(QUESTION, mode, context, evidence, settings(), str(uuid4()),
                                completion_client=callback)

    callback.assert_not_called()
    assert answer["status"] == "INSUFFICIENT_EVIDENCE"
    assert not answer["citations"] and answer["required_sources"]
    ai.validate_answer(answer, evidence)


@pytest.mark.parametrize("mode", ["answer", "solution"])
@pytest.mark.parametrize("metadata,status", [
    ({"required_facts": ["business_date"]}, "NEEDS_CLARIFICATION"),
    ({"conflict_group": "synthetic-conflict"}, "CONFLICT"),
])
def test_callback_is_not_called_before_clarification_or_conflict_resolution(mode, metadata, status):
    source = source_record(**metadata)
    callback = Mock(side_effect=AssertionError("Unresolved evidence must not call a model"))

    answer = ai.generate_answer(QUESTION, mode, {}, [source], settings(), str(uuid4()),
                                completion_client=callback)

    callback.assert_not_called()
    assert answer["status"] == status
    if status == "NEEDS_CLARIFICATION":
        assert [item["field"] for item in answer["missing_facts"]] == ["business_date"]
    ai.validate_answer(answer, [source])


@pytest.mark.parametrize("tamper,code", [
    (lambda a: a["citations"][0].update(content_sha256="0" * 64), "CITATION_PROVENANCE_INVALID"),
    (lambda a: a["claims"][0].update(text="基金管理费率为0.99%。"), "NUMERIC_UNIT_SUPPORT_MISSING"),
    (lambda a: a["claims"][0].update(evidence_ids=["unknown"]), "CLAIM_CITATION_MISSING"),
])
def test_oauth_callback_output_cannot_bypass_answer_validation(tamper, code):
    source = source_record()

    def invalid_response(payload):
        raw = completion_response(payload)
        message = raw["choices"][0]["message"]
        candidate = json.loads(message["content"])
        tamper(candidate)
        message["content"] = json.dumps(candidate, ensure_ascii=False)
        return raw

    callback = Mock(side_effect=invalid_response)
    answer = ai.generate_answer(QUESTION, "answer", {}, [source], settings(), str(uuid4()),
                                completion_client=callback)

    callback.assert_called_once()
    assert any(code in item for item in answer["limitations"])
    assert answer["summary"] != SUMMARY
    assert answer["claims"][0]["text"] == source["text"]
    assert answer["citations"][0]["content_sha256"] == source["content_sha256"]
    ai.validate_answer(answer, [source])


@pytest.mark.parametrize("finish_reason,tool_calls,code", [
    ("length", None, "MODEL_OUTPUT_TRUNCATED_OR_TOOL_REQUESTED"),
    ("stop", [{"id": "synthetic-tool-call"}], "MODEL_TOOL_CALL_FORBIDDEN"),
])
def test_oauth_callback_rejects_truncation_and_tool_requests(finish_reason, tool_calls, code):
    source = source_record()

    def invalid_response(payload):
        raw = completion_response(payload)
        raw["choices"][0]["finish_reason"] = finish_reason
        raw["choices"][0]["message"]["tool_calls"] = tool_calls
        return raw

    callback = Mock(side_effect=invalid_response)
    answer = ai.generate_answer(QUESTION, "answer", {}, [source], settings(), str(uuid4()),
                                completion_client=callback)

    callback.assert_called_once()
    assert any(code in item for item in answer["limitations"])
    assert answer["summary"] != SUMMARY
    ai.validate_answer(answer, [source])


def test_oauth_callback_failure_falls_back_without_direct_http_retry():
    source = source_record()
    callback = Mock(side_effect=ProviderError("CODEX_TEXT_ISOLATION_UNVERIFIED"))

    answer = ai.generate_answer(QUESTION, "answer", {}, [source], settings(), str(uuid4()),
                                completion_client=callback)

    callback.assert_called_once()
    assert any("CODEX_TEXT_ISOLATION_UNVERIFIED" in item for item in answer["limitations"])
    assert answer["claims"][0]["text"] == source["text"]
    ai.validate_answer(answer, [source])


def reference_source(**overrides):
    return source_record(**({
        "kind": "document", "source_verified": False, "evidence_scope": "reference",
        "state": "DRAFT", "legal_status": "UNKNOWN",
    } | overrides))


def assert_reference_answer(answer, evidence, *, mode="extractive"):
    assert answer["review_status"] == "REQUIRES_EXPERT"
    assert answer["limitations"][0] == REFERENCE_NOTICE
    assert answer["limitations"].count(REFERENCE_NOTICE) == 1
    ai.validate_answer(answer, evidence, mode=mode, answer_scope="reference")


@pytest.mark.parametrize("mode", ["answer", "auto", "solution"])
@pytest.mark.parametrize("source_verified", [False, True])
def test_formal_scope_cannot_use_reference_markers_even_for_verified_records(mode, source_verified):
    source = reference_source(source_verified=source_verified)
    original = copy.deepcopy(source)
    callback = Mock(side_effect=AssertionError("Formal scope cannot use reference-only material"))

    for scope_args in ({}, {"answer_scope": "formal"}):
        answer = ai.generate_answer(QUESTION, mode, {}, [source], settings(), str(uuid4()),
                                    completion_client=callback, **scope_args)
        assert answer["status"] == "INSUFFICIENT_EVIDENCE"
        assert not answer["citations"]
        ai.validate_answer(answer, [source])
    callback.assert_not_called()
    assert ai._usable_records([source]) == []
    assert source == original


@pytest.mark.parametrize("mode", ["answer", "auto", "solution"])
def test_reference_callback_preserves_unverified_citations_metadata_and_server_notice(mode):
    source = reference_source()
    original = copy.deepcopy(source)
    callback = Mock(side_effect=completion_response)
    run_id = str(uuid4())

    answer = ai.generate_answer(QUESTION, mode, {}, [source], settings(), run_id,
                                completion_client=callback, answer_scope="reference")

    callback.assert_called_once()
    payload = callback.call_args.args[0]
    request = json.loads(payload["messages"][1]["content"])
    system = payload["messages"][0]["content"]
    assert REFERENCE_NOTICE in system and "answer_scope=reference" in system
    assert request["answer_scope"] == "reference"
    record = request["evidence"][0]
    assert record["source_verified"] is False and record["evidence_scope"] == "reference"
    assert record["draft"] is True and record["state"] == "DRAFT" and record["legal_status"] == "UNKNOWN"
    assert record["text"] == source["text"]
    assert answer["status"] == "ANSWERED" and answer["summary"] == SUMMARY
    assert answer["run_id"] == run_id
    citation = answer["citations"][0]
    for field in ("resource_id", "version_id", "block_id", "content_sha256", "locator"):
        assert citation[field] == source[field]
    assert citation["excerpt"] == source["text"] and citation["source_title"] == source["title"]
    assert_reference_answer(answer, [source], mode="grounded")
    assert source == original
    # A reference-generated answer must not become valid merely by switching the verifier to formal.
    with pytest.raises(ai.AnswerValidationError, match="CITATION_PROVENANCE_INVALID"):
        ai.validate_answer(answer, [source], mode="grounded")


@pytest.mark.parametrize("metadata,expected", [
    ({"draft": "original-draft-marker", "state": "IN_REVIEW", "legal_status": "UNKNOWN"},
     {"draft": "original-draft-marker", "state": "IN_REVIEW", "legal_status": "UNKNOWN"}),
    ({"state": None, "legal_status": None}, {"draft": None, "state": None, "legal_status": None}),
])
def test_reference_prompt_does_not_promote_or_replace_supplied_metadata(metadata, expected):
    source = reference_source(**metadata)
    callback = Mock(side_effect=completion_response)
    answer = ai.generate_answer(QUESTION, "answer", {}, [source], settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")

    callback.assert_called_once()
    record = json.loads(callback.call_args.args[0]["messages"][1]["content"])["evidence"][0]
    assert {key: record[key] for key in expected} == expected
    assert record["source_verified"] is False
    assert_reference_answer(answer, [source], mode="grounded")


def test_reference_missing_metadata_stays_unknown_in_model_context():
    source = reference_source(knowledge_type="source")
    del source["state"], source["legal_status"]
    original = copy.deepcopy(source)
    callback = Mock(side_effect=completion_response)
    answer = ai.generate_answer(QUESTION, "answer", {}, [source], settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")

    callback.assert_called_once()
    record = json.loads(callback.call_args.args[0]["messages"][1]["content"])["evidence"][0]
    assert record["draft"] is None and record["state"] == "UNKNOWN" and record["legal_status"] == "UNKNOWN"
    assert record["source_verified"] is False
    assert answer["summary"] == SUMMARY
    assert_reference_answer(answer, [source], mode="grounded")
    assert source == original


@pytest.mark.parametrize("answer_scope", ["formal", "reference"])
def test_scope_controls_which_mixed_evidence_is_sent_to_callback(answer_scope):
    formal_source = source_record()
    draft_source = reference_source()
    evidence = [formal_source, draft_source]
    original = copy.deepcopy(evidence)
    callback = Mock(side_effect=completion_response)

    answer = ai.generate_answer(QUESTION, "answer", {}, evidence, settings(), str(uuid4()),
                                completion_client=callback, answer_scope=answer_scope)

    callback.assert_called_once()
    request = json.loads(callback.call_args.args[0]["messages"][1]["content"])
    expected = evidence if answer_scope == "reference" else [formal_source]
    expected_ids = [source["block_id"] for source in expected]
    assert [source["block_id"] for source in request["evidence"]] == expected_ids
    assert [citation["block_id"] for citation in answer["citations"]] == expected_ids
    assert answer["summary"] == SUMMARY
    ai.validate_answer(answer, evidence, mode="grounded", answer_scope=answer_scope)
    if answer_scope == "reference":
        assert_reference_answer(answer, evidence, mode="grounded")
    assert evidence == original


@pytest.mark.parametrize("mode", ["answer", "solution"])
@pytest.mark.parametrize("route", ["evidence", "unconfigured_http", "callback_failure", "invalid_callback"])
def test_reference_notice_and_source_validation_survive_all_extractive_paths(mode, route):
    source = reference_source()
    config = settings()
    callback = Mock(side_effect=AssertionError("Unexpected callback"))
    if route == "evidence":
        config.llm_provider = "evidence"
    elif route == "unconfigured_http":
        callback = None
    elif route == "callback_failure":
        callback.side_effect = ProviderError("CODEX_TEXT_ISOLATION_UNVERIFIED")
    else:
        def invalid_response(payload):
            raw = completion_response(payload)
            candidate = json.loads(raw["choices"][0]["message"]["content"])
            candidate["citations"][0]["content_sha256"] = "0" * 64
            raw["choices"][0]["message"]["content"] = json.dumps(candidate, ensure_ascii=False)
            return raw
        callback.side_effect = invalid_response

    answer = ai.generate_answer(QUESTION, mode, {}, [source], config, str(uuid4()),
                                completion_client=callback, answer_scope="reference")

    assert answer["status"] == "ANSWERED"
    assert answer["citations"][0]["excerpt"] == source["text"]
    assert_reference_answer(answer, [source])
    if route in {"callback_failure", "invalid_callback"}:
        callback.assert_called_once()
        assert not any("未调用生成模型" in item for item in answer["limitations"])
        code = "CODEX_TEXT_ISOLATION_UNVERIFIED" if route == "callback_failure" else "CITATION_PROVENANCE_INVALID"
        assert any(code in item for item in answer["limitations"])
    elif route == "evidence":
        callback.assert_not_called()
    else:
        assert any("MODEL_NOT_CONFIGURED" in item for item in answer["limitations"])


@pytest.mark.parametrize("mode", ["answer", "solution"])
@pytest.mark.parametrize("case,status", [
    ("empty", "INSUFFICIENT_EVIDENCE"), ("bad_hash", "INSUFFICIENT_EVIDENCE"),
    ("unmarked", "INSUFFICIENT_EVIDENCE"), ("clarification", "NEEDS_CLARIFICATION"),
    ("conflict", "CONFLICT"),
])
def test_reference_scope_keeps_preflight_gates_and_never_calls_without_eligible_evidence(mode, case, status):
    evidence = {
        "empty": [], "bad_hash": [reference_source(content_sha256="0" * 64)],
        "unmarked": [reference_source(evidence_scope="formal")],
        "clarification": [reference_source(required_facts=["business_date"])],
        "conflict": [reference_source(conflict_group="synthetic-conflict")],
    }[case]
    callback = Mock(side_effect=AssertionError("Reference mode must retain preflight gates"))

    answer = ai.generate_answer(QUESTION, mode, {}, evidence, settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")

    callback.assert_not_called()
    assert answer["status"] == status
    assert_reference_answer(answer, evidence)


@pytest.mark.parametrize("case", ["unrelated", "unsafe", "inapplicable", "unmarked"])
def test_reference_marker_does_not_bypass_content_or_applicability_checks(case):
    source = reference_source()
    if case in {"unrelated", "unsafe"}:
        text = "仓库库存盘点。" if case == "unrelated" else "基金合同<script>bad()</script>"
        source.update(text=text, data={"text": text}, block_type="paragraph", title="合成测试资料",
                      content_sha256=text_sha256(text))
    elif case == "inapplicable":
        source["applicability"] = {"all": [{"field": "share_class", "op": "eq", "values": ["C"]}]}
    else:
        del source["evidence_scope"]
    callback = Mock(side_effect=AssertionError("Ineligible reference evidence must not call a model"))

    answer = ai.generate_answer(QUESTION, "answer", {"share_class": "A"}, [source], settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")

    callback.assert_not_called()
    assert answer["status"] == "INSUFFICIENT_EVIDENCE" and not answer["citations"]
    assert_reference_answer(answer, [source])


def test_reference_solution_preflight_propagates_scope_for_non_sop_source():
    source = reference_source(knowledge_type="source", required_facts=["business_date"])
    callback = Mock(side_effect=AssertionError("Clarification must not call a model"))
    answer = ai.generate_answer(QUESTION, "solution", {}, [source], settings(llm_provider="evidence"),
                                str(uuid4()), completion_client=callback, answer_scope="reference")

    callback.assert_not_called()
    assert answer["status"] == "NEEDS_CLARIFICATION"
    assert_reference_answer(answer, [source])


@pytest.mark.parametrize("tamper", [
    lambda a: a.update(review_status="MACHINE_CHECKED"),
    lambda a: a.update(review_status="EXPERT_REVIEWED"),
    lambda a: a.update(limitations=[]),
    lambda a: a["limitations"].reverse(),
])
def test_reference_validator_rejects_removed_or_downgraded_safeguards(tamper):
    source = reference_source()
    answer = ai.generate_answer(QUESTION, "answer", {}, [source], settings(llm_provider="evidence"),
                                str(uuid4()), answer_scope="reference")
    tamper(answer)

    with pytest.raises(ai.AnswerValidationError, match="REFERENCE_SAFEGUARDS_REQUIRED"):
        ai.validate_answer(answer, [source], answer_scope="reference")


@pytest.mark.parametrize("answer_scope", [None, "", "REFERENCE", "draft", True, ["reference"]])
def test_invalid_answer_scope_is_rejected_before_any_callback(answer_scope):
    callback = Mock(side_effect=AssertionError("Invalid answer scope must not call a model"))
    with pytest.raises(ValueError, match="INVALID_ANSWER_SCOPE"):
        ai.generate_answer(QUESTION, "answer", {}, [reference_source()], settings(), str(uuid4()),
                           completion_client=callback, answer_scope=answer_scope)
    callback.assert_not_called()
    with pytest.raises(ValueError, match="INVALID_ANSWER_SCOPE"):
        ai._usable_records([], answer_scope=answer_scope)
    with pytest.raises(ValueError, match="INVALID_ANSWER_SCOPE"):
        ai.validate_answer({}, [], answer_scope=answer_scope)


@pytest.mark.parametrize("summary", [
    "1. 核对基金合同版本与业务日期。", "2) 核对基金合同版本与业务日期。",
    "本回答不代表已完成审批。", "不能视为已发布。",
    "也不表示任何账务核对已完成。", "本答复不意味着本次清算已执行。",
])
def test_reference_formatting_and_explicit_disclaimers_do_not_force_fallback(summary):
    source = reference_source()

    def reply(payload):
        answer = json.loads(payload["messages"][1]["content"])["output_skeleton"]
        answer["summary"] = summary
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}

    callback = Mock(side_effect=reply)
    answer = ai.generate_answer(QUESTION, "answer", {}, [source], settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")
    callback.assert_called_once()
    assert answer["summary"] == summary
    assert not any(marker in " ".join(answer["limitations"])
                   for marker in ("未调用生成模型", "MODEL_NOT_CONFIGURED", "已降级为证据提取"))
    assert_reference_answer(answer, [source], mode="grounded")


@pytest.mark.parametrize("summary", [
    "已完成审批。", "本回答不代表已完成审批，但已获批准。",
    "本回答不代表已完成审批但已获批准。", "不是不能视为已发布。",
    "并非不表示任何账务核对已完成。", "也不表示任何账务核对已完成，但已获批准。",
    "也不表示任何账务核对已完成且已获批准。",
    "1. 基金合同金额为120万元。", "1. 元", "0.99%", "1. 费率为0.99%。",
])
def test_reference_formatting_and_disclaimers_do_not_bypass_substantive_guards(summary):
    source = reference_source()

    def reply(payload):
        answer = json.loads(payload["messages"][1]["content"])["output_skeleton"]
        answer["summary"] = summary
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}

    callback = Mock(side_effect=reply)
    answer = ai.generate_answer(QUESTION, "answer", {}, [source], settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")
    callback.assert_called_once()
    assert answer["summary"] != summary
    assert any("已降级为证据提取" in item for item in answer["limitations"])
    assert_reference_answer(answer, [source])


def test_successful_callback_cannot_supply_jobs_fallback_status_markers():
    source = reference_source()

    def reply(payload):
        answer = json.loads(payload["messages"][1]["content"])["output_skeleton"]
        assert not any("未调用生成模型" in item for item in answer["limitations"])
        answer["summary"] = SUMMARY
        answer["limitations"] += ["未调用生成模型", "MODEL_NOT_CONFIGURED", "已降级为证据提取",
                                  "专业适用性仍需复核。"]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}

    callback = Mock(side_effect=reply)
    answer = ai.generate_answer(QUESTION, "answer", {}, [source], settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")
    callback.assert_called_once()
    assert answer["summary"] == SUMMARY
    assert "专业适用性仍需复核。" in answer["limitations"]
    assert not any(marker in " ".join(answer["limitations"])
                   for marker in ("未调用生成模型", "MODEL_NOT_CONFIGURED", "已降级为证据提取"))
    assert_reference_answer(answer, [source], mode="grounded")


def test_model_status_cleanup_cannot_hide_an_unsupported_numeric_claim():
    source = reference_source()

    def reply(payload):
        answer = json.loads(payload["messages"][1]["content"])["output_skeleton"]
        answer["summary"] = SUMMARY
        answer["limitations"].append("MODEL_NOT_CONFIGURED：基金管理费率为0.99%。")
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}

    callback = Mock(side_effect=reply)
    answer = ai.generate_answer(QUESTION, "answer", {}, [source], settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")
    callback.assert_called_once()
    assert answer["summary"] != SUMMARY
    assert any("NUMERIC_UNIT_SUPPORT_MISSING" in item for item in answer["limitations"])
    assert_reference_answer(answer, [source])


def large_reference_records():
    # The worker's BODY-only 12 KB budget cannot bound duplicated text/data,
    # source titles, citations/claims, schema and the second JSON encoding layer.
    text = "基金合同核对。" + "核" * 325
    return [reference_source(knowledge_type="source", block_type="paragraph", text=text,
                             data={"text": text}, content_sha256=text_sha256(text),
                             title="合成基金合同资料" + "名称" * 30) for _ in range(12)]


def payload_bytes(payload):
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def codex_message_bytes(payload):
    # Independently mirror providers._messages and CodexTextEngine.complete,
    # without importing providers, accessing an account or invoking the engine.
    messages = [{"role": "system", "content":
                 "Return a valid JSON object only. No markdown fences or tool calls."}, *payload["messages"]]
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


def test_large_reference_prunes_whole_blocks_to_actual_utf8_payload_budget(monkeypatch):
    evidence = large_reference_records()
    original = copy.deepcopy(evidence)
    assert sum(len(record["text"].encode("utf-8")) for record in evidence) <= 12000
    unbounded_callback = Mock(side_effect=completion_response)
    with monkeypatch.context() as permissive:
        permissive.setattr(ai, "_REFERENCE_REQUEST_BUDGET_BYTES", 1000000)
        ai.generate_answer(QUESTION, "answer", {}, evidence, settings(), str(uuid4()),
                           completion_client=unbounded_callback, answer_scope="reference")
    unbounded_callback.assert_called_once()
    assert codex_message_bytes(unbounded_callback.call_args.args[0]) > 65536

    baseline_builder = Mock(wraps=ai._evidence_answer)
    monkeypatch.setattr(ai, "_evidence_answer", baseline_builder)
    callback = Mock(side_effect=completion_response)
    answer = ai.generate_answer(QUESTION, "answer", {}, evidence, settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")

    callback.assert_called_once()
    payload = callback.call_args.args[0]
    request = json.loads(payload["messages"][1]["content"])
    retained = request["evidence"]
    assert 1 <= len(retained) < len(evidence)
    assert 1 < baseline_builder.call_count <= len(evidence)
    assert payload_bytes(payload) <= 60000
    assert codex_message_bytes(payload) < 65536
    assert [record["block_id"] for record in retained] == [record["block_id"] for record in evidence[:len(retained)]]
    assert [c["block_id"] for c in answer["citations"]] == [r["block_id"] for r in retained]
    for sent, original_record, citation in zip(retained, evidence, answer["citations"], strict=False):
        assert sent["text"] == original_record["text"] == citation["excerpt"]
        assert sent["content_sha256"] == original_record["content_sha256"] == citation["content_sha256"]
        assert sent["data"] == original_record["data"]
        assert sent["source_verified"] is False
    assert answer["summary"] == SUMMARY
    assert_reference_answer(answer, evidence[:len(retained)], mode="grounded")
    assert evidence == original


@pytest.mark.parametrize("oversize", ["single_block", "all_blocks", "question"])
def test_oversized_reference_request_fails_closed_without_any_callback(oversize, monkeypatch):
    evidence = [reference_source()]
    question = QUESTION
    if oversize == "question":
        question = "基金合同" * 4900  # Valid question length, but the whole UTF-8 request cannot fit.
    else:
        text = "基金合同核对。" + "核" * 20000
        evidence = [reference_source(knowledge_type="source", block_type="paragraph", text=text,
                                     data={"text": text}, content_sha256=text_sha256(text))
                    for _ in range(3 if oversize == "all_blocks" else 1)]
    original = copy.deepcopy(evidence)
    baseline_builder = Mock(wraps=ai._evidence_answer)
    monkeypatch.setattr(ai, "_evidence_answer", baseline_builder)
    callback = Mock(side_effect=AssertionError("Oversized payload must not call a model"))

    answer = ai.generate_answer(question, "answer", {}, evidence, settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")

    callback.assert_not_called()
    assert baseline_builder.call_count <= len(evidence)
    assert any("MODEL_CONTEXT_BUDGET_EXCEEDED" in item for item in answer["limitations"])
    assert [c["block_id"] for c in answer["citations"]] == [evidence[0]["block_id"]]
    assert answer["citations"][0]["excerpt"] == evidence[0]["text"][:1600]
    assert_reference_answer(answer, evidence[:1])
    assert evidence == original


@pytest.mark.parametrize("failure", ["removed_citation", "provider_failure"])
def test_pruned_reference_cannot_restore_removed_evidence_on_validation_or_fallback(failure):
    evidence = large_reference_records()

    def reply(payload):
        assert payload_bytes(payload) <= 60000
        request = json.loads(payload["messages"][1]["content"])
        assert evidence[-1]["block_id"] not in {record["block_id"] for record in request["evidence"]}
        if failure == "provider_failure":
            raise ProviderError("SYNTHETIC_PROVIDER_FAILURE")
        answer = request["output_skeleton"]
        answer["summary"] = SUMMARY
        answer["citations"].append(ai._citation(evidence[-1], len(answer["citations"]) + 1))
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}

    callback = Mock(side_effect=reply)
    answer = ai.generate_answer(QUESTION, "answer", {}, evidence, settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")

    callback.assert_called_once()
    retained = json.loads(callback.call_args.args[0]["messages"][1]["content"])["evidence"]
    assert [c["block_id"] for c in answer["citations"]] == [record["block_id"] for record in retained]
    code = "CITATION_PROVENANCE_INVALID" if failure == "removed_citation" else "SYNTHETIC_PROVIDER_FAILURE"
    assert any(code in item for item in answer["limitations"])
    assert_reference_answer(answer, evidence[:len(retained)])


def test_large_reference_solution_rebuilds_partial_sop_fallback_from_retained_blocks_only():
    evidence = large_reference_records()
    version_id, resource_id = str(uuid4()), str(uuid4())
    for index, record in enumerate(evidence):
        data = {"action": record["text"], "owner_role": "运营复核岗",
                "output": "核对记录", "verification": "记录与基金合同一致"}
        text = block_text({"block_type": "step", "data": data})
        record.update(knowledge_type="sop", block_type="step", data=data, text=text,
                      content_sha256=text_sha256(text), ordinal=index, version_step_ordinals=list(range(12)),
                      version_id=version_id, resource_id=resource_id)
    original = copy.deepcopy(evidence)
    callback = Mock(side_effect=ProviderError("SYNTHETIC_PROVIDER_FAILURE"))

    answer = ai.generate_answer(QUESTION, "solution", {}, evidence, settings(), str(uuid4()),
                                completion_client=callback, answer_scope="reference")

    callback.assert_called_once()
    payload = callback.call_args.args[0]
    retained = json.loads(payload["messages"][1]["content"])["evidence"]
    assert payload_bytes(payload) <= 60000 and codex_message_bytes(payload) < 65536
    assert 1 <= len(retained) < len(evidence)
    assert answer["status"] == "INSUFFICIENT_EVIDENCE"
    assert [c["block_id"] for c in answer["citations"]] == [r["block_id"] for r in retained]
    assert [step["action"] for step in answer["solution"]["steps"]] == [r["data"]["action"] for r in retained]
    assert any("SYNTHETIC_PROVIDER_FAILURE" in item for item in answer["limitations"])
    assert_reference_answer(answer, evidence[:len(retained)])
    assert evidence == original
