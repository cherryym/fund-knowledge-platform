"""Source-preserving semantic projection integration; synthetic Qdrant only."""
import copy
from collections import Counter
from types import SimpleNamespace
from uuid import uuid4

import pytest

from fund_kb.ingestion import text_sha256
from fund_kb.retrieval import EmbeddingProvider, VectorIndex


def settings(tmp_path, **changes):
    return SimpleNamespace(app_env="development", qdrant_path=tmp_path / "vectors", qdrant_url=None,
        embedding_mode="hashing", embedding_model="synthetic", embedding_dimensions=64,
        embedding_chunk_strategy="semantic_sections_v2", embedding_max_tokens=480,
        embedding_overlap_tokens=32, embedding_context_tokens=32, **changes)


def records(texts, title="估值标准"):
    rid, vid = str(uuid4()), str(uuid4())
    return [{"resource_id": rid, "version_id": vid, "block_id": str(uuid4()), "ordinal": i,
        "title": title, "text": t, "content_sha256": text_sha256(t), "locator": {"source_page": 1},
        "block_type": "paragraph", "data": {"text": t}, "source_kind": "document"}
        for i, t in enumerate(texts)]


def test_semantic_projection_roundtrip_multi_block_group_and_source_integrity(tmp_path):
    vector = VectorIndex(settings(tmp_path))
    source = records(["第十二条 含权品种估值。", "行使回售权的，", "回售登记日至实际收款日期间，",
        "建议采用第三方估值全价。", "未行使回售权的采用长待偿期价格。", "第十三条 特殊品种。"])
    snapshot = copy.deepcopy(source)
    try:
        receipt = vector.stage_version(source, "v2")
        assert receipt["block_count"] == len(source)
        assert 0 < receipt["chunk_count"] < len(source)
        vector.activate_version(source[0]["version_id"], "v2", receipt["chunk_count"])
        assert vector.projection_is_complete(source[0]["version_id"], "v2", receipt["chunk_count"])
        hits = vector.lexical_search("回售估值", [source[0]["version_id"]], allowed_projection_ids=["v2"])
        assert hits and any(len(h["block_ids"]) > 1 for h in hits)
        for h in hits:
            originals = {r["block_id"]: r for r in source}
            for span in h["source_spans"]:
                original = originals[span["block_id"]]
                assert original["text"][span["start"]:span["end"]] == h["text"][span["text_start"]:span["text_end"]]
        assert source == snapshot
        assert vector.lexical_search("回售", [], allowed_projection_ids=["v2"]) == []
        assert vector.search("回售", [str(uuid4())], allowed_projection_ids=["v2"]) == []
    finally:
        vector.close()


def test_semantic_projection_cannot_pass_with_dropped_text_or_forged_span(tmp_path, monkeypatch):
    from fund_kb import semantic_embedding
    vector = VectorIndex(settings(tmp_path))
    source = records(["第十二条 回售估值。", "必须保留该条全部原文字句。"])
    original = semantic_embedding.build_semantic_units
    def omit(records, **kwargs):
        return original(records[:1], **kwargs)
    monkeypatch.setattr(semantic_embedding, "build_semantic_units", omit)
    try:
        with pytest.raises(ValueError, match="COVERAGE_INCOMPLETE"):
            vector._prepare(source, "diagnostic", ready=False)
        monkeypatch.setattr(semantic_embedding, "build_semantic_units", original)
        prepared, _ = vector._prepare(source, "diagnostic", ready=False)
        bad = copy.deepcopy(prepared[0][0])
        bad["source_spans"][0]["text_end"] += 1
        with pytest.raises(RuntimeError, match="SEMANTIC_SPANS_INVALID"):
            vector._check_candidate({**bad, "ready": True}, [source[0]["version_id"]], ["diagnostic"])
    finally:
        vector.close()


def test_semantic_candidate_channels_limit_same_document_units_not_other_sources(tmp_path):
    vector = VectorIndex(settings(tmp_path))
    sources = [records([f"第{i}条 回售估值条件{i}。" for i in range(1, 9)], title="回售估值") for _ in range(5)]
    ids, projections = [], []
    try:
        for index, source in enumerate(sources):
            projection = f"p{index}"
            receipt = vector.stage_version(source, projection)
            vector.activate_version(source[0]["version_id"], projection, receipt["chunk_count"])
            ids.append(source[0]["version_id"])
            projections.append(projection)
        for method in (vector.search, vector.lexical_search):
            hits = method("回售估值", ids, limit=5, allowed_projection_ids=projections)
            counts = Counter(h["version_id"] for h in hits)
            assert len(counts) == 5
            assert max(counts.values()) <= 3
    finally:
        vector.close()


def test_new_namespace_leaves_old_projection_fingerprint_independent(tmp_path):
    new = settings(tmp_path)
    old = copy.copy(new)
    old.embedding_chunk_strategy = "legacy_blocks"
    assert EmbeddingProvider(old).fingerprint != EmbeddingProvider(new).fingerprint


def test_semantic_token_count_uses_non_truncating_local_tokenizer(tmp_path):
    from tokenizers import Tokenizer, models, pre_tokenizers
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "hello": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.enable_truncation(max_length=5)
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    config = settings(tmp_path)
    config.embedding_mode = "fastembed"
    config.embedding_model_path = tmp_path
    provider = EmbeddingProvider(config)
    assert provider.token_count("hello " * 50) == 50
