"""Real local Qwen output-head or explicit batch comparison; same tokens/candidates.

Only built-in synthetic inputs. Reads only the named non-secret JSON retrieval
profile and pinned local model files; no .env, application DB or model download.
Run alone on the GPU. Output is a new JSON file containing metrics, never text,
token IDs, input paths or exception messages.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from fund_kb.qwen_reranker import BATCHING_STRATEGY, LABEL_TOKEN_IDS, OUTPUT_PROJECTION


def _script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


padding = _script("probe-qwen-padding")
FULL_HEAD = "full_vocab_last_token_reference_v1"
VARIANTS = (FULL_HEAD, OUTPUT_PROJECTION)
BATCH_VARIANTS = ("label_head_batch_2", "label_head_batch_8")


class HeadProbe(padding.ProbeReranker):
    def _label_logits(self, inputs, masks):
        if self.output_projection == FULL_HEAD:
            output = self._model(input_ids=inputs, attention_mask=masks, use_cache=False,
                return_dict=True, logits_to_keep=1, output_hidden_states=False)
            if output.logits.shape != (len(inputs), 1, self._model.config.vocab_size):
                raise RuntimeError("PROBE_FULL_HEAD_SHAPE_FAILED")
            return output.logits[:, 0, list(LABEL_TOKEN_IDS)]
        return self._experimental_label_logits(inputs, masks)

    def variant(self, requests, variant):
        if variant not in VARIANTS:
            raise RuntimeError("PROBE_UNKNOWN_VARIANT")
        self.output_projection = variant
        self.projected_output_columns = self._model.config.vocab_size if variant == FULL_HEAD else 2
        return self.measure(requests, BATCHING_STRATEGY)


class BatchProbe(HeadProbe):
    """Explicit standalone experiment only; never writes a profile or service setting."""

    def variant(self, requests, variant):
        if variant not in BATCH_VARIANTS:
            raise RuntimeError("PROBE_UNKNOWN_VARIANT")
        self.batch_size = 2 if variant == BATCH_VARIANTS[0] else 8
        self.output_projection, self.projected_output_columns = OUTPUT_PROJECTION, 2
        return self.measure(requests, BATCHING_STRATEGY)


def profile_settings(path):
    if not path.is_absolute() or path.resolve() != path or path.name != "retrieval-profile.json":
        raise RuntimeError("PROBE_EXPECTS_NON_SECRET_RETRIEVAL_PROFILE")
    raw = path.read_bytes()
    profile = json.loads(raw)
    keys = ("reranker_model", "reranker_revision", "reranker_model_path", "reranker_device",
            "reranker_dtype", "reranker_max_tokens", "reranker_batch_size", "reranker_instruction")
    settings = SimpleNamespace(**{key: profile[key] for key in keys if key in profile})
    # Never substitute a batch/device/dtype for the authorized profile.
    if any(not hasattr(settings, key) for key in keys[:-1]):
        raise RuntimeError("PROBE_INCOMPLETE_PROFILE")
    return settings, hashlib.sha256(raw).hexdigest()


def workloads(suite):
    if suite == "smoke":
        requests, _, _ = padding.synthetic_cases()
        return [("synthetic_short_and_long", requests)]
    if suite not in {"shared", "representative", "scale"}:
        raise RuntimeError("PROBE_UNKNOWN_SUITE")
    questions = ["图书馆周六几点开门？", "入馆需要如何预约？", "设备检修前必须做什么？",
        "检修结束后需要记录什么？", "合成系统的备份副本存放在哪里？", "变更发布前由谁复核？",
        "档案借阅后何时归还？", "会议室使用结束要做什么？", "临时通行证如何办理？",
        "仓库盘点如何登记差异？", "培训签到表由谁归档？"]
    rules = ["图书馆每周六上午九点开放，入馆需要提前登记预约。",
        "设备检修开始前先断电并确认能源隔离，完成后记录维护时间。",
        "合成系统的备份副本存放在异地加密存储中，变更发布前由独立人员复核。",
        "合成档案借阅后应在约定日期归还，会议室使用结束后需要整理设备。",
        "临时通行证由前台核验登记，仓库盘点应逐项登记差异。",
        "培训签到表由培训管理员归档，合成记录仅用于离线计算测试。"]
    background = "合成背景说明：灯光采用柔和色调，桌椅按编号摆放，室内物品按计划检查。"
    documents = [f"合成记录编号{i:03d}。" + background * (1, 3, 8, 16, 24)[i % 5]
                 + rules[i % len(rules)] for i in range(80)]
    if suite == "representative":
        long_requests, _, _ = padding.synthetic_cases()
        return [("synthetic_shared_2x80", [(q, list(documents)) for q in (questions[0], questions[5])]),
                ("synthetic_short_and_long", long_requests)]
    return [(f"synthetic_shared_{count}x80", [(q, list(documents)) for q in questions[:count]])
            for count in ((11,) if suite == "scale" else (11, 9))]


def _metrics(snapshot):
    diag = snapshot["diagnostics"]
    return {**padding._metrics(snapshot), "started_at_utc": snapshot.get("started_at_utc"),
        "completed_at_utc": snapshot.get("completed_at_utc"), **{key: diag[key] for key in
        ("output_projection", "projected_output_columns", "actual_projected_logits", "pair_count")}}


def _compare(before, after, atol, *, require_same_batches=True):
    result = padding._comparison(before, after, atol, 0.0)
    # Padding, batch membership and ordering are held fixed, not another treatment.
    same_batches = all(before["diagnostics"][key] == after["diagnostics"][key] for key in
        ("actual_padded_tokens", "actual_padding_tokens", "actual_batch_count", "batching_strategy"))
    if require_same_batches and not same_batches:
        raise RuntimeError("PROBE_BATCHES_CHANGED")
    changed = tied = 0
    for old, new in zip(before["scores"], after["scores"], strict=True):
        for i in range(len(old)):
            for j in range(i):
                a, b = old[i] - old[j], new[i] - new[j]
                if (a > 0) - (a < 0) != (b > 0) - (b < 0):
                    changed += 1
                    tied += a == 0 or b == 0
    return {**result, "candidate_pair_order_changes": changed, "changes_involving_ties": tied,
        "exact_scores": result["max_abs_window_logit_delta"] == 0,
        "same_batch_schedule": same_batches}


def measure_case(model, name, requests, rounds, atol, variants=VARIANTS):
    reference, candidate = variants
    compare_batches = variants == BATCH_VARIANTS
    def comparison_of(before, after):
        return _compare(before, after, atol, require_same_batches=not compare_batches)
    def measure(variant):
        started = datetime.now(UTC).isoformat()
        snapshot = model.variant(requests, variant)
        snapshot.update(started_at_utc=started, completed_at_utc=datetime.now(UTC).isoformat())
        return snapshot
    print(json.dumps({"stage": "paired_case", "case": name, "pair_count": sum(len(d) for _, d in requests)}), flush=True)
    warmups = {}
    for variant in variants:
        warmups[variant] = measure(variant)
        print(json.dumps({"stage": "warmup_complete", "case": name,
                          "variant": variant, "seconds": warmups[variant]["seconds"]}), flush=True)
    comparisons = [comparison_of(warmups[reference], warmups[candidate])]
    elapsed = {variant: [] for variant in variants}
    samples = []
    for repeat in range(rounds):
        order = variants if repeat % 2 == 0 else tuple(reversed(variants))
        snapshots = {variant: measure(variant) for variant in order}
        comparison = comparison_of(snapshots[reference], snapshots[candidate])
        repeat_drift = {variant: _compare(warmups[variant], snapshots[variant], atol) for variant in variants}
        comparisons.extend([comparison, *repeat_drift.values()])
        for variant in variants:
            elapsed[variant].append(snapshots[variant]["seconds"])
        samples.append({"round": repeat + 1, "execution_order": order,
            "variants": {v: _metrics(snapshots[v]) for v in variants},
            "comparison": comparison, "repeat_drift": repeat_drift})
        print(json.dumps({"stage": "paired_round", "case": name, "round": repeat + 1,
            "reference_seconds": elapsed[reference][-1], "candidate_seconds": elapsed[candidate][-1],
            "max_abs_window_delta": comparison["max_abs_window_logit_delta"],
            "same_ranking": comparison["same_full_ranking"]}), flush=True)
    medians = {variant: statistics.median(times) for variant, times in elapsed.items()}
    return {"case": name, "warmups": {v: _metrics(warmups[v]) for v in variants},
        "warmup_comparison": comparisons[0], "rounds": samples, "median_seconds": medians,
        "elapsed_reduction_fraction": 1 - medians[candidate] / medians[reference],
        "speed_ratio": medians[reference] / medians[candidate],
        "all_window_scores_exact": all(c["exact_scores"] for c in comparisons),
        "equivalence": "PASS" if all(c["within_tolerance"] and c["same_full_ranking"] for c in comparisons)
            else "INCONCLUSIVE", "scores_last_round": {v: snapshots[v]["scores"] for v in variants}}


def measure_head_only(model, repeats=20):
    torch = model._torch
    # Cache a final hidden state from synthetic text to isolate output projection.
    ids = model._frames("图书馆何时开放？", "合成说明：图书馆上午九点开放。")[0][0]
    inputs = torch.tensor([ids] * model.batch_size, dtype=torch.long, device=model.device)
    masks = torch.ones_like(inputs)
    with torch.inference_mode():
        hidden = model._model.model(input_ids=inputs, attention_mask=masks, use_cache=False,
            return_dict=True, output_hidden_states=False).last_hidden_state[:, -1:, :]
        calls = {FULL_HEAD: lambda: model._model.lm_head(hidden),
                 OUTPUT_PROJECTION: lambda: torch.nn.functional.linear(hidden, model._label_weight)}
        for call in calls.values():
            call()
        model._synchronize()
        times = {v: [] for v in VARIANTS}
        labels = {}
        for repeat in range(repeats):
            for variant in (VARIANTS if repeat % 2 == 0 else tuple(reversed(VARIANTS))):
                model._synchronize()
                start = time.monotonic()
                result = calls[variant]()
                model._synchronize()
                times[variant].append(time.monotonic() - start)
                # Copy only the last measured values, outside the timed region.
                if repeat == repeats - 1:
                    labels[variant] = (result[:, 0, list(LABEL_TOKEN_IDS)] if variant == FULL_HEAD
                                       else result[:, 0, :]).float().cpu()
        full, selected = labels[FULL_HEAD], labels[OUTPUT_PROJECTION]
        vocab, width = model._model.config.vocab_size, hidden.shape[-1]
    return {"repeats": repeats, "batch_size": model.batch_size, "hidden_size": width,
        "output_columns": {FULL_HEAD: vocab, OUTPUT_PROJECTION: 2},
        "multiply_accumulates_per_batch": {FULL_HEAD: model.batch_size * width * vocab,
                                           OUTPUT_PROJECTION: model.batch_size * width * 2},
        "median_seconds": {v: statistics.median(times[v]) for v in VARIANTS},
        "max_abs_label_logit_delta": float((full - selected).abs().max()),
        "max_abs_score_delta": float(((full[:, 0] - full[:, 1]) - (selected[:, 0] - selected[:, 1])).abs().max())}


def run(settings, suite, rounds, atol, compare_batch_size=None):
    if compare_batch_size is not None and (compare_batch_size != 8 or settings.reranker_batch_size != 2):
        raise RuntimeError("PROBE_BATCH_COMPARISON_REQUIRES_PROFILE_BATCH_2")
    variants = VARIANTS if compare_batch_size is None else BATCH_VARIANTS
    model = (HeadProbe if compare_batch_size is None else BatchProbe)(settings)
    try:
        started = time.monotonic()
        model._load_model()
        model._synchronize()
        load_seconds = time.monotonic() - started
        head = model._model.lm_head
        identity = (id(model._model), id(head), head.weight.data_ptr())
        cases = [measure_case(model, name, requests, rounds, atol, variants) for name, requests in workloads(suite)]
        head_only = measure_head_only(model) if compare_batch_size is None else None
        unchanged = identity == (id(model._model), id(model._model.lm_head), model._model.lm_head.weight.data_ptr())
        equivalent = (unchanged and all(c["equivalence"] == "PASS" for c in cases)
                      and (head_only is None or head_only["max_abs_score_delta"] <= atol))
        return {"status": "PASS" if equivalent else "INCONCLUSIVE",
            "model": model.model_name, "revision": model.revision, "device": model.device, "dtype": model.dtype,
            "batch_size": model.batch_size, "max_tokens": model.max_tokens, "load_seconds": load_seconds,
            "comparison": "output_head_only" if compare_batch_size is None else "batch_size_only",
            "profile_batch_size": settings.reranker_batch_size,
            "evaluated_batch_sizes": [settings.reranker_batch_size] if compare_batch_size is None else [2, 8],
            "production_default_changed": False,
            "model_object_and_full_head_unchanged": unchanged, "baseline_includes_existing_padding_and_dedup": True,
            "aggregation": "max_raw_logit_all_windows", "atol": atol, "rtol": 0.0,
            "versions": {k: importlib.metadata.version(k) for k in ("torch", "transformers", "huggingface-hub")},
            "cases": cases, "head_only": head_only, "real_local_reranker_executed": True,
            "synthetic_inputs_only": True, "external_model_calls": 0,
            "business_accuracy": "NOT_EVALUATED", "original_business_queries_replayed": False}
    finally:
        model.close()


def main(argv=None):
    started_at_utc = datetime.now(UTC).isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New JSON file, existing output rejected")
    parser.add_argument("--suite", choices=("smoke", "shared", "representative", "scale"), default="smoke",
                        help="scale: all 11x80 pairs; shared: both 11x80 and 9x80")
    parser.add_argument("--timing-context", choices=("unqualified", "quiet", "contended"), default="unqualified",
                        help="Explicit workload context; numeric equivalence and timing qualify separately")
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--atol", type=float, default=0.0, help="Default requires exact scores as well as ranks")
    parser.add_argument("--compare-batch-size", type=int, choices=(8,),
                        help="Explicit experiment: profile batch2 vs batch8, same two-label head; no profile write")
    args = parser.parse_args(argv)
    if args.rounds < 2 or args.rounds % 2 or not math.isfinite(args.atol) or args.atol < 0:
        print(json.dumps({"status": "FAIL", "error": "PROBE_INVALID_ARGUMENTS"}))
        return 1
    try:
        if args.output.exists() or args.output.is_symlink() or not args.output.parent.is_dir():
            raise RuntimeError("PROBE_OUTPUT_REJECTED")
        settings, profile_hash = profile_settings(args.profile)
        if (args.output.resolve().is_relative_to(Path(settings.reranker_model_path).resolve())
                or args.output.resolve().is_relative_to(args.profile.parent.resolve())
                or args.output.resolve().is_relative_to(ROOT / "data")):
            raise RuntimeError("PROBE_OUTPUT_REJECTED")
    except Exception:
        print(json.dumps({"status": "FAIL", "error": "PROBE_INPUT_OR_OUTPUT_REJECTED"}))
        return 1
    try:
        helper = _script("prepare-universal-models")
        helper.configure_download_environment()
        with tempfile.TemporaryDirectory(prefix="qwen-head-probe-") as temporary:
            cache = Path(temporary)
            os.environ.update({"HF_HOME": str(cache), "HF_HUB_CACHE": str(cache / "hub"),
                "HF_XET_CACHE": str(cache / "xet"), "HF_TOKEN_PATH": str(cache / "unused-token"),
                "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"})
            with padding.offline_guard(helper):
                import torch
                torch.set_num_threads(4)
                report = run(settings, args.suite, args.rounds, args.atol, args.compare_batch_size)
        report["profile_unchanged"] = profile_hash == hashlib.sha256(args.profile.read_bytes()).hexdigest()
        report["profile_sha256"] = profile_hash
        report["started_at_utc"] = started_at_utc
        report["completed_at_utc"] = datetime.now(UTC).isoformat()
        report["adapter_sha256"] = hashlib.sha256((ROOT / "backend/fund_kb/qwen_reranker.py").read_bytes()).hexdigest()
        report["timing_context"] = args.timing_context
        report["performance_status"] = "MEASURED_QUIET_SYNTHETIC" if args.timing_context == "quiet" else "UNQUALIFIED"
        if not report["profile_unchanged"]:
            report["status"] = "FAIL"
    except KeyboardInterrupt:
        report = {"status": "INCOMPLETE", "error": "PROBE_INTERRUPTED", "business_accuracy": "NOT_EVALUATED"}
    except Exception:
        # Do not echo errors which may include a model or profile path.
        report = {"status": "FAIL", "error": "PROBE_EXECUTION_FAILED", "business_accuracy": "NOT_EVALUATED"}
    try:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    except Exception:
        print(json.dumps({"status": "FAIL", "error": "PROBE_OUTPUT_REJECTED"}))
        return 1
    print(json.dumps({"status": report["status"], "business_accuracy": "NOT_EVALUATED"}), flush=True)
    return 0 if report["status"] == "PASS" else (130 if report["status"] == "INCOMPLETE" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
