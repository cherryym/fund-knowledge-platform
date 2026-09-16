"""Synthetic providers + production parsers/ACL/worker; no service or real DB."""
import io
import asyncio
import json
import re
import threading
from types import SimpleNamespace

import httpx
import pytest

from test_wiki_reader_job import base_env, env, prepare, execute  # noqa: F401
from test_wiki import env as http_env  # noqa: F401
from test_codex_stream_backpressure import (
    EngineHarness, encoded, event, make_transport, start_reader,  # noqa: F401
    forbid_real_identity_process_and_network,  # noqa: F401
)
from fund_kb import ai, api_consultation, models as m, providers, services as svc
from fund_kb.answer_preview import PublicTextBuffer, PreviewStore, previews, public_paragraphs
from fund_kb.jobs import JobDispatcher, JobError
from fund_kb.provider_stream import ResponseStream, StreamError


@pytest.fixture(autouse=True)
def empty_preview_store():
    with previews.lock:
        previews.entries.clear()
    yield
    with previews.lock:
        previews.entries.clear()


@pytest.mark.parametrize("width", [1, 2, 7, 64])
def test_boundaries_hide_split_thoughts_secrets_and_unfinished_paragraphs(width):
    text = "<think>PRIVATE\n\nSTILL_PRIVATE</think>公开一段。\n\npassword: synthetic-secret\n\n公开末段未结束"
    seen, clock = [], [0]
    buffer = PublicTextBuffer(seen.append, clock=lambda: clock[0])
    for offset in range(0, len(text), width):
        buffer.append(text[offset:offset + width])
        clock[0] += 1
        buffer.flush()
    assert seen
    assert "PRIVATE" not in str(seen) and "synthetic-secret" not in str(seen)
    assert "公开末段未结束" not in str(seen)
    assert "公开一段。" in seen[-1]
    assert "敏感信息已隐藏" in seen[-1]


@pytest.mark.parametrize("text", [
    "<script>unsafe\n\n", "&lt;script&gt;unsafe\n\n", "<img\n\nsrc=x>",
    "```html\nunsafe\n\n", "READ W1\n\n", "SEARCH 内部规划\n\n",
    "&lt;think&gt;PRIVATE\n\n", "<reasoning>PRIVATE\n\n",
])
def test_unsafe_or_non_answer_prefix_has_no_preview(text):
    assert public_paragraphs(text) == ""


def test_throttle_capacity_and_retraction_are_bounded():
    clock, seen = [0.], []
    buffer = PublicTextBuffer(seen.append, max_bytes=10000, clock=lambda: clock[0])
    for _ in range(1000):
        buffer.append("x\n\n")
        buffer.flush()
        clock[0] += .0001
    assert len(seen) == 1
    clock[0] += 1
    buffer.flush()
    assert len(seen) == 2 and len(buffer.raw) == 3000
    buffer.append("x" * 10001)
    assert seen[-1] is None and buffer.disabled and not buffer.raw
    buffer.append("never revive\n\n")
    buffer.flush()
    assert seen[-1] is None


def test_failed_retraction_callback_does_not_mask_provider_failure():
    def broken(_): raise RuntimeError("synthetic callback failure")
    stream = ResponseStream(50000, _on_public_text=broken)
    with pytest.raises(StreamError, match="UNSUPPORTED_TOOL_CALL"):
        stream.feed(sse("response.output_item.added", item={"type": "function_call"}))


def binding(number=1):
    return dict(run_id=f"r{number}", job_id="j", attempt=1, owner_id="owner", request_number=1,
        request_hash="q", evidence_hash="e", model={}, catalog_stamp="c", policy_stamp="p")


def test_store_has_bounded_capacity_ttl_and_old_attempt_cannot_revive():
    clock = [0]
    store = PreviewStore(capacity=2, ttl=10, clock=lambda: clock[0])
    first = store.begin(**binding(1))
    store.update(first, "safe\n\n")
    store.begin(**binding(2))
    store.begin(**binding(3))
    store.update(first, "stale\n\n")
    assert store.get("r1") is None and len(store.entries) == 2
    clock[0] = 11
    assert store.get("r2") is None and len(store.entries) == 2


