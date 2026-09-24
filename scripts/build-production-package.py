"""Build a sanitized ONLINE installer; never imply that image archives exist.

Reads Git-tracked public source plus the explicitly scoped production packaging
directory and compiled frontend. Never reads the local business knowledge base.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0-production.20260923"
NAME = f"fundkb-windows-x64-oracle-{VERSION}-online"
FORBIDDEN = {"data", "runtime", "node_modules", ".venv", "__pycache__", ".git", "output", ".pytest_cache", ".ruff_cache"}
PRIVATE_SUFFIXES = {".key", ".pem", ".sqlite", ".sqlite3", ".db", ".log", ".safetensors", ".onnx", ".gguf"}
CURRENT_SOURCE_DIRS = ("backend/fund_kb", "backend/tests", "frontend/src", "contracts", "integrations", "evals/rag")
CURRENT_SOURCE_FILES = (
    "scripts/configure-retrieval.py", "scripts/prepare-qwen-reranker.py", "scripts/probe-qwen-reranker.py",
    "scripts/probe-context-reranker.py", "scripts/private-transfer.py", "scripts/restore-private-transfer.py",
    "docs/business-reading-focus.md", "docs/context-completion-validation-20260923.md",
    "docs/qwen-reranker-validation-20260923.md",
    "scripts/evaluate-rag.py", "scripts/inspect-rag-fusion.py", "scripts/probe-qwen-padding.py",
    "docs/rag-evaluation.md", "docs/rag-structure-audit.md", "docs/qwen-rerank-padding.md",
    "docs/rag-chain-optimization-20260924.md",
    "docs/evidence-review-v1.md",
    "docs/rag-repair-20260924.md", "docs/qwen-label-head-20260924.md",
    "scripts/probe-qwen-label-head.py", "docs/qwen-label-head-smoke-20260924.json",
    "docs/qwen-label-head-shared-20260924.json",
    "docs/qwen-label-head-quiet-scale-20260924.json",
)


def package_candidates(tracked):
    """Include reviewed current runtime sources, not only the older Git HEAD list."""
    candidates = {ROOT / name for name in tracked if name}
    candidates.add(Path(__file__).resolve())
    for folder in (*CURRENT_SOURCE_DIRS, "deploy/production"):
        for path in (ROOT / folder).rglob("*"):
            if not path.is_file() or any(part in FORBIDDEN for part in path.relative_to(ROOT).parts):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            safe_file(path)
            candidates.add(path)
    candidates.update(ROOT / name for name in CURRENT_SOURCE_FILES if (ROOT / name).is_file())
    for path in candidates:
        safe_file(path)
    return candidates


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_file(path):
    relative = path.relative_to(ROOT)
    if (path.is_symlink() or not path.is_file() or any(part in FORBIDDEN for part in relative.parts)
            or path.suffix in PRIVATE_SUFFIXES or path.name in {"settings.json", "auth.json", "host.json", "archive.key"}
            or (path.name.startswith(".env") and path.name != ".env.example")):
        raise ValueError("Refusing non-public path: " + relative.as_posix())
    return relative


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True, help="New output directory; never overwritten")
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.exists():
        raise SystemExit("Output already exists; choose a new directory")
    static = ROOT / "frontend/dist/client"
    if not (static / "index.html").is_file():
        raise SystemExit("Run frontend npm run build first")
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    candidates = package_candidates(tracked)
    destination.mkdir(parents=True)
    bundle = destination / NAME
    bundle.mkdir()
    for path in sorted(candidates):
        target = bundle / safe_file(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    shutil.copytree(static, bundle / "frontend-static")
    shutil.copy2(ROOT / "deploy/production/Deploy.ps1", bundle / "Deploy.ps1")
    shutil.copy2(ROOT / "deploy/production/先读.md.in", bundle / "先读我.md")
    shutil.copy2(ROOT / "deploy/production/VALIDATION.md", bundle / "VALIDATION.md")
    metadata = {"package": NAME, "version": VERSION, "date": "2026-09-23", "source_commit": source_sha,
                "source_contains_local_packaging_changes": True,
                "source_contains_uncommitted_application_changes": True,
                "current_source_sha256": {safe_file(p).as_posix(): digest(p) for p in sorted(candidates)},
                "delivery_type": "ONLINE_BUILD_INSTALLER", "target": "Windows x64 controlling Linux amd64 containers / Oracle 12.2+",
                "frontend_prebuilt": True, "prebuilt_container_images_included": False,
                "business_data_included": False, "model_weights_included": False,
                "migration_deliverable": "PLAN_ONLY_NOT_EXECUTED",
                "production_database_and_windows_acceptance": "NOT_EVALUATED"}
    (bundle / "PACKAGE.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = sorted(path for path in bundle.rglob("*") if path.is_file())
    manifest = "".join(f"{digest(path)}  {path.relative_to(bundle).as_posix()}\n" for path in files)
    (bundle / "SHA256SUMS").write_text(manifest, encoding="utf-8")
    archive = destination / (NAME + ".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
            output.write(path, arcname=NAME + "/" + path.relative_to(bundle).as_posix())
    with zipfile.ZipFile(archive) as check:
        if check.testzip():
            raise SystemExit("ZIP integrity failure")
        if len(check.namelist()) != len(files) + 1:
            raise SystemExit("Archive file count mismatch")
    archive_hash = digest(archive)
    (destination / (archive.name + ".sha256")).write_text(archive_hash + "  " + archive.name + "\n", encoding="utf-8")
    print(json.dumps({"archive": str(archive), "sha256": archive_hash, "files": len(files) + 1,
                      "bytes": archive.stat().st_size, "delivery_type": metadata["delivery_type"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
