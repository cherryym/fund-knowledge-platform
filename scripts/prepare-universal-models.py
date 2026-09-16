"""Explicit public BAAI provisioning. Writes only data/universal-models.

backend/.venv/bin/python scripts/prepare-universal-models.py --download
Use --download-only to stage weights before the synthetic local probe.
No application settings, retrieval profile, business material, or credentials.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import ipaddress
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urljoin, urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = ROOT / "data" / "universal-models"
sys.path.insert(0, str(ROOT / "backend"))
from fund_kb.local_encoders import MODEL_SPECS, verify_model_file

MODELSCOPE_REVISIONS = {
    "BAAI/bge-m3": "e44369c5623cc146f016da906583db4ee0e3488d",
    "BAAI/bge-reranker-v2-m3": "e099d4b9cdbd291b1569d416f19aaa6523570bc3",
}


def mirror_hostname(url):
    """Only the observed official ModelScope HTTPS chain; never userinfo or LAN hosts."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or host not in {"modelscope.cn", "cdn-lfs-cn-1.modelscope.cn"}):
        raise ValueError("UNAPPROVED_MODELSCOPE_REDIRECT")
    return host


def public_address(address):
    ip = ipaddress.ip_address(address)
    return ip.version == 4 and ip.is_global and not ip.is_multicast and not ip.is_reserved


def public_dns_addresses(payload):
    addresses = [item["data"] for item in payload.get("Answer", []) if item.get("type") == 1]
    if payload.get("Status") != 0 or not addresses or any(
        not public_address(address) for address in addresses
    ):
        raise ValueError("MODELSCOPE_DNS_NOT_PUBLIC")
    return addresses


class ModelScopeMirror:
    """Fixed BAAI mirror, request-local DNS, original Host/SNI, verified TLS.

    No system resolver changes or global socket patches. The only extra endpoint
    is AliDNS's public HTTPS JSON resolver. Signed redirect URLs stay in memory.
    """

    def __init__(self, spec):
        self.spec = spec
        self.revision = MODELSCOPE_REVISIONS[spec["repo"]]
        self._dns = {}
        self._lock = threading.Lock()
        self.hosts = set()

    def address(self, host):
        import httpx

        # Also validate before any public DNS lookup, not only before connection.
        mirror_hostname(f"https://{host}/")
        with self._lock:
            cached = self._dns.get(host)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            with httpx.Client(trust_env=False, verify=True, follow_redirects=False,
                              timeout=httpx.Timeout(10, connect=5)) as client:
                response = client.get("https://223.5.5.5/resolve", params={"name": host, "type": "A"})
                response.raise_for_status()
                payload = response.json()
            addresses = public_dns_addresses(payload)
            ttl = min(300, min(int(item.get("TTL", 30)) for item in payload["Answer"] if item.get("type") == 1))
            self._dns[host] = (addresses[0], time.monotonic() + max(1, ttl))
            self.hosts.add(host)
            print(json.dumps({"stage": "mirror_public_dns", "host": host, "address": addresses[0]}), flush=True)
            return addresses[0]

    @contextmanager
    def stream(self, url, headers=None):
        import httpx

        for _ in range(4):
            host = mirror_hostname(url)
            address = self.address(host)
            if not public_address(address):
                raise ValueError("MODELSCOPE_DNS_NOT_PUBLIC")
            # HTTPCore's documented sni_hostname extension keeps certificate
            # hostname verification on the original host while connecting to IP.
            target = httpx.URL(url).copy_with(host=address)
            with (httpx.Client(trust_env=False, verify=True, follow_redirects=False,
                               timeout=httpx.Timeout(60, connect=10)) as client,
                  client.stream("GET", target, headers={"Host": host, "Accept-Encoding": "identity", **(headers or {})},
                                extensions={"sni_hostname": host}) as response):
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers["location"])
                    mirror_hostname(url)  # Reject a new/unapproved host before its DNS lookup.
                    continue
                response.raise_for_status()
                yield response
                return
        raise ValueError("MODELSCOPE_REDIRECT_LIMIT")

    def verify_identity(self):
        url = (f"https://modelscope.cn/api/v1/models/{self.spec['repo']}/repo/files"
               f"?Revision={self.revision}&Recursive=true")
        with self.stream(url) as response:
            payload = bytearray()
            for block in response.iter_bytes(65536):
                payload.extend(block)
                if len(payload) > 2 * 1024 * 1024:
                    raise ValueError("MODELSCOPE_METADATA_TOO_LARGE")
        metadata = json.loads(payload)
        files = (metadata.get("Data") or {}).get("Files", [])
        weight = self.spec["weight"]
        size, digest = self.spec["files"][weight]
        matches = [item for item in files if item.get("Path") == weight]
        if (metadata.get("Code") != 200 or len(matches) != 1 or matches[0].get("Size") != size
                or "sha256:" + str(matches[0].get("Sha256")) != digest):
            raise ValueError("MODELSCOPE_WEIGHT_NOT_IDENTICAL_TO_HF_PIN")

    def weight_url(self):
        return (f"https://modelscope.cn/api/v1/models/{self.spec['repo']}/repo"
                f"?Revision={self.revision}&FilePath={quote(self.spec['weight'])}")


