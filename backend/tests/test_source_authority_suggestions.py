"""Prefill is a read-only proposal, never implicit confirmation or source repair."""
import copy

import pytest
from sqlalchemy import func, select
from test_reference_review import make_version
from test_source_authority import (
    base_env,  # noqa: F401
    context,
    create,
    setup_sources,
)
from test_source_authority import env as env  # noqa: PLC0414

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb import source_authority as authority
from fund_kb.ingestion import text_sha256
from fund_kb.source_authority_suggestions import extract_facts, suggestions


def rows(*texts):
    return [{"text": text, "ordinal": n, "version_id": "test-version", "block_id": f"test-block-{n}", "locator": {"page": 1}}
        for n, text in enumerate(texts)]


def proof(env, data, *texts):
    with env.db.begin() as db:
        vid = data["evidence_version_id"]
        existing = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == vid))
        existing.data = {"text": texts[0]}; existing.search_text = texts[0]; existing.content_sha256 = text_sha256(texts[0])
        for index, text in enumerate(texts[1:], 1):
            db.add(m.ContentBlock(version_id=vid, block_id=svc.uid(), ordinal=index, block_type="paragraph",
                data={"text": text}, search_text=text, content_sha256=text_sha256(text), locator={"paragraph": index + 1}))


def result(env, user=None):
    with env.db() as db:
        return suggestions(db, user or env.owner, env.space)


def ready(env):
    data = setup_sources(env)
    proof(env, data, "现公布《后续规则》，自公布之日起施行，原《早期规则》同时废止。", "2023年5月1日")
    return data, result(env)["items"][0]


def test_all_required_elements_are_prefilled_with_exact_source_bindings_and_no_writes(env):
    data, item = ready(env)
    assert item["status"] == "READY" and item["missing_fields"] == []
    assert item["effective_from"] == "2023-05-01" and item["scope"] == "full"
    assert all(item[key] == data[key] for key in authority.VERSION_KEYS)
    assert len(item["expected_sources"]) == 3 and item["excerpts"][0]["text"].startswith("现公布")
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.AuditEvent)) == 0
        assert db.scalar(select(m.RuntimePolicy.id).where(m.RuntimePolicy.name.startswith(authority.PREFIX))) is None
        for vid in (data[key] for key in authority.VERSION_KEYS):
            version = db.get(m.ResourceVersion, vid)
            assert version.state == "DRAFT" and version.legal_status == "UNKNOWN"
    assert result(env)["items"] == [item]


def test_confirm_prefilled_payload_uses_existing_authorized_create_path(env):
    _, item = ready(env)
    payload = {key: item[key] for key in ("space_id", *authority.VERSION_KEYS, "effective_from", "scope", "reason", "expected_sources")}
    record = create(env, payload)
    assert record["state"] == "ACTIVE" and "expected_sources" not in record
    after = result(env)["items"]
    assert len(after) == 1 and after[0]["status"] == "CONFIRMED" and after[0]["existing_record_id"] == record["id"]
    assert after[0]["missing_fields"] == []


@pytest.mark.parametrize("change", ["revision", "body", "access_epoch", "missing_binding", "duplicate_binding", "extra_binding"])
def test_prefill_cannot_confirm_changed_or_incomplete_source_snapshot(env, change):
    data, item = ready(env)
    payload = {key: copy.deepcopy(item[key]) for key in ("space_id", *authority.VERSION_KEYS, "effective_from", "scope", "reason", "expected_sources")}
    if change in {"missing_binding", "duplicate_binding", "extra_binding"}:
        if change == "missing_binding": payload["expected_sources"].pop()
        elif change == "duplicate_binding": payload["expected_sources"].append(payload["expected_sources"][0])
        else: payload["expected_sources"].append({**payload["expected_sources"][0], "version_id": svc.uid()})
    else:
        with env.db.begin() as db:
            version = db.get(m.ResourceVersion, data["evidence_version_id"])
            if change == "revision": version.revision += 1
            elif change == "access_epoch": db.get(m.Resource, version.resource_id).access_epoch += 1
            else:
                block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == version.id))
                block.data = {"text": "证据已修改"}; block.search_text = block.data["text"]; block.content_sha256 = text_sha256(block.search_text)
    with pytest.raises(svc.APIError) as err: create(env, payload)
    assert err.value.code == "SOURCE_AUTHORITY_PREFILL_STALE"
    with env.db() as db:
        assert db.scalar(select(m.RuntimePolicy.id).where(m.RuntimePolicy.name.startswith(authority.PREFIX))) is None


@pytest.mark.parametrize("change", ["hidden", "suspended", "deleted", "scan_rejected"])
def test_hidden_or_deleted_proof_is_not_prefilled_or_restored(env, change):
    data, _ = ready(env)
    with env.db.begin() as db:
        v = db.get(m.ResourceVersion, data["evidence_version_id"]); r = db.get(m.Resource, v.resource_id)
        if change == "hidden": r.restricted = True
        elif change == "suspended": r.suspended = True
        elif change == "deleted": r.deleted_at = svc.now()
        else: db.get(m.Blob, v.source_blob_id).scan_state = "REJECTED"
    assert result(env)["items"] == []