@pytest.mark.parametrize("pause_before_first", [True, False])
def test_long_thinking_and_later_pauses_expire_body_without_disabling_updates(pause_before_first):
    clock = [0]
    store = PreviewStore(ttl=30, clock=lambda: clock[0])
    entry = store.begin(**binding())
    if not pause_before_first:
        store.update(entry, "第一段\n\n")
        assert store.get("r1").text == "第一段"
    clock[0] = 120
    for _ in range(5): assert store.get("r1") is None
    store.update(entry, "第一段\n\n后续段\n\n")
    assert store.get("r1").text == "第一段\n\n后续段"
    store.discard("r1")
    store.update(entry, "已取消不能恢复\n\n")
    assert store.get("r1") is None


def test_batch_timing_snapshot_preserves_shared_non_additive_receipt(env, monkeypatch):
    from sqlalchemy import select
    from fund_kb import universal_retrieval
    from test_universal_query_job import test_model_plans_then_reads_units_wiki_and_sources_without_named_answer_rules
    original = universal_retrieval.search_many_catalog
    batch = {"phases_ms": {"encode": 10, "rerank": 25, "source_check": 7}, "wall_ms": 42}
    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        result["batch_execution"] = batch
        for search in result["searches"]:
            search.update(timing_scope="shared_batch_not_additive", reranking={"elapsed_ms": 25})
        return result
    monkeypatch.setattr(universal_retrieval, "search_many_catalog", wrapped)
    test_model_plans_then_reads_units_wiki_and_sources_without_named_answer_rules(env, monkeypatch)
    with env.db() as db:
        snapshot = db.scalar(select(m.ConsultationRun)).model_snapshot
        assert snapshot["query_path"]["batch_execution"] == batch
        history = snapshot["hybrid_retrieval"]["queries"]
        assert history[0]["batch_execution"] == batch
        assert all(row["timing_scope"] == "shared_batch_not_additive" for row in history[0]["searches"])
        assert snapshot["query_path"]["batch_execution"]["phases_ms"]["rerank"] == 25


def test_direct_get_reuses_source_records_without_catalog_or_extra_body_reads(env, monkeypatch):
    import test_universal_query_job as scenario
    from fund_kb import answer_preview, reference_evidence, wiki_catalog
    captures, phase = {}, [None]
    original_source, original_hash = reference_evidence.reference_evidence, svc.check_frozen_hash
    original_catalog = wiki_catalog.build_catalog
    def counted_source(*args, **kwargs):
        if phase[0]: captures[phase[0]]["source"] += 1
        return original_source(*args, **kwargs)
    def counted_hash(*args, **kwargs):
        if phase[0]: captures[phase[0]]["hash"] += 1
        return original_hash(*args, **kwargs)
    def counted_catalog(*args, **kwargs):
        assert phase[0] is None, "Direct synthesis GET must not rebuild the full catalog"
        return original_catalog(*args, **kwargs)
    monkeypatch.setattr(reference_evidence, "reference_evidence", counted_source)
    monkeypatch.setattr(svc, "check_frozen_hash", counted_hash)
    monkeypatch.setattr(wiki_catalog, "build_catalog", counted_catalog)
    def prepared(env, monkeypatch, respond):
        rid, jid, calls = preview_prepare(env, monkeypatch, respond)
        fake_complete = providers.complete
        def streamed(connection, messages, **kwargs):
            if callback := connection.get("_on_public_text"):
                callback("完整公开段落。[E1]\n\n")
                assert previews.get(rid).catalog_stamp is None
                captures["baseline"] = {"source": 0, "hash": 0}
                phase[0] = "baseline"
                with monkeypatch.context() as patch:
                    patch.setattr(answer_preview, "readable_preview", lambda *a, **k: None)
                    view(env, rid)
                captures["preview"] = {"source": 0, "hash": 0}
                phase[0] = "preview"
                assert view(env, rid)["model_snapshot"]["public_preview"]["text"] == "完整公开段落。[E1]"
                phase[0] = None
            return fake_complete(connection, messages, **kwargs)
        monkeypatch.setattr(providers, "complete", streamed)
        return rid, jid, calls
    monkeypatch.setattr(scenario, "prepare", prepared)
    scenario.test_model_plans_then_reads_units_wiki_and_sources_without_named_answer_rules(env, monkeypatch)
    assert captures["preview"]["source"] == 1
    assert captures["preview"] == captures["baseline"]


