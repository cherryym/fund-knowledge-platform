import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fusion_ablation", ROOT / "scripts/inspect-rag-fusion.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def trace():
    return {"version": "candidate_lineage_v1", "query": "PRIVATE_QUERY", "units": [
        {"unit_id": "A", "text": "PRIVATE_BODY", "channel_ranks": {"vector": 1, "bm25": 20}},
        {"unit_id": "B", "text": "PRIVATE_BODY", "channel_ranks": {"vector": 20, "bm25": 1}}]}


def test_counterfactual_fusion_preserves_union_and_does_not_claim_retrieval_or_quality():
    report = module.ablate(trace())
    assert report["profiles"]["vector_signal"]["unit_order"] == ["A", "B"]
    assert report["profiles"]["bm25_signal"]["unit_order"] == ["B", "A"]
    assert report["new_retrieval_executed"] is False and report["weight_optimality"] == "NOT_EVALUATED"
    assert "PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize("rank", [True, 0, -1, "1", float("nan")])
def test_invalid_ranks_cannot_create_a_counterfactual(rank):
    value = trace()
    value["units"][0]["channel_ranks"]["vector"] = rank
    with pytest.raises(ValueError, match="INVALID_CANDIDATE_RANKS"):
        module.ablate(value)


def test_duplicate_candidate_identity_is_rejected():
    value = trace()
    value["units"][1]["unit_id"] = "A"
    with pytest.raises(ValueError, match="INVALID_CANDIDATE_RANKS"):
        module.ablate(value)