def test_unknown_or_ambiguous_title_only_leaves_that_endpoint_missing(env):
    data = setup_sources(env)
    proof(env, data, "现公布《后续规则》，自2023年5月1日起施行，原《库里没有的旧规则》同时废止。")
    item = result(env)["items"][0]
    assert item["predecessor_version_id"] is None
    assert item["successor_version_id"] == data["successor_version_id"]
    assert item["missing_fields"] == ["predecessor_version_id"]
    duplicate = make_version(env, "document")
    with env.db.begin() as db: db.get(m.ResourceVersion, duplicate).title = "后续规则"
    item = result(env)["items"][0]
    assert item["successor_version_id"] is None and item["status"] == "NEEDS_INPUT"


def test_partial_clause_is_not_prefilled_as_whole_document_replacement(env):
    data = setup_sources(env)
    proof(env, data, "现公布《后续规则》，自2023年5月1日起施行，原《早期规则》第三条同时废止。")
    item = result(env)["items"][0]
    assert item["status"] == "READY" and item["scope"] == "partial"


@pytest.mark.parametrize("text", [
    "现公布《后续规则》，原《早期规则》不废止。",
    "现公布《后续规则》，原《早期规则》并未废止。",
    "拟将《后续规则》替代《早期规则》。",
    "《后续规则》不能替代《早期规则》。",
    "征求意见稿拟将《早期规则》同时废止。",
    "原《早期规则》可能同时废止。",
])
def test_negative_or_tentative_text_is_not_machine_confirmation(text):
    assert extract_facts(rows(text)) == []


@pytest.mark.parametrize("dates,expected", [
    (("2023年5月1日",), "2023-05-01"),
    (("2023-05-01",), "2023-05-01"),
    (("2023年5月1日", "2024年6月2日"), None),
    (("网页生成日期：2026-09-12",), None),
    (("2023年2月30日",), None),
])
def test_publication_date_requires_unambiguous_signature_not_page_timestamp(dates, expected):
    facts = extract_facts(rows("现公布《后续规则》，自公布之日起施行，原《早期规则》同时废止。", *dates))
    assert facts[0]["effective_from"] == expected


def test_date_is_not_guessed_from_current_time_or_a_plain_signature():
    fact = extract_facts(rows("现公布《后续规则》，原《早期规则》同时废止。", "2023年5月1日"))[0]
    assert fact["effective_from"] is None


def test_two_explicit_effective_dates_are_not_silently_chosen():
    fact = extract_facts(rows("现公布《后续规则》，自2023年5月1日起施行，原《早期规则》同时废止。",
        "部分条款自2024年1月1日起施行。"))[0]
    assert fact["effective_from"] is None


def test_different_issuer_prefixes_match_only_identical_complete_about_titles(env):
    data = setup_sources(env)
    with env.db.begin() as db:
        db.get(m.ResourceVersion, data["successor_version_id"]).title = "中国证券监督管理委员会关于合成新规则的规定"
    proof(env, data, "现公布《中国证监会关于合成新规则的规定》，自2023年5月1日起施行，原《早期规则》同时废止。")
    assert result(env)["items"][0]["successor_version_id"] == data["successor_version_id"]


def test_unrelated_issuers_with_same_topic_are_not_matched(env):
    data = setup_sources(env)
    with env.db.begin() as db:
        db.get(m.ResourceVersion, data["successor_version_id"]).title = "某地方委员会关于合成新规则的规定"
    proof(env, data, "现公布《另一机构关于合成新规则的规定》，自2023年5月1日起施行，原《早期规则》同时废止。")
    assert result(env)["items"][0]["successor_version_id"] is None


def test_newly_revised_publication_and_embedded_repeal_can_prefill_successor(env):
    data = setup_sources(env)
    proof(env, data, "新修订的《后续规则》（以下简称《规则》）已经批准，现予以发布。",
        "《规则》自2023年5月1日起施行。原《早期规则》同时废止。")
    assert result(env)["items"][0]["successor_version_id"] == data["successor_version_id"]


def test_rule_body_may_be_its_own_proof_without_inventing_a_separate_file(env):
    data = setup_sources(env)
    with env.db.begin() as db: db.get(m.ResourceVersion, data["evidence_version_id"]).title = "自带公告的后续规则"
    proof(env, data, "本规则自2023年5月1日起施行。原《早期规则》同时废止。")
    item = result(env)["items"][0]
    assert item["successor_version_id"] == item["evidence_version_id"]
    assert len(item["expected_sources"]) == 2


def test_revoked_fact_is_not_reproposed_as_new_ready_item(env):
    _, item = ready(env)
    payload = {key: item[key] for key in ("space_id", *authority.VERSION_KEYS, "effective_from", "scope", "reason")}
    record = create(env, payload)
    with env.db.begin() as db: authority.revoke_record(context(env, db, {"reason": "用户撤销"}, record))
    assert result(env)["items"] == []


def test_ordinary_reader_does_not_gain_access_to_draft_prefill_or_management(env):
    ready(env)
    response = result(env, env.reader)
    assert response["can_manage"] is False and response["items"] == []