def test_real_http_preview_contract_read_only_projection_and_next_request_revocation(http_env, monkeypatch):
    from test_wiki import page
    from test_reference_security_review import queued_run
    from fund_kb.reference_evidence import reference_evidence
    from fund_kb.source_reading_policy import policy_stamp
    from fund_kb.projection_read import active
    import fund_kb.answer_preview as preview_module
    resource, vid, bid = page(http_env, "预览合成来源", "合成完整来源正文。", kind="document")
    cid = svc.uid()
    rid, jid = queued_run(http_env, selection={"connection_id": cid, "model_id": "synthetic"})
    model = {"id": cid, "owner_user_id": http_env.owner, "revision": 1, "allow_document_transfer": True,
        "context_space_id": http_env.space, "protocol": "responses"}
    with http_env.db.begin() as db:
        db.add(m.RuntimePolicy(id=cid, name=providers.NAMESPACE + cid, updated_by=http_env.owner,
            config={"owner_user_id": http_env.owner, "enabled": True, "allow_document_transfer": True}))
        rows = reference_evidence(db, http_env.owner, http_env.space, reading=True, version_ids={vid})
        run = db.get(m.ConsultationRun, rid)
        run.state = "RUNNING"
        run.model_snapshot = {"answer_engine": "wiki_reader", "model_request_count": 1,
            "last_request": {"phase": "synthesis", "attempt": 1}}
        run.evidence_snapshot = [{**{key: row[key] for key in ("resource_id", "version_id", "block_id", "content_sha256", "reference_signature")},
            "evidence_id": "E1"} for row in rows]
        entry = previews.begin(run_id=rid, job_id=jid, attempt=1, owner_id=http_env.owner, request_number=1,
            request_hash=svc.digest(run.request), evidence_hash=svc.digest(run.evidence_snapshot), model=model,
            catalog_stamp=None, policy_stamp=policy_stamp(db, http_env.space))
    previews.update(entry, "暂存公开段落。[E1]\n\n")
    observed = []
    original = preview_module.readable_preview
    def inspect_scope(ctx, *args, **kwargs):
        assert active(ctx.db) and kwargs["fresh_records"] is not None
        observed.append(True)
        return original(ctx, *args, **kwargs)
    monkeypatch.setattr(preview_module, "readable_preview", inspect_scope)
    result = http_env.call("GET", "/runs/" + rid)
    assert result.status_code == 200, result.text
    assert result.json()["answer"] is None and result.json()["model_snapshot"]["public_preview"]["text"] == "暂存公开段落。[E1]"
    with http_env.db.begin() as db:
        db.get(m.Resource, resource).suspended = True
    result = http_env.call("GET", "/runs/" + rid)
    assert result.status_code == 200 and "public_preview" not in result.json()["model_snapshot"]
    assert observed == [True, True] and previews.get(rid) is None
    http_env.login(http_env.reader)
    result = http_env.call("GET", "/runs/" + rid)
    assert result.status_code == 404 and "暂存公开段落" not in result.text


def view(env, rid):
    with env.db() as db:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            settings=env.settings, answer_validator=ai.answer_validator())))
        ctx = svc.Context(request, db, db.get(m.User, env.owner), {}, {}, "getRun")
        return api_consultation.run_dict(ctx, db.get(m.ConsultationRun, rid))


