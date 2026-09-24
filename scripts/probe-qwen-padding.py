"""Paired offline Qwen batching probe; built-in synthetic texts and local weights only.

Running this entrypoint loads real weights and needs separate authorization.
The insertion-order reference is a counterfactual, not the pre-change checkout
(which already sorted frames by length). Reports contain no input text or paths.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import importlib.util
import json
import math
import os
from pathlib import Path
import socket
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from fund_kb.qwen_reranker import BATCHING_STRATEGY, QwenReranker
from fund_kb.qwen_reranker_spec import QWEN_RERANKER_SPEC

INSERTION_ORDER = "insertion_order_reference_v1"
STRATEGIES = (INSERTION_ORDER, BATCHING_STRATEGY)


def _script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_cases():
    # Reuse the existing probe's four built-in cases, without running its main.
    cases = _script("probe-qwen-reranker").CASES
    requests = [(query, list(passages)) for query, passages, _ in cases]
    expected_tops = [correct for _, _, correct in cases]
    answer = "合成关键材料：图书馆每周六上午九点对读者开放。"
    background = "这是一段合成背景，讨论室内灯光颜色，不包含开放时间。" * 180
    local = background + answer
    requests.append(("图书馆周六几点开门？", [answer, local, background, local * 2, local]))
    return requests, expected_tops, len(requests) - 1


@contextmanager
def offline_guard(helper):
    # Reuse the existing credential/TCP guard; also block DNS, connect_ex and UDP.
    # This is a regression guard for this standalone process, not an OS sandbox.
    def forbidden(*args, **kwargs):
        raise RuntimeError("LOCAL_PROBE_NETWORK_OR_TOKEN_ACCESS_FORBIDDEN")

    with helper.offline_probe_guard(), ExitStack() as stack:
        for owner, names in ((socket, ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr")),
                             (socket.socket, ("connect_ex", "sendto", "sendmsg"))):
            for name in names:
                if hasattr(owner, name):
                    stack.enter_context(patch.object(owner, name, forbidden))
        yield


class ProbeReranker(QwenReranker):
    """Only scheduling differs; traces stay in memory and never enter JSON."""

    def _frames(self, query, text):
        frames = super()._frames(query, text)
        self._trace.append((query, tuple((tuple(ids), loc["start_char"], loc["end_char"])
                                         for ids, loc in frames)))
        covered = 0
        for _, loc in frames:
            if not 0 <= loc["start_char"] <= covered or not loc["start_char"] <= loc["end_char"] <= len(text):
                raise RuntimeError("PROBE_WINDOW_COVERAGE_FAILED")
            covered = max(covered, loc["end_char"])
        if not frames or covered != len(text):
            raise RuntimeError("PROBE_WINDOW_COVERAGE_FAILED")
        return frames

    def _ordered_frame_keys(self, unique):
        self._scheduled = (list(unique) if self.batching_strategy == INSERTION_ORDER
                           else super()._ordered_frame_keys(unique))
        return self._scheduled

    def _forward(self, rows):
        keys = self._scheduled[self._cursor:self._cursor + len(rows)]
        if [key[1] for key in keys] != [tuple(row) for row in rows]:
            raise RuntimeError("PROBE_FORWARD_MAPPING_FAILED")
        values = super()._forward(rows)
        self._window_values.update(zip(keys, values, strict=True))
        self._cursor += len(rows)
        return values

    def _synchronize(self):
        if self.device == "mps":
            self._torch.mps.synchronize()

    def measure(self, requests, strategy):
        if strategy not in STRATEGIES:
            raise RuntimeError("PROBE_UNKNOWN_STRATEGY")
        self.batching_strategy = strategy
        self._trace, self._scheduled, self._window_values, self._cursor = [], [], {}, 0
        self._synchronize()
        started = time.monotonic()
        scores = self.score_many(requests)
        self._synchronize()
        seconds = time.monotonic() - started
        if self._cursor != self.last_diagnostics["computed_window_count"]:
            raise RuntimeError("PROBE_FORWARD_COUNT_FAILED")
        documents = []
        for query, frames in self._trace:
            values = [float(self._window_values[query, ids]) for ids, _, _ in frames]
            documents.append({"window_count": len(values), "distinct_window_count": len({ids for ids, _, _ in frames}),
                "window_scores": values, "max": max(values), "min": min(values),
                "mean": statistics.fmean(values)})
        flat_scores = [value for row in scores for value in row]
        if flat_scores != [document["max"] for document in documents]:
            raise RuntimeError("PROBE_MAX_MAPPING_FAILED")
        return {"seconds": seconds, "scores": scores, "diagnostics": self.last_diagnostics,
                "documents": documents, "trace": tuple(self._trace)}


def _rank(scores):
    return sorted(range(len(scores)), key=lambda index: (-scores[index], index))


def _comparison(before, after, atol, rtol):
    if before["trace"] != after["trace"]:
        raise RuntimeError("PROBE_TOKENS_OR_WINDOW_MAPPING_CHANGED")
    left, right = before["diagnostics"], after["diagnostics"]
    for key in ("requests", "pair_count", "window_count", "computed_window_count", "reused_window_count",
                "actual_useful_tokens", "aggregation", "dtype", "model", "revision", "device"):
        if left[key] != right[key]:
            raise RuntimeError("PROBE_PAIRING_CHANGED")
    pairs = list(zip((v for row in before["scores"] for v in row),
                     (v for row in after["scores"] for v in row), strict=True))
    windows = list(zip((v for doc in before["documents"] for v in doc["window_scores"]),
                       (v for doc in after["documents"] for v in doc["window_scores"]), strict=True))
    ranks = [(_rank(a), _rank(b)) for a, b in zip(before["scores"], after["scores"], strict=True)]
    padded_before, padded_after = left["actual_padded_tokens"], right["actual_padded_tokens"]
    return {"exact_token_and_mapping_match": True, "complete_window_coverage": True,
        "max_abs_candidate_logit_delta": max((abs(a - b) for a, b in pairs), default=0.0),
        "max_abs_window_logit_delta": max((abs(a - b) for a, b in windows), default=0.0),
        "within_tolerance": all(abs(a - b) <= atol + rtol * max(abs(a), abs(b)) for a, b in pairs + windows),
        "same_full_ranking": all(a == b for a, b in ranks),
        "same_top1": all(a[:1] == b[:1] for a, b in ranks),
        "padded_token_reduction": padded_before - padded_after,
        "padded_token_reduction_fraction": (padded_before - padded_after) / padded_before if padded_before else 0.0}


def _metrics(snapshot):
    diag = snapshot["diagnostics"]
    keys = ("batching_strategy", "actual_batch_count", "actual_useful_tokens", "actual_padded_tokens",
            "actual_padding_tokens", "computed_window_count", "reused_window_count", "window_count")
    return {"seconds": snapshot["seconds"], **{key: diag[key] for key in keys}}


def run_probe(model, repeats=2, atol=0.05, rtol=0.01):
    requests, expected_tops, long_index = synthetic_cases()
    start = time.monotonic()
    model._load_model()
    model._synchronize()
    load_seconds = time.monotonic() - start
    warmups = {strategy: model.measure(requests, strategy) for strategy in STRATEGIES}
    warmup_comparison = _comparison(warmups[INSERTION_ORDER], warmups[BATCHING_STRATEGY], atol, rtol)
    rounds, elapsed = [], {strategy: [] for strategy in STRATEGIES}
    snapshots = warmups
    all_comparisons = [warmup_comparison]
    for repeat in range(repeats):
        order = STRATEGIES if repeat % 2 == 0 else tuple(reversed(STRATEGIES))
        snapshots = {strategy: model.measure(requests, strategy) for strategy in order}
        comparison = _comparison(snapshots[INSERTION_ORDER], snapshots[BATCHING_STRATEGY], atol, rtol)
        # Include same-strategy repeat drift, not only within-round agreement.
        repeat_comparisons = {strategy: _comparison(warmups[strategy], snapshots[strategy], atol, rtol)
                              for strategy in STRATEGIES}
        all_comparisons.extend([comparison, *repeat_comparisons.values()])
        for strategy in STRATEGIES:
            elapsed[strategy].append(snapshots[strategy]["seconds"])
        rounds.append({"round": repeat + 1, "execution_order": order,
            "strategies": {strategy: _metrics(snapshots[strategy]) for strategy in STRATEGIES},
            "comparison": comparison, "repeat_drift": repeat_comparisons})
    medians = {strategy: statistics.median(times) for strategy, times in elapsed.items()}
    long_start = sum(len(texts) for _, texts in requests[:long_index])
    long_risk = {}
    synthetic_results = {}
    for strategy, snapshot in snapshots.items():
        documents = snapshot["documents"][long_start:]
        maxima = [doc["max"] for doc in documents]
        means = [doc["mean"] for doc in documents]
        long_risk[strategy] = {"candidates": documents, "max_ranking": _rank(maxima),
            "mean_ranking_observation_only": _rank(means), "max_minus_mean": [a - b for a, b in zip(maxima, means)],
            "multi_window_candidate_count": sum(doc["window_count"] > 1 for doc in documents),
            "local_minus_compact_max": maxima[1] - maxima[0],
            "repeated_minus_local_max": maxima[3] - maxima[1],
            "duplicate_candidate_max_delta": abs(maxima[1] - maxima[4])}
        synthetic_results[strategy] = [{"case": i + 1, "expected_top": correct,
            "actual_top": _rank(snapshot["scores"][i])[0],
            "pass": _rank(snapshot["scores"][i])[0] == correct} for i, correct in enumerate(expected_tops)]
    long_windows_exercised = all(value["candidates"][1]["window_count"] > 1
                                 and value["candidates"][3]["window_count"] > 1
                                 for value in long_risk.values())
    equivalent = all(c["within_tolerance"] and c["same_full_ranking"] for c in all_comparisons)
    return {"status": "PASS" if equivalent and long_windows_exercised else "INCONCLUSIVE", "model": model.model_name,
        "revision": model.revision, "device": model.device, "dtype": model.dtype,
        "max_tokens": model.max_tokens, "batch_size": model.batch_size, "repeats": repeats,
        "reference": "counterfactual_insertion_order_same_query_dedup",
        "pre_change_checkout_already_length_sorted": True,
        "aggregation": "max_raw_logit_all_windows", "model_load_seconds": load_seconds,
        "warmups": {strategy: _metrics(value) for strategy, value in warmups.items()},
        "warmup_comparison": warmup_comparison, "rounds": rounds, "median_seconds": medians,
        "median_speed_ratio_insertion_over_length": medians[INSERTION_ORDER] / medians[BATCHING_STRATEGY]
            if medians[BATCHING_STRATEGY] else None,
        "tolerance": {"atol": atol, "rtol": rtol, "calibrated_on_business_corpus": False},
        "scores_last_round": {strategy: value["scores"] for strategy, value in snapshots.items()},
        "synthetic_short_cases": synthetic_results,
        "synthetic_relevance_status": "PASS" if all(case["pass"] for cases in synthetic_results.values()
                                                       for case in cases) else "FAIL",
        "long_window_max_risk": {"status": "REVIEW_REQUIRED", "aggregation_changed": False,
            "multi_window_test_exercised": long_windows_exercised,
            "compact_candidate": 0, "local_relevance_candidate": 1, "unrelated_candidate": 2,
            "repeated_candidate": 3, "exact_duplicate_candidates": [1, 4], "strategies": long_risk},
        "real_local_reranker_executed": True, "external_model_calls": 0, "business_corpus_used": False,
        "professional_accuracy": "NOT_EVALUATED"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="Existing pinned local model directory")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; existing paths rejected")
    parser.add_argument("--device", choices=("cpu", "mps"), default="mps")
    parser.add_argument("--dtype", choices=("float32", "float16", "bfloat16"), default="bfloat16")
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=2, help="Even number >= 2 for alternating AB/BA rounds")
    parser.add_argument("--atol", type=float, default=0.05, help="Diagnostic logit tolerance, not business calibration")
    parser.add_argument("--rtol", type=float, default=0.01)
    args = parser.parse_args(argv)
    if (args.repeats < 2 or args.repeats % 2 or args.batch_size < 1 or not 1 <= args.max_tokens <= 32768
            or any(not math.isfinite(v) or v < 0 for v in (args.atol, args.rtol))):
        print(json.dumps({"status": "FAIL", "error": "PROBE_INVALID_ARGUMENTS"}))
        return 1
    model = None
    try:
        if args.output.exists() or args.output.is_symlink():
            raise RuntimeError("PROBE_OUTPUT_EXISTS")
        if (not args.directory.is_absolute() or args.directory.resolve() != args.directory
                or not args.directory.is_dir()):
            raise RuntimeError("PROBE_LOCAL_DIRECTORY_REQUIRED")
        if args.output.resolve().is_relative_to(args.directory):
            raise RuntimeError("PROBE_OUTPUT_INSIDE_MODEL_DIRECTORY")
        if args.output.resolve().is_relative_to((ROOT / "data").resolve()):
            raise RuntimeError("PROBE_OUTPUT_INSIDE_APPLICATION_DATA")
        # No overwrite, even on a creation race. Parent must already exist.
        args.output.mkdir(exist_ok=False)
    except Exception:
        print(json.dumps({"status": "FAIL", "error": "PROBE_PATH_REJECTED"}))
        return 1
    try:
        helper = _script("prepare-universal-models")
        helper.configure_download_environment()
        cache = args.output.resolve() / ".offline-cache"
        os.environ.update({"HF_HOME": str(cache), "HF_HUB_CACHE": str(cache / "hub"),
            "HF_XET_CACHE": str(cache / "xet"), "HF_TOKEN_PATH": str(cache / "unused-token"),
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"})
        with offline_guard(helper):
            import torch
            torch.set_num_threads(4)
            settings = SimpleNamespace(reranker_model=QWEN_RERANKER_SPEC["repo"],
                reranker_revision=QWEN_RERANKER_SPEC["revision"], reranker_model_path=args.directory,
                reranker_device=args.device, reranker_dtype=args.dtype,
                reranker_max_tokens=args.max_tokens, reranker_batch_size=args.batch_size)
            model = ProbeReranker(settings)
            try:
                report = run_probe(model, args.repeats, args.atol, args.rtol)
            finally:
                model.close()
    except Exception:
        # Do not serialize exceptions: tokenizer/loader messages may embed paths.
        report = {"status": "FAIL", "error": "PROBE_EXECUTION_FAILED", "completed": False,
                  "professional_accuracy": "NOT_EVALUATED"}
    try:
        with (args.output / "report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    except Exception:
        print(json.dumps({"status": "FAIL", "error": "PROBE_REPORT_WRITE_FAILED"}))
        return 1
    print(json.dumps({"status": report["status"], "professional_accuracy": "NOT_EVALUATED"}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
