"""Strict, offline evaluation of frozen exports; never imports the serving stack.

The JSON schema is the structural contract. Cross-document and interval checks below
are additional requirements. Reports are an explicit allowlist: no question, answer,
source text, annotation prose, or reviewer rationale is copied into them.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "evals" / "rag" / "schema.json"
STAGES = ("candidate", "final", "read")
IDENTITY = ("resource_id", "version_id", "block_id", "content_sha256", "valid_from", "valid_to")
LABELS = ("supported", "partial", "contradicted", "insufficient")
KINDS = ("dataset", "predictions", "judgments", "capture", "source_manifest")


class EvaluationInputError(ValueError):
    """A safe error code, never a serialization of private input or filenames."""

    def __init__(self, code: str, path: str = "$") -> None:
        self.code, self.path = code, path
        super().__init__(f"{code} at {path}")

    def as_dict(self) -> dict:
        return {"status": "INVALID", "code": self.code, "path": self.path}


def _fail(code: str, path: str = "$") -> None:
    raise EvaluationInputError(code, path)


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    _fail("NON_FINITE_NUMBER")


def _finite_tree(value: Any) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            _fail("NON_FINITE_NUMBER")
    elif isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail("NON_STRING_JSON_KEY")
            _finite_tree(child)
    elif isinstance(value, list):
        for child in value:
            _finite_tree(child)
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            _fail("INVALID_UNICODE_STRING")


def load_json(path: str | Path) -> dict:
    """Read one explicitly provided local JSON export, rejecting lossy JSON forms."""
    try:
        with Path(path).open(encoding="utf-8") as stream:
            result = json.load(stream, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)
    except EvaluationInputError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        _fail("INVALID_JSON")
    _finite_tree(result)
    return result


@lru_cache(maxsize=1)
def _schema() -> dict:
    schema = load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return schema


def _safe_path(path: Any) -> str:
    # Schema errors can contain user-supplied property names. Never echo those.
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else ".*" for p in path)


def _unique(rows: list[dict], fields: tuple[str, ...], code: str) -> None:
    keys = [tuple(row[field] for field in fields) for row in rows]
    if len(set(keys)) != len(keys):
        _fail(code)


def _check_source(source: dict) -> None:
    if source["span"]["end"] <= source["span"]["start"]:
        _fail("INVALID_SPAN_INTERVAL")
    if source["valid_to"] is not None and source["valid_to"] <= source["valid_from"]:
        _fail("INVALID_VALIDITY_INTERVAL")


def _check_item_ranks(items: list[dict]) -> None:
    ranks: dict = {}
    units: dict = {}
    for item in items:
        rank, unit = item["rank"], item.get("unit_id")
        # One retrieval unit can contain several original block spans. They share
        # a rank; flattening them into consecutive ranks would corrupt rank metrics.
        if rank in ranks and (unit is None or ranks[rank] != unit):
            _fail("AMBIGUOUS_STAGE_RANK")
        ranks[rank] = unit
        if unit is not None:
            identity = (rank, item["source"]["resource_id"], item["source"]["version_id"])
            if unit in units and units[unit] != identity:
                _fail("INCONSISTENT_UNIT_IDENTITY")
            units[unit] = identity


def _valid_on(source: dict, as_of: str) -> bool:
    return source["valid_from"] <= as_of and (
        source["valid_to"] is None or as_of < source["valid_to"]
    )


def validate_document(document: dict, kind: str | None = None) -> None:
    """Validate schema plus local invariants. No network, model, corpus, or DB read."""
    _finite_tree(document)
    if not isinstance(document, dict):
        _fail("EXPECTED_JSON_OBJECT")
    kind = kind or document.get("kind")
    if kind not in KINDS:
        _fail("UNKNOWN_DOCUMENT_KIND")
    schema = _schema()
    validator = Draft202012Validator(
        {"$ref": f"#/$defs/{kind}", "$defs": schema["$defs"]}, format_checker=FormatChecker()
    )
    error = next(validator.iter_errors(document), None)
    if error is not None:
        _fail("SCHEMA_VALIDATION", _safe_path(error.absolute_path))

    if kind == "dataset":
        _unique(document["cases"], ("case_id",), "DUPLICATE_CASE_ID")
        for case in document["cases"]:
            review, provenance = case["review_status"], case["review_provenance"]
            if review == "expert_confirmed" and (
                document["evidence_domain"] != "professional"
                or provenance is None or provenance["kind"] != "human_expert"
            ):
                _fail("EXPERT_PROVENANCE_REQUIRED")
            if review == "engineering_confirmed" and (
                document["evidence_domain"] != "synthetic"
                or provenance is None or provenance["kind"] != "human_engineering"
            ):
                _fail("ENGINEERING_PROVENANCE_REQUIRED")
            _unique(case["required_evidence_groups"], ("group_id",), "DUPLICATE_GROUP_ID")
            for group in case["required_evidence_groups"]:
                _unique(group["alternatives"], ("alternative_id",), "DUPLICATE_ALTERNATIVE_ID")
                for alternative in group["alternatives"]:
                    for source in alternative["spans"]:
                        _check_source(source)
                        if not _valid_on(source, case["as_of"]):
                            _fail("GOLD_OUTSIDE_AS_OF")
    elif kind == "predictions":
        _unique(document["predictions"], ("case_id", "sample_id"), "DUPLICATE_PREDICTION")
        for prediction in document["predictions"]:
            if prediction.get("run_state", "COMPLETED") != "COMPLETED" and prediction["answer_complete"]:
                _fail("INCOMPLETE_RUN_CANNOT_HAVE_COMPLETE_ANSWER")
            _unique(prediction["claims"], ("claim_id",), "DUPLICATE_CLAIM_ID")
            for stage in prediction["stages"].values():
                if stage is None:
                    continue
                if stage["status"] == "complete" and stage.get("failure_codes"):
                    _fail("COMPLETE_STAGE_WITH_INCOMPLETE_DIAGNOSTICS")
                _check_item_ranks(stage["items"])
                for item in stage["items"]:
                    _check_source(item["source"])
            if "answer_text" in prediction:
                digest = hashlib.sha256(prediction["answer_text"].encode("utf-8")).hexdigest()
                if prediction["answer_sha256"] != digest:
                    _fail("ANSWER_HASH_MISMATCH")
            timing = prediction.get("latency", {})
            complete, first = timing.get("complete_response_ms"), timing.get("first_token_ms")
            if complete is not None and first is not None and first > complete:
                _fail("TIMING_ORDER_INVALID")
    elif kind == "judgments":
        _unique(document["judgments"], ("case_id", "sample_id"), "DUPLICATE_JUDGMENT")
        for judgment in document["judgments"]:
            _unique(judgment["claims"], ("claim_id",), "DUPLICATE_CLAIM_JUDGMENT")
    elif kind == "source_manifest":
        _unique(document["sources"], IDENTITY[:4], "AMBIGUOUS_SOURCE_METADATA")
        for source in document["sources"]:
            _check_source({**source, "span": {"start": 0, "end": 1}})
    elif kind == "capture":
        _unique(document["records"], ("case_id", "sample_id"), "DUPLICATE_CAPTURE_SAMPLE")
        for record in document["records"]:
            direct, nested = record.get("retrieval_trace"), record.get("search_result", {}).get("retrieval_trace")
            if direct is not None and nested is not None and direct != nested:
                _fail("AMBIGUOUS_RETRIEVAL_TRACE")
            trace = direct if direct is not None else nested
            if trace is not None:
                if trace["reranked_count"] + trace["unscored_count"] != trace["candidate_count"]:
                    _fail("TRACE_COUNTS_INCONSISTENT")
                if "units" in trace:
                    units = trace["units"]
                    _unique(units, ("unit_id",), "DUPLICATE_TRACE_UNIT")
                    if (len(units) != trace["candidate_count"]
                            or sum(unit["reranked"] for unit in units) != trace["reranked_count"]
                            or sorted(unit["rank"] for unit in units) != list(range(1, len(units) + 1))):
                        _fail("TRACE_UNITS_INCONSISTENT")
                    for unit in units:
                        for span in unit["source_spans"]:
                            if "start" in span and "end" in span and span["end"] <= span["start"]:
                                _fail("INVALID_TRACE_SPAN")


def validate_bundle(dataset: dict, predictions: dict, judgments: dict | None = None) -> None:
    """Reject unknown case/sample/run/claim/group references instead of dropping rows."""
    validate_document(dataset, "dataset")
    validate_document(predictions, "predictions")
    if predictions["dataset_id"] != dataset["dataset_id"]:
        _fail("DATASET_ID_MISMATCH")
    cases = {case["case_id"]: case for case in dataset["cases"]}
    expected = {(case["case_id"], sid) for case in cases.values() for sid in case["expected_sample_ids"]}
    rows = {(row["case_id"], row["sample_id"]): row for row in predictions["predictions"]}
    if set(rows) - expected:
        _fail("UNKNOWN_PREDICTION_CASE_OR_SAMPLE")
    exposure = predictions.get("exposure")
    if exposure and set(exposure["development_case_ids"]) - set(cases):
        _fail("UNKNOWN_EXPOSURE_CASE")
    # External development families are allowed: they may refer to material outside
    # this benchmark, but every overlap with a holdout family is still a leak.
    if judgments is None:
        return
    validate_document(judgments, "judgments")
    if judgments["dataset_id"] != dataset["dataset_id"]:
        _fail("JUDGMENT_DATASET_MISMATCH")
    if judgments["run_id"] != predictions["run_id"]:
        _fail("JUDGMENT_RUN_MISMATCH")
    for judgment in judgments["judgments"]:
        key = judgment["case_id"], judgment["sample_id"]
        if key not in expected:
            _fail("UNKNOWN_JUDGMENT_CASE_OR_SAMPLE")
        if key not in rows:
            _fail("ORPHAN_JUDGMENT_WITHOUT_PREDICTION")
        claims = {claim["claim_id"] for claim in rows[key]["claims"]}
        groups = {group["group_id"] for group in cases[key[0]]["required_evidence_groups"]}
        for claim in judgment["claims"]:
            if claim["claim_id"] not in claims:
                _fail("UNKNOWN_JUDGMENT_CLAIM")
            if set(claim["evidence_group_ids"]) - groups:
                _fail("UNKNOWN_JUDGMENT_EVIDENCE_GROUP")


def _status(statuses: list[str]) -> str:
    if "FAIL" in statuses:
        return "FAIL"
    return "PASS" if statuses and all(value == "PASS" for value in statuses) else "UNKNOWN"


def _union_length(intervals: list[tuple[int, int]]) -> int:
    total, end = 0, -1
    for left, right in sorted(intervals):
        total += max(0, right - max(left, end))
        end = max(end, right)
    return total


def _span_result(required: dict, items: list[dict], as_of: str, date_matches: bool) -> dict:
    intervals: list[tuple[int, int]] = []
    start, end = required["span"]["start"], required["span"]["end"]
    first_rank = None
    mismatches: Counter = Counter()
    for item in sorted(items, key=lambda row: row["rank"]):
        observed = item["source"]
        if not date_matches or not _valid_on(observed, as_of):
            mismatches["AS_OF_OR_VALIDITY_MISMATCH"] += 1
            continue
        different = [field for field in IDENTITY if required[field] != observed[field]]
        if different:
            for field in different:
                mismatches[f"MISMATCH_{field.upper()}"] += 1
            continue
        left, right = max(start, observed["span"]["start"]), min(end, observed["span"]["end"])
        if left < right:
            intervals.append((left, right))
        if first_rank is None and _union_length(intervals) == end - start:
            first_rank = item["rank"]
    covered = _union_length(intervals)
    return {
        "source": {field: required[field] for field in IDENTITY},
        "required_span": dict(required["span"]), "required_chars": end - start,
        "covered_chars": covered, "fully_covered": first_rank is not None,
        "first_full_rank": first_rank, "mismatch_counts": dict(sorted(mismatches.items())),
        "failure_codes": [] if first_rank is not None else [
            "SPAN_PARTIAL" if covered else "NO_MATCHING_SPAN"
        ],
    }


def _stage_result(case: dict, prediction: dict | None, name: str) -> dict:
    groups = case["required_evidence_groups"]
    stage = prediction["stages"].get(name) if prediction else None
    complete = stage is not None and stage["status"] == "complete"
    evaluable = complete and bool(groups)
    reasons = list(stage.get("failure_codes", [])) if stage else []
    if prediction is None:
        reasons.append("MISSING_PREDICTION")
    if stage is None:
        reasons.append("MISSING_STAGE")
    elif not complete:
        reasons.append("INCOMPLETE_STAGE")
    if not groups:
        reasons.append("ZERO_GOLD_GROUPS")
    date_matches = prediction is not None and prediction["as_of"] == case["as_of"]
    if prediction is not None and not date_matches:
        reasons.append("PREDICTION_AS_OF_MISMATCH")
    results = []
    for group in groups:
        alternatives = []
        for alternative in group["alternatives"]:
            spans = [_span_result(source, stage["items"] if stage else [], case["as_of"], date_matches)
                     for source in alternative["spans"]]
            covered = all(span["fully_covered"] for span in spans)
            alternatives.append({
                "alternative_id": alternative["alternative_id"], "fully_covered": covered,
                "first_full_rank": max(span["first_full_rank"] for span in spans) if covered else None,
                "spans": spans,
            })
        ranks = [alt["first_full_rank"] for alt in alternatives if alt["fully_covered"]]
        results.append({
            "group_id": group["group_id"], "critical": group["critical"],
            "status": ("PASS" if ranks else "FAIL") if evaluable else "UNKNOWN",
            "observed_fully_covered": bool(ranks), "first_full_rank": min(ranks) if ranks else None,
            "alternatives": alternatives,
        })
    hits = sum(row["observed_fully_covered"] for row in results)
    critical = [row for row in results if row["critical"]]
    all_covered = hits == len(groups) if evaluable else None
    all_critical = all(row["observed_fully_covered"] for row in critical) if evaluable and critical else None
    if evaluable and not all_covered:
        reasons.append("REQUIRED_EVIDENCE_NOT_COVERED")
    return {
        "status": ("PASS" if all_covered else "FAIL") if evaluable else "UNKNOWN",
        "stage_present": stage is not None, "stage_complete": complete,
        "observation_scope": stage.get("observation_scope", "frozen_export") if stage else None,
        "rank_basis": stage.get("rank_basis", "export_rank") if stage else None,
        "required_group_count": len(groups), "observed_covered_group_count": hits,
        "recall": hits / len(groups) if evaluable else None,
        "all_required_groups_covered": all_covered, "critical_group_count": len(critical),
        "all_critical_groups_covered": all_critical,
        "all_required_groups_rank": max(row["first_full_rank"] for row in results) if all_covered else None,
        "critical_groups_rank": max(row["first_full_rank"] for row in critical) if all_critical else None,
        "mean_group_reciprocal_rank": (
            sum(1 / row["first_full_rank"] if row["first_full_rank"] else 0 for row in results) / len(groups)
        ) if evaluable else None,
        "failure_codes": reasons, "groups": results,
    }


def _semantic_result(case: dict, prediction: dict | None, judgment: dict | None) -> dict:
    claims = prediction["claims"] if prediction else []
    reasons = []
    trusted_review = False
    if prediction is None:
        reasons.append("MISSING_PREDICTION")
    elif not prediction["answer_complete"]:
        reasons.append("ANSWER_INCOMPLETE")
    if not claims:
        reasons.append("ZERO_ANSWER_CLAIMS")
    if judgment is None:
        reasons.append("MISSING_HUMAN_JUDGMENT")
    else:
        provenance = judgment["review_provenance"]
        trusted_review = judgment["review_status"] == "human_reviewed" and provenance is not None
        if not trusted_review:
            reasons.append("JUDGMENT_NOT_HUMAN_REVIEWED")
        if prediction is None or judgment["answer_sha256"] != prediction["answer_sha256"]:
            reasons.append("JUDGMENT_ANSWER_HASH_MISMATCH")
            trusted_review = False
        if not judgment["inventory_complete"]:
            reasons.append("CLAIM_INVENTORY_NOT_CONFIRMED")
    by_id = {row["claim_id"]: row for row in judgment["claims"]} if judgment else {}
    missing = [claim["claim_id"] for claim in claims if claim["claim_id"] not in by_id]
    if missing:
        reasons.append("MISSING_CLAIM_JUDGMENTS")
    counts = Counter(row["label"] for row in by_id.values()) if trusted_review else Counter()
    if trusted_review and any(
        row["label"] == "supported" and not row["evidence_group_ids"] for row in by_id.values()
    ):
        reasons.append("SUPPORTED_CLAIM_WITHOUT_EVIDENCE_BINDING")
    # IDs only bind a human's decision to this export. No lexical/ID entailment inference.
    if trusted_review and (counts["partial"] or counts["contradicted"]):
        status = "FAIL"
        reasons.append("HUMAN_FOUND_PARTIAL_OR_CONTRADICTED_CLAIM")
    elif reasons or counts["insufficient"]:
        status = "UNKNOWN"
        if counts["insufficient"]:
            reasons.append("HUMAN_INSUFFICIENT_EVIDENCE")
    else:
        status = "PASS"
    return {
        "status": status, "expected_claim_count": len(claims),
        "human_reviewed_claim_count": sum(counts.values()), "missing_claim_ids": missing,
        "human_label_counts": {label: counts[label] for label in LABELS},
        "claim_outcomes": [{"claim_id": row["claim_id"], "label": row["label"],
                            "evidence_group_ids": row["evidence_group_ids"]}
                           for row in by_id.values()] if trusted_review else [],
        "human_expert_reviewed": bool(trusted_review and judgment["review_provenance"]["kind"] == "human_expert"),
        "failure_codes": reasons,
    }


def _leakage(dataset: dict, run: dict) -> dict:
    dev = {case["family_id"] for case in dataset["cases"] if case["split"] == "dev"}
    holdout = {case["family_id"] for case in dataset["cases"] if case["split"] == "holdout"}
    cases = {case["case_id"]: case for case in dataset["cases"]}
    exposure = run.get("exposure")
    seen = set(exposure["development_family_ids"]) if exposure else set()
    if exposure:
        seen |= {cases[cid]["family_id"] for cid in exposure["development_case_ids"]}
    split_overlap, exposed = sorted(dev & holdout), sorted(seen & holdout)
    declared = exposure is not None and exposure["attestation"] == "declared"
    reasons = []
    if split_overlap:
        reasons.append("FAMILY_IN_BOTH_SPLITS")
    if exposed:
        reasons.append("HOLDOUT_FAMILY_USED_IN_DEVELOPMENT")
    if not declared:
        reasons.append("EXPOSURE_HISTORY_UNKNOWN")
    return {
        "status": "FAIL" if split_overlap or exposed else "PASS" if declared else "UNKNOWN",
        "split_overlap_family_ids": split_overlap, "exposed_holdout_family_ids": exposed,
        "holdout_family_count": len(holdout), "exposure_attested": declared,
        "basis": "EXPLICIT_FAMILY_ASSIGNMENTS_AND_DECLARED_HISTORY_ONLY",
        "external_or_semantic_leakage_independently_verified": False, "failure_codes": reasons,
    }


def _timing(prediction: dict | None) -> dict:
    latency = prediction.get("latency", {}) if prediction else {}
    complete = prediction is not None and prediction["answer_complete"]
    value = latency.get("complete_response_ms") if complete else None
    return {
        "complete_response_ms": value,
        "cache_state": latency.get("cache_state", "unknown"),
        "query_state": latency.get("query_state", "unknown"),
        "measurement_scope": latency.get("measurement_scope", "unknown"),
        "status": "KNOWN" if value is not None else "UNKNOWN",
        "failure_codes": [] if value is not None else ["COMPLETE_RESPONSE_TIMING_MISSING_OR_INCOMPLETE"],
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def _distribution(values: list[float], expected: int) -> dict:
    return {"expected_sample_count": expected, "measured_sample_count": len(values),
            "unknown_sample_count": expected - len(values), "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95), "percentile_method": "nearest_rank"}


def _latency_groups(samples: list[dict]) -> list[dict]:
    groups: dict = defaultdict(list)
    for row in samples:
        timing = row["latency"]
        groups[(row["split"], timing["cache_state"], timing["query_state"], timing["measurement_scope"])].append(row)
    return [{"split": key[0], "cache_state": key[1], "query_state": key[2],
             "measurement_scope": key[3],
             "metric": "complete_response_ms", "first_token_used_as_complete_response": False,
             "case_count": len({row["case_id"] for row in rows}),
             **_distribution([row["latency"]["complete_response_ms"] for row in rows
                              if row["latency"]["complete_response_ms"] is not None], len(rows))}
            for key, rows in sorted(groups.items())]


def _stage_summary(samples: list[dict], name: str) -> dict:
    all_rows = [row["stages"][name] for row in samples]
    known = [row for row in all_rows if row["recall"] is not None]
    denominator = sum(row["required_group_count"] for row in known)
    critical = [row for row in all_rows if row["all_critical_groups_covered"] is not None]
    return {
        "status": _status([row["status"] for row in all_rows]), "expected_sample_count": len(all_rows),
        "known_sample_count": len(known), "unknown_sample_count": len(all_rows) - len(known),
        "known_only_macro_recall": sum(row["recall"] for row in known) / len(known) if known else None,
        "known_only_micro_recall": (
            sum(row["observed_covered_group_count"] for row in known) / denominator
        ) if denominator else None,
        "known_group_denominator": denominator,
        "full_required_coverage_sample_count": sum(row["all_required_groups_covered"] is True for row in known),
        "critical_evaluable_sample_count": len(critical),
        "critical_unknown_sample_count": len(all_rows) - len(critical),
        "critical_full_coverage_sample_count": sum(row["all_critical_groups_covered"] is True for row in critical),
        "known_only_critical_full_coverage_rate": (
            sum(row["all_critical_groups_covered"] for row in critical) / len(critical)
        ) if critical else None,
        "known_only_mean_group_reciprocal_rank": (
            sum(row["mean_group_reciprocal_rank"] for row in known) / len(known)
        ) if known else None,
    }


def evaluate(dataset: dict, predictions: dict, judgments: dict | None = None, *, split: str = "all") -> dict:
    """Evaluate every frozen expected case/sample in the selected, declared scope."""
    if split not in ("all", "dev", "holdout"):
        _fail("INVALID_SPLIT")
    validate_bundle(dataset, predictions, judgments)
    selected = [case for case in dataset["cases"] if split == "all" or case["split"] == split]
    if not selected:
        _fail("EMPTY_SELECTED_SCOPE")
    by_key = {(row["case_id"], row["sample_id"]): row for row in predictions["predictions"]}
    reviews = {(row["case_id"], row["sample_id"]): row for row in judgments["judgments"]} if judgments else {}
    samples = []
    for case in selected:
        for sid in case["expected_sample_ids"]:
            key = case["case_id"], sid
            prediction = by_key.get(key)
            stages = {name: _stage_result(case, prediction, name) for name in STAGES}
            semantic = _semantic_result(case, prediction, reviews.get(key))
            engineering = _status([row["status"] for row in stages.values()] + [semantic["status"]])
            expert = case["review_status"] == "expert_confirmed"
            professional_reasons = []
            if dataset["evidence_domain"] != "professional":
                professional_reasons.append("SYNTHETIC_IS_NOT_PROFESSIONAL_EVIDENCE")
            if not expert:
                professional_reasons.append("CASE_NOT_EXPERT_CONFIRMED")
            if not semantic["human_expert_reviewed"]:
                professional_reasons.append("ANSWER_NOT_EXPERT_REVIEWED")
            samples.append({
                "case_id": case["case_id"], "family_id": case["family_id"], "sample_id": sid,
                "split": case["split"], "review_status": case["review_status"],
                "prediction_present": prediction is not None, "stages": stages, "semantic": semantic,
                "engineering_status": engineering,
                "professional_status": "UNKNOWN" if professional_reasons else engineering,
                "professional_failure_codes": professional_reasons, "latency": _timing(prediction),
                "adapter_diagnostics": prediction.get("adapter_diagnostics", []) if prediction else [],
                "execution_observation": prediction.get("execution_observation") if prediction else None,
                "run_state": prediction.get("run_state", "UNKNOWN") if prediction else "UNKNOWN",
                "adapter_provenance": prediction.get("adapter_provenance") if prediction else None,
                "retrieval_observation": prediction.get("retrieval_observation") if prediction else None,
            })
    leakage = _leakage(dataset, predictions)
    present_ids = {row["case_id"] for row in samples if row["prediction_present"]}
    engineering = _status([row["engineering_status"] for row in samples] + [leakage["status"]])
    professional = _status([row["professional_status"] for row in samples] + [leakage["status"]])
    # Synthetic runs can never acquire a professional PASS, even if all fixtures pass.
    if dataset["evidence_domain"] == "synthetic":
        professional = "UNKNOWN"
    return {
        "schema_version": "1.0", "kind": "evaluation_report", "dataset_id": dataset["dataset_id"],
        "run_id": predictions["run_id"], "evidence_domain": dataset["evidence_domain"],
        "status": professional if dataset["evidence_domain"] == "professional" else engineering,
        "engineering_status": engineering, "professional_status": professional,
        "coverage": {
            "scope_id": dataset["coverage"]["scope_id"], "selected_split": split,
            "dataset_case_count": len(dataset["cases"]), "expected_case_count": len(selected),
            "present_case_count": len(present_ids), "expected_sample_count": len(samples),
            "present_sample_count": sum(row["prediction_present"] for row in samples),
            "expert_confirmed_case_count": sum(case["review_status"] == "expert_confirmed" for case in selected),
            "pending_case_count": sum(case["review_status"] == "pending" for case in selected),
            "zero_gold_case_count": sum(not case["required_evidence_groups"] for case in selected),
            "excluded_case_ids": [case["case_id"] for case in dataset["cases"] if case not in selected],
            "missing_case_ids": [case["case_id"] for case in selected if case["case_id"] not in present_ids],
            "missing_samples": [{"case_id": row["case_id"], "sample_id": row["sample_id"]}
                                for row in samples if not row["prediction_present"]],
            "scope_statement": "ONLY_FROZEN_LISTED_CASES_AND_EXPECTED_SAMPLES_NO_POPULATION_GENERALIZATION",
            "source_bytes_rehashed": False, "models_executed": False,
            "all_corpus_recall": "NOT_EVALUATED",
        },
        "leakage": leakage, "stages": {name: _stage_summary(samples, name) for name in STAGES},
        "semantic_status_counts": dict(Counter(row["semantic"]["status"] for row in samples)),
        "latency": _latency_groups(samples), "samples": samples,
        "privacy": {"query_text_included": False, "source_text_included": False,
                    "answer_text_included": False, "reviewer_rationale_included": False},
    }


def _delta_summary(values: list[float], expected: int) -> dict:
    scale = max((abs(value) for value in values), default=0)
    mean = math.fsum(value / scale for value in values) / len(values) * scale if scale else 0.0
    return {"expected_pair_count": expected, "comparable_pair_count": len(values),
            "unknown_pair_count": expected - len(values),
            "mean_candidate_minus_baseline": mean if values else None,
            "increased": sum(value > 0 for value in values), "decreased": sum(value < 0 for value in values),
            "unchanged": sum(value == 0 for value in values)}


def compare(dataset: dict, baseline: dict, candidate: dict, baseline_judgments: dict | None = None,
            candidate_judgments: dict | None = None, *, split: str = "all") -> dict:
    """Paired comparison over the frozen expected universe, never a successful intersection."""
    if baseline.get("run_id") == candidate.get("run_id"):
        _fail("PAIRED_RUN_IDS_MUST_DIFFER")
    before = evaluate(dataset, baseline, baseline_judgments, split=split)
    after = evaluate(dataset, candidate, candidate_judgments, split=split)
    pairs, latency_groups = [], defaultdict(list)
    for left, right in zip(before["samples"], after["samples"], strict=True):
        deltas = {}
        for stage in STAGES:
            a, b = left["stages"][stage], right["stages"][stage]
            deltas[stage] = {
                "baseline_status": a["status"], "candidate_status": b["status"],
                "recall_delta": b["recall"] - a["recall"] if a["recall"] is not None and b["recall"] is not None else None,
                "group_reciprocal_rank_delta": (
                    b["mean_group_reciprocal_rank"] - a["mean_group_reciprocal_rank"]
                ) if a["recall"] is not None and b["recall"] is not None else None,
                "critical_full_coverage_delta": (
                    int(b["all_critical_groups_covered"]) - int(a["all_critical_groups_covered"])
                ) if a["all_critical_groups_covered"] is not None and b["all_critical_groups_covered"] is not None else None,
            }
        a, b = left["latency"], right["latency"]
        same_cohort = all(a[field] == b[field] for field in ("cache_state", "query_state", "measurement_scope"))
        timed = same_cohort and a["complete_response_ms"] is not None and b["complete_response_ms"] is not None
        delta = b["complete_response_ms"] - a["complete_response_ms"] if timed else None
        pair = {
            "case_id": left["case_id"], "sample_id": left["sample_id"], "split": left["split"],
            "baseline_present": left["prediction_present"], "candidate_present": right["prediction_present"],
            "stages": deltas, "complete_response_ms_delta": delta,
            "latency_failure_codes": [] if timed else [
                "LATENCY_COHORT_MISMATCH" if not same_cohort else "MISSING_COMPLETE_RESPONSE_TIMING"
            ],
            "baseline_semantic_status": left["semantic"]["status"],
            "candidate_semantic_status": right["semantic"]["status"],
            "baseline_professional_status": left["professional_status"],
            "candidate_professional_status": right["professional_status"],
        }
        pairs.append(pair)
        cohort = (left["split"], a["cache_state"], a["query_state"], b["cache_state"], b["query_state"],
                  a["measurement_scope"], b["measurement_scope"])
        latency_groups[cohort].append((a["complete_response_ms"], b["complete_response_ms"], delta))
    summaries = {}
    for stage in STAGES:
        summaries[stage] = {metric: _delta_summary(
            [pair["stages"][stage][metric] for pair in pairs if pair["stages"][stage][metric] is not None], len(pairs)
        ) for metric in ("recall_delta", "group_reciprocal_rank_delta", "critical_full_coverage_delta")}
    latency = []
    for cohort, rows in sorted(latency_groups.items()):
        matched = [row for row in rows if row[2] is not None]
        latency.append({
            "split": cohort[0], "baseline_cache_state": cohort[1], "baseline_query_state": cohort[2],
            "candidate_cache_state": cohort[3], "candidate_query_state": cohort[4],
            "baseline_measurement_scope": cohort[5], "candidate_measurement_scope": cohort[6],
            "baseline": _distribution([row[0] for row in matched], len(rows)),
            "candidate": _distribution([row[1] for row in matched], len(rows)),
            "paired_candidate_minus_baseline": _distribution([row[2] for row in matched], len(rows)),
        })
    complete_pairs = sum(pair["baseline_present"] and pair["candidate_present"] for pair in pairs)
    leakage_status = _status([before["leakage"]["status"], after["leakage"]["status"]])
    slower = sum(pair["complete_response_ms_delta"] is not None and pair["complete_response_ms_delta"] > 0
                 for pair in pairs)
    timing_unknown = sum(pair["complete_response_ms_delta"] is None for pair in pairs)
    adoption_blocks = []
    if after["professional_status"] != "PASS":
        adoption_blocks.append("CANDIDATE_PROFESSIONAL_CONFIRMATION_NOT_PASS")
    if complete_pairs < len(pairs):
        adoption_blocks.append("INCOMPLETE_PAIR_COVERAGE")
    if leakage_status != "PASS":
        adoption_blocks.append("HOLDOUT_LEAKAGE_NOT_CLEARED")
    if slower:
        adoption_blocks.append("PERFORMANCE_REGRESSION_OBSERVED")
    if timing_unknown:
        adoption_blocks.append("PERFORMANCE_COMPARISON_INCOMPLETE")
    if any(row["latency"]["measurement_scope"] == "unknown"
           for report in (before, after) for row in report["samples"]):
        adoption_blocks.append("PERFORMANCE_MEASUREMENT_SCOPE_UNKNOWN")
    # This status describes evaluation completeness/validity, not candidate superiority.
    return {
        "schema_version": "1.0", "kind": "paired_evaluation_report",
        "status": _status([before["status"], after["status"], leakage_status]),
        "decision": "NO_AUTOMATIC_SUPERIORITY_CLAIM", "expected_pair_count": len(pairs),
        "adoption_guard": {
            "status": "BLOCKED" if adoption_blocks else "REVIEW_REQUIRED",
            "automatic_adoption_allowed": False, "blocking_reasons": adoption_blocks,
            "slower_comparable_pair_count": slower, "unknown_timing_pair_count": timing_unknown,
            "explicit_adoption_approval_recorded": False,
        },
        "present_on_both_count": complete_pairs, "missing_on_either_count": len(pairs) - complete_pairs,
        "pairing": "DATASET_CASE_ID_AND_FROZEN_SAMPLE_ID", "leakage_status": leakage_status,
        "stages": summaries, "latency": latency, "pairs": pairs,
        "baseline": before, "candidate": after,
    }


def adapt_capture(capture: dict, source_manifest: dict | None = None) -> dict:
    """Convert actual search traces/model snapshots, without reading gold or source bodies.

    A trace proves the observed candidate pool only. Final selections and read
    receipts must be explicitly exported. Effective dates come from an independent
    authorized metadata export bound to the *observed* hash, never from benchmark
    annotations. Missing metadata makes the affected stage incomplete/UNKNOWN.
    """
    validate_document(capture, "capture")
    if source_manifest is not None:
        validate_document(source_manifest, "source_manifest")
    sources = source_manifest["sources"] if source_manifest else []
    metadata = {tuple(row[field] for field in IDENTITY[:4]): row for row in sources}
    known_blocks = {key[:3] for key in metadata}
    predictions = []
    for record in capture["records"]:
        diagnostics: list[str] = []

        def bind_span(resource: str, version: str, span: dict, rank: int, unit_id: str | None = None):
            if any(field not in span for field in ("block_id", "content_sha256", "start", "end")):
                return None, "SPAN_OFFSETS_MISSING"
            key = resource, version, span["block_id"], span["content_sha256"]
            meta = metadata.get(key)
            if meta is None:
                return None, "SOURCE_HASH_NOT_IN_MANIFEST" if key[:3] in known_blocks else "SOURCE_METADATA_MISSING"
            if span["end"] <= span["start"]:
                _fail("INVALID_ADAPTER_SPAN")
            if "block_char_count" in meta and span["end"] > meta["block_char_count"]:
                _fail("SPAN_OUT_OF_BLOCK_BOUNDS")
            source = {field: meta[field] for field in IDENTITY}
            source["span"] = {"start": span["start"], "end": span["end"]}
            item = {"rank": rank, "source": source}
            if unit_id is not None:
                item["unit_id"] = unit_id
            return item, None

        def from_units(units: list[dict], scope: str, basis: str) -> dict:
            items, issues = [], []
            for index, unit in enumerate(units, 1):
                if not unit["source_spans"]:
                    issues.append("EMPTY_UNIT_SPANS")
                for span in unit["source_spans"]:
                    item, issue = bind_span(unit["resource_id"], unit["version_id"], span,
                                            unit["rank"] if basis == "trace_rank" else index, unit["unit_id"])
                    if issue:
                        issues.append(issue)
                    else:
                        items.append(item)
            return {"status": "incomplete" if issues else "complete", "items": items,
                    "rank_basis": basis, "observation_scope": scope, "failure_codes": sorted(set(issues))}

        trace = record.get("retrieval_trace", record.get("search_result", {}).get("retrieval_trace"))
        stages: dict = {}
        if trace is None:
            diagnostics.append("TRACE_MISSING")
        elif "units" not in trace:
            diagnostics.append("TRACE_SUMMARY_ONLY")
        else:
            stages["candidate"] = from_units(trace["units"], "current_authorized_candidate_pool", "trace_rank")
        if "final_unit_ids" in record:
            units = {unit["unit_id"]: unit for unit in trace.get("units", [])} if trace else {}
            if set(record["final_unit_ids"]) - set(units):
                _fail("UNKNOWN_FINAL_UNIT_ID")
            stages["final"] = from_units([units[uid] for uid in record["final_unit_ids"]],
                                          "explicit_final_selection", "explicit_order")
        else:
            diagnostics.append("FINAL_STAGE_NOT_EXPORTED")
        if "read_receipt" in record:
            receipt = record["read_receipt"]
            items, issues = [], []
            for row in receipt["items"]:
                item, issue = bind_span(row["resource_id"], row["version_id"], row, row["rank"], row.get("unit_id"))
                if issue:
                    issues.append(issue)
                else:
                    items.append(item)
            diagnostics.extend(issues)
            stages["read"] = {"status": "incomplete" if issues else receipt["status"], "items": items,
                              "rank_basis": "export_rank", "observation_scope": "explicit_read_receipt",
                              "failure_codes": sorted(set(issues))}
        else:
            diagnostics.append("READ_STAGE_NOT_EXPORTED")
        complete = record["run_state"] == "COMPLETED" and record.get("answer_complete", False)
        if record["run_state"] != "COMPLETED":
            diagnostics.append("RUN_NOT_COMPLETED")
        if not complete:
            diagnostics.append("ANSWER_NOT_CONFIRMED_COMPLETE")
        digest = record.get("answer_sha256")
        if "answer_text" in record:
            computed = hashlib.sha256(record["answer_text"].encode("utf-8")).hexdigest()
            if digest is not None and digest != computed:
                _fail("CAPTURE_ANSWER_HASH_MISMATCH")
            digest = computed
        timing = record.get("model_snapshot", {}).get("pipeline_timing")
        latency = {"cache_state": record.get("cache_state", "unknown"),
                   "query_state": record.get("query_state", "unknown"), "first_token_ms": None,
                   "measurement_scope": "server_execution_to_completed_answer",
                   "complete_response_ms": timing["execution_elapsed_ms"] if timing and complete else None}
        if timing is None:
            diagnostics.append("PIPELINE_TIMING_MISSING")
        diagnostics.extend(issue for stage in stages.values() for issue in stage.get("failure_codes", []))
        row = {
            "case_id": record["case_id"], "sample_id": record["sample_id"], "as_of": record["as_of"],
            "run_state": record["run_state"],
            "answer_complete": complete, "answer_sha256": digest,
            "claims": [{"claim_id": claim["claim_id"]} for claim in record.get("claims", [])],
            "stages": stages, "latency": latency, "adapter_diagnostics": sorted(set(diagnostics)),
            "adapter_provenance": {
                "adapter_version": "candidate_lineage_v1_to_rag_eval_v1",
                "source_manifest_id": source_manifest["manifest_id"] if source_manifest else None,
                "source_export_id": source_manifest["provenance"]["export_id"] if source_manifest else None,
                "source_bytes_rehashed": False,
            },
        }
        if trace is not None:
            row["retrieval_observation"] = {
                key: trace[key] for key in ("version", "scope", "candidate_count", "reranked_count",
                                           "unscored_count", "source_text_included", "all_corpus_recall",
                                           "semantic_support")
            }
            row["retrieval_observation"]["phases_ms"] = {
                key: value for key, value in trace.get("phases_ms", {}).items() if key in {
                    "initial_authority_ms", "candidate_search_ms", "candidate_verification_and_fusion_ms",
                    "reranking_ms", "final_authority_ms",
                }
            }
            for field, choices in {
                "timing_scope": {"single_query", "shared_batch"},
                "reranking_state": {"local_cross_encoder", "disabled", "unavailable"},
                "candidate_policy": {"complete_pool", "ranked_prefix"},
            }.items():
                value = trace.get(field)
                row["retrieval_observation"][field] = value if value in choices else "unknown"
            row["retrieval_observation"]["channel_counts"] = {
                key: value for key, value in trace.get("channel_counts", {}).items()
                if key in {"bm25", "vector", "catalog"}
            }
        if timing is not None:
            row["execution_observation"] = {
                field: timing[field] for field in ("execution_elapsed_ms", "phases", "durations_additive",
                                                  "first_visible_answer_ms", "first_visible_answer_status")
            }
        predictions.append(row)
    result = {"schema_version": "1.0", "kind": "predictions", "dataset_id": capture["dataset_id"],
              "run_id": capture["run_id"], "exposure": capture.get("exposure"), "predictions": predictions}
    validate_document(result, "predictions")
    return result