def preview_prepare(env, monkeypatch, respond):
    rid, jid, calls = prepare(env, monkeypatch, respond)
    resolve = providers.resolve_connection
    def supported(*args, **kwargs):
        return {**resolve(*args, **kwargs), "protocol": "responses", "allow_document_transfer": True,
            "context_space_id": env.space}
    monkeypatch.setattr(providers, "resolve_connection", supported)
    return rid, jid, calls


def navigation(calls, connection):
    if len(calls) <= 2:
        assert "_on_public_text" not in connection
        return "核对适用条件。" if len(calls) == 1 else "READ " + " ".join(re.findall(r"W\d+", calls[-1]))


def test_worker_get_preview_is_separate_and_final_replaces_it(env, monkeypatch):
    def respond(calls, connection):
        if value := navigation(calls, connection):
            return value
        connection["_on_public_text"]("<think>PRIVATE</think>公开完整段落。[E1]\n\n尚未结束")
        result = view(env, rid)
        assert result["answer"] is None and result["state"] == "RUNNING"
        preview = result["model_snapshot"]["public_preview"]
        assert preview["text"] == "公开完整段落。[E1]"
        assert preview["notice"] == "生成中，尚未完成核验"
        assert "PRIVATE" not in str(result)
        with env.db() as db:
            assert "public_preview" not in db.get(m.ConsultationRun, rid).model_snapshot
        return "最终完整说明。[E1]"
    rid, jid, _ = preview_prepare(env, monkeypatch, respond)
    execute(env, jid)
    result = view(env, rid)
    assert result["state"] == "COMPLETED" and result["answer"]["narrative_markdown"] == "最终完整说明。[E1]"
    assert "public_preview" not in result["model_snapshot"] and previews.get(rid) is None


@pytest.mark.parametrize("change", ["acl", "body", "source_lineage", "cancel", "lease", "attempt", "identity", "connection", "policy"])
def test_get_rechecks_current_source_chain_identity_and_cancellation(env, monkeypatch, change):
    def respond(calls, connection):
        if value := navigation(calls, connection):
            return value
        callback = connection["_on_public_text"]
        callback("暂存公开段落。[E1]\n\n")
        assert view(env, rid)["model_snapshot"].get("public_preview")
        with env.db.begin() as db:
            source = db.get(m.ResourceVersion, env.source)
            resource = db.get(m.Resource, source.resource_id)
            if change == "acl": resource.restricted = True
            elif change == "body": source.title += " changed"
            elif change == "source_lineage": resource.access_epoch += 1
            elif change == "cancel": db.get(m.Job, jid).cancel_requested = True
            elif change == "lease": db.get(m.Job, jid).lease_until = svc.now()
            elif change == "attempt": db.get(m.Job, jid).attempts += 1
            elif change == "identity": db.get(m.User, env.owner).active = False
            elif change == "connection": db.get(m.RuntimePolicy, connection["id"]).revision += 1
            elif change == "policy":
                db.add(m.RuntimePolicy(id=svc.uid(), name=f"answer-source-reading:{env.space}",
                    config={"changed": True}, updated_by=env.owner))
        assert "public_preview" not in view(env, rid)["model_snapshot"]
        callback("不能恢复旧预览\n\n")
        assert previews.get(rid) is None
        raise JobError("SYNTHETIC_STOP")
    rid, jid, _ = preview_prepare(env, monkeypatch, respond)
    with pytest.raises(JobError, match="SYNTHETIC_STOP"):
        execute(env, jid)
    assert previews.get(rid) is None


@pytest.mark.parametrize("failure", ["provider", "final_validation"])
def test_final_failure_always_discards_preview(env, monkeypatch, failure):
    def respond(calls, connection):
        if value := navigation(calls, connection):
            return value
        connection["_on_public_text"]("暂存完整段落\n\n")
        assert previews.get(rid) is not None
        if failure == "provider":
            raise providers.ProviderError("UNSUPPORTED_TOOL_CALL")
        return "合成最终正文。[E1]"
    rid, jid, _ = preview_prepare(env, monkeypatch, respond)
    if failure == "final_validation":
        def reject(*args):
            raise JobError("SYNTHETIC_FINAL_REJECTED")
        monkeypatch.setattr(JobDispatcher, "_validate_answer", reject)
    with pytest.raises(JobError): execute(env, jid)
    assert previews.get(rid) is None
    with env.db() as db:
        assert db.get(m.ConsultationRun, rid).response is None