@contextmanager
def hf_range_stream(url, headers):
    import httpx

    with (httpx.Client(trust_env=False, follow_redirects=True,
                       timeout=httpx.Timeout(60, connect=20)) as client,
          client.stream("GET", url, headers=headers) as response):
        yield response


def utc_now():
    return datetime.now(UTC).isoformat()


def configure_download_environment(transport="ranged"):
    # Set before importing Hub/Transformers; never query any inherited token.
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN"):
        os.environ[name] = ""
    os.environ.update({
        "HF_HOME": str(MODEL_ROOT / ".hf"),
        "HF_HUB_CACHE": str(MODEL_ROOT / ".hf" / "hub"),
        "HF_XET_CACHE": str(MODEL_ROOT / ".hf" / "xet"),
        "HF_TOKEN_PATH": str(MODEL_ROOT / ".hf" / "unused-token"),
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_XET": "0" if transport == "xet" else "1",
        "HF_XET_NUM_CONCURRENT_RANGE_GETS": "16",
        "HF_HUB_DOWNLOAD_TIMEOUT": "60",
        "HF_HUB_ETAG_TIMEOUT": "30", "DO_NOT_TRACK": "1",
    })


def candidate_settings(device):
    embedding, reranker = MODEL_SPECS["embedding"], MODEL_SPECS["reranker"]
    return {
        "embedding_mode": "transformers", "embedding_model": embedding["repo"],
        "embedding_model_path": str(MODEL_ROOT / "bge-m3" / embedding["revision"]),
        "embedding_revision": embedding["revision"], "embedding_dimensions": 1024,
        "embedding_device": device, "embedding_model_max_tokens": 8192,
        "embedding_query_instruction": "", "embedding_batch_size": 8,
        "embedding_allow_downloads": False, "embedding_chunk_strategy": "semantic_sections_v3",
        "embedding_max_tokens": 768,
        "reranker_mode": "local", "reranker_model": reranker["repo"],
        "reranker_model_path": str(MODEL_ROOT / "bge-reranker-v2-m3" / reranker["revision"]),
        "reranker_revision": reranker["revision"], "reranker_device": device,
        "reranker_max_tokens": 1024, "reranker_batch_size": 8,
    }


def download_ranges(spec, name, directory, workers, mirror=None):
    """Anonymous, resumable HTTPS ranges from the same fixed official HF revision.

    Every Content-Range is checked, then the complete file is checked against its
    pre-pinned repository Hash before publication. No .netrc, cookies or auth.
    """
    import httpx

    size, _ = spec["files"][name]
    part_size = 16 * 1024 * 1024
    parts = directory / ".parts" / name
    if parts.is_symlink() or not parts.resolve().is_relative_to(directory.resolve()):
        raise ValueError("UNSAFE_DOWNLOAD_PARTS_PATH")
    parts.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{name}"
    chunks = [(start, min(size, start + part_size)) for start in range(0, size, part_size)]

    def fetch(span):
        start, end = span
        path = parts / f"{start:012d}-{end:012d}.part"
        if path.is_symlink():
            raise ValueError("UNSAFE_DOWNLOAD_PART_PATH")
        for attempt in range(5):
            present = path.stat().st_size if path.exists() else 0
            if present == end - start:
                return path
            if present > end - start:
                raise ValueError("DOWNLOAD_PART_SIZE_MISMATCH")
            try:
                # Per-request client avoids sharing cookies or credentials. Each
                # part resumes only after validating the exact server range.
                headers = {"Range": f"bytes={start+present}-{end-1}", "Accept-Encoding": "identity"}
                stream_context = (mirror.stream(mirror.weight_url(), headers) if mirror
                                  else hf_range_stream(url, headers))
                with stream_context as response:
                    response.raise_for_status()
                    expected = f"bytes {start+present}-{end-1}/{size}"
                    if response.status_code != 206 or response.headers.get("content-range") != expected:
                        raise ValueError("DOWNLOAD_CONTENT_RANGE_MISMATCH")
                    with path.open("ab") as stream:
                        for chunk in response.iter_bytes(1024 * 1024):
                            if stream.tell() + len(chunk) > end - start:
                                raise ValueError("DOWNLOAD_PART_OVERFLOW")
                            stream.write(chunk)
                if path.stat().st_size == end - start:
                    print(json.dumps({"stage": "download_part_complete", "model": spec["repo"],
                                      "start": start, "bytes": end-start}), flush=True)
                    return path
            except (httpx.HTTPError, OSError):
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError("DOWNLOAD_PART_INCOMPLETE")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        files = list(pool.map(fetch, chunks))
    # Unique assembly file: incomplete downloads cannot look like a ready weight.
    assembled_name = f"{name}.{uuid4().hex}.assembling"
    assembled = directory / assembled_name
    with assembled.open("xb") as destination:
        for part in files:
            with part.open("rb") as source:
                for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
                    destination.write(block)
    verify_model_file(directory, assembled_name, spec["files"][name])
    # An exclusive hardlink publishes the verified bytes atomically; never replaces
    # a concurrently prepared destination. Temporary download parts remain reusable.
    try:
        os.link(assembled, directory / name)
    except FileExistsError:
        verify_model_file(directory, name, spec["files"][name])


