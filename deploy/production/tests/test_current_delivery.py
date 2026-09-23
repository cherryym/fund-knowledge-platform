"""Current-source packaging and local-model portability; synthetic only."""
import importlib.util
import json
import plistlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "deploy/production/ops")]
from prepare_local_profiles import rebase_profile
from stage_local_data import check_sanitized_source


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_current_untracked_runtime_is_packaged():
    package = load("package_current", "scripts/build-production-package.py")
    files = {p.relative_to(ROOT).as_posix() for p in package.package_candidates([])}
    assert {"backend/fund_kb/business_reading.py", "backend/fund_kb/evidence_context.py",
            "backend/fund_kb/source_associations.py", "backend/fund_kb/qwen_reranker.py",
            "backend/fund_kb/qwen_reranker_spec.py"} <= files
    assert not any(".venv" in f or "node_modules" in f or "__pycache__" in f for f in files)


def test_launchd_credentials_are_sanitized_without_dropping_program_arguments():
    transfer = load("transfer_current", "scripts/private-transfer.py")
    value = {"ProgramArguments": ["python", "serve.py"], "EnvironmentVariables": {
        "FKB_PROVIDER_MASTER_KEY": "SYNTHETIC_ONLY_MASTER_VALUE", "FKB_QDRANT_API_KEY": "SYNTHETIC_ONLY_KEY",
        "FKB_APP_ENV": "development"}}
    clean, changes = transfer.scrub_plist(value)
    raw = plistlib.dumps(clean)
    assert b"SYNTHETIC_ONLY" not in raw and clean["ProgramArguments"] == value["ProgramArguments"]
    assert clean["EnvironmentVariables"]["FKB_APP_ENV"] == "development" and len(changes) == 2


def test_cpu_rebasing_preserves_qwen_vector_space_and_dtype():
    from fund_kb.qwen_model_spec import QWEN4B_SPEC
    from fund_kb.qwen_reranker_spec import QWEN_RERANKER_SPEC
    value = {"embedding_mode": "transformers", "embedding_model": QWEN4B_SPEC["repo"],
        "embedding_dimensions": 2560, "embedding_revision": QWEN4B_SPEC["revision"],
        "embedding_model_path": "/old/data/universal-models/qwen3-embedding-4b/pinned",
        "embedding_cache_dir": "/old/data/cache", "embedding_dtype": "bfloat16", "embedding_device": "mps",
        "embedding_chunk_strategy": "semantic_sections_v3", "embedding_model_max_tokens": 8192,
        "reranker_mode": "local", "reranker_model": QWEN_RERANKER_SPEC["repo"],
        "reranker_revision": QWEN_RERANKER_SPEC["revision"], "reranker_device": "mps", "reranker_dtype": "bfloat16",
        "reranker_model_path": "/old/data/universal-models/qwen3-reranker-4b/pinned"}
    clean, receipt = rebase_profile(value)
    assert clean["embedding_device"] == clean["reranker_device"] == "cpu"
    assert clean["embedding_dtype"] == "bfloat16" and receipt["embedding_fingerprint"]
    assert clean["embedding_model_path"] == "/app/data/universal-models/qwen3-embedding-4b/pinned"
    assert value["embedding_device"] == "mps"
    value["embedding_model_path"] = "/old/universal-models/../secret"
    with pytest.raises(ValueError): rebase_profile(value)


def test_data_staging_rejects_live_credentials_before_copy(tmp_path):
    dbpath=tmp_path/'fund_kb.sqlite3'
    with sqlite3.connect(dbpath) as db:
        db.executescript('CREATE TABLE runtime_policies(name TEXT, config TEXT); CREATE TABLE login_sessions(token_hash TEXT,revoked_at TEXT);')
        db.execute('INSERT INTO runtime_policies VALUES (?,?)',('model-connection:synthetic',json.dumps({'enabled':True,'credential_ciphertext':'synthetic'})))
    with pytest.raises(ValueError,match='SOURCE_MODEL_CREDENTIAL_NOT_CLEARED'):
        check_sanitized_source(tmp_path)
    with sqlite3.connect(dbpath) as db:
        db.execute('UPDATE runtime_policies SET config=?',(json.dumps({'enabled':False,'credential_ciphertext':None}),))
    check_sanitized_source(tmp_path)
    (tmp_path/'private').mkdir()
    with pytest.raises(ValueError,match='CREDENTIAL_DIRECTORY_IN_SOURCE'):
        check_sanitized_source(tmp_path)