def sse(kind, **value):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **value}, ensure_ascii=False) + "\n\n").encode()


PUBLIC = "公开第一段。\n\n剩余完整正文。"
ITEM = {"type": "message", "id": "msg1", "role": "assistant", "status": "completed",
    "content": [{"type": "output_text", "text": PUBLIC}]}
ADDED = sse("response.output_item.added", output_index=0, item={**ITEM, "status": "in_progress", "content": []})
DELTA = sse("response.output_text.delta", item_id="msg1", output_index=0, content_index=0, delta=PUBLIC)
FINAL = sse("response.completed", response={"id": "response1", "status": "completed", "output": [ITEM]})


@pytest.mark.parametrize("width", [1, 2, 7, 9999])
def test_real_sse_parser_public_text_with_utf8_boundaries_and_final(width):
    seen = []
    collector = ResponseStream(50000, _on_public_text=seen.append)
    for raw in [ADDED, DELTA]:
        for offset in range(0, len(raw), width):
            collector.feed(raw[offset:offset + width])
            collector.preview.last = float("-inf")
    assert seen == ["公开第一段。\n\n"]
    collector.feed(FINAL)
    assert collector.finish()["output"] == [ITEM]
    assert "剩余完整正文" not in str(seen)


@pytest.mark.parametrize("failure", ["tool", "terminal_tool", "invalid", "refusal", "incomplete"])
@pytest.mark.parametrize("after_revision", [False, True])
def test_real_sse_parser_revokes_previews_on_late_failure(failure, after_revision):
    seen = []
    stream = ResponseStream(50000, _on_public_text=seen.append)
    stream.feed(ADDED)
    stream.preview.last = float("-inf")
    stream.feed(DELTA)
    assert seen[0] == "公开第一段。\n\n"
    if after_revision:
        stream.feed(sse("response.output_item.done", output_index=0,
            item={**ITEM, "content": [{"type": "output_text", "text": "修正后的完整正文"}]}))
        assert stream.preview.disabled and seen[-1] is None
    bad = {
        "tool": sse("response.output_item.added", item={"type": "function_call", "name": "exec"}),
        "terminal_tool": sse("response.completed", response={"status": "completed", "output": [{"type": "function_call"}]}),
        "invalid": b"data: invalid\n\n",
        "refusal": sse("response.content_part.added", part={"type": "refusal", "refusal": "no"}),
        "incomplete": sse("response.incomplete", response={"status": "incomplete", "output": []}),
    }[failure]
    if failure in {"incomplete", "refusal"}: stream.feed(bad)
    else:
        with pytest.raises(StreamError): stream.feed(bad)
    assert seen[-1] is None


@pytest.mark.parametrize("kind", ["response.content_part.added", "response.content_part.done"])
@pytest.mark.parametrize("part", [{"type": "reasoning_text", "text": "PRIVATE"},
    {"type": "reasoning_summary_text", "text": "PRIVATE"}, {"type": "unknown"}, None])
def test_optional_non_public_content_never_kills_valid_terminal_response(kind, part):
    seen = []
    stream = ResponseStream(50000, _on_public_text=seen.append)
    stream.feed(ADDED + DELTA)
    stream.feed(sse(kind, item_id="reasoning1", output_index=1, content_index=0, part=part))
    assert stream.preview.disabled and seen[-1] is None
    stream.feed(FINAL)
    value = providers.normalize_response("responses", stream.finish(), "synthetic")
    assert value["choices"][0]["message"]["content"] == PUBLIC
    assert "PRIVATE" not in str(seen)


