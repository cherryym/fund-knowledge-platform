"""Offline synthetic evidence-group acceptance using the installed Qwen model."""
import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from fund_kb.evidence_context import order_context_groups
from fund_kb.qwen_reranker import QwenReranker
from fund_kb.qwen_reranker_spec import QWEN_RERANKER_SPEC


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("OUTPUT_EXISTS")
    settings = SimpleNamespace(reranker_model=QWEN_RERANKER_SPEC["repo"],
        reranker_revision=QWEN_RERANKER_SPEC["revision"], reranker_model_path=args.directory,
        reranker_device="mps", reranker_dtype="bfloat16", reranker_max_tokens=2048, reranker_batch_size=2)
    spec = importlib.util.spec_from_file_location("guard", ROOT / "scripts/prepare-universal-models.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.configure_download_environment()
    pages = {}
    for i, (title, text) in enumerate([
        ("门禁维护", "维护门禁时需登记设备号。"),
        ("图书馆开放", "图书馆周六上午九点开放。预约要求见预约规则。"),
        ("预约规则", "预约需登记读者证号；开放时间以开放说明为准。"),
        ("安全检查", "工作人员每天检查消防设施。"),
    ], 1):
        pid = f"W{i}"
        pages[pid] = {"title": title, "records": [{"version_id": f"v{i}", "block_id": f"b{i}",
            "content_sha256": "synthetic", "evidence_id": f"E{i}", "text": text}]}
    import torch
    torch.set_num_threads(4)
    with guard.offline_probe_guard():
        model = QwenReranker(settings)
        vector = SimpleNamespace(settings=settings, rerank=model.score)
        try:
            start = time.monotonic()
            order, receipt = order_context_groups("图书馆周六几点开放，需要如何预约？", pages, set(pages), [("W2", "W3")], vector, {})
            cold = time.monotonic() - start
            cache = {}
            start = time.monotonic()
            warm_order, _ = order_context_groups("图书馆周六几点开放，需要如何预约？", pages, set(pages), [("W2", "W3")], vector, cache)
            warm = time.monotonic() - start
            start = time.monotonic()
            _, cached = order_context_groups("图书馆周六几点开放，需要如何预约？", pages, set(pages), [("W2", "W3")], vector, cache)
            cached_ms = (time.monotonic() - start) * 1000
            assert receipt["status"] == "scored" and order[0] == "W2", "GROUP_RELEVANCE_FAILED"
            assert order.index("W3") == 1 and set(order) == set(pages), "DEPENDENCY_LOST"
            assert order == warm_order and cached["cache_hit"]
            report = {"status": "PASS", "cold_seconds": round(cold, 3), "warm_seconds": round(warm, 3),
                "request_cache_ms": round(cached_ms, 3), "receipt": receipt,
                "cloud_calls": 0, "business_data_used": False, "professional_accuracy": "NOT_EVALUATED"}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
            print(json.dumps(report, ensure_ascii=False))
        finally:
            model.close()


if __name__ == "__main__":
    main()