def prepare_files(settings, roles=("embedding", "reranker"), transport="ranged", workers=16):
    from huggingface_hub import hf_hub_download

    def prepare_role(role):
        spec = MODEL_SPECS[role]
        directory = Path(settings[f"{role}_model_path"])
        if directory.is_symlink() or not directory.resolve().is_relative_to(MODEL_ROOT.resolve()):
            raise ValueError("MODEL_PATH_OUTSIDE_PREPARATION_ROOT")
        directory.mkdir(parents=True, exist_ok=True)
        records = []
        mirror = ModelScopeMirror(spec) if transport == "modelscope" else None
        if mirror is not None:
            mirror.verify_identity()
        for name, pin in spec["files"].items():
            print(json.dumps({"stage": "prepare_file", "role": role, "file": name,
                              "bytes": pin[0], "revision": spec["revision"]}), flush=True)
            if not (directory / name).exists():
                if transport in {"ranged", "modelscope"} and name == spec["weight"]:
                    download_ranges(spec, name, directory, workers, mirror=mirror)
                else:
                    hf_hub_download(repo_id=spec["repo"], filename=name, revision=spec["revision"],
                                    token=False, local_dir=str(directory),
                                    cache_dir=str(MODEL_ROOT / ".hf" / "hub"))
            records.append(verify_model_file(directory, name, pin))
        record = {"model": spec["repo"], "revision": spec["revision"], "path": str(directory),
                  "files": records, "verified_at_utc": utc_now()}
        if mirror is not None:
            record["mirror"] = {"provider": "ModelScope", "repo": spec["repo"], "revision": mirror.revision,
                                "same_hf_weight_sha256": True, "actual_hosts": sorted(mirror.hosts),
                                "request_local_dns": True, "tls_verification": True}
        return record

    with ThreadPoolExecutor(max_workers=2) as pool:
        return dict(zip(roles, pool.map(prepare_role, roles), strict=True))


@contextmanager
def offline_probe_guard():
    """Standalone preparation process only: catch network/token regressions in real loaders."""
    import socket
    from unittest.mock import patch

    import huggingface_hub
    import huggingface_hub.utils._auth as auth
    import huggingface_hub.utils._headers as headers

    def forbidden(*args, **kwargs):
        raise RuntimeError("LOCAL_PROBE_NETWORK_OR_TOKEN_ACCESS_FORBIDDEN")

    with (patch.object(socket.socket, "connect", forbidden), patch.object(socket, "create_connection", forbidden),
          patch.object(huggingface_hub, "get_token", forbidden), patch.object(auth, "get_token", forbidden),
          patch.object(headers, "get_token", forbidden)):
        yield


