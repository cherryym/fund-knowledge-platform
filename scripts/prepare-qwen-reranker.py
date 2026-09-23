"""Explicit anonymous preparation of the pinned Qwen3-Reranker-4B. No source documents."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from fund_kb.local_encoders import verify_model_file
from fund_kb.qwen_reranker_spec import QWEN_RERANKER_SPEC


def prepare(directory, workers, transport="modelscope"):
    spec = QWEN_RERANKER_SPEC
    if directory.is_symlink() or directory.resolve() != directory:
        raise ValueError("UNSAFE_MODEL_DIRECTORY")
    directory.mkdir(parents=True, exist_ok=True)
    loader = importlib.util.spec_from_file_location("qwen_public_download", ROOT / "scripts/prepare-universal-models.py")
    helper = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(helper)
    helper.MODEL_ROOT = directory.parent
    helper.configure_download_environment("ranged")
    import httpx

    class OfficialMirror(helper.ModelScopeMirror):
        @contextmanager
        def stream(self, url, headers=None):
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(60, connect=15)) as client:
                for _ in range(4):
                    helper.mirror_hostname(url)
                    client.cookies.clear()
                    with client.stream("GET", url, headers={"Accept-Encoding": "identity", **(headers or {})}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = urljoin(url, response.headers["location"])
                            helper.mirror_hostname(url)
                            continue
                        response.raise_for_status()
                        yield response
                        return
            raise ValueError("MODELSCOPE_REDIRECT_LIMIT")

    helper.MODELSCOPE_REVISIONS[spec["repo"]] = "05654501725b0890edac70ec31325cf54fd00eac"

    def fetch(name):
        pin = spec["files"][name]
        destination = directory / name
        if destination.exists() or destination.is_symlink():
            return verify_model_file(directory, name, pin)
        destination.parent.mkdir(parents=True, exist_ok=True)
        print(json.dumps({"phase": "prepare_file", "file": name, "bytes": pin[0]}), flush=True)
        if name in spec["weights"]:
            mirror = OfficialMirror({**spec, "weight": name}) if transport == "modelscope" else None
            if mirror is not None:
                mirror.verify_identity()
            helper.download_ranges(spec, name, directory, workers, mirror=mirror)
        else:
            url = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{name}"
            temporary = directory / f"{name}.{uuid4().hex}.downloading"
            with httpx.Client(trust_env=False, follow_redirects=True, timeout=60) as client:
                response = client.get(url)
                response.raise_for_status()
                if len(response.content) != pin[0]:
                    raise ValueError("MODEL_FILE_SIZE_MISMATCH")
                with temporary.open("xb") as stream:
                    stream.write(response.content)
            verify_model_file(directory, temporary.relative_to(directory).as_posix(), pin)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
        return verify_model_file(directory, name, pin)

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(fetch, [name for name in spec["files"] if name not in spec["weights"]]))
    for name in spec["weights"]:
        records.append(fetch(name))
    receipt = {"state": "VERIFIED", "model": spec["repo"], "revision": spec["revision"],
               "files": records, "verified_at": helper.utc_now(), "credentials_used": False,
               "generation_model_calls": 0, "transport": transport, "same_official_hf_hashes": True}
    (directory / "preparation-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"state": "VERIFIED", "model": spec["repo"], "files": len(records)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=8)
    parser.add_argument("--transport", choices=("hf", "modelscope"), default="modelscope")
    args = parser.parse_args()
    directory = args.directory or ROOT / "data/universal-models/qwen3-reranker-4b" / QWEN_RERANKER_SPEC["revision"]
    if not directory.is_absolute():
        raise ValueError("ABSOLUTE_MODEL_DIRECTORY_REQUIRED")
    if not args.download:
        print(json.dumps({"mode": "plan", "model": QWEN_RERANKER_SPEC["repo"], "target": str(directory),
                          "bytes": sum(pin[0] for pin in QWEN_RERANKER_SPEC["files"].values())}))
        return
    prepare(directory, args.workers, args.transport)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - public redirects may contain temporary signed URL parameters.
        print(json.dumps({"state": "PREPARATION_FAILED", "error_type": type(exc).__name__,
                          "http_status": getattr(getattr(exc, "response", None), "status_code", None)}), flush=True)
        raise SystemExit(1) from None
