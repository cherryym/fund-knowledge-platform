import copy

import pytest

from fund_kb.planning_cache import PlanningCache, planning_cache_key


def inputs():
    return {"namespace": "deployment-a", "owner_id": "owner-a", "space_id": "space-a",
        "request": {"question": "合成业务问题", "context": {}, "mode": "auto", "answer_scope": "reference"},
        "connection": {"id": "connection-a", "model_id": "model-a", "revision": 1,
            "auth_epoch": 1, "allow_document_transfer": True, "protocol": "responses", "api_key": "NOT_REAL"},
        "prompt_version": "v2", "system_sha256": "system-a", "planning_instruction": "plan-a",
        "max_output_tokens": 16384, "business_day": "2026-09-13"}


def plan():
    return {"interpretation": "合成事项", "initial_assessment": "ISSUE 查证输入\nSEARCH 原文条件",
        "search_queries": ["合成业务问题", "原文条件"], "focus_terms": [], "decision_points": [], "missing_facts": []}


def test_hit_is_a_copy_and_does_not_cache_final_answers_or_model_secrets():
    clock = [100]
    cache = PlanningCache(clock=lambda: clock[0])
    key = planning_cache_key(**inputs()); value = plan()
    assert cache.put(key, plan=value, source_run_id="original-run")
    value["search_queries"].append("later mutation")
    clock[0] += 2
    hit = cache.get(key)
    assert hit == {"plan": plan(), "source_run_id": "original-run", "age_ms": 2000}
    hit["plan"]["search_queries"].clear()
    assert cache.get(key)["plan"] == plan()
    assert "NOT_REAL" not in repr(cache._rows)


@pytest.mark.parametrize("field,value", [("namespace", "deployment-b"), ("owner_id", "owner-b"),
    ("space_id", "space-b"), ("business_day", "2026-09-14"), ("system_sha256", "system-b"),
    ("planning_instruction", "plan-b"), ("prompt_version", "v3"), ("max_output_tokens", 8192)])
def test_key_isolates_runtime_identity_and_execution_contract(field, value):
    original = inputs(); altered = copy.deepcopy(original); altered[field] = value
    assert planning_cache_key(**original) != planning_cache_key(**altered)


@pytest.mark.parametrize("field,value", [("id", "connection-b"), ("model_id", "model-b"), ("revision", 2),
    ("auth_epoch", 2), ("protocol", "openai"), ("allow_document_transfer", False), ("supported_parameters", ["temperature"])])
def test_key_isolates_connection_revision_auth_model_and_capabilities(field, value):
    original = inputs(); altered = copy.deepcopy(original); altered["connection"][field] = value
    assert planning_cache_key(**original) != planning_cache_key(**altered)


@pytest.mark.parametrize("field,value", [("question", "合成业务问题 "), ("context", {"business_date": "2026-09-14"}),
    ("mode", "solution"), ("answer_scope", "formal"), ("attachment_version_ids", ["attachment"]),
    ("retrieval_selection", {"profile_id": "another-index"})])
def test_key_matches_the_exact_request_not_a_similar_question(field, value):
    original = inputs(); altered = copy.deepcopy(original); altered["request"][field] = value
    assert planning_cache_key(**original) != planning_cache_key(**altered)


def test_credentials_callbacks_and_dict_order_do_not_enter_the_key():
    original = inputs(); altered = copy.deepcopy(original)
    altered["connection"]["api_key"] = "DIFFERENT_NOT_REAL"
    altered["connection"]["_cancel_check"] = lambda: True
    altered["request"] = dict(reversed(list(altered["request"].items())))
    assert planning_cache_key(**original) == planning_cache_key(**altered)


def test_ttl_discard_clear_and_clock_rollback():
    clock = [10]; cache = PlanningCache(clock=lambda: clock[0], ttl=5)
    assert cache.put("key", plan=plan(), source_run_id="run")
    clock[0] = 15; assert cache.get("key") is None and cache._bytes == 0
    assert cache.put("key", plan=plan(), source_run_id="run")
    clock[0] = 1; assert cache.get("key") is None
    cache.put("a", plan=plan(), source_run_id="run"); cache.discard("a"); assert cache.get("a") is None
    cache.put("a", plan=plan(), source_run_id="run"); cache.clear(); assert cache._bytes == 0


def test_lru_and_byte_budget_do_not_truncate_an_oversized_plan():
    cache = PlanningCache(max_entries=2, max_bytes=1500)
    for key in ("a", "b"): assert cache.put(key, plan=plan(), source_run_id="run")
    cache.get("a"); cache.put("c", plan=plan(), source_run_id="run")
    assert cache.get("b") is None and cache.get("a") and cache.get("c")
    big = plan(); big["initial_assessment"] = "全文" * 1000
    assert not cache.put("large", plan=big, source_run_id="run")
    assert cache.get("large") is None and cache.get("a")["plan"] == plan()


def test_malformed_or_unregistered_inputs_are_cache_misses_not_generation_failures():
    value = inputs(); value["connection"].pop("id")
    assert planning_cache_key(**value) is None
    value = inputs(); value["request"]["unsupported"] = float("nan")
    assert planning_cache_key(**value) is None
    cache = PlanningCache()
    assert cache.get(None) is None and not cache.put(None, plan=plan(), source_run_id="run")
    assert not cache.put("a", plan={"initial_assessment": "", "search_queries": []}, source_run_id="run")
