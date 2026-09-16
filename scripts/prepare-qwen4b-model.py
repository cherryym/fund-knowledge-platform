"""Prepare explicitly requested public Qwen weights; never read credentials or business text."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts")]
from fund_kb.qwen_model_spec import QWEN4B_SPEC
from fund_kb.local_encoders import verify_model_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--workers", type=int, default=8, choices=range(1, 17))
    parser.add_argument("--transport", choices=("hf", "modelscope"), default="modelscope")
    args = parser.parse_args()
    spec = QWEN4B_SPEC
    directory = ROOT / "data/universal-models/qwen3-embedding-4b" / spec["revision"]
    if not args.download:
        print(json.dumps({"mode": "plan", "model": spec["repo"], "revision": spec["revision"],
            "bytes": sum(p[0] for p in spec["files"].values()), "target": str(directory)}, ensure_ascii=False))
        return
    if directory.is_symlink() or directory.resolve() != directory:
        raise RuntimeError("QWEN_MODEL_PATH_UNSAFE")
    directory.mkdir(parents=True, exist_ok=True)
    loader = importlib.util.spec_from_file_location("public_model_download", ROOT / "scripts/prepare-universal-models.py")
    helper = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(helper)
    helper.configure_download_environment("ranged")
    import httpx

    class OfficialMirror(helper.ModelScopeMirror):
        @contextmanager
        def stream(self, url, headers=None):
            # Use the user's ordinary HTTPS connectivity, without changing DNS
            # or system networking. Only the verified official redirect hosts.
            with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(60, connect=15)) as client:
                for _ in range(4):
                    helper.mirror_hostname(url)
                    with client.stream("GET", url, headers={"Accept-Encoding": "identity", **(headers or {})}) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            url = urljoin(url, response.headers["location"])
                            helper.mirror_hostname(url)
                            continue
                        response.raise_for_status()
                        yield response
                        return
            raise ValueError("MODELSCOPE_REDIRECT_LIMIT")

    helper.MODELSCOPE_REVISIONS[spec["repo"]] = "b0321180e6a2038de74aae8e3a5463104ca9b0b0"

    def fetch_file(name):
        pin = spec["files"][name]
        path = directory / name
        if path.exists() or path.is_symlink():
            return verify_model_file(directory, name, pin)
        path.parent.mkdir(parents=True, exist_ok=True)
        print(json.dumps({"stage": "prepare_file", "file": name, "bytes": pin[0]}), flush=True)
        if name in spec["weights"]:
            mirror = OfficialMirror({**spec, "weight": name}) if args.transport == "modelscope" else None
            if mirror is not None:
                mirror.verify_identity()
            helper.download_ranges(spec, name, directory, args.workers, mirror=mirror)
        else:
            url = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{name}"
            tmp_name = f"{name}.{uuid4().hex}.downloading"
            target = directory / tmp_name
            with httpx.Client(trust_env=False, follow_redirects=True, timeout=60) as client:
                response = client.get(url)
                response.raise_for_status()
                if len(response.content) != pin[0]:
                    raise RuntimeError("MODEL_FILE_SIZE_MISMATCH")
                with target.open("xb") as output:
                    output.write(response.content)
            verify_model_file(directory, tmp_name, pin)
            try:
                os.link(target, path)
            except FileExistsError:
                pass
        return verify_model_file(directory, name, pin)

    small = [name for name in spec["files"] if name not in spec["weights"]]
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(fetch_file, small))
    # Separate resumable shard ranges; all bytes use one fixed upstream revision.
    for name in spec["weights"]:
        records.append(fetch_file(name))
    report = {"state": "VERIFIED", "model": spec["repo"], "revision": spec["revision"],
        "directory": str(directory), "files": records, "verified_at": helper.utc_now(),
        "generation_model_calls": 0, "credentials_used": False,
        "transport": args.transport, "same_official_hf_hashes": True}
    output = directory / "preparation-receipt.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"state": report["state"], "files": len(records), "model": spec["repo"], "receipt": str(output)}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Public asset redirects can contain temporary signatures. Never dump
        # arbitrary HTTP exception URLs or token-bearing traceback text.
        response = getattr(exc, "response", None)
        print(json.dumps({"state": "PREPARATION_FAILED", "error_type": type(exc).__name__,
            "http_status": getattr(response, "status_code", None)}), flush=True)
        raise SystemExit(1) from None
