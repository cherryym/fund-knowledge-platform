"""Real offline Qwen reranker smoke; synthetic passages only, no generation or business writes."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from fund_kb.qwen_reranker import QwenReranker
from fund_kb.qwen_reranker_spec import QWEN_RERANKER_SPEC

CASES = [
    ("图书馆周六几点开门？", ["图书馆每周六上午九点对读者开放。", "会议室需要提前登记预约。", "本周仓库清点了二十箱物料。"], 0),
    ("设备开始检修前应先做什么？", ["检修后需要登记维护时间。", "阅览室开放时间为下午两点。", "检修开始前必须先断电并确认能源隔离。"], 2),
    ("这份合成指引要求谁复核估值结果？", ["合成示例：原始资料由录入人员整理归档。", "合成示例：估值结果由独立复核人员核对，处理人员不得自行复核。", "合成示例：考勤表每周提交一次。"], 1),
    ("Where are the backup copies stored?", ["For this synthetic system, backup copies are kept in encrypted off-site storage.", "The front desk opens at eight.", "Changes must be reviewed before release."], 0),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "mps"), default="mps")
    parser.add_argument("--dtype", choices=("float32", "float16", "bfloat16"), default="bfloat16")
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("PROBE_OUTPUT_EXISTS")
    settings = SimpleNamespace(reranker_model=QWEN_RERANKER_SPEC["repo"],
        reranker_revision=QWEN_RERANKER_SPEC["revision"], reranker_model_path=args.directory,
        reranker_device=args.device, reranker_dtype=args.dtype, reranker_max_tokens=2048, reranker_batch_size=2)
    helper_spec = importlib.util.spec_from_file_location("offline_guard", ROOT / "scripts/prepare-universal-models.py")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    helper.configure_download_environment()
    import torch
    torch.set_num_threads(4)
    with helper.offline_probe_guard():
        model = QwenReranker(settings)
        try:
            requests = [(query, passages) for query, passages, _ in CASES]
            print(json.dumps({"stage": "real_offline_reranking", "pairs": sum(len(p) for _, p in requests)}), flush=True)
            start = time.monotonic()
            scores = model.score_many(requests)
            cold = time.monotonic() - start
            cold_diagnostic = model.last_diagnostics
            start = time.monotonic()
            warm_scores = model.score_many(requests)
            warm = time.monotonic() - start
            results = [{"case": index + 1, "scores": row, "expected_top": correct,
                        "actual_top": max(range(len(row)), key=row.__getitem__),
                        "pass": max(range(len(row)), key=row.__getitem__) == correct}
                       for index, (row, (_, _, correct)) in enumerate(zip(scores, CASES, strict=True))]
            assert all(result["pass"] for result in results), "SYNTHETIC_RELEVANCE_FAILED"
            assert all(math.isfinite(value) for row in scores for value in row)
            assert all(max(range(len(a)), key=a.__getitem__) == max(range(len(b)), key=b.__getitem__)
                       for a, b in zip(scores, warm_scores, strict=True)), "WARM_RANK_CHANGED"
            background = "这是一段合成背景，讨论室内灯光颜色，不包含开放时间。" * 180
            positive = background + "合成关键材料：图书馆周六上午九点开门。"
            start = time.monotonic()
            long_scores = model.score("图书馆周六几点开门？", [background, positive])
            long_seconds = time.monotonic() - start
            long_diagnostic = model.last_diagnostics
            for text, windows in zip([background, positive], long_diagnostic["requests"][0]["windows"], strict=True):
                covered = set()
                for window in windows:
                    covered.update(range(window["start_char"], window["end_char"]))
                assert covered == set(range(len(text))), "LONG_DOCUMENT_INCOMPLETE"
            assert long_scores[1] > long_scores[0], "TAIL_RELEVANCE_NOT_PRESERVED"
            report = {"status": "PASS", "model": model.model_name, "revision": model.revision,
                      "device": model.device, "dtype": model.dtype, "cold_seconds": round(cold, 3),
                      "warm_seconds": round(warm, 3), "cases": results,
                      "long_tail_scores": long_scores, "long_tail_seconds": round(long_seconds, 3),
                      "cold_diagnostic": cold_diagnostic, "long_diagnostic": long_diagnostic,
                      "real_local_reranker_executed": True, "cloud_generation_calls": 0,
                      "business_corpus_sent": False, "professional_accuracy": "NOT_EVALUATED"}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=False, indent=2)
            print(json.dumps({key: report[key] for key in ("status", "model", "device", "dtype", "cold_seconds", "warm_seconds", "long_tail_seconds")}), flush=True)
        finally:
            model.close()


if __name__ == "__main__":
    main()