def run_probes(candidate):
    """Public synthetic fixtures only. No Settings()/.env or application imports."""
    import copy
    import math

    import torch

    from fund_kb.local_encoders import LocalEmbedding, LocalEncoderError, LocalReranker

    settings = SimpleNamespace(**candidate)
    results = {}
    # Makes CPU fallback tolerable without modifying another process's thread count.
    torch.set_num_threads(4)
    with offline_probe_guard():
        model = LocalEmbedding(settings)
        try:
            print(json.dumps({"stage": "synthetic_embedding_probe"}), flush=True)
            texts = ["猫。", "The library opens at nine in the morning.", "公共图书馆周末开放，读者可以借阅图书。", "猫。"]
            started = time.monotonic()
            counts = [model.token_count(text) for text in texts]
            if model._model is not None:
                raise RuntimeError("TOKEN_COUNT_LOADED_WEIGHTS")
            tokenizer_seconds = time.monotonic() - started
            # Deliberately exceed 8192; rejection must occur before weight loading.
            overlong = "hello " * 9000
            overflow_count = model.token_count(overlong)
            try:
                model.embed([texts[0], overlong])
            except LocalEncoderError as exc:
                if not str(exc).startswith("EMBEDDING_INPUT_TOO_LONG"):
                    raise
            else:
                raise RuntimeError("EMBEDDING_OVERFLOW_NOT_REJECTED")
            if model._model is not None or overflow_count <= 8192:
                raise RuntimeError("EMBEDDING_OVERFLOW_PROBE_INVALID")
            started = time.monotonic()
            vectors = model.embed(texts)
            cold_seconds = time.monotonic() - started
            diagnostic = copy.deepcopy(model.last_diagnostics)
            single = [model.embed([text])[0] for text in texts]
            delta = max(abs(a-b) for row, other in zip(vectors, single, strict=True)
                        for a, b in zip(row, other, strict=True))
            if len(vectors) != len(texts) or any(len(row) != 1024 for row in vectors):
                raise RuntimeError("EMBEDDING_DIMENSION_PROBE_FAILED")
            norms = [math.sqrt(sum(v*v for v in row)) for row in vectors]
            if any(not math.isfinite(norm) or abs(norm-1) > 1e-5 for norm in norms) or delta > 1e-4:
                raise RuntimeError("EMBEDDING_ALIGNMENT_OR_NORMALIZATION_FAILED")
            if diagnostic["input_tokens"] != counts or len(set(counts)) < 2:
                raise RuntimeError("EMBEDDING_PER_TEXT_COUNT_PROBE_FAILED")
            # A genuine input above the 768 retrieval-chunk budget remains valid to
            # the 8192-capacity encoder. No truncation to the retrieval budget.
            long_text = "The observatory measures the motion of stars. " * 100
            long_count = model.token_count(long_text)
            long_vectors = model.embed(["star", long_text])
            if not 768 < long_count <= 8192 or len(long_vectors[1]) != 1024:
                raise RuntimeError("EMBEDDING_LONG_INPUT_PROBE_FAILED")
            if model.last_diagnostics["input_tokens"] != [model.token_count("star"), long_count]:
                raise RuntimeError("EMBEDDING_LONG_INPUT_COUNT_FAILED")
            results["embedding"] = {"status": "PASS", "token_count_only_loaded_tokenizer": True,
                "tokenizer_load_seconds": round(tokenizer_seconds, 3), "cold_batch_seconds": round(cold_seconds, 3),
                "input_tokens": counts, "dimensions": 1024, "norms": norms,
                "batch_single_max_abs_difference": delta, "overflow_rejected_tokens": overflow_count,
                "long_input_tokens": long_count, "long_input": copy.deepcopy(model.last_diagnostics),
                "inference": diagnostic}
        finally:
            model.close()

        reranker = LocalReranker(settings)
        try:
            print(json.dumps({"stage": "synthetic_reranker_probe"}), flush=True)
            query = "What does the giant panda eat?"
            texts = ["The giant panda eats bamboo.", "The train leaves the station at noon.",
                     "The giant panda eats bamboo."]
            started = time.monotonic()
            scores = reranker.score(query, texts)
            cold_seconds = time.monotonic() - started
            diagnostic = copy.deepcopy(reranker.last_diagnostics)
            single = [reranker.score(query, [text])[0] for text in texts]
            delta = max(abs(a-b) for a, b in zip(scores, single, strict=True))
            if delta > 1e-4 or not scores[0] > scores[1] or abs(scores[0]-scores[2]) > 1e-4:
                raise RuntimeError("RERANK_ALIGNMENT_OR_SYNTHETIC_RELEVANCE_FAILED")
            long_text = "The railway timetable lists a train and a station. " * 130 + texts[0]
            long_scores = reranker.score(query, [texts[1], long_text, texts[0]])
            long_diagnostic = copy.deepcopy(reranker.last_diagnostics)
            if len(long_diagnostic["windows"][1]) < 2:
                raise RuntimeError("RERANK_LONG_PROBE_NOT_MULTIPLE_WINDOWS")
            for count, windows, score in zip(long_diagnostic["text_tokens"], long_diagnostic["windows"],
                                              long_scores, strict=True):
                covered_end = 0
                for window in windows:
                    if window["start_token"] > covered_end or window["input_tokens"] > settings.reranker_max_tokens:
                        raise RuntimeError("RERANK_WINDOW_GAP_OR_OVERFLOW")
                    covered_end = max(covered_end, window["end_token"])
                if covered_end != count or score != max(window["score"] for window in windows):
                    raise RuntimeError("RERANK_TAIL_OR_AGGREGATION_PROBE_FAILED")
            results["reranker"] = {"status": "PASS", "cold_batch_seconds": round(cold_seconds, 3),
                "scores": scores, "batch_single_max_abs_difference": delta, "inference": diagnostic,
                "long_scores": long_scores, "long_input": long_diagnostic, "full_window_coverage": True}
        finally:
            reranker.close()
    results["network_and_host_token_tripwires"] = "PASS"
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Explicit public-download authorization")
    parser.add_argument("--download-only", action="store_true", help="Stage and hash files without inference")
    parser.add_argument("--device", choices=("auto", "cpu", "mps"), default="auto")
    parser.add_argument("--models", choices=("both", "embedding", "reranker"), default="both")
    parser.add_argument("--manifest-name", help="New basename under data/universal-models; never overwritten")
    parser.add_argument("--transport", choices=("ranged", "http", "xet", "modelscope"), default="ranged")
    parser.add_argument("--download-workers", type=int, choices=range(1, 33), default=16)
    args = parser.parse_args(argv)
    if not args.download:
        parser.error("--download is required; nothing was read, downloaded or prepared")
    if args.models != "both" and not args.download_only:
        parser.error("--models requires --download-only; readiness probes always cover both models")
    if args.manifest_name and (Path(args.manifest_name).name != args.manifest_name
                               or not args.manifest_name.endswith(".json")):
        parser.error("--manifest-name must be a JSON basename")
    if MODEL_ROOT.is_symlink() or not MODEL_ROOT.resolve().is_relative_to(ROOT):
        parser.error("Model output must remain inside this project")
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    configure_download_environment(args.transport)
    settings = candidate_settings(args.device)
    started = time.monotonic()
    manifest = {"schema_version": 1, "started_at_utc": utc_now(), "status": "PREPARING",
                "candidate_settings": settings, "current_profile_written": False,
                "business_documents_read": 0, "business_documents_sent": 0, "generation_model_calls": 0,
                "semantic_quality": "NOT_EVALUATED", "local_files_only": True,
                "trust_remote_code": False, "token": False,
                "download_transport": args.transport, "download_workers_per_model": args.download_workers,
                "sources": [f"https://huggingface.co/{s['repo']}/tree/{s['revision']}"
                            for s in MODEL_SPECS.values()]}
    output = MODEL_ROOT / (args.manifest_name or
                          f"preparation-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}.json")
    if output.exists():
        parser.error("Manifest already exists; use a new name")
    print(json.dumps({"stage": "preparation_started", "manifest": str(output)}), flush=True)
    result = 0
    try:
        roles = ("embedding", "reranker") if args.models == "both" else (args.models,)
        manifest["models"] = prepare_files(settings, roles, args.transport, args.download_workers)
        manifest["status"] = "DOWNLOADED_NOT_PROBED"
        if not args.download_only:
            manifest["probes"] = run_probes(settings)
            manifest["status"] = "LOCAL_MODELS_PROBE_PASSED"
        manifest["dependencies"] = {name: importlib.metadata.version(name) for name in
                                    ("torch", "transformers", "huggingface-hub", "tokenizers", "safetensors")}
    except KeyboardInterrupt:
        manifest.update(status="INTERRUPTED", failure_type="KeyboardInterrupt")
        result = 130
    except Exception as exc:  # noqa: BLE001 -- CLI receipt must sanitize signed URLs in all failure types.
        # Exception strings can contain signed download URLs; keep them out of the receipt.
        manifest.update(status="FAILED", failure_type=type(exc).__name__)
        from fund_kb.local_encoders import LocalEncoderError
        if isinstance(exc, LocalEncoderError):
            manifest["failure_code"] = str(exc)
        print(json.dumps({"stage": "failed", "failure_type": type(exc).__name__}), flush=True)
        result = 1
    finally:
        manifest.update(finished_at_utc=utc_now(), elapsed_seconds=round(time.monotonic() - started, 3))
        with output.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
        print(json.dumps({"status": manifest["status"], "manifest": str(output)}, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