@pytest.mark.parametrize("content,code", [([{ "type": "refusal", "refusal": "no"}], "UNSUPPORTED_RESPONSE_CONTENT"),
    ([{"type": "reasoning_text", "text": "PRIVATE"}], "UNSUPPORTED_RESPONSE_CONTENT")])
def test_terminal_nonpublic_content_still_rejected_after_preview_revoke(content, code):
    stream = ResponseStream(50000, _on_public_text=lambda _: None)
    stream.feed(sse("response.content_part.added", part={"type": "reasoning_text"}))
    with pytest.raises(StreamError, match=code):
        stream.feed(sse("response.completed", response={"status": "completed", "output": [{**ITEM, "content": content}]}))


@pytest.mark.parametrize("done_event,terminal_id", [(True, "msg1"), (False, "msg1"), (False, "revised-message")])
def test_sse_delta_difference_revokes_preview_and_keeps_authoritative_completion(done_event, terminal_id):
    seen = []
    collector = ResponseStream(50000, _on_public_text=seen.append)
    collector.feed(ADDED + DELTA)
    assert seen == ["公开第一段。\n\n"]
    item = {**ITEM, "id": terminal_id, "content": [{"type": "output_text", "text": "权威完整答复。"}]}
    if done_event:
        collector.feed(sse("response.output_item.done", output_index=0, item=item))
        assert seen[-1] is None
    response = {"status": "completed", "output": [item]}
    collector.feed(sse("response.completed", response=response))
    assert seen == ["公开第一段。\n\n", None]
    assert collector.preview.disabled and collector.finish() == response
    normalized = providers.normalize_response("responses", collector.finish(), "synthetic")
    assert normalized["choices"][0]["message"]["content"] == "权威完整答复。"
    assert normalized["choices"][0]["finish_reason"] == "stop"


def test_safety_error_in_same_sse_chunk_never_emits_text_and_reasoning_is_never_forwarded():
    seen = []
    stream = ResponseStream(50000, _on_public_text=seen.append)
    with pytest.raises(StreamError):
        stream.feed(ADDED + DELTA + sse("response.output_item.added", item={"type": "function_call"}))
    assert not any(seen)


def test_secret_echo_split_across_sse_events_is_rejected_before_preview():
    seen = []
    stream = ResponseStream(50000, _on_public_text=seen.append, secrets=("synthetic-secret",))
    stream.feed(ADDED)
    stream.feed(sse("response.output_text.delta", item_id="msg1", output_index=0, content_index=0, delta="synthetic-"))
    with pytest.raises(StreamError, match="PROVIDER_SECRET_ECHO"):
        stream.feed(sse("response.output_text.delta", item_id="msg1", output_index=0, content_index=0, delta="secret\n\n"))
    assert not any(seen)
    other = ResponseStream(50000, _on_public_text=seen.append)
    other.feed(sse("response.reasoning_text.delta", delta="PRIVATE\n\n"))
    assert not any(seen)


