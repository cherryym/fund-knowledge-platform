"""Offline contract/negative tests. All sources, claims and reviewers are synthetic."""
from __future__ import annotations

import copy
import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from fund_kb.rag_evaluation import (
    EvaluationInputError,
    adapt_capture,
    compare,
    evaluate,
    load_json,
    validate_bundle,
    validate_document,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evals" / "rag" / "fixtures"
CLI = ROOT / "scripts" / "evaluate-rag.py"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("OFFLINE_TEST_NETWORK_FORBIDDEN")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def bundle():
    return tuple(load_json(FIXTURES / name) for name in (
        "synthetic.dataset.json", "synthetic.candidate.json", "synthetic.candidate-judgments.json"
    ))


def first_stage(report, stage="read"):
    return report["samples"][0]["stages"][stage]


def simulated_professional(bundle):
    """Exercise gates only; these mutated in-memory fixtures are not expert gold."""
    dataset, _, judgments = bundle
    dataset["evidence_domain"] = "professional"
    for case in dataset["cases"]:
        case["review_status"] = "expert_confirmed"
        case["review_provenance"]["kind"] = "human_expert"
    for judgment in judgments["judgments"]:
        judgment["review_provenance"]["kind"] = "human_expert"


def test_synthetic_pass_has_no_professional_pass_and_exact_ranks(bundle):
    report = evaluate(*bundle)
    assert report["status"] == "PASS"
    assert report["professional_status"] == "UNKNOWN"
    assert report["coverage"]["expected_sample_count"] == 2
    for name in ("candidate", "final", "read"):
        stage = first_stage(report, name)
        assert stage["recall"] == 1
        assert stage["all_critical_groups_covered"] is True
        assert stage["all_required_groups_rank"] == 4
        assert stage["mean_group_reciprocal_rank"] == pytest.approx((1 / 2 + 1 / 4) / 2)


def test_missing_prediction_stays_in_denominator(bundle):
    dataset, predictions, _ = bundle
    predictions["predictions"].pop(0)
    report = evaluate(dataset, predictions)
    assert report["coverage"]["missing_case_ids"] == ["syn-dev"]
    assert report["coverage"]["expected_case_count"] == 2
    assert report["coverage"]["present_sample_count"] == 1
    assert first_stage(report)["recall"] is None
    assert report["stages"]["read"]["unknown_sample_count"] == 1
    assert report["stages"]["read"]["known_only_macro_recall"] == 1
    assert report["status"] != "PASS"


@pytest.mark.parametrize("stage", ["candidate", "final", "read"])
@pytest.mark.parametrize("form", ["absent", "null", "incomplete"])
def test_missing_or_incomplete_stage_never_passes(bundle, stage, form):
    dataset, predictions, judgments = bundle
    stages = predictions["predictions"][0]["stages"]
    if form == "absent":
        del stages[stage]
    elif form == "null":
        stages[stage] = None
    else:
        stages[stage]["status"] = "incomplete"
    result = first_stage(evaluate(dataset, predictions, judgments), stage)
    assert result["status"] == "UNKNOWN"
    assert result["recall"] is None
    assert result["all_critical_groups_covered"] is None


def test_observed_empty_stage_is_zero_not_unknown(bundle):
    bundle[1]["predictions"][0]["stages"]["read"]["items"] = []
    stage = first_stage(evaluate(*bundle))
    assert stage["recall"] == 0
    assert stage["status"] == "FAIL"


def test_zero_gold_has_no_vacuous_pass(bundle):
    dataset, predictions, _ = bundle
    dataset["cases"][0]["required_evidence_groups"] = []
    report = evaluate(dataset, predictions)
    stage = first_stage(report)
    assert stage["recall"] is None
    assert stage["all_required_groups_covered"] is None
    assert stage["all_critical_groups_covered"] is None
    assert "ZERO_GOLD_GROUPS" in stage["failure_codes"]
    assert report["coverage"]["zero_gold_case_count"] == 1


def test_duplicate_synonyms_and_duplicate_observations_do_not_inflate_groups(bundle):
    dataset, predictions, judgments = bundle
    group = dataset["cases"][0]["required_evidence_groups"][0]
    duplicate = copy.deepcopy(group["alternatives"][0])
    duplicate["alternative_id"] = "synonym-of-canonical"
    group["alternatives"].append(duplicate)
    items = predictions["predictions"][0]["stages"]["read"]["items"]
    items.append({**copy.deepcopy(items[0]), "rank": 5})
    stage = first_stage(evaluate(dataset, predictions, judgments))
    assert stage["required_group_count"] == stage["observed_covered_group_count"] == 2
    assert stage["recall"] == 1
    assert stage["all_required_groups_rank"] == 4


def test_equivalent_source_is_one_group_hit(bundle):
    dataset, predictions, judgments = bundle
    alternative = dataset["cases"][0]["required_evidence_groups"][0]["alternatives"][1]["spans"][0]
    stage = predictions["predictions"][0]["stages"]["read"]
    stage["items"] = [{"rank": 1, "source": alternative}, *stage["items"][2:]]
    result = first_stage(evaluate(dataset, predictions, judgments))
    assert result["recall"] == 1
    assert result["groups"][0]["first_full_rank"] == 1


@pytest.mark.parametrize("intervals,expected_chars,hit_rank", [
    ([(2, 7), (7, 12)], 10, 2),
    ([(2, 8), (6, 12)], 10, 2),
    ([(2, 7), (8, 12)], 9, None),
    ([(2, 7), (2, 7)], 5, None),
    ([(0, 99)], 10, 1),
    ([(12, 20)], 0, None),
])
def test_original_span_union_is_exact_not_any_overlap(bundle, intervals, expected_chars, hit_rank):
    source = copy.deepcopy(bundle[1]["predictions"][0]["stages"]["read"]["items"][0]["source"])
    items = [{"rank": i, "source": {**source, "span": {"start": a, "end": b}}}
             for i, (a, b) in enumerate(intervals, 1)]
    bundle[1]["predictions"][0]["stages"]["read"]["items"] = items
    stage = first_stage(evaluate(*bundle))
    span = stage["groups"][0]["alternatives"][0]["spans"][0]
    assert span["covered_chars"] == expected_chars
    assert span["first_full_rank"] == hit_rank
    assert stage["recall"] == (0.5 if hit_rank else 0)


def test_multi_span_alternative_requires_every_source(bundle):
    bundle[1]["predictions"][0]["stages"]["read"]["items"].pop()
    stage = first_stage(evaluate(*bundle))
    assert stage["recall"] == 0.5
    assert stage["all_critical_groups_covered"] is False
    assert stage["groups"][1]["status"] == "FAIL"


@pytest.mark.parametrize("field,value", [
    ("content_sha256", "0" * 64), ("version_id", "old-version"), ("resource_id", "another-resource"),
    ("block_id", "another-block"), ("valid_from", "2025-01-01"), ("valid_to", "2026-01-15"),
])
def test_old_hash_identity_and_wrong_effective_dates_do_not_hit(bundle, field, value):
    for item in bundle[1]["predictions"][0]["stages"]["read"]["items"]:
        item["source"][field] = value
    stage = first_stage(evaluate(*bundle))
    assert stage["recall"] == 0
    assert stage["status"] == "FAIL"
    assert stage["groups"][0]["alternatives"][0]["spans"][0]["mismatch_counts"]


def test_wrong_business_date_never_hits(bundle):
    bundle[1]["predictions"][0]["as_of"] = "2026-01-14"
    assert first_stage(evaluate(*bundle))["recall"] == 0


def test_gold_outside_business_date_is_invalid(bundle):
    bundle[0]["cases"][0]["as_of"] = "2027-01-01"
    with pytest.raises(EvaluationInputError, match="GOLD_OUTSIDE_AS_OF"):
        evaluate(*bundle)


@pytest.mark.parametrize("label,status", [
    ("supported", "PASS"), ("partial", "FAIL"), ("contradicted", "FAIL"), ("insufficient", "UNKNOWN")
])
def test_only_external_human_judgments_determine_semantic_status(bundle, label, status):
    bundle[2]["judgments"][0]["claims"][0]["label"] = label
    report = evaluate(*bundle)
    assert report["samples"][0]["semantic"]["status"] == status
    assert first_stage(report)["status"] == "PASS"  # same citation identities, different human decisions


@pytest.mark.parametrize("change", ["pending", "no_provenance", "inventory", "hash", "missing_claim", "no_group"])
def test_unreviewed_or_unbound_judgments_are_unknown(bundle, change):
    judgment = bundle[2]["judgments"][0]
    if change == "pending":
        judgment["review_status"] = "pending"
    elif change == "no_provenance":
        judgment["review_provenance"] = None
    elif change == "inventory":
        judgment["inventory_complete"] = False
    elif change == "hash":
        judgment["answer_sha256"] = "0" * 64
    elif change == "missing_claim":
        judgment["claims"].pop()
    else:
        judgment["claims"][0]["evidence_group_ids"] = []
    assert evaluate(*bundle)["samples"][0]["semantic"]["status"] == "UNKNOWN"


def test_citation_ids_alone_cannot_prove_semantic_support(bundle):
    report = evaluate(bundle[0], bundle[1])
    assert report["samples"][0]["semantic"]["status"] == "UNKNOWN"
    assert report["samples"][0]["semantic"]["human_reviewed_claim_count"] == 0


def test_empty_claim_inventory_is_unknown(bundle):
    bundle[1]["predictions"][0]["claims"] = []
    bundle[2]["judgments"][0]["claims"] = []
    assert evaluate(*bundle)["samples"][0]["semantic"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("confirmed", [True, False])
def test_professional_pass_requires_case_and_answer_expert_confirmation(bundle, confirmed):
    simulated_professional(bundle)
    if not confirmed:
        bundle[0]["cases"][0]["review_status"] = "pending"
        bundle[0]["cases"][0]["review_provenance"] = None
    report = evaluate(*bundle)
    assert report["professional_status"] == ("PASS" if confirmed else "UNKNOWN")


def test_non_expert_answer_review_cannot_give_professional_pass(bundle):
    simulated_professional(bundle)
    bundle[2]["judgments"][0]["review_provenance"]["kind"] = "human_engineering"
    assert evaluate(*bundle)["professional_status"] == "UNKNOWN"


@pytest.mark.parametrize("leak", ["split_family", "development_family", "development_case"])
def test_holdout_leakage_uses_family_not_just_case_id(bundle, leak):
    dataset, predictions, _ = bundle
    if leak == "split_family":
        dataset["cases"][1]["family_id"] = dataset["cases"][0]["family_id"]
    elif leak == "development_family":
        predictions["exposure"]["development_family_ids"] = [dataset["cases"][1]["family_id"]]
    else:
        predictions["exposure"]["development_case_ids"] = [dataset["cases"][1]["case_id"]]
    report = evaluate(*bundle)
    assert report["leakage"]["status"] == "FAIL"
    assert report["status"] == "FAIL"


def test_unknown_exposure_history_cannot_pass(bundle):
    del bundle[1]["exposure"]
    assert evaluate(*bundle)["leakage"]["status"] == "UNKNOWN"
    assert evaluate(*bundle)["status"] == "UNKNOWN"


@pytest.mark.parametrize("mutation,code", [
    ("unknown_prediction", "UNKNOWN_PREDICTION"), ("unknown_sample", "UNKNOWN_PREDICTION"),
    ("duplicate_prediction", "DUPLICATE_PREDICTION"), ("duplicate_case", "DUPLICATE_CASE_ID"),
    ("unknown_exposure", "UNKNOWN_EXPOSURE_CASE"), ("unknown_judgment", "UNKNOWN_JUDGMENT_CASE"),
    ("unknown_claim", "UNKNOWN_JUDGMENT_CLAIM"), ("unknown_group", "UNKNOWN_JUDGMENT_EVIDENCE_GROUP"),
    ("wrong_run", "JUDGMENT_RUN_MISMATCH"), ("orphan_judgment", "ORPHAN_JUDGMENT"),
])
def test_invalid_or_unknown_references_are_rejected_not_ignored(bundle, mutation, code):
    dataset, predictions, judgments = bundle
    if mutation == "unknown_prediction":
        predictions["predictions"][0]["case_id"] = "nonexistent"
    elif mutation == "unknown_sample":
        predictions["predictions"][0]["sample_id"] = "nonexistent"
    elif mutation == "duplicate_prediction":
        predictions["predictions"].append(copy.deepcopy(predictions["predictions"][0]))
    elif mutation == "duplicate_case":
        dataset["cases"].append(copy.deepcopy(dataset["cases"][0]))
    elif mutation == "unknown_exposure":
        predictions["exposure"]["development_case_ids"] = ["nonexistent"]
    elif mutation == "unknown_judgment":
        judgments["judgments"][0]["case_id"] = "nonexistent"
    elif mutation == "unknown_claim":
        judgments["judgments"][0]["claims"][0]["claim_id"] = "nonexistent"
    elif mutation == "unknown_group":
        judgments["judgments"][0]["claims"][0]["evidence_group_ids"] = ["nonexistent"]
    elif mutation == "wrong_run":
        judgments["run_id"] = "other-run"
    else:
        predictions["predictions"].pop(0)
    with pytest.raises(EvaluationInputError, match=code):
        evaluate(*bundle)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf"), -1, True, "10"])
def test_invalid_timings_rejected(bundle, number):
    bundle[1]["predictions"][0]["latency"]["complete_response_ms"] = number
    with pytest.raises(EvaluationInputError):
        evaluate(*bundle)


def test_first_token_is_never_used_for_complete_response(bundle):
    prediction = bundle[1]["predictions"][0]
    del prediction["latency"]["complete_response_ms"]
    report = evaluate(*bundle)
    group = next(row for row in report["latency"] if row["split"] == "dev")
    assert group["measured_sample_count"] == 0
    assert group["unknown_sample_count"] == 1
    assert group["p50_ms"] is None and group["p95_ms"] is None


def test_percentiles_are_per_expected_sample_and_nearest_rank(bundle):
    dataset, predictions, _ = bundle
    dataset["cases"][0]["expected_sample_ids"] = ["s1", "s2", "s3", "s4"]
    source = predictions["predictions"][0]
    source["latency"]["first_token_ms"] = 1
    source["latency"]["complete_response_ms"] = 10
    for sid, value in [("s2", 30), ("s3", 100)]:
        row = copy.deepcopy(source)
        row["sample_id"], row["latency"]["complete_response_ms"] = sid, value
        predictions["predictions"].append(row)
    report = evaluate(dataset, predictions)
    group = next(row for row in report["latency"] if row["split"] == "dev" and row["cache_state"] == "cold")
    assert group["case_count"] == 1 and group["measured_sample_count"] == 3
    assert (group["p50_ms"], group["p95_ms"]) == (30, 100)
    assert sum(row["expected_sample_count"] for row in report["latency"]) == 5
    assert report["coverage"]["missing_samples"] == [{"case_id": "syn-dev", "sample_id": "s4"}]


def test_incomplete_answer_timing_is_retained_as_unknown(bundle):
    bundle[1]["predictions"][0]["answer_complete"] = False
    assert evaluate(*bundle)["samples"][0]["latency"]["complete_response_ms"] is None


def test_paired_comparison_reports_real_deltas_and_does_not_claim_superiority(bundle):
    baseline = load_json(FIXTURES / "synthetic.baseline.json")
    baseline_judgments = load_json(FIXTURES / "synthetic.baseline-judgments.json")
    report = compare(bundle[0], baseline, bundle[1], baseline_judgments, bundle[2])
    assert report["expected_pair_count"] == 2
    assert report["stages"]["read"]["recall_delta"]["mean_candidate_minus_baseline"] == 0.5
    assert report["latency"][0]["paired_candidate_minus_baseline"]["p50_ms"] == -400
    assert report["decision"] == "NO_AUTOMATIC_SUPERIORITY_CLAIM"


def test_paired_missing_samples_on_both_sides_are_not_intersection_filtered(bundle):
    dataset, candidate, _ = bundle
    baseline = copy.deepcopy(candidate)
    baseline["run_id"] = "baseline"
    candidate["predictions"].pop(0)
    baseline["predictions"].pop(0)
    report = compare(dataset, baseline, candidate)
    assert report["expected_pair_count"] == 2
    assert report["present_on_both_count"] == 1
    assert report["missing_on_either_count"] == 1
    assert report["pairs"][0]["stages"]["read"]["recall_delta"] is None
    assert report["stages"]["read"]["recall_delta"]["unknown_pair_count"] == 1


@pytest.mark.parametrize("field,value", [("cache_state", "warm"), ("query_state", "repeat"),
                                         ("measurement_scope", "request_to_complete_response")])
def test_paired_latency_does_not_mix_cohorts(bundle, field, value):
    baseline = copy.deepcopy(bundle[1])
    baseline["run_id"] = "baseline"
    bundle[1]["predictions"][0]["latency"][field] = value
    report = compare(bundle[0], baseline, bundle[1])
    assert report["pairs"][0]["complete_response_ms_delta"] is None
    assert report["pairs"][0]["latency_failure_codes"] == ["LATENCY_COHORT_MISMATCH"]


def test_split_scope_is_declared_and_unknown_rows_outside_scope_still_rejected(bundle):
    report = evaluate(*bundle, split="holdout")
    assert report["coverage"]["expected_case_count"] == 1
    assert report["coverage"]["dataset_case_count"] == 2
    assert report["coverage"]["excluded_case_ids"] == ["syn-dev"]
    bundle[1]["predictions"][0]["case_id"] = "unknown-dev"
    with pytest.raises(EvaluationInputError, match="UNKNOWN_PREDICTION"):
        evaluate(*bundle, split="holdout")


def test_report_never_copies_private_query_source_answer_or_review_text(bundle):
    private = "PRIVATE_CANARY_隐私正文不准出现在报告"
    dataset, predictions, judgments = bundle
    dataset["cases"][0]["question"] = private
    dataset["coverage"]["statement"] = private
    row = predictions["predictions"][0]
    row["query_text"], row["answer_text"] = private, private
    row["answer_sha256"] = hashlib.sha256(private.encode()).hexdigest()
    row["claims"][0]["text"] = private
    row["stages"]["read"]["items"][0]["text"] = private
    judgments["judgments"][0]["answer_sha256"] = row["answer_sha256"]
    judgments["judgments"][0]["claims"][0]["rationale"] = private
    rendered = json.dumps(evaluate(*bundle), ensure_ascii=False)
    assert private not in rendered
    assert "rationale" not in rendered.replace("reviewer_rationale_included", "")


@pytest.mark.parametrize("text,code", [
    ('{"a":1,"a":2}', "DUPLICATE_JSON_KEY"), ('{"x":NaN}', "NON_FINITE"),
    ('{"x":Infinity}', "NON_FINITE"), ('{"x":1e999}', "NON_FINITE"),
    ('{"PRIVATE_JSON_CANARY":', "INVALID_JSON"),
])
def test_strict_json_load_errors_do_not_echo_input(tmp_path, text, code):
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(EvaluationInputError, match=code) as caught:
        load_json(path)
    assert "PRIVATE_JSON_CANARY" not in str(caught.value)


def test_schema_errors_do_not_echo_private_values_or_property_names(bundle):
    bundle[1]["predictions"][0]["latency"]["PRIVATE_VALUE"] = "PRIVATE_VALUE"
    with pytest.raises(EvaluationInputError) as caught:
        evaluate(*bundle)
    assert "PRIVATE_VALUE" not in json.dumps(caught.value.as_dict())


def test_40_templates_are_actionable_pending_and_never_gold():
    dataset = load_json(ROOT / "evals/rag/valuation-40.candidate-template.json")
    predictions = load_json(FIXTURES / "template-not-executed.predictions.json")
    validate_document(dataset)
    assert len(dataset["cases"]) == 40
    assert sum(row["split"] == "dev" for row in dataset["cases"]) == 20
    assert sum(row["split"] == "holdout" for row in dataset["cases"]) == 20
    for case in dataset["cases"]:
        assert case["review_status"] == "pending" and case["review_provenance"] is None
        assert case["required_evidence_groups"] == []
        assert len(case["annotation_plan"]["verification_tasks"]) >= 3
        assert len(case["annotation_plan"]["misleading_source_types"]) >= 2
        assert len(case["annotation_plan"]["source_slots"]) == 3
        assert all(value is None for slot in case["annotation_plan"]["source_slots"]
                   for value in slot["pending_locator"].values())
    report = evaluate(dataset, predictions)
    assert report["status"] == report["professional_status"] == "UNKNOWN"
    assert report["coverage"]["zero_gold_case_count"] == report["coverage"]["pending_case_count"] == 40
    assert len(report["coverage"]["missing_case_ids"]) == 40


@pytest.fixture
def native_capture(bundle):
    # Calls the *actual* trace/timing producers with synthetic data, not a guessed demo shape.
    from fund_kb.answer_timing import AnswerTimings
    from fund_kb.retrieval_observation import observe_candidates

    dataset, predictions, _ = bundle
    units, metadata, receipt = [], {}, []
    for item in predictions["predictions"][0]["stages"]["read"]["items"]:
        source = item["source"]
        span = {"block_id": source["block_id"], "content_sha256": source["content_sha256"],
                **source["span"]}
        unit_id = f"unit-{item['rank']}"
        units.append({"unit_id": unit_id, "resource_id": source["resource_id"],
                      "version_id": source["version_id"], "page_id": "synthetic-page",
                      "source_spans": [span], "fusion_rank": item["rank"], "channel_ranks": {"bm25": 1},
                      "channels": ["bm25"], "source_kind": "document", "rerank_score": 0.5})
        meta = {key: value for key, value in source.items() if key != "span"}
        metadata[(source["block_id"], source["content_sha256"])] = {**meta, "block_char_count": 128}
        receipt.append({"rank": item["rank"], "unit_id": unit_id, "resource_id": source["resource_id"],
                        "version_id": source["version_id"], **span})
    trace = observe_candidates("PRIVATE_SEARCH_QUERY", units, {"mode": "local_cross_encoder"},
                               phases_ms={"candidate_search_ms": 10})
    clock = [0.0]
    timer = AnswerTimings(clock=lambda: clock[0])
    timer.add("model_synthesis", 0.9)
    clock[0] = 1.0
    record = {"case_id": "syn-dev", "sample_id": "s1", "as_of": "2026-01-15",
              "run_state": "COMPLETED", "answer_complete": True, "answer_sha256": "e" * 64,
              "claims": predictions["predictions"][0]["claims"],
              "search_result": {"query": "PRIVATE_SEARCH_QUERY", "retrieval_trace": trace},
              "model_snapshot": {"pipeline_timing": timer.snapshot(), "private_ignored": "PRIVATE_SNAPSHOT"},
              "final_unit_ids": [unit["unit_id"] for unit in units],
              "read_receipt": {"status": "complete", "items": receipt},
              "cache_state": "cold", "query_state": "first"}
    capture = {"schema_version": "1.0", "kind": "capture", "dataset_id": dataset["dataset_id"],
               "run_id": predictions["run_id"], "exposure": predictions["exposure"], "records": [record]}
    manifest = {"schema_version": "1.0", "kind": "source_manifest", "manifest_id": "synthetic-metadata",
                "provenance": {"kind": "authorized_source_metadata_export", "export_id": "synthetic-export",
                               "exported_at": "2026-01-16T00:00:00Z"}, "sources": list(metadata.values())}
    return capture, manifest


def test_actual_trace_and_model_snapshot_adapter_integrates_with_evaluator(bundle, native_capture):
    converted = adapt_capture(*native_capture)
    judgments = copy.deepcopy(bundle[2])
    judgments["judgments"] = judgments["judgments"][:1]
    report = evaluate(bundle[0], converted, judgments, split="dev")
    assert report["status"] == "PASS"
    assert first_stage(report, "candidate")["observation_scope"] == "current_authorized_candidate_pool"
    assert report["samples"][0]["latency"]["complete_response_ms"] == 1000
    assert report["latency"][0]["measurement_scope"] == "server_execution_to_completed_answer"
    assert converted["predictions"][0]["latency"]["first_token_ms"] is None
    assert converted["predictions"][0]["execution_observation"]["first_visible_answer_status"] == "NOT_MEASURED"
    assert "PRIVATE_" not in json.dumps(converted)
    assert "PRIVATE_" not in json.dumps(report)


def test_adapter_never_derives_final_or_read_from_citations_or_candidate_pool(bundle, native_capture):
    capture, manifest = native_capture
    del capture["records"][0]["final_unit_ids"]
    del capture["records"][0]["read_receipt"]
    converted = adapt_capture(capture, manifest)
    report = evaluate(bundle[0], converted, split="dev")
    assert first_stage(report, "candidate")["recall"] == 1
    assert first_stage(report, "final")["status"] == "UNKNOWN"
    assert first_stage(report, "read")["status"] == "UNKNOWN"


def test_adapter_missing_metadata_cannot_borrow_gold_dates(bundle, native_capture):
    converted = adapt_capture(native_capture[0])
    report = evaluate(bundle[0], converted, split="dev")
    assert first_stage(report, "candidate")["recall"] is None
    assert "SOURCE_METADATA_MISSING" in converted["predictions"][0]["adapter_diagnostics"]


def test_adapter_never_replaces_old_trace_hash_with_manifest_hash(bundle, native_capture):
    capture, manifest = native_capture
    capture["records"][0]["search_result"]["retrieval_trace"]["units"][0]["source_spans"][0]["content_sha256"] = "0" * 64
    converted = adapt_capture(capture, manifest)
    assert "SOURCE_HASH_NOT_IN_MANIFEST" in converted["predictions"][0]["adapter_diagnostics"]
    assert first_stage(evaluate(bundle[0], converted, split="dev"), "candidate")["status"] == "UNKNOWN"


def test_adapter_summary_without_units_stays_unknown(bundle, native_capture):
    from fund_kb.retrieval_observation import observation_summary

    capture, manifest = native_capture
    row = capture["records"][0]
    row["search_result"]["retrieval_trace"] = observation_summary(row["search_result"]["retrieval_trace"])
    del row["final_unit_ids"]
    converted = adapt_capture(capture, manifest)
    assert "TRACE_SUMMARY_ONLY" in converted["predictions"][0]["adapter_diagnostics"]
    assert first_stage(evaluate(bundle[0], converted, split="dev"), "candidate")["status"] == "UNKNOWN"


def test_adapter_failed_planning_10s_is_not_complete_answer_or_retrieval_zero(bundle, native_capture):
    capture, manifest = native_capture
    row = capture["records"][0]
    row["run_state"] = "FAILED"
    for key in ("search_result", "final_unit_ids", "read_receipt", "claims"):
        del row[key]
    timing = row["model_snapshot"]["pipeline_timing"]
    timing["execution_elapsed_ms"] = 10000
    timing["phases"] = {"model_planning": {"elapsed_ms": 10000, "calls": 1}}
    converted = adapt_capture(capture, manifest)
    report = evaluate(bundle[0], converted, split="dev")
    assert first_stage(report, "candidate")["recall"] is None
    assert report["samples"][0]["execution_observation"]["execution_elapsed_ms"] == 10000
    assert report["latency"][0]["measured_sample_count"] == 0
    assert report["status"] == "UNKNOWN"


@pytest.mark.parametrize("fault,code", [("count", "TRACE_UNITS_INCONSISTENT"),
                                       ("unknown_final", "UNKNOWN_FINAL_UNIT_ID"),
                                       ("overflow_span", "SPAN_OUT_OF_BLOCK_BOUNDS"),
                                       ("nan_phase", "NON_FINITE_NUMBER")])
def test_adapter_rejects_invalid_native_export(native_capture, fault, code):
    capture, manifest = native_capture
    row = capture["records"][0]
    if fault == "count":
        row["search_result"]["retrieval_trace"]["units"].pop()
    elif fault == "unknown_final":
        row["final_unit_ids"].append("never-observed")
    elif fault == "overflow_span":
        row["search_result"]["retrieval_trace"]["units"][0]["source_spans"][0]["end"] = 9999
    else:
        row["model_snapshot"]["pipeline_timing"]["phases"]["model_synthesis"]["elapsed_ms"] = float("nan")
    with pytest.raises(EvaluationInputError, match=code):
        adapt_capture(capture, manifest)


def test_adapter_preserves_one_unit_rank_for_multiple_original_spans(bundle, native_capture):
    capture, manifest = native_capture
    row = capture["records"][0]
    trace = row["search_result"]["retrieval_trace"]
    first, second = trace["units"][:2]
    first["source_spans"].extend(second["source_spans"])
    trace["units"].pop(1)
    for i, unit in enumerate(trace["units"], 1):
        unit["rank"] = i
    trace["candidate_count"] = trace["reranked_count"] = 3
    row["final_unit_ids"].remove(second["unit_id"])
    converted = adapt_capture(capture, manifest)
    result = first_stage(evaluate(bundle[0], converted, split="dev"), "candidate")
    assert result["groups"][0]["first_full_rank"] == 1
    assert result["all_required_groups_rank"] == 3


def cli(*args):
    return subprocess.run([sys.executable, "-B", str(CLI), *map(str, args)],
                          cwd=ROOT, text=True, capture_output=True, check=False)


@pytest.mark.parametrize("mode,expected", [("pass", 0), ("fail", 1), ("unknown", 3)])
def test_cli_evaluation_exit_statuses(mode, expected):
    run = "synthetic.baseline" if mode == "fail" else "synthetic.candidate"
    args = ["evaluate", "--dataset", FIXTURES / "synthetic.dataset.json",
            "--predictions", FIXTURES / f"{run}.json"]
    if mode != "unknown":
        args.extend(["--judgments", FIXTURES / f"{run}-judgments.json"])
    result = cli(*args)
    assert result.returncode == expected, result.stderr
    assert json.loads(result.stdout)["status"] == {0: "PASS", 1: "FAIL", 3: "UNKNOWN"}[expected]


def test_cli_invalid_does_not_emit_private_text_and_never_overwrites(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"PRIVATE_CLI_CANARY":', encoding="utf-8")
    result = cli("validate", invalid)
    assert result.returncode == 2
    assert "PRIVATE_CLI_CANARY" not in result.stdout + result.stderr
    output = tmp_path / "report.json"
    output.write_text("preserve", encoding="utf-8")
    result = cli("evaluate", "--dataset", FIXTURES / "synthetic.dataset.json", "--predictions",
                 FIXTURES / "synthetic.candidate.json", "--output", output)
    assert result.returncode == 2
    assert output.read_text() == "preserve"


def test_cli_adapt_is_real_pipeline_and_body_free(tmp_path, native_capture, bundle):
    capture, manifest = native_capture
    capture_path, manifest_path = tmp_path / "capture.json", tmp_path / "manifest.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = cli("adapt", "--capture", capture_path, "--source-manifest", manifest_path,
                 "--dataset", FIXTURES / "synthetic.dataset.json")
    assert result.returncode == 0, result.stderr
    converted = json.loads(result.stdout)
    validate_bundle(bundle[0], converted)
    assert "PRIVATE_" not in result.stdout
    report = evaluate(bundle[0], converted, split="dev")
    assert first_stage(report, "candidate")["recall"] == 1


def test_unicode_offsets_are_preserved_without_utf8_or_utf16_conversion(bundle, native_capture):
    capture, manifest = native_capture
    raw = "甲乙🙂丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # Block a's [2,12) is 10 code points, independent of UTF-8 byte/UTF-16 unit length.
    for case in bundle[0]["cases"]:
        case["required_evidence_groups"][0]["alternatives"][0]["spans"][0]["content_sha256"] = digest
    for meta in manifest["sources"]:
        if meta["block_id"] == "syn-block-a":
            meta["content_sha256"], meta["block_char_count"] = digest, len(raw)
    for unit in capture["records"][0]["search_result"]["retrieval_trace"]["units"]:
        for span in unit["source_spans"]:
            if span["block_id"] == "syn-block-a":
                span["content_sha256"] = digest
    for item in capture["records"][0]["read_receipt"]["items"]:
        if item["block_id"] == "syn-block-a":
            item["content_sha256"] = digest
    result = first_stage(evaluate(bundle[0], adapt_capture(capture, manifest), split="dev"))
    assert result["recall"] == 1
    assert result["groups"][0]["alternatives"][0]["spans"][0]["covered_chars"] == 10


def test_old_hash_with_independent_metadata_is_known_failure_not_a_hit(bundle, native_capture):
    capture, manifest = native_capture
    for meta in manifest["sources"]:
        if meta["block_id"] == "syn-block-a":
            meta["content_sha256"] = "0" * 64
    for unit in capture["records"][0]["search_result"]["retrieval_trace"]["units"][:2]:
        unit["source_spans"][0]["content_sha256"] = "0" * 64
    result = first_stage(evaluate(bundle[0], adapt_capture(capture, manifest), split="dev"), "candidate")
    assert result["status"] == "FAIL" and result["recall"] == 0.5


def test_adapter_trace_summary_preserves_counts_and_only_safe_phase_names(native_capture):
    capture, manifest = native_capture
    trace = capture["records"][0]["search_result"]["retrieval_trace"]
    trace["phases_ms"]["PRIVATE_PHASE_NAME"] = 10
    converted = adapt_capture(capture, manifest)
    observation = converted["predictions"][0]["retrieval_observation"]
    assert observation["candidate_count"] == 4
    assert observation["reranked_count"] == 4
    assert observation["all_corpus_recall"] == observation["semantic_support"] == "NOT_EVALUATED"
    assert "PRIVATE_PHASE_NAME" not in json.dumps(converted)


def test_no_critical_groups_does_not_claim_critical_coverage(bundle):
    for group in bundle[0]["cases"][0]["required_evidence_groups"]:
        group["critical"] = False
    result = first_stage(evaluate(*bundle))
    assert result["recall"] == 1 and result["all_critical_groups_covered"] is None


def test_unreviewed_negative_label_is_not_a_human_finding(bundle):
    judgment = bundle[2]["judgments"][0]
    judgment["review_status"] = "pending"
    judgment["claims"][0]["label"] = "contradicted"
    semantic = evaluate(*bundle)["samples"][0]["semantic"]
    assert semantic["status"] == "UNKNOWN"
    assert semantic["human_label_counts"]["contradicted"] == 0


@pytest.mark.parametrize("fault", ["ambiguous_rank", "bad_span", "bad_date", "fake_review", "contradictory_stage"])
def test_structural_claims_cannot_bypass_validation(bundle, fault):
    dataset, predictions, _ = bundle
    item = predictions["predictions"][0]["stages"]["read"]["items"][0]
    if fault == "ambiguous_rank":
        item["rank"] = 2
    elif fault == "bad_span":
        item["source"]["span"]["end"] = item["source"]["span"]["start"]
    elif fault == "bad_date":
        predictions["predictions"][0]["as_of"] = "2026-02-30"
    elif fault == "fake_review":
        dataset["cases"][0]["review_status"] = "expert_confirmed"
        dataset["cases"][0]["review_provenance"] = None
    else:
        predictions["predictions"][0]["stages"]["read"]["failure_codes"] = ["SOURCE_METADATA_MISSING"]
    with pytest.raises(EvaluationInputError):
        evaluate(*bundle)


def test_no_expected_cases_is_invalid_instead_of_empty_pass(bundle):
    bundle[0]["cases"] = []
    with pytest.raises(EvaluationInputError):
        evaluate(*bundle)


def test_evaluation_and_adapter_are_deterministic_and_do_not_mutate_inputs(bundle, native_capture):
    frozen_bundle, frozen_capture = copy.deepcopy(bundle), copy.deepcopy(native_capture)
    assert evaluate(*bundle) == evaluate(*bundle)
    assert adapt_capture(*native_capture) == adapt_capture(*native_capture)
    assert bundle == frozen_bundle and native_capture == frozen_capture


def test_shared_batch_trace_timing_keeps_its_scope_and_unscored_tail(native_capture):
    capture, manifest = native_capture
    trace = capture["records"][0]["search_result"]["retrieval_trace"]
    trace["timing_scope"] = "shared_batch"
    trace["reranked_count"], trace["unscored_count"] = 3, 1
    trace["units"][-1]["reranked"] = False
    observation = adapt_capture(capture, manifest)["predictions"][0]["retrieval_observation"]
    assert observation["timing_scope"] == "shared_batch"
    assert observation["unscored_count"] == 1
    assert observation["channel_counts"] == {"bm25": 4}


def test_adapter_invalid_extension_type_produces_safe_validation_error(native_capture):
    native_capture[0]["records"][0]["search_result"]["retrieval_trace"]["candidate_policy"] = {"PRIVATE": 1}
    with pytest.raises(EvaluationInputError, match="SCHEMA_VALIDATION") as caught:
        adapt_capture(*native_capture)
    assert "PRIVATE" not in str(caught.value)


def test_invalid_unicode_in_private_answer_is_safe_input_error(bundle):
    bundle[1]["predictions"][0]["answer_text"] = "PRIVATE_TEXT_" + chr(0xD800)
    with pytest.raises(EvaluationInputError, match="INVALID_UNICODE_STRING") as caught:
        evaluate(*bundle)
    assert "PRIVATE_TEXT" not in str(caught.value)


@pytest.mark.parametrize("scenario", ["slower", "unreviewed", "failed"])
def test_paired_adoption_is_blocked_for_regression_or_unconfirmed_incomplete_samples(bundle, scenario):
    simulated_professional(bundle)
    dataset, candidate, candidate_judgments = bundle
    baseline, baseline_judgments = copy.deepcopy(candidate), copy.deepcopy(candidate_judgments)
    baseline["run_id"] = baseline_judgments["run_id"] = "baseline"
    for run in (baseline, candidate):
        for row in run["predictions"]:
            row["latency"]["measurement_scope"] = "server_execution_to_completed_answer"
    if scenario == "slower":
        for row in candidate["predictions"]:
            row["latency"]["complete_response_ms"] += 100
        reason = "PERFORMANCE_REGRESSION_OBSERVED"
    elif scenario == "unreviewed":
        candidate_judgments = None
        reason = "CANDIDATE_PROFESSIONAL_CONFIRMATION_NOT_PASS"
    else:
        row = candidate["predictions"][0]
        row["run_state"], row["answer_complete"], row["stages"] = "FAILED", False, {}
        candidate_judgments["judgments"].pop(0)
        reason = "PERFORMANCE_COMPARISON_INCOMPLETE"
    report = compare(dataset, baseline, candidate, baseline_judgments, candidate_judgments)
    assert report["adoption_guard"]["automatic_adoption_allowed"] is False
    assert report["adoption_guard"]["status"] == "BLOCKED"
    assert reason in report["adoption_guard"]["blocking_reasons"]
    if scenario == "failed":
        assert report["candidate"]["samples"][0]["stages"]["candidate"]["status"] == "UNKNOWN"
        assert report["pairs"][0]["complete_response_ms_delta"] is None
    if scenario == "slower":
        assert report["adoption_guard"]["slower_comparable_pair_count"] == 2
        assert report["pairs"][0]["complete_response_ms_delta"] == 100
