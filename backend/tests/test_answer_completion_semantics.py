"""Provider-independent completion speech acts; synthetic text/evidence only.

The live diagnostic is a user-provided excerpt, not a claim that its entire
answer or the underlying bond valuation has been professionally verified.
"""
from __future__ import annotations

import copy
import socket
import sqlite3
from uuid import uuid4

import pytest
from test_retrieval_ai import record

from fund_kb import ai

LIVE_EXCERPT = (
    "本次提供的损益类/摊余成本类债券估值日计量相关节点为DRAFT(待核)状态，"
    "其内容不能作为已确认结论，仅可用于条件式框架说明。"
)
ROLES = ("assertion", "condition", "requirement")


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Completion semantics tests must not invoke a model, network or database")

    monkeypatch.setattr(ai, "post_json", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("text", [
    LIVE_EXCERPT,
    "其内容不能作为已确认结论。",
    "这些材料不足以证明审批已完成。",
    "该方案不等同于已取得审批。",
    "本答复不能被视为已经批准付款。",
    "资料不构成已确认的执行依据。",
    "本说明不意味着相关账务处理已经完成。",
    "尚不能证明本次由托管人办理的清算已执行。",
    "目前无法确认款项已付款。",
    "本助手没有声称已经执行付款。",
    "我不认为本次付款已经完成。",
    "不能把待复核资料当作已批准的操作方案。",
    "这不是已经完成审批的凭证。",
    "**其内容不能作为**已确认结论。",
])
def test_negative_disclaimers_do_not_assert_completed_actions(text, role):
    ai._check_grounded_text(text, "", completion_role=role)


@pytest.mark.parametrize("text", [
    "若审批已完成，则核对付款依据。",
    "如果已经取得审批，需核对凭据再办理。",
    "假设已确认行使回售权，应核对实际收款日期。",
    "仅当审批已通过，才可进入下一步。",
    "核对审批是否已经完成。",
    "需先确认托管复核已完成。",
    "请检查已经批准的方案是否适用于本次业务。",
    "先确认审批已通过，再办理付款。",
    "审批通过后再付款。",
    "已完成复核后方可执行付款。",
    "前提是已取得授权。",
    "本助手需要核对审批是否已完成。",
    "我需要确认款项是否已支付。",
])
def test_explicit_conditions_questions_and_normative_checks_are_not_reports(text):
    ai._check_grounded_text(text, "")


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("text", [
    "不是不表示任何账务核对已完成。",
    "并非不代表本次审批已经完成。",
    "不能说没有已经付款。",
    "不是没有已经完成付款。",
    "并非未完成付款。",
    "不是没有付款。",
    "不能否认已经完成过账。",
    "不得不承认已付款。",
    "不仅已经付款，而且已经过账。",
    "难道不是已经付款了吗？",
])
def test_double_negative_or_rhetorical_affirmation_is_not_a_disclaimer(text, role):
    with pytest.raises(ai.AnswerValidationError, match="EXECUTION_OR_APPROVAL_UNVERIFIED"):
        ai._check_grounded_text(text, "", completion_role=role)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("text", [
    "不能作为已确认结论，但已获批准。",
    "无法确认已付款，款项已过账。",
    "不表示任何账务核对已完成且已获批准。",
    "若审批已经完成，则款项已付款。",
    "假设已确认行使回售权；实际已完成收款。",
    "已完成审批。已执行付款。",
    "若已完成审批然后已过账。",
    "已取得授权，已经付款。",
    "前提：已获批准，不过已经执行付款。",
    "若已完成审批，并实际已完成付款。",
])
def test_later_positive_clause_is_checked_even_inside_typed_fields(text, role):
    with pytest.raises(ai.AnswerValidationError, match="EXECUTION_OR_APPROVAL_UNVERIFIED"):
        ai._check_grounded_text(text, "", completion_role=role)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("text", [
    "本助手已执行付款。", "我已付款。", "我们已经完成过账。", "已为你完成审批。",
    "我已经为您确认回售。", "如果本助手已经执行了付款，应进行核对。",
    "假设我已经付款。", "条件是本助手已批准该方案。", "我支付了款项。",
    "本助手需要核对审批是否完成，但已经过账。",
])
def test_assistant_cannot_claim_execution_even_in_a_condition_or_with_matching_source(text, role):
    with pytest.raises(ai.AnswerValidationError, match="EXECUTION_OR_APPROVAL_UNVERIFIED"):
        ai._check_grounded_text(text, text, completion_role=role)


@pytest.mark.parametrize("text", [
    "已付款。", "已经完成过账。", "确已支付款项。", "付款成功。", "审批通过。",
    "当前记录显示已获批准。", "账务已经复核。", "付款已完成。",
])
def test_positive_completed_actions_require_source_support(text):
    with pytest.raises(ai.AnswerValidationError, match="EXECUTION_OR_APPROVAL_UNVERIFIED"):
        ai._check_grounded_text(text, "应核对授权和付款凭据。")


@pytest.mark.parametrize("support", [
    "该说明不能作为已确认结论。", "必须核对是否已确认结论。", "若已确认结论，应保留复核记录。",
])
def test_negative_or_conditional_source_is_not_proof_of_positive_completion(support):
    with pytest.raises(ai.AnswerValidationError, match="EXECUTION_OR_APPROVAL_UNVERIFIED"):
        ai._check_grounded_text("已确认结论。", support)


def test_actual_source_record_can_support_report_but_not_invent_assistant_execution():
    support = "付款凭据记载已完成付款。"
    ai._check_grounded_text("凭据记载已完成付款。", support)
    with pytest.raises(ai.AnswerValidationError, match="EXECUTION_OR_APPROVAL_UNVERIFIED"):
        ai._check_grounded_text("本助手已完成付款。", support)


@pytest.mark.parametrize("role", ["condition", "requirement"])
@pytest.mark.parametrize("text", [
    "审批已完成", "已取得托管确认", "已获批准且凭据已核对", "核对结果已经记录且差异已经复核",
    "估值日处于回售登记日至实际收款日之间（已确认行使回售权）",
])
def test_typed_prerequisite_is_not_a_verified_fact(text, role):
    ai._check_grounded_text(text, "核对业务条件与原始凭据。", completion_role=role)
    with pytest.raises(ai.AnswerValidationError, match="EXECUTION_OR_APPROVAL_UNVERIFIED"):
        ai._check_grounded_text(text, "核对业务条件与原始凭据。")


def answer_fixture():
    evidence = record("办理业务前需核对授权、适用日期、审批、付款和过账凭据，保留独立复核记录。")
    answer = ai._envelope("如何核对业务流程", "solution", {}, str(uuid4()))
    answer.update(status="ANSWERED", summary="先核对授权与凭据，再按制度办理。", review_status="REQUIRES_EXPERT",
        claims=[{"id": "C1", "text": "应核对业务授权与凭据。", "evidence_ids": ["E1"]}],
        citations=[ai._citation(evidence, 1)], limitations=["以下是待执行方案，并不代表业务完成。"],
        analysis={"interpretation": "按业务流程核对条件。", "checks": [
            {"title": "核对依据", "reason": "需核对授权与凭据。", "evidence_ids": ["E1"]}],
            "branches": [{"condition": "资料齐备", "action": "核对授权。", "evidence_ids": ["E1"]}]},
        solution={"goal": "核对业务条件", "preconditions": ["资料齐备"], "materials": ["原始凭据"],
            "steps": [{"id": "S1", "action": "核对授权与凭据", "owner_role": "复核岗", "inputs": ["原始凭据"],
                "output": "核对记录", "verification": "凭据与记录一致", "evidence_ids": ["E1"], "depends_on": []}],
            "branches": [{"condition": "资料齐备", "action": "核对授权"}],
            "completion_checks": ["核对记录完整"], "escalation": ["异常提交独立复核"]})
    return answer, [evidence]


def replace_field(answer, path, value):
    item = answer
    for part in path[:-1]:
        item = item[part]
    item[path[-1]] = value


TYPED_FIELDS = [
    ("analysis", "branches", 0, "condition"), ("solution", "branches", 0, "condition"),
    ("solution", "preconditions", 0), ("solution", "completion_checks", 0), ("solution", "materials", 0),
    ("solution", "steps", 0, "verification"), ("solution", "steps", 0, "inputs", 0),
    ("solution", "steps", 0, "output"),
]
ASSERTION_FIELDS = [
    ("summary",), ("claims", 0, "text"), ("analysis", "interpretation"), ("analysis", "checks", 0, "reason"),
    ("analysis", "branches", 0, "action"), ("solution", "branches", 0, "action"), ("solution", "steps", 0, "action"),
]


@pytest.mark.parametrize("path", TYPED_FIELDS)
def test_full_validator_uses_field_semantics_without_mutating_the_answer(path):
    answer, evidence = answer_fixture()
    replace_field(answer, path, "审批已完成")
    before = copy.deepcopy(answer)
    ai.validate_answer(answer, evidence, mode="grounded")
    assert answer == before


@pytest.mark.parametrize("path", ASSERTION_FIELDS)
def test_full_validator_still_rejects_unverified_actual_execution(path):
    answer, evidence = answer_fixture()
    replace_field(answer, path, "审批已完成")
    with pytest.raises(ai.AnswerValidationError, match="EXECUTION_OR_APPROVAL_UNVERIFIED"):
        ai.validate_answer(answer, evidence, mode="grounded")


@pytest.mark.parametrize("path", TYPED_FIELDS)
@pytest.mark.parametrize("text", ["本助手已付款", "审批已完成，但款项已经过账", "并非没有已经付款"])
def test_typed_fields_cannot_launder_self_reports_consequences_or_double_negatives(path, text):
    answer, evidence = answer_fixture()
    replace_field(answer, path, text)
    with pytest.raises(ai.AnswerValidationError, match="EXECUTION_OR_APPROVAL_UNVERIFIED"):
        ai.validate_answer(answer, evidence, mode="grounded")


@pytest.mark.parametrize("path", [*TYPED_FIELDS, *ASSERTION_FIELDS, ("limitations", 0)])
def test_reported_live_disclaimer_passes_full_validator_in_each_text_field(path):
    answer, evidence = answer_fixture()
    replace_field(answer, path, LIVE_EXCERPT)
    ai.validate_answer(answer, evidence, mode="grounded")


@pytest.mark.parametrize("path", TYPED_FIELDS)
@pytest.mark.parametrize("text,code", [
    ("已取得审批且付款为120万元", "NUMERIC_UNIT_SUPPORT_MISSING"),
    ("已取得审批，token=synthetic_secret_canary", "SENSITIVE_MODEL_OUTPUT"),
    ("已取得审批<script>run()</script>", "UNSAFE_MODEL_OUTPUT"),
])
def test_typed_exemptions_never_skip_numeric_secret_or_unsafe_checks(path, text, code):
    answer, evidence = answer_fixture()
    replace_field(answer, path, text)
    with pytest.raises(ai.AnswerValidationError, match=code):
        ai.validate_answer(answer, evidence, mode="grounded")


@pytest.mark.parametrize("change", ["citation_hash", "source_hash", "source_unverified", "unknown_evidence_id"])
def test_execution_semantics_never_bypass_evidence_and_hash_validation(change):
    answer, evidence = answer_fixture()
    answer["solution"]["preconditions"] = ["已完成审批"]
    if change == "citation_hash":
        answer["citations"][0]["content_sha256"] = "0" * 64
    elif change == "source_hash":
        evidence[0]["content_sha256"] = "0" * 64
    elif change == "source_unverified":
        evidence[0]["source_verified"] = False
    else:
        answer["analysis"]["branches"][0]["evidence_ids"] = ["E999"]
    with pytest.raises(ai.AnswerValidationError):
        ai.validate_answer(answer, evidence, mode="grounded")


def test_legacy_condition_flag_still_checks_later_assertions_and_quantities():
    ai._check_grounded_text("已确认行使回售权", "", completion=False)
    with pytest.raises(ai.AnswerValidationError, match="EXECUTION_OR_APPROVAL_UNVERIFIED"):
        ai._check_grounded_text("已确认行使回售权，但已完成付款", "", completion=False)
    with pytest.raises(ai.AnswerValidationError, match="NUMERIC_UNIT_SUPPORT_MISSING"):
        ai._check_grounded_text("回售后7日", "", completion=False)


def test_unknown_field_role_fails_closed():
    with pytest.raises(ValueError, match="INVALID_COMPLETION_ROLE"):
        ai._check_grounded_text("已付款", "", completion_role="skip")