@pytest.mark.parametrize("unlimited", [False, True])
def test_providers_complete_threads_callback_only_through_real_responses_sse(monkeypatch, unlimited):
    seen, wire = [], []
    class Chunks(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            yield ADDED + DELTA
            assert seen == ["公开第一段。\n\n"]
            yield FINAL
        async def __aiter__(self):
            for chunk in self: yield chunk
    async def addresses(*args): return ["93.184.216.34"]
    monkeypatch.setattr(providers, "_resolve_cancellable", addresses)
    monkeypatch.setattr(providers, "_dns_addresses", lambda *args: ["93.184.216.34"])
    def handler(request):
        wire.append(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Chunks())
    result = providers.complete({"protocol": "responses", "provider_id": "openai", "kind": "direct",
        "model_id": "synthetic", "base_url": "https://provider.test/v1", "api_key": "synthetic-only",
        "_cancel_check": lambda: True, "_on_public_text": seen.append},
        [{"role": "user", "content": "synthetic"}], json_mode=False, timeout=None if unlimited else 5,
        transport=httpx.MockTransport(handler))
    assert wire[0]["stream"] is True and result["choices"][0]["message"]["content"] == PUBLIC


def test_non_streaming_http_does_not_manufacture_previews(monkeypatch):
    seen = []
    monkeypatch.setattr(providers, "_network", lambda *a, **k: {
        "choices": [{"message": {"content": PUBLIC}, "finish_reason": "stop"}]})
    result = providers.complete({"protocol": "openai", "provider_id": "custom", "model_id": "synthetic",
        "kind": "direct", "_on_public_text": seen.append}, [{"role": "user", "content": "synthetic"}], json_mode=False)
    assert result["choices"][0]["message"]["content"] == PUBLIC and seen == []


def test_responses_json_fallback_does_not_manufacture_previews(monkeypatch):
    seen = []
    monkeypatch.setattr(providers, "_dns_addresses", lambda *args: ["93.184.216.34"])
    result = providers.complete({"protocol": "responses", "provider_id": "openai", "model_id": "synthetic",
        "kind": "direct", "base_url": "https://provider.test/v1", "api_key": "synthetic-only", "_on_public_text": seen.append},
        [{"role": "user", "content": "synthetic"}], json_mode=False, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"status": "completed", "output": [ITEM]})))
    assert result["choices"][0]["message"]["content"] == PUBLIC and seen == []


def test_codex_delta_opt_in_is_per_connection_and_keeps_queue_limits(monkeypatch):
    from pathlib import Path
    from test_codex_stream_backpressure import FakeProcess
    from fund_kb import codex_bridge as bridge
    from fund_kb.codex_text import TextStdioTransport
    from fund_kb.codex_bridge_config import CodexBridgeConfig
    class Executable:
        def is_absolute(self): return True
        def is_symlink(self): return False
        def is_file(self): return True
        def __str__(self): return "/synthetic/codex"
    calls = []
    monkeypatch.setattr(bridge, "Path", lambda _: Executable())
    monkeypatch.setattr(bridge, "_private_directory", lambda path, **kwargs: path)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=bridge.VERIFIED_VERSION.encode()))
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(bridge.AuthStdioTransport, "_read", lambda _: None)
    monkeypatch.setattr(bridge.AuthStdioTransport, "call", lambda self, method, params=None: calls.append(params) or {})
    monkeypatch.setattr(bridge.AuthStdioTransport, "_write", lambda *a: None)
    for enabled in (True, False):
        rpc = TextStdioTransport(CodexBridgeConfig(executable=Path("/synthetic/codex")), Path("/synthetic-home"), public_text=enabled)
        try:
            assert rpc.events.maxsize == rpc.responses.maxsize == 128
            assert calls[-1]["capabilities"] == ({"experimentalApi": False} if enabled else {
                "experimentalApi": False, "optOutNotificationMethods": ["item/agentMessage/delta"]})
        finally:
            rpc.close()
            rpc.reader.join(timeout=1)
    assert TextStdioTransport.NOTIFICATION_OPT_OUT == ("item/agentMessage/delta",)
    assert bridge.AuthStdioTransport.NOTIFICATION_OPT_OUT == ()


def test_http_cancellation_revokes_preview_and_joins_async_request(monkeypatch):
    seen, closed = [], []
    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ADDED + DELTA
            await asyncio.Event().wait()
        async def aclose(self): closed.append(True)
    async def addresses(*args): return ["93.184.216.34"]
    monkeypatch.setattr(providers, "_resolve_cancellable", addresses)
    with pytest.raises(providers.ProviderError, match="CANCELLED"):
        providers.complete({"protocol": "responses", "provider_id": "openai", "kind": "direct",
            "model_id": "synthetic", "base_url": "https://provider.test/v1", "api_key": "synthetic-only",
            "_cancel_check": lambda: not seen, "_on_public_text": seen.append},
            [{"role": "user", "content": "synthetic"}], json_mode=False, timeout=None,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Waiting())))
    assert seen[0] == "公开第一段。\n\n" and seen[-1] is None and closed
    assert not any(thread.name == "fkb-cancellable-http" for thread in threading.enumerate())


