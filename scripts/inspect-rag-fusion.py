"""Body-free offline RRF ablation over a frozen candidate pool, not a new search."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def ablate(trace):
    if (not isinstance(trace, dict) or trace.get("version") != "candidate_lineage_v1"
            or not isinstance(trace.get("units"), list)):
        raise ValueError("CANDIDATE_TRACE_REQUIRED")
    profiles = {"balanced": {"vector": .45, "bm25": .45, "catalog": .1},
                "vector_signal": {"vector": 1., "bm25": 0., "catalog": 0.},
                "bm25_signal": {"vector": 0., "bm25": 1., "catalog": 0.},
                "exact_signal": {"vector": .3, "bm25": .6, "catalog": .1}}
    units = trace["units"]
    if (any(not isinstance(row, dict) or not isinstance(row.get("unit_id"), str)
            or not isinstance(row.get("channel_ranks"), dict)
            or any(k not in {"vector", "bm25", "catalog"} or type(v) is not int or v < 1
                   for k, v in row["channel_ranks"].items()) for row in units)
            or len({row["unit_id"] for row in units}) != len(units)):
        raise ValueError("INVALID_CANDIDATE_RANKS")
    return {"scope": "counterfactual_order_of_supplied_candidate_union", "new_retrieval_executed": False,
        "model_calls": 0, "source_text_included": False, "weight_optimality": "NOT_EVALUATED",
        "professional_accuracy": "NOT_EVALUATED", "profiles": {
            name: {"weights": weights, "unit_order": [row["unit_id"] for row in sorted(units,
                key=lambda row: (-sum(weights[k] / (60 + rank) for k, rank in row["channel_ranks"].items()), row["unit_id"]))]}
            for name, weights in profiles.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New file, never overwritten")
    args = parser.parse_args()
    try:
        payload = json.loads(args.trace.read_text(encoding="utf-8"))
        result = ablate(payload.get("retrieval_trace", payload))
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    except (ValueError, TypeError, OSError, AttributeError):
        print('{"status":"INVALID","code":"FUSION_ABLATION_INPUT_OR_OUTPUT_INVALID"}')
        return 2
    print('{"status":"CREATED","evaluation":"COUNTERFACTUAL_NOT_LIVE_RECALL"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
