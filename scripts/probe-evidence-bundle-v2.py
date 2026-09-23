#!/usr/bin/env python3
"""Paired real local Qwen probe, synthetic data only; no network or business DB."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from fund_kb.qwen_reranker import QwenReranker
from fund_kb.qwen_reranker_spec import QWEN_RERANKER_SPEC

CASES = [
    ("合成图书馆周六几点开门？", ["该合成图书馆周六上午九点开门。", "该停车场周六上午七点收费。", "图书馆的书架是木制的。"], 0),
    ("合成维修流程开始之前有什么前提？", ["维修报告使用蓝色封面。", "合成操作规程：检修之前必须断电并确认能源隔离。", "检修完成后归档报告。"], 1),
    ("合成指引要求谁复核计算结果？", ["资料录入人员负责归档。", "午餐时间由行政部安排。", "合成指引：计算结果由独立复核人员核对。"], 2),
    ("Where are backup copies stored in this synthetic system?", ["The meeting starts at nine.", "Backup copies are stored in encrypted off-site storage.", "The developer reviews code changes."], 1),
]


def reference_scores(model, requests):
    """Frozen pre-v2 algorithm: every frame computed, same framing/dtype/batch."""
    with model.tokenization_scope():
        scores = [[-math.inf] * len(texts) for _, texts in requests]
        work = []
        for i, (query, texts) in enumerate(requests):
            for j, text in enumerate(texts):
                work.extend((i, j, ids) for ids, _ in model._frames(query, text))
        work.sort(key=lambda row: len(row[2]))
        for start in range(0, len(work), model.batch_size):
            batch = work[start:start + model.batch_size]
            values = model._forward([row[2] for row in batch])
            for (i, j, _), score in zip(batch, values, strict=True):
                scores[i][j] = max(scores[i][j], float(score))
        return scores, len(work)


def ranks(rows):
    return [sorted(range(len(row)), key=lambda i: (-row[i], i)) for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--device", choices=("cpu", "mps"), default="mps")
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("OUTPUT_ALREADY_EXISTS")
    helper_spec = importlib.util.spec_from_file_location("offline_guard", ROOT / "scripts/prepare-universal-models.py")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    helper.configure_download_environment()
    import torch
    torch.set_num_threads(4)
    settings = SimpleNamespace(reranker_model=QWEN_RERANKER_SPEC["repo"], reranker_revision=QWEN_RERANKER_SPEC["revision"],
        reranker_model_path=args.directory, reranker_device=args.device, reranker_dtype="bfloat16",
        reranker_max_tokens=2048, reranker_batch_size=2)
    # A fixed duplicate-heavy workload models shared snippets; it is not a
    # representative production latency sample and is clearly labelled below.
    unique = [(q, texts) for q, texts, _ in CASES]
    repeated = [(q, texts * 3) for q, texts, _ in CASES] * 2
    report = {"model": settings.reranker_model, "revision": settings.reranker_revision,
        "cloud_generation_calls": 0, "business_data_read": False, "network_allowed": False,
        "professional_accuracy": "NOT_EVALUATED", "end_to_end_latency": "NOT_EVALUATED",
        "workload": "fixed_synthetic_duplicate_heavy_and_unique_controls"}
    with helper.offline_probe_guard():
        model = QwenReranker(settings)
        try:
            started = time.perf_counter()
            initial = model.score_many(unique)
            report["cold_seconds"] = time.perf_counter() - started
            report["relevance_cases"] = [{"case": i + 1, "expected": expected,
                "actual": ranks([scores])[0][0], "pass": ranks([scores])[0][0] == expected}
                for i, (scores, (_, _, expected)) in enumerate(zip(initial, CASES, strict=True))]
            report["paired_runs"] = []
            for i in range(5):
                results = {}
                for mode in (["before", "after"] if i % 2 == 0 else ["after", "before"]):
                    started = time.perf_counter()
                    if mode == "before":
                        values, windows = reference_scores(model, repeated)
                    else:
                        values = model.score_many(repeated)
                        windows = model.last_diagnostics["computed_window_count"]
                    results[mode] = {"seconds": time.perf_counter() - started, "computed_windows": windows, "scores": values}
                left, right = results["before"].pop("scores"), results["after"].pop("scores")
                results.update(rank_equal=ranks(left) == ranks(right),
                    max_score_delta=max(abs(a - b) for x, y in zip(left, right, strict=True) for a, b in zip(x, y, strict=True)))
                report["paired_runs"].append(results)
                print(json.dumps({"pair": i + 1, **results}), flush=True)
            reference, _ = reference_scores(model, unique)
            current = model.score_many(unique)
            report["unique_control_rank_equal"] = ranks(reference) == ranks(current)
            report["before_median_seconds"] = statistics.median(r["before"]["seconds"] for r in report["paired_runs"])
            report["after_median_seconds"] = statistics.median(r["after"]["seconds"] for r in report["paired_runs"])
            report["status"] = "PASS" if (all(c["pass"] for c in report["relevance_cases"])
                and all(r["rank_equal"] for r in report["paired_runs"]) and report["unique_control_rank_equal"]) else "FAIL"
        finally:
            model.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k not in {"paired_runs", "relevance_cases"}}), flush=True)
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