@pytest.mark.parametrize("phase,ending", [("final_answer", "completed"), ("final_answer", "tool"),
    (None, "completed"), (None, "tool"), (None, "mismatch"), ("commentary", "completed"),
    ("future-unknown", "completed"), ("final_answer", "mismatch"),
    *[("final_answer", "mismatch_" + fault) for fault in (
        "tool", "failed_turn", "wrong_turn", "bad_text", "terminal_text", "revoked")]])
def test_codex_preview_uses_real_bounded_parser_and_preserves_final_fences(monkeypatch, make_transport, phase, ending):
    ready, seen = threading.Event(), []
    item = {"type": "agentMessage", "id": "message-test", "text": PUBLIC, **({"phase": phase} if phase else {})}
    events = [event("item/started", item={**item, "text": ""}),
        event("item/agentMessage/delta", itemId="message-test", delta=PUBLIC)]
    if ending == "tool":
        events.append(event("item/started", item={"type": "commandExecution", "id": "tool"}))
    completed_item = {**item, "text": None if ending == "mismatch_bad_text" else "changed" if ending.startswith("mismatch") else PUBLIC}
    events.append(event("item/completed", item=completed_item))
    if ending == "mismatch_tool":
        events.append(event("item/started", item={"type": "commandExecution", "id": "late-tool"}))
    events.append(event("turn/completed", turn={
        "id": "wrong-turn" if ending == "mismatch_wrong_turn" else "turn-test",
        "status": "failed" if ending == "mismatch_failed_turn" else "completed",
        "items": [{**completed_item, "text": "conflicting terminal text"}] if ending == "mismatch_terminal_text" else [completed_item]}))
    class Gated(io.BytesIO):
        lines = 0
        def readline(self, size=-1):
            if self.lines == 2 and phase in (None, "final_answer"):
                assert ready.wait(2), "No preview before terminal events"
            self.lines += 1
            return super().readline(size)
    rpc = make_transport(capacity=4, max_bytes=5000)
    rpc.process.stdout = Gated(encoded(events))
    harness = EngineHarness(monkeypatch, [])
    harness.adapter.transport_factory = lambda *_: rpc
    def call(method, params):
        result = harness.call(method, params)
        if method == "turn/start": start_reader(rpc)
        return result
    rpc.call = call
    def callback(text):
        seen.append(text)
        if text is None and ending == "mismatch_revoked": harness.revoked = True
        ready.set()
    harness.snapshot["_on_public_text"] = callback
    def invoke():
        return harness.adapter.complete(harness.snapshot, [{"role": "user", "content": "synthetic"}],
            max_tokens=4096, json_mode=False, timeout=5)
    failures = {"tool": "UNSUPPORTED_TOOL_CALL", "mismatch_tool": "UNSUPPORTED_TOOL_CALL",
        "mismatch_failed_turn": "PROVIDER_RESPONSE_INCOMPLETE", "mismatch_wrong_turn": "PROVIDER_RESPONSE_INCOMPLETE",
        "mismatch_bad_text": "PROVIDER_INVALID_RESPONSE", "mismatch_terminal_text": "CODEX_PUBLIC_TEXT_MISMATCH",
        "mismatch_revoked": "CONNECTION_REVISION_CHANGED"}
    if ending in failures:
        with pytest.raises(providers.ProviderError, match=failures[ending]): invoke()
        assert seen[-1] is None
    else:
        result = invoke()
        assert result["choices"][0]["message"]["content"] == ("changed" if ending == "mismatch" else PUBLIC)
        assert result["choices"][0]["finish_reason"] == "stop"
        if ending == "mismatch": assert seen == ["公开第一段。\n\n", None]
    if phase in (None, "final_answer"): assert seen[0] == "公开第一段。\n\n"
    else: assert not any(seen)
    assert rpc.events.maxsize == 4
