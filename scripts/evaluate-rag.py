#!/usr/bin/env python3
"""Offline RAG evaluation. Input exports only; no service, model, DB, or credential access."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fund_kb.rag_evaluation import (
    EvaluationInputError,
    adapt_capture,
    compare,
    evaluate,
    load_json,
    validate_bundle,
    validate_document,
)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default can echo private argument values.
        self.exit(2, '{"status":"INVALID","code":"CLI_ARGUMENT_ERROR"}\n')


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate one input JSON document")
    validate.add_argument("input", type=Path)
    adapt = commands.add_parser("adapt", help="Convert exported search traces and answer model snapshots")
    adapt.add_argument("--capture", required=True, type=Path)
    adapt.add_argument("--dataset", required=True, type=Path, help="Validate case/sample references only")
    adapt.add_argument("--source-manifest", type=Path, help="Independent authorized metadata export")
    adapt.add_argument("--output", type=Path, help="Create a NEW body-free predictions export")
    run = commands.add_parser("evaluate", help="Evaluate all expected samples; optionally compare paired runs")
    run.add_argument("--dataset", required=True, type=Path)
    run.add_argument("--predictions", required=True, type=Path, help="Candidate or single-run export")
    run.add_argument("--judgments", type=Path, help="External human review for this exact candidate run")
    run.add_argument("--baseline", type=Path)
    run.add_argument("--baseline-judgments", type=Path)
    run.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    run.add_argument("--output", type=Path, help="Create a NEW report; never overwrite existing files")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            document = load_json(args.input)
            validate_document(document)
            report = {"status": "PASS", "validation": "STRUCTURE_AND_LOCAL_INVARIANTS_ONLY",
                      "kind": document["kind"], "professional_accuracy_evaluated": False}
            output = None
        elif args.command == "adapt":
            capture = load_json(args.capture)
            manifest = load_json(args.source_manifest) if args.source_manifest else None
            report = adapt_capture(capture, manifest)
            validate_bundle(load_json(args.dataset), report)
            output = args.output
        else:
            if args.baseline_judgments and not args.baseline:
                raise EvaluationInputError("BASELINE_REQUIRED_FOR_BASELINE_JUDGMENTS")
            dataset, predictions = load_json(args.dataset), load_json(args.predictions)
            judgments = load_json(args.judgments) if args.judgments else None
            if args.baseline:
                baseline = load_json(args.baseline)
                baseline_judgments = load_json(args.baseline_judgments) if args.baseline_judgments else None
                report = compare(dataset, baseline, predictions, baseline_judgments, judgments, split=args.split)
            else:
                report = evaluate(dataset, predictions, judgments, split=args.split)
            output = args.output
        rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        status = report.get("status", "PASS")  # adapt succeeds structurally; it does not evaluate accuracy.
        if output:
            with output.open("x", encoding="utf-8") as stream:
                stream.write(rendered)
            print(json.dumps({"status": status, "operation": args.command, "output_created": True}))
        else:
            print(rendered, end="")
        return {"PASS": 0, "FAIL": 1, "UNKNOWN": 3}[status]
    except EvaluationInputError as error:
        print(json.dumps(error.as_dict()), file=sys.stderr)
        return 2
    except OSError:
        print('{"status":"INVALID","code":"LOCAL_FILE_IO_ERROR"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
