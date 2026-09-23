"""Interactive Wiki query: whole catalog -> complete pages -> linked reads -> Markdown.

Source/credential authority stays in the existing services. Model prose is not a
database command, and a READ can address only this request's registered pages.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from hashlib import sha256

from sqlalchemy import select

from . import models as m
from . import providers
from . import services as svc
from .answer_packing import fits_context, pack_text
from .answer_preview import previews
from .planning_cache import planning_cache, planning_cache_key, validated_plan
from .query_context import compact_catalog, compact_evidence, graph_query_text
from .query_cost import model_cost_hint
from .query_path_cache import get_query_path, put_query_path
from .reference_evidence import evidence_signature, reference_evidence
from .source_reading_policy import (
    build_reading_plan,
    check_primary_citations,
    plan_instructions,
    policy_stamp,
)
from .wiki_catalog import build_catalog, catalog_signature, related_reads
from .wiki_reader import (
    ADAPTIVE_PROMPT_VERSION,
    ADAPTIVE_SYNTHESIS_INSTRUCTION,
    ADAPTIVE_SYSTEM,
    ADAPTIVE_SYSTEM_SHA256,
    NOTES_INSTRUCTION,
    PLANNING_INSTRUCTION,
    PROMPT_VERSION,
    SELECTION_INSTRUCTION,
    SYNTHESIS_INSTRUCTION,
    SYSTEM,
    SYSTEM_SHA256,
    UNIVERSAL_PLANNING_INSTRUCTION,
    UNIVERSAL_PROMPT_VERSION,
    UNIVERSAL_SYNTHESIS_INSTRUCTION,
    UNIVERSAL_SYSTEM,
    UNIVERSAL_SYSTEM_SHA256,
    fallback_pages,
    full_read_requests,
    index_lines,
    page_text,
    read_requests,
    requests_catalog,
    search_requests,
    planning_search_requests,
    section_read_requests,
)
from .wiki_section_reader import outline_text, read_scoped_pages, source_anchors


def run_wiki_answer(dispatcher, job_id, attempt):
    try:
        return _run_wiki_answer(dispatcher, job_id, attempt)
    finally:
        # Covers cancellation, stale leases, final validation and cleanup errors.
        previews.discard_job(job_id, attempt)


def _run_wiki_answer(dispatcher, job_id, attempt):
    from .jobs import JobError
    from .wiki_answer_content import build_narrative_answer

    path_started = time.monotonic()
    with dispatcher.session_factory.begin() as db:
        job = dispatcher._fence(db, job_id, attempt)
        actor, run, thread = dispatcher._run_context(db, job, validate_attachments=False)
        previous = (run.policy_snapshot or {}).get("question_analysis")
        previous_calls = int((run.model_snapshot or {}).get("model_request_count", 0))
        settings, _ = dispatcher._answer_policy(db, run)
        universal = settings.wiki_query_strategy == "universal"
        adaptive = universal or settings.wiki_query_strategy == "adaptive"
        system = UNIVERSAL_SYSTEM if universal else ADAPTIVE_SYSTEM if adaptive else SYSTEM
        prompt_version = UNIVERSAL_PROMPT_VERSION if universal else ADAPTIVE_PROMPT_VERSION if adaptive else PROMPT_VERSION
        system_sha = UNIVERSAL_SYSTEM_SHA256 if universal else ADAPTIVE_SYSTEM_SHA256 if adaptive else SYSTEM_SHA256
        request, context, space_id = dict(run.request), dict(run.request.get("context", {})), thread.space_id
        from .business_reading import business_profile, profile_instructions
        business = business_profile(db, space_id) if universal else None
        business_stamp = policy_stamp(db, space_id) if business else None
        if business:
            system += profile_instructions(business)
            system_sha = sha256(system.encode()).hexdigest()
        if settings.llm_provider != "http":
            raise JobError("MODEL_REQUIRED")
        reuse = False
        if (not adaptive and attempt > 1 and previous and isinstance(previous.get("plan"), dict)
                and previous.get("prompt_version") == PROMPT_VERSION
                and previous.get("system_prompt_sha256") == SYSTEM_SHA256):
            plan_hash = svc.digest(previous["plan"])
            reuse = any((event.details or {}).get("plan_sha256") == plan_hash
                and (event.details or {}).get("system_prompt_sha256") == SYSTEM_SHA256 for event in db.scalars(
                select(m.AuditEvent).where(m.AuditEvent.object_id == job_id, m.AuditEvent.action == "answer.planning_completed")))
        run.state, run.error_code, run.completed_at, run.response = "RUNNING", None, None, None
        run.model_snapshot = {**(run.model_snapshot or {}), "answer_engine": "wiki_reader",
            "prompt_version": prompt_version, "system_prompt_sha256": system_sha,
            "query_strategy": settings.wiki_query_strategy,
            "model_invoked": False, "model_request_count": previous_calls, "last_request": None,
            "planning_reused": reuse, "validation_status": "pending", "execution_mode": "wiki_reading"}
        run.policy_snapshot = {**run.policy_snapshot, "answer_engine": "wiki_reader"}
        if adaptive and not universal:
            run.model_snapshot["planning_model_invoked"] = False
            run.policy_snapshot = {**run.policy_snapshot, "question_analysis": {},
                "query_planning": {"source": "application_graph_router", "model_invoked": False,
                    "requested_reasoning_strategy": request.get("reasoning_strategy")}}
        if reuse:
            run.policy_snapshot = {**run.policy_snapshot, "question_analysis": previous}
            dispatcher._audit(db, job, "answer.planning_reused", {"plan_sha256": plan_hash, "additional_model_calls": 0})
        revision = (run.model_snapshot or {}).get("revision")
        run_id = run.id

    choice = request.get("model_selection")
    scope = request.get("answer_scope", "formal")
    question = request["question"]
    # Reading scope follows actual source structure, not question wording.
    reference_requested, dependency_searches, group_rank_cache = {}, set(), {}
    context_report = None
    read_records, read_pages, notes = {}, set(), []
    unsent_records = {}
    full_requested, section_requested, discovered_anchors = set(), {}, {}
    manual_requested = set()
    catalog_stamp = None
    source_plan = None
    graph_plan = None
    query_path = None
    model_elapsed_ms = 0.0
    navigation_links = {}
    pending_path_cache_key = None
    authority_requirements = {}
    public_plan_key, cached_plan, cache_entry = None, None, None
    planning_complete = False

    def reading_progress(stage, **values):
        with dispatcher.session_factory.begin() as db:
            dispatcher._fence(db, job_id, attempt)
            current = db.get(m.ConsultationRun, run_id)
            previous = (current.model_snapshot or {}).get("reading_progress", {})
            current.model_snapshot = {**current.model_snapshot, "reading_progress": {
                **previous, **values, "stage": stage, "updated_at": svc.primitive(svc.now())}}

    def remember_commands(text, pages):
        full_requested.update(full_read_requests(text, pages))
        requested = section_read_requests(text, pages)
        for pid, ids in requested.items():
            section_requested.setdefault(pid, set()).update(ids)
        ids = list(dict.fromkeys([*read_requests(text, pages), *full_read_requests(text, pages), *requested]))
        manual_requested.update(ids)
        return ids

    def authority():
        with dispatcher.read_session_factory() as db:
            current = db.get(m.Job, job_id)
            if (not current or current.cancel_requested or current.attempts != attempt or current.state != "RUNNING"
                    or current.lease_until is None or current.lease_until <= svc.now()):
                raise JobError("MODEL_JOB_CANCELLED_OR_STALE")
            user, _, _ = dispatcher._run_context(db, current, validate_attachments=False, read_only=True)
            if choice:
                policy = providers.load_connection(db, user, choice["connection_id"], settings)
                if policy.revision != revision or not policy.config.get("enabled") or not policy.config.get("allow_document_transfer"):
                    raise JobError("MODEL_CONNECTION_OR_AUTHORITY_CHANGED")
            return user.id

    def verify(records):
        if not records:
            authority()
            return
        with dispatcher.read_session_factory() as db:
            job = db.get(m.Job, job_id)
            user, _, _ = dispatcher._run_context(db, job, read_only=True)
            values = reference_evidence(db, user, space_id, context, reading=True,
                version_ids={r["version_id"] for r in records}) if scope == "reference" else svc.eligible_evidence(db, user, space_id, context,
                    version_ids={r["version_id"] for r in records})
            allowed = {evidence_signature(row) for row in values}
            if any(evidence_signature(row) not in allowed for row in records):
                raise JobError("EVIDENCE_ACCESS_CHANGED")
            from .source_authority import verify_bindings
            try:
                verify_bindings(db, user, list(authority_requirements.values()))
            except svc.APIError as exc:
                raise JobError(exc.code) from exc

    def complete(body, phase, records=(), *, preview_source_bound=False):
        nonlocal query_path, model_elapsed_ms
        from .wiki_answer_content import _redact
        previews.discard(run_id, job_id=job_id, attempt=attempt)
        body, redacted = _redact(body)
        authority()
        def before_send():
            # This is the pre-dispatch full check; do not repeat the identical
            # body/hash read immediately before it. Codex rechecks again after
            # any provider queue wait, immediately before turn/start.
            verify(list(records))
            if business:
                with dispatcher.read_session_factory() as db:
                    if policy_stamp(db, space_id) != business_stamp:
                        raise JobError("SOURCE_READING_POLICY_CHANGED")
            # The index and relation labels are protected metadata too. This
            # recheck loads no paragraph bodies and never trusts a browser cache.
            if catalog_stamp is not None:
                with dispatcher.read_session_factory() as db:
                    actor_id = authority()
                    fresh_catalog = build_catalog(db, actor_id, space_id, context, scope=scope)
                    if catalog_signature(fresh_catalog) != catalog_stamp:
                        raise JobError("WIKI_CATALOG_CHANGED")
                    if source_plan is not None and policy_stamp(db, space_id) != source_plan["policy_stamp"]:
                        raise JobError("SOURCE_READING_POLICY_CHANGED")
        before_send()
        with dispatcher.session_factory.begin() as db:
            job = dispatcher._fence(db, job_id, attempt)
            user, current, _ = dispatcher._run_context(db, job, validate_attachments=phase != "planning")
            connection = providers.resolve_connection(db, user, space_id, choice["connection_id"], choice["model_id"],
                settings, require_transfer=True, expected_revision=revision) if choice else None
            job.stage = {"planning": "PLANNING_QUESTION", "wiki_index": "READING_WIKI_INDEX",
                "wiki_notes": "READING_WIKI_PAGES", "synthesis": "GENERATING"}.get(phase, "READING_WIKI_PAGES")
            limits = {"total_seconds": None, "read_idle_seconds": None, "connect_seconds": 10,
                "max_output_tokens": settings.wiki_answer_max_output_tokens}
            current.model_snapshot = {**current.model_snapshot, "model_invoked": True,
                "planning_model_invoked": phase == "planning" or current.model_snapshot.get("planning_model_invoked", False),
                "answer_model_invoked": phase == "synthesis" or current.model_snapshot.get("answer_model_invoked", False),
                "model_request_count": current.model_snapshot.get("model_request_count", 0) + 1,
                "last_request": {"phase": phase, "state": "waiting", "attempt": attempt,
                    "started_at": svc.primitive(svc.now()), "limits": limits}}
            if phase == "synthesis":
                protocol = connection.get("protocol") if connection else "openai"
                current.model_snapshot["public_preview_support"] = {
                    "status": "conditional" if protocol in {"codex_app_server", "responses"} else "unsupported",
                    "protocol": protocol, "storage": "process_local",
                    "requires": "explicit_public_events_and_complete_paragraphs" if protocol in {"codex_app_server", "responses"}
                        else "complete_response_only"}
            if adaptive and query_path is not None and phase == "wiki_index":
                query_path = {**query_path, "selection_model_calls": query_path.get("selection_model_calls", 0) + 1,
                    "route": "expanded_discovery"}
                current.model_snapshot = {**current.model_snapshot, "query_path": query_path}
            dispatcher._audit(db, job, "answer.model_invocation_started", {"phase": phase, "limits": limits,
                "prompt_version": prompt_version, "system_prompt_sha256": system_sha,
                "format": "markdown", "records_sent": len(records), "sensitive_input_redacted": redacted,
                "local_sources_loaded": 0 if phase == "planning" else None})
            preview_binding = {"run_id": run_id, "job_id": job_id, "attempt": attempt,
                "owner_id": user.id, "request_number": current.model_snapshot["model_request_count"],
                "request_hash": svc.digest(current.request), "evidence_hash": svc.digest(current.evidence_snapshot),
                "catalog_stamp": None if preview_source_bound else catalog_stamp,
                "policy_stamp": source_plan["policy_stamp"] if source_plan else None}
        started = time.monotonic()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": body}]
        try:
            if connection:
                connection = {**connection, "_cancel_check": authority,
                    "_before_send_check": before_send}
                if phase == "synthesis" and records and connection.get("protocol") in {"codex_app_server", "responses"}:
                    entry = previews.begin(**preview_binding, model=providers.public_snapshot(connection))
                    # Transport callbacks do no database work. GET supplies the
                    # fresh authority/source fence before any staged text leaves.
                    connection["_on_public_text"] = lambda text: previews.update(entry, text)
                raw = providers.complete(connection, messages, max_tokens=settings.wiki_answer_max_output_tokens,
                    json_mode=False, output_schema=None, timeout=None)
            else:
                from .ai_transport import post_json
                raw = post_json(settings.llm_base_url, "chat/completions", {"model": settings.llm_model,
                    "messages": messages, "max_tokens": settings.wiki_answer_max_output_tokens, "stream": False},
                    settings.llm_api_key, None, cancel_check=authority)
            dispatcher._record_answer_model_response(job_id, attempt, phase, raw, time.monotonic() - started)
            text = raw["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise JobError("PROVIDER_EMPTY_OUTPUT")
            # Never display hidden reasoning accidentally embedded by a text-only model.
            public = build_narrative_answer(question, "answer", {}, [], text, run_id)["narrative_markdown"]
            return public, raw["choices"][0].get("finish_reason", "unknown")
        except providers.ProviderError as exc:
            dispatcher._record_answer_model_failure(job_id, attempt, phase, exc.code,
                time.monotonic() - started, exc.diagnostic)
            with dispatcher.session_factory.begin() as db:
                current = db.get(m.ConsultationRun, run_id)
                current.policy_snapshot = {**current.policy_snapshot, "generation_diagnostic": {**exc.diagnostic, "code": exc.code, "phase": phase}}
            raise JobError(exc.code) from None
        finally:
            # A provider completion is not a validated business answer. Do not
            # retain previews during subsequent reading or final validation.
            previews.discard(run_id, job_id=job_id, attempt=attempt)
            model_elapsed_ms += (time.monotonic() - started) * 1000

    manager = getattr(dispatcher.vector_index,"model_runtime",None)
    if manager is not None and manager.supported:
        from .model_warmup import ModelWarmupError
        dispatcher._checkpoint(job_id,attempt,"WARMING_RETRIEVAL_MODELS",{})
        manager.start()
        with dispatcher.session_factory.begin() as db:
            dispatcher._fence(db,job_id,attempt)
            current = db.get(m.ConsultationRun,run_id)
            current.model_snapshot = {**current.model_snapshot,"retrieval_runtime":manager.snapshot()}
        try:
            manager.ensure_ready(cancel_check=authority)
        except ModelWarmupError as exc:
            with dispatcher.session_factory.begin() as db:
                dispatcher._fence(db,job_id,attempt)
                current = db.get(m.ConsultationRun,run_id)
                current.model_snapshot = {**current.model_snapshot,"retrieval_runtime":manager.snapshot()}
            raise JobError(exc.code) from None
        with dispatcher.session_factory.begin() as db:
            dispatcher._fence(db,job_id,attempt)
            current = db.get(m.ConsultationRun,run_id)
            current.model_snapshot = {**current.model_snapshot,"retrieval_runtime":manager.snapshot()}

    # Exact, source-free plan reuse never reuses an answer or source admission.
    # A fresh connection and a current originating-run/source check precede any
    # use; the new retrieval/reading/synthesis chain below remains unchanged.
    if universal and choice and attempt == 1:
        actor_id = authority()
        with dispatcher.read_session_factory() as db:
            actor = db.get(m.User, actor_id)
            connection = providers.resolve_connection(db, actor, space_id, choice["connection_id"], choice["model_id"],
                settings, require_transfer=True, expected_revision=revision)
            public_plan_key = planning_cache_key(namespace=svc.digest({
                "storage": str(settings.storage_dir.resolve()), "database": settings.database_url}),
                owner_id=actor_id, space_id=space_id, request=request, connection=connection,
                prompt_version=prompt_version, system_sha256=system_sha,
                planning_instruction=UNIVERSAL_PLANNING_INSTRUCTION,
                max_output_tokens=settings.wiki_answer_max_output_tokens,
                business_day=str(svc.effective_date(context)))
            cache_entry = planning_cache.get(public_plan_key)
            if cache_entry:
                try:
                    cached_plan = validated_plan(db, actor, space_id, request, public_plan_key, cache_entry)
                except svc.APIError:
                    cached_plan = None  # New run still goes through normal fresh admission.
                if cached_plan is None:
                    planning_cache.discard(public_plan_key)
        authority()
        with dispatcher.session_factory.begin() as db:
            job = dispatcher._fence(db, job_id, attempt)
            current = db.get(m.ConsultationRun, run_id)
            metadata = {"hit": cached_plan is not None, "saved_model_requests": int(cached_plan is not None),
                "fresh_source_checks": True, "final_answer_reused": False}
            if cached_plan is not None:
                metadata.update(source_run_id=cache_entry["source_run_id"], age_ms=cache_entry["age_ms"])
                current.policy_snapshot = {**current.policy_snapshot, "question_analysis": {
                    "source": "model_prior_knowledge_unverified", "local_sources_loaded": 0, "plan": cached_plan,
                    "prompt_version": prompt_version, "system_prompt_sha256": system_sha}}
                dispatcher._audit(db, job, "answer.planning_cache_reused", {
                    **metadata, "plan_sha256": svc.digest(cached_plan), "additional_model_calls": 0})
            current.model_snapshot = {**current.model_snapshot, "planning_cache": metadata,
                "planning_reused": cached_plan is not None, "planning_model_invoked": False}

    if cached_plan is not None:
        plan = cached_plan
    elif adaptive and not universal:
        plan = {"interpretation": question, "initial_assessment": "程序路径装配，未调用独立规划模型；不是业务结论。",
            "search_queries": [question], "focus_terms": [], "decision_points": [], "missing_facts": []}
    elif reuse:
        plan = previous["plan"]
    else:
        planning_instruction = UNIVERSAL_PLANNING_INSTRUCTION if universal else PLANNING_INSTRUCTION
        preliminary, planning_finish = complete(planning_instruction +
            f"\n问题：{question}\n用户背景：{json.dumps(context, ensure_ascii=False)}", "planning")
        planning_complete = planning_finish == "stop"
        plan = {"interpretation": question, "initial_assessment": preliminary,
            "search_queries": list(dict.fromkeys([question, *planning_search_requests(preliminary)])),
            "focus_terms": [], "decision_points": [], "missing_facts": []}
        with dispatcher.session_factory.begin() as db:
            job = dispatcher._fence(db, job_id, attempt)
            current = db.get(m.ConsultationRun, run_id)
            current.policy_snapshot = {**current.policy_snapshot, "question_analysis": {
                "source": "model_prior_knowledge_unverified", "local_sources_loaded": 0, "plan": plan,
                "prompt_version": prompt_version, "system_prompt_sha256": system_sha}}
            dispatcher._audit(db, job, "answer.planning_completed", {"plan_sha256": svc.digest(plan), "local_sources_loaded": 0,
                "prompt_version": prompt_version, "system_prompt_sha256": system_sha})

    dispatcher._checkpoint(job_id, attempt, "LOADING_WIKI_CATALOG", {})
    with dispatcher.read_session_factory() as db:
        job = db.get(m.Job, job_id)
        actor, _, _ = dispatcher._run_context(db, job, read_only=True)
        pages = build_catalog(db, actor, space_id, context, scope=scope)
        connection = providers.resolve_connection(db, actor, space_id, choice["connection_id"], choice["model_id"],
            settings, require_transfer=True, expected_revision=revision) if choice else {}
        source_plan = build_reading_plan(db, space_id, question, context, pages)
        from .business_reading import add_foundations
        add_foundations(db, business, pages, source_plan, context)
    catalog_stamp = catalog_signature(pages)
    next_evidence, unavailable_pages = 1, set()
    # Measure the actual adapter envelope; do not divide its capacity by three.
    def fits(body):
        return fits_context(system, body, connection, default_capacity=settings.provider_max_request_bytes)
    intro = f"问题：{question}\n用户背景：{json.dumps(context,ensure_ascii=False)}\n前置研判（未核验）：{plan.get('initial_assessment','')}\n"
    if adaptive and not universal:
        intro = f"问题：{question}\n用户背景：{json.dumps(context,ensure_ascii=False)}\n程序已组织阅读路径；没有单独的前置模型研判。\n"
        intro += ("本轮为资料辅助答疑：可以使用已准入的待核验资料作有条件的参考解释，不能冒称正式制度。\n"
                  if scope == "reference" else "本轮为正式依据范围，实际来源资格由服务端核对。\n")
    if not business:
        intro += plan_instructions(source_plan)
    if universal:
        intro += "\n阅读计划用于指引查证，不是固定答案；请结合实际原文自主修正、补全并综合推理。\n"
    required_anchors = {source["page_id"]: source["anchor_block_ids"] for source in source_plan["sources"]}
    with dispatcher.session_factory.begin() as db:
        job = dispatcher._fence(db, job_id, attempt)
        current = db.get(m.ConsultationRun, run_id)
        current.model_snapshot = {**current.model_snapshot, "source_reading_plan": {
            "required_sources": [{key: source[key] for key in ("page_id", "resource_id", "version_id", "title", "role")}
                for source in source_plan["sources"]], "warnings": source_plan["warnings"]}}
    catalog = compact_catalog(pages) if adaptive else "\n".join(index_lines(pages))
    full_catalog_sent = False
    searched, retrieval_history = set(), []
    coverage_queries = list(plan.get("search_queries") or [question])
    coverage_attempted = set()
    discovery_selections = set()

    def select_full_catalog():
        nonlocal full_catalog_sent
        full_catalog_sent = True
        selected = []
        prefix = intro + "\n以下是本轮完整目录的一部分。优先选完整Wiki页；原始大文档将定位相关完整章节，避免整册无差别展开。"
        suffix = "\n用READ W编号选择，不需要JSON；目录不是已阅读正文，不得仅据标题作答。"
        for packet in pack_text(catalog, prefix, suffix, fits):
            response, _ = complete(prefix + packet + suffix, "wiki_index")
            selected.extend(read_requests(response, pages, selection=True))
            selected.extend(remember_commands(response, pages))
        return selected

    def discover(queries):
        nonlocal graph_plan, query_path, navigation_links
        from .hybrid_retrieval import candidate_context, search_catalog
        selected, pending_queries = [], list(queries)
        while pending_queries:
            current_queries, pending_queries = pending_queries, []
            merged, summaries = {}, []
            for query in current_queries:
                query = query.strip()
                if not query or query in searched:
                    continue
                searched.add(query)
                if universal and query not in coverage_queries:
                    coverage_queries.append(query)
                authority()
                dispatcher._checkpoint(job_id, attempt, "HYBRID_RETRIEVAL", {"retrieval_queries":len(searched)})
                with dispatcher.read_session_factory() as db:
                    user_id = authority()
                    result = search_catalog(db, user_id, space_id, query, pages=pages, scope=scope,
                        context=context, vector=dispatcher.vector_index, limit=settings.hybrid_candidate_limit)
                summaries.append(result)
                for hit in result["hits"]:
                    pid = hit["page_id"]
                    discovered_anchors.setdefault(pid, set()).update(hit.get("matched_block_ids", []))
                    if pid not in merged or hit["score"] > merged[pid]["score"]:
                        merged[pid] = hit
                retrieval_history.append({**{key:result[key] for key in ("query","mode","catalog_pages",
                    "indexed_catalog_pages","total_candidates","returned","warnings","timing_ms")},
                    **({"batch_execution": result["batch_execution"]} if result.get("batch_execution") is not None else {}),
                    "candidate_preview_stats": result.get("candidate_preview_stats", {})})
                with dispatcher.session_factory.begin() as db:
                    job = dispatcher._fence(db, job_id, attempt)
                    current = db.get(m.ConsultationRun, run_id)
                    current.model_snapshot = {**current.model_snapshot, "hybrid_retrieval":{
                        "strategy":"wiki_rag_rrf","queries":retrieval_history,
                        "candidate_snippets_loaded_for_discovery": sum(x.get("candidate_preview_stats", {}).get("verified_snippets", 0)
                            for x in retrieval_history),
                        "source_blocks_checked_for_discovery": sum(x.get("candidate_preview_stats", {}).get("source_blocks_checked", 0)
                            for x in retrieval_history), "full_catalog_available":True}}
                    dispatcher._audit(db,job,"answer.hybrid_discovery",{**retrieval_history[-1],
                        "candidate_version_ids":[hit["version_id"] for hit in result["hits"]]})
            signature = tuple((pid, tuple(sorted(discovered_anchors.get(pid, ())))) for pid in sorted(merged))
            if not merged or signature in discovery_selections:
                continue
            discovery_selections.add(signature)
            combined = {**summaries[0], "hits": sorted(merged.values(), key=lambda hit: (-hit["score"], hit["page_id"])),
                "warnings": list(dict.fromkeys(w for result in summaries for w in result["warnings"]))}
            if universal:
                combined["units"] = [dict(unit, matched_queries=[result["query"]], query_rank=rank)
                    for result in summaries for rank, unit in enumerate(result.get("units", []), 1)]
            if adaptive:
                from .query_graph import plan_graph_reads
                if not navigation_links:
                    from .wiki_navigation import load_inline_navigation
                    with dispatcher.read_session_factory() as db:
                        navigation_links = load_inline_navigation(db, authority(), space_id, pages)["links"]
                extra = plan_graph_reads(graph_query_text(question), pages, combined["hits"], inline_links=navigation_links,
                    required_sources=source_plan["sources"])
                if universal:
                    from .evidence_router import plan_evidence_reads
                    extra = plan_evidence_reads(question, pages, combined.get("units", []), graph_plan=extra,
                        max_seed_units=max(settings.retrieval_seed_units, len(current_queries)), conservative_graph=True,
                        include_query_routes=True)
                for pid, bids in extra["anchors"].items():
                    discovered_anchors.setdefault(pid, set()).update(bids)
                selected.extend(extra["requested"])
                if graph_plan is not None:
                    graph_plan["used_edges"] = [*graph_plan.get("used_edges", []), *extra.get("used_edges", [])]
                    if universal:
                        from .evidence_coverage import merge_routes
                        graph_plan["query_routes"] = merge_routes(graph_plan.get("query_routes", {}), extra.get("query_routes", {}))
                if query_path is not None:
                    query_path = {**query_path, "route": "expanded_grounded",
                        "additional_search_queries": len(searched), "used_edges": graph_plan.get("used_edges", [])}
                continue
            prompt = candidate_context(combined, pages)
            prefix = intro + "\n多个检索表达已合并去重，统一选择相关Wiki页与确需核对的来源。"
            suffix = "\n" + SELECTION_INSTRUCTION
            for packet in pack_text(prompt, prefix, suffix, fits):
                response, _ = complete(prefix + packet + suffix, "wiki_index")
                selected.extend(read_requests(response,pages,selection=True))
                selected.extend(remember_commands(response,pages))
                pending_queries.extend(search_requests(response))
                if requests_catalog(response) and not full_catalog_sent:
                    selected.extend(select_full_catalog())
        return list(dict.fromkeys(selected))

    fusion = settings.retrieval_mode == "hybrid" and dispatcher.vector_index is not None
    if adaptive:
        from .hybrid_retrieval import search_catalog
        from .query_graph import plan_graph_reads
        from .wiki_navigation import load_inline_navigation
        # Only identifier routes are reused. Current catalog, policy and source
        # verification are still rebuilt/rechecked for this actor and context.
        path_key = svc.digest(["evidence-bundle-path-v2", actor.id, space_id, scope, question, context,
            svc.primitive(svc.effective_date(context)),
            catalog_stamp, source_plan["policy_stamp"], prompt_version, system_sha,
            dispatcher.vector_index.embedding.fingerprint if dispatcher.vector_index else None,
            *([plan.get("search_queries"), settings.reranker_mode, settings.reranker_model, settings.reranker_revision,
               settings.reranker_max_tokens, settings.reranker_dtype, settings.reranker_instruction,
               settings.retrieval_strategy, settings.retrieval_unit_candidates, settings.retrieval_seed_units] if universal else [])])
        searched.update(plan.get("search_queries", [question]) if universal else [question])
        graph_plan = get_query_path(path_key)
        cache_hit = graph_plan is not None
        navigation = {"cache_hit": False, "body_blocks_loaded": 0, "body_characters_loaded": 0, "warnings": []}
        retrieval_ms = 0.0
        if graph_plan is None:
            dispatcher._checkpoint(job_id, attempt, "HYBRID_RETRIEVAL", {})
            start = time.monotonic()
            with dispatcher.read_session_factory() as db:
                user_id = authority()
                if universal:
                    from .universal_retrieval import search_many_catalog
                    result = search_many_catalog(db, user_id, space_id, plan.get("search_queries") or [question],
                        pages=pages, scope=scope, context=context, vector=dispatcher.vector_index,
                        limit=settings.hybrid_candidate_limit, checkpoint=authority,
                        session_factory=dispatcher.read_session_factory)
                else:
                    result = search_catalog(db, user_id, space_id, question, pages=pages, scope=scope,
                        context=context, vector=dispatcher.vector_index, limit=settings.hybrid_candidate_limit)
                navigation = load_inline_navigation(db, user_id, space_id, pages)
                navigation_links = navigation["links"]
            retrieval_ms = round((time.monotonic() - start) * 1000, 3)
            graph_plan = plan_graph_reads(graph_query_text(question), pages, result["hits"], inline_links=navigation["links"],
                required_sources=source_plan["sources"])
            if universal:
                from .evidence_router import plan_evidence_reads
                graph_plan = plan_evidence_reads(question, pages, result.get("units", []), graph_plan=graph_plan,
                    max_seed_units=max(settings.retrieval_seed_units, len(plan.get("search_queries", []))),
                    conservative_graph=True, include_query_routes=True)
                if business:
                    from .business_reading import core_catalog, merge_primary_route
                    primary_pages = core_catalog(business, pages)
                    primary_query = business["label"] + "：" + question
                    with dispatcher.read_session_factory() as db:
                        core_result = search_many_catalog(db, authority(), space_id, [primary_query],
                            pages=primary_pages, scope=scope, context=context, vector=dispatcher.vector_index,
                            limit=settings.hybrid_candidate_limit, checkpoint=authority,
                            session_factory=dispatcher.read_session_factory)
                    primary = plan_evidence_reads(primary_query, pages, core_result.get("units", []),
                        max_seed_units=settings.retrieval_seed_units, conservative_graph=True)
                    graph_plan = merge_primary_route(graph_plan, primary, primary_pages)
                    graph_plan["domain_retrieval"] = {"label": business["label"], "catalog_pages": len(primary_pages),
                        "returned": core_result["returned"], "query": primary_query,
                        "timing_ms": core_result["timing_ms"], "warnings": core_result["warnings"]}
                    if not graph_plan["domain_primary_anchors"]:
                        graph_plan["warnings"].append("DOMAIN_CORE_NOT_LOCATED")
                    retrieval_ms = round((time.monotonic() - start) * 1000, 3)
            graph_plan["warnings"] = list(dict.fromkeys([*graph_plan.get("warnings", []),
                *result["warnings"], *navigation.get("warnings", [])]))
            # Commit an ID-only route only after its complete selected sources
            # have actually passed normal scoped reading below. Advisory graph
            # warnings elsewhere in the catalog are not permission decisions.
            if not result["warnings"]:
                pending_path_cache_key = path_key
            retrieval_history.append({**{key:result[key] for key in ("query", "mode", "catalog_pages",
                "indexed_catalog_pages", "total_candidates", "returned", "warnings", "timing_ms")},
                **({"batch_execution": result["batch_execution"]} if result.get("batch_execution") is not None else {}),
                "candidate_preview_stats": result.get("candidate_preview_stats", {}),
                **({"searches": result.get("searches", [])} if universal else {})})
        if business:
            from .business_reading import bind_primary_route
            bind_primary_route(source_plan, graph_plan, pages)
            if not graph_plan.get("domain_primary_anchors"):
                source_plan["warnings"].append("DOMAIN_CORE_NOT_LOCATED")
            for source in source_plan["sources"]:
                pid = source["page_id"]
                required_anchors[pid] = list(dict.fromkeys([*required_anchors.get(pid, []), *source["anchor_block_ids"]]))
            intro += plan_instructions(source_plan)
        wanted = [pid for pid in graph_plan["requested"] if pid in pages]
        discovered_anchors = {pid: set(ids) for pid, ids in graph_plan["anchors"].items() if pid in pages}
        query_path = {"strategy": "universal_wiki_rag" if universal else "adaptive_graph",
            "route": "planned_grounded" if universal and wanted else "direct_grounded" if wanted else "clarification_or_evidence_gap",
            "cache_hit": cache_hit, "target_ms": settings.wiki_query_target_seconds * 1000,
            **({"batch_execution": result["batch_execution"]} if not cache_hit and result.get("batch_execution") is not None else {}),
            "separate_planning_model_calls": 1 if universal and cached_plan is None else 0, "selection_model_calls": 0,
            "retrieval_ms": retrieval_ms, "navigation_cache_hit": navigation.get("cache_hit", False),
            "navigation_body_blocks_loaded": navigation.get("body_blocks_loaded", 0),
            "navigation_body_characters_loaded": navigation.get("body_characters_loaded", 0),
            "requested_pages": wanted, "used_edges": graph_plan.get("used_edges", []),
            "reasons": graph_plan.get("reasons", {}), "warnings": graph_plan.get("warnings", []),
            "stats": graph_plan.get("stats", {}), "coverage_is_professional_verification": False,
            **({"domain_retrieval": graph_plan.get("domain_retrieval")} if business else {}),
            **({"reranker_model": settings.reranker_model, "reranker_revision": settings.reranker_revision,
                "reading_plan_source": "model_public_plan"} if universal else {})}
        with dispatcher.read_session_factory() as db:
            query_path["model_cost_hint"] = model_cost_hint(db, authority(), choice, query_path["target_ms"], connection)
        with dispatcher.session_factory.begin() as db:
            job = dispatcher._fence(db, job_id, attempt)
            current = db.get(m.ConsultationRun, run_id)
            current.model_snapshot = {**current.model_snapshot, "query_path": query_path,
                "source_reading_plan": {"required_sources": [{key: source[key] for key in
                    ("page_id", "resource_id", "version_id", "title", "role")} for source in source_plan["sources"]],
                    "warnings": source_plan["warnings"]},
                "hybrid_retrieval": {"strategy": "wiki_rag_graph_adaptive", "queries": retrieval_history,
                    "full_catalog_available": True}}
            dispatcher._audit(db, job, "answer.query_path_planned", query_path)
    else:
        wanted = discover(plan.get("search_queries") or [question]) if fusion else select_full_catalog()
    if not wanted and required_anchors:
        wanted = list(required_anchors)
    if universal and not wanted and pages and not full_catalog_sent:
        # A failed/missing unit index must not make the authorized library
        # inaccessible. This explicit model-read fallback is visible in audits.
        wanted = select_full_catalog()
    if not adaptive and fusion and not wanted and not full_catalog_sent:
        # A missing/partial/irrelevant index does not make the rest of the Wiki
        # unreachable. The complete catalog is a real, labelled fallback.
        wanted = select_full_catalog()
    if not adaptive and not wanted:
        wanted = fallback_pages(question, pages)
    wanted = list(dict.fromkeys([*wanted, *required_anchors]))
    final, finish = "", "unknown"
    reading_round = 0
    seen_reads = set()

    def read_signature(pid, anchors):
        page = pages[pid]
        available = {s["section_id"] for s in page.get("source_outline", [])}
        valid = set(section_requested.get(pid, ())) & available
        return (pid, pid in full_requested, tuple(sorted(valid)), tuple(sorted(anchors.get(pid, ()))),
                tuple(sorted(reference_requested.get(pid, ()))))

    while True:
        reading_round += 1
        authority()
        if universal:
            # The unit/graph planner already closed real source dependencies.
            # Do not re-expand all legacy/proposed backlinks after it made a
            # selective plan. Explicit model READs get their own real closure.
            from .evidence_router import plan_evidence_reads
            explicit = plan_evidence_reads(question, pages, [], conservative_graph=True,
                explicit_pages=[pid for pid in wanted if pid in manual_requested])
            expanded = list(dict.fromkeys([*wanted, *explicit["requested"]]))
            for pid, bids in explicit["anchors"].items():
                discovered_anchors.setdefault(pid, set()).update(bids)
            if graph_plan is not None:
                graph_plan["used_edges"] = [*graph_plan.get("used_edges", []), *explicit["used_edges"]]
        else:
            expanded = related_reads(pages, wanted)
        from .source_authority import extend_reads
        authority_reads = extend_reads(pages, expanded, discovered_anchors)
        expanded = authority_reads["requested"]
        full_requested.update(authority_reads["full_pages"])
        authority_requirements.update({fact["fact_id"]: fact for fact in authority_reads["requirements"]})
        direct_anchors = {pid: discovered_anchors.get(pid, ()) for pid in wanted
                          if not section_requested.get(pid) and pid not in full_requested}
        anchors = (defaultdict(set, {pid: set(discovered_anchors.get(pid, ())) for pid in expanded})
                   if universal else source_anchors(pages, expanded, direct_anchors))
        for pid, bids in required_anchors.items():
            if pid in expanded:
                anchors[pid].update(bids)
        fresh_ids = [pid for pid in expanded if pid not in unavailable_pages and read_signature(pid, anchors) not in seen_reads and (
            pid not in read_pages or pid in full_requested and not pages[pid].get("full_text_loaded")
            or set(section_requested.get(pid, ())) - {s["section_id"] for s in pages[pid].get("read_sections", [])}
            or set(reference_requested.get(pid, ())) - set(pages[pid].get("incoming_context", {}))
            or set(anchors.get(pid, ())) - {r["block_id"] for r in pages[pid].get("records", [])})]
        reading_progress("loading_sections", round=reading_round, requested_pages=len(fresh_ids),
                         current_batch=0, total_batches=0, completed_batches=0)
        with dispatcher.read_session_factory() as db:
            fresh, unavailable, next_evidence, outlines = read_scoped_pages(db, authority(), space_id, pages, fresh_ids,
                context=context, scope=scope, anchors=anchors, sections=section_requested,
                full_pages=full_requested, next_evidence=next_evidence,
                **({"structure_version": "v3", "context_completion": True,
                    "reference_locators": reference_requested} if universal else {}))
        seen_reads.update(read_signature(pid, anchors) for pid in fresh_ids)
        unavailable_pages.update(unavailable)
        fresh_ids = [pid for pid in fresh_ids if pid not in unavailable_pages and pages[pid].get("body_loaded")]
        verify(fresh)
        read_pages.update(fresh_ids)
        read_records.update({(row["version_id"], row["block_id"]): row for row in fresh})
        unsent_records.update({(row["version_id"], row["block_id"]): row for row in fresh})
        if (adaptive and pending_path_cache_key and not unavailable_pages and not outlines
                and set(graph_plan["requested"]) <= read_pages):
            put_query_path(pending_path_cache_key, graph_plan)
            pending_path_cache_key = None
        with dispatcher.session_factory.begin() as db:
            job = dispatcher._fence(db, job_id, attempt)
            current = db.get(m.ConsultationRun, run_id)
            current.evidence_snapshot = [{key: row[key] for key in ("resource_id", "version_id", "block_id", "content_sha256", "reference_signature", "evidence_id")
                if key in row} for row in read_records.values()]
            existing_evidence = set(db.execute(select(m.RunEvidence.version_id, m.RunEvidence.block_id)
                .where(m.RunEvidence.run_id == run_id)))
            for row in fresh:
                if (row["version_id"], row["block_id"]) not in existing_evidence:
                    db.add(m.RunEvidence(run_id=run_id, version_id=row["version_id"], block_id=row["block_id"], resource_access_epoch=row["resource_access_epoch"]))
            current.model_snapshot = {**current.model_snapshot, "wiki_reading": {"catalog_pages": len(pages),
                "loaded_pages": len(read_pages), "loaded_blocks": len(read_records),
                "loaded_characters": sum(len(row["text"]) for row in read_records.values()),
                "page_titles": [pages[pid]["title"] for pid in sorted(read_pages)],
                "full_text_loaded": bool(read_pages) and all(pages[pid].get("full_text_loaded", True) for pid in read_pages),
                "scoped_source_pages": sum(pages[pid].get("read_scope") == "sections" for pid in read_pages),
                "full_source_pages": sum(pages[pid]["kind"] == "document" and pages[pid].get("full_text_loaded") is True for pid in read_pages),
                "source_sections": sum(len(pages[pid].get("read_sections", [])) for pid in read_pages if pages[pid]["kind"] == "document"),
                "catalog_body_blocks_loaded": 0, "unavailable_pages": len(unavailable_pages),
                "typed_relation_navigation": True}}
            dispatcher._audit(db, job, "answer.wiki_pages_loaded", {"pages": fresh_ids, "blocks": len(fresh),
                "catalog_pages": len(pages), "whole_wiki_pages": True, "source_mode": "complete_sections"})
        ordered_pages = [pid for pid in pages if pid in read_pages]
        if universal:
            from .evidence_context import context_plan, context_instructions, order_context_groups
            from .evidence_coverage import reading_coverage, coverage_instructions
            closure = context_plan(pages, read_pages, unavailable_pages)
            context_report = closure["report"]
            coverage = reading_coverage(coverage_queries, graph_plan.get("query_routes", {}), pages, read_pages,
                unavailable=unavailable_pages, edges=graph_plan.get("used_edges", []))
            context_report.update(reading_coverage=coverage["report"],
                direction_count=coverage["report"]["direction_count"],
                direction_source_read_count=coverage["report"]["source_read_count"],
                direction_gap_count=coverage["report"]["gap_count"])
            followups = []
            for pid, bids in coverage["next_reads"].items():
                signature = (pid, pages[pid]["version_id"], tuple(bids))
                if signature not in coverage_attempted:
                    coverage_attempted.add(signature)
                    discovered_anchors.setdefault(pid, set()).update(bids)
                    followups.append(pid)
            if followups:
                # Use the ordinary exact dependency reader. No additional model
                # request, permission bypass, or arbitrary full-book expansion.
                from .evidence_router import plan_evidence_reads
                completion = plan_evidence_reads(question, pages, [], conservative_graph=True, explicit_pages=followups)
                followups = list(dict.fromkeys([*followups, *completion["requested"]]))
                for pid, bids in completion["anchors"].items():
                    discovered_anchors.setdefault(pid, set()).update(bids)
                graph_plan["used_edges"] = [*graph_plan.get("used_edges", []), *completion["used_edges"]]
            for pid, locators in closure["requests"].items():
                new = locators - set(reference_requested.get(pid, ()))
                if new:
                    reference_requested.setdefault(pid, set()).update(new)
                    followups.append(pid)
            missing_queries = [q for q in closure["searches"] if q not in dependency_searches and q not in searched]
            if missing_queries:
                dependency_searches.update(missing_queries)
                reading_progress("completing_dependencies", round=reading_round)
                # Same reference is searched once per run; unchanged absence is
                # an explicit gap, not an unbounded paid retry loop.
                followups.extend(discover(missing_queries))
            context_report["additional_searches"] = len(dependency_searches)
            with dispatcher.session_factory.begin() as db:
                job = dispatcher._fence(db, job_id, attempt)
                current = db.get(m.ConsultationRun, run_id)
                current.model_snapshot = {**current.model_snapshot, "context_completion": context_report}
            if followups:
                wanted = list(dict.fromkeys(followups))
                continue
            reading_progress("reranking_context", round=reading_round)
            authority()
            verify(list(read_records.values()))
            # No DB transaction held across native model inference. Authority,
            # cancellation and source identities are rechecked after the wait.
            ordered_pages, rank_receipt = order_context_groups(question, pages, read_pages,
                [*closure["edges"], *((e["source"], e["target"]) for e in graph_plan.get("used_edges", [])
                    if e.get("verification_status") == "REGISTERED_NOT_BUSINESS_VERIFIED"
                    and e.get("origin") != "proposed" and e.get("type") in {"CITES", "REQUIRES", "DEPENDS_ON", "APPLIES_TO", "EXCEPTION_OF"})],
                dispatcher.vector_index, group_rank_cache)
            authority()
            verify(list(read_records.values()))
            context_report["group_rerank"] = rank_receipt
            with dispatcher.session_factory.begin() as db:
                job = dispatcher._fence(db, job_id, attempt)
                current = db.get(m.ConsultationRun, run_id)
                current.model_snapshot = {**current.model_snapshot, "context_completion": context_report}
                dispatcher._audit(db, job, "answer.context_completed", {"status": context_report["status"],
                    "gaps": context_report["gap_count"], "references": context_report["reference_count"],
                    "group_rerank_status": rank_receipt["status"], "dropped_pages": 0})
        if business:
            from .business_reading import primary_first
            ordered_pages = primary_first(ordered_pages, source_plan)
        # Previously read material remains in direct synthesis whenever it fits.
        # For explicit very-large reads only NEW source sections need compiling;
        # do not restart a whole-handbook reading pass after every follow-up READ.
        body = compact_evidence(pages, ordered_pages, used_edges=graph_plan.get("used_edges", [])) \
            if adaptive else "\n\n".join(page_text(pages[pid], related_pages=read_pages) for pid in pages if pid in read_pages)
        if context_report is not None:
            body += context_instructions(context_report)
            body += coverage_instructions(context_report["reading_coverage"])
        if outlines:
            body += "\n" + outline_text(pages, outlines)
        if not body:
            body = "本次没有匹配或可读的本地正文。可以给明确标注的一般分析，不得伪造本地依据。"
        if unavailable_pages:
            body += "\n下列页当前未通过完整正文/版本校验，未加载也不可作依据：" + " ".join(sorted(unavailable_pages))
        prefix = intro + "\n本次已核对的完整Wiki/原文小节：\n"
        instruction = "\n" + (UNIVERSAL_SYNTHESIS_INSTRUCTION if universal else
                              ADAPTIVE_SYNTHESIS_INSTRUCTION if adaptive else SYNTHESIS_INSTRUCTION)
        if context_report and context_report["gap_count"]:
            instruction += f"\n本轮原文显式依赖仍有{context_report['gap_count']}处未精确定位，请保留相应限制，不得宣称这些依赖已核验。"
        if adaptive and query_path is not None:
            query_path = {**query_path, "preparation_ms": round((time.monotonic() - path_started) * 1000, 3),
                "complete_selected_units": True, "loaded_pages": len(read_pages), "loaded_blocks": len(read_records),
                "loaded_wiki_pages": sum(pages[p]["kind"] == "knowledge" for p in read_pages),
                "loaded_source_pages": sum(pages[p]["kind"] == "document" for p in read_pages),
                "request_fits_single_synthesis": fits(prefix + body + instruction), "reading_round": reading_round}
            query_path["preparation_ms"] = round(query_path["preparation_ms"] - model_elapsed_ms, 3)
            with dispatcher.session_factory.begin() as db:
                job = dispatcher._fence(db, job_id, attempt)
                current = db.get(m.ConsultationRun, run_id)
                current.model_snapshot = {**current.model_snapshot, "query_path": query_path}
                dispatcher._audit(db, job, "answer.query_path_materialized", query_path)
        total_chars = len(body)
        if fits(prefix + body + instruction):
            reading_progress("synthesis", round=reading_round, current_batch=1, total_batches=1,
                completed_batches=0, total_characters=total_chars, sent_characters=0,
                loaded_blocks=len(read_records), requested_pages=len(fresh_ids))
            final, finish = complete(prefix + body + instruction, "synthesis", list(read_records.values()),
                preview_source_bound=adaptive and not outlines and not notes and
                    {source["version_id"] for source in source_plan["sources"]} <=
                    {row["version_id"] for row in read_records.values()})
            reading_progress("batch_completed", current_batch=1, total_batches=1, completed_batches=1,
                             sent_characters=total_chars)
            packets = [body]
            if read_requests(final, pages) or full_read_requests(final, pages) or section_read_requests(final, pages):
                # Public intermediate prose can carry working notes into a later
                # over-capacity round; it is not hidden reasoning or source truth.
                notes = [final]
        else:
            if notes and unsent_records:
                # Automatic dependency reads can span several iterations before
                # the next model call. Keep ALL not-yet-sent blocks, not only
                # the last iteration's fresh list.
                fresh_keys = set(unsent_records)
                body = "\n\n".join(page_text({**pages[pid], "records": [row for row in pages[pid]["records"]
                    if (row["version_id"], row["block_id"]) in fresh_keys], "read_scope": "sections"}, related_pages=read_pages)
                    for pid in ordered_pages if any((r["version_id"], r["block_id"]) in fresh_keys for r in pages[pid]["records"]))
                if context_report is not None:
                    body += context_instructions(context_report)
                    body += coverage_instructions(context_report["reading_coverage"])
                if outlines:
                    body += "\n" + outline_text(pages, outlines)
            note_instruction = "\n" + NOTES_INSTRUCTION
            packets = pack_text(body, prefix, note_instruction, fits)
            sent_chars = 0
            for index, packet in enumerate(packets):
                last = index == len(packets) - 1
                material = "\n\n".join(notes)
                combined = prefix + "此前阅读提要（来源仍可继续核对）：\n" + material + "\n新增完整内容：\n" + packet + instruction
                # The final source batch and accumulated notes may fit directly.
                direct = last and fits(combined)
                reading_progress("synthesis" if direct else "reading_sections", round=reading_round,
                    current_batch=index + 1, total_batches=len(packets), completed_batches=index,
                    total_characters=len(body), sent_characters=sent_chars, loaded_blocks=len(read_records))
                if direct:
                    final, finish = complete(combined, "synthesis", list(read_records.values()))
                else:
                    note, _ = complete(prefix + packet + note_instruction, "wiki_notes", list(read_records.values()))
                    notes.append(note)
                sent_chars += len(packet)
                reading_progress("batch_completed", completed_batches=index + 1, sent_characters=sent_chars)
            if not direct:
                material = "\n\n".join(notes)
                summary_prefix = intro + "\n以下为本次已完整阅读材料的公开核对提要，原始E编号保持可追溯：\n"
                if not fits(summary_prefix + material + instruction):
                    reading_progress("consolidating")
                    compacted = []
                    for part in pack_text(material, summary_prefix, note_instruction, fits):
                        note, _ = complete(summary_prefix + part + note_instruction, "wiki_notes", list(read_records.values()))
                        compacted.append(note)
                    notes, material = compacted, "\n\n".join(compacted)
                if not fits(summary_prefix + material + instruction):
                    raise JobError("PROVIDER_CONTEXT_CAPACITY_REQUIRED")
                reading_progress("synthesis", current_batch=len(packets), completed_batches=len(packets), total_batches=len(packets))
                final, finish = complete(summary_prefix + material + instruction, "synthesis", list(read_records.values()))
        with dispatcher.session_factory.begin() as db:
            job = dispatcher._fence(db, job_id, attempt)
            dispatcher._audit(db, job, "answer.wiki_pages_read", {"pages": sorted(read_pages),
                "blocks": len(read_records), "packets": len(packets), "complete_selected_units_sent": True})
        unsent_records.clear()
        requested = remember_commands(final, pages)
        if fusion:
            requested.extend(discover(search_requests(final)))
            if requests_catalog(final) and not full_catalog_sent:
                requested.extend(select_full_catalog())
        next_anchors = source_anchors(pages, related_reads(pages, requested), {
            pid: discovered_anchors.get(pid, ()) for pid in requested
            if not section_requested.get(pid) and pid not in full_requested})
        wanted = [pid for pid in requested if pid not in unavailable_pages and read_signature(pid, next_anchors) not in seen_reads and (
            pid not in read_pages or pid in full_requested and not pages[pid].get("full_text_loaded")
            or set(section_requested.get(pid, ())) - {s["section_id"] for s in pages[pid].get("read_sections", [])})]
        if wanted:
            continue
        if (read_requests(final, pages) or full_read_requests(final, pages) or section_read_requests(final, pages)
                or adaptive and (search_requests(final) or requests_catalog(final))):
            # A repeated READ cannot loop/bill forever. Ask for a useful final
            # explanation based on the already read texts, not another schema retry.
            final_body = intro + "\n本次重复或无法定位的阅读/检索没有带来新正文。请依据下列实际已读内容给出Markdown答复，未加载章节不可冒称已读；缺口明确说明，不再重复相同请求。\n" + body
            if not fits(final_body):
                final_body = intro + "\n请依据已阅读材料的提要给出Markdown答复，明确剩余缺口，不重复READ。\n" + "\n".join(notes)
            final, finish = complete(final_body, "synthesis", list(read_records.values()))
        break

    authority()
    verify(list(read_records.values()))
    with dispatcher.read_session_factory() as db:
        if policy_stamp(db, space_id) != source_plan["policy_stamp"]:
            raise JobError("SOURCE_READING_POLICY_CHANGED")
    answer = build_narrative_answer(question, "solution" if request.get("mode") == "solution" else "answer",
        context, list(read_records.values()), final, run_id)
    primary_coverage = check_primary_citations(source_plan, list(read_records.values()), answer)
    from .source_authority import check_coverage
    authority_coverage = check_coverage(pages, list(authority_requirements.values()), list(read_records.values()), answer)
    citation_trace = None
    if universal:
        from .citation_map import public_citation_map
        citation_trace = public_citation_map(answer["narrative_markdown"], list(read_records.values()))
    if finish != "stop":
        answer["quality_warnings"].append({"code": "MODEL_OUTPUT_INCOMPLETE", "message": "服务商未标记完整结束；已保留收到的公开答复，内容可能未完成。"})
    if context_report and context_report["gap_count"]:
        answer["quality_warnings"].append({"code": "SOURCE_CONTEXT_GAPS", "message":
            f"已读资料中仍有 {context_report['gap_count']} 处显式引用未精确定位；请查看执行记录中的关联补全详情，相关内容不可视为已核验依据。"})
    if context_report and context_report.get("direction_gap_count"):
        answer["quality_warnings"].append({"code": "READING_COVERAGE_GAPS", "message":
            f"仍有 {context_report['direction_gap_count']} 个查证方向尚未关联到实际已读原文；请核对阅读账本。已读状态本身也不代表结论获原文支持。"})
    dispatcher._validate_answer(answer, list(read_records.values()))
    with dispatcher.session_factory.begin() as db:
        job = dispatcher._fence(db, job_id, attempt)
        actor, current, _ = dispatcher._run_context(db, job)
        if choice:
            providers.resolve_connection(db, actor, space_id, choice["connection_id"], choice["model_id"],
                settings, require_transfer=True, expected_revision=revision)
        db.scalar(select(m.Space).where(m.Space.id == space_id).with_for_update())
        if policy_stamp(db, space_id) != source_plan["policy_stamp"]:
            raise JobError("SOURCE_READING_POLICY_CHANGED")
        for vid in sorted({row["version_id"] for row in read_records.values()}):
            dispatcher._version(db, actor, vid)
        from .source_authority import verify_bindings
        try:
            verify_bindings(db, actor, list(authority_requirements.values()))
        except svc.APIError as exc:
            raise JobError(exc.code) from exc
        current_records = reference_evidence(db, actor, space_id, context, reading=True,
            version_ids={row["version_id"] for row in read_records.values()}) if scope == "reference" else svc.eligible_evidence(db, actor, space_id, context,
                version_ids={row["version_id"] for row in read_records.values()})
        signatures = {evidence_signature(row) for row in current_records}
        if any(evidence_signature(row) not in signatures for row in read_records.values()):
            raise JobError("EVIDENCE_ACCESS_CHANGED")
        from .source_authority import authority_stamp
        final_authority_stamp = authority_stamp(db, space_id)
        current.response, current.state, current.completed_at = answer, "COMPLETED", svc.now()
        current.model_snapshot = {**current.model_snapshot, "validation_status": "review_required",
            "execution_mode": "wiki_reading", "answer_model_invoked": True, "evidence_count": len(answer["citations"]),
            "primary_source_coverage": primary_coverage, "source_authority_coverage": authority_coverage,
            "source_authority_stamp": final_authority_stamp}
        if citation_trace is not None:
            current.model_snapshot["citation_integrity"] = citation_trace
        if adaptive:
            execution_ms = round((time.monotonic() - path_started) * 1000, 3)
            elapsed_ms = round((svc.now() - current.created_at).total_seconds() * 1000, 3)
            current.model_snapshot["query_path"] = {**(current.model_snapshot.get("query_path") or {}),
                "elapsed_ms": elapsed_ms, "within_target": elapsed_ms <= settings.wiki_query_target_seconds * 1000,
                "execution_elapsed_ms": execution_ms,
                "model_call_elapsed_ms": round(model_elapsed_ms, 3),
                "model_requests": current.model_snapshot.get("model_request_count", 0) - previous_calls,
                "professional_accuracy": "NOT_EVALUATED"}
        current.policy_snapshot = {**current.policy_snapshot, "professional_accuracy": "NOT_EVALUATED",
            "validation": "SOURCE_IDENTITIES_CHECKED_PROSE_REQUIRES_REVIEW", "generation_diagnostic": {}}
        cacheable = bool(public_plan_key and planning_complete and finish == "stop" and answer.get("citations")
            and all(w.get("code") == "BUSINESS_VERIFICATION_REQUIRED" for w in answer.get("quality_warnings", [])))
        if cacheable:
            current.policy_snapshot["planning_cache_binding"] = {"key": public_plan_key,
                "plan_sha256": svc.digest(plan), "source_reading_policy_stamp": source_plan["policy_stamp"]}
        dispatcher._succeed(db, job, {"run_id": run_id})
    # Publish only after commit. Failed/cancelled/incomplete runs never seed the
    # cache, and a hit never refreshes TTL by chaining a new originating run.
    if cacheable:
        planning_cache.put(public_plan_key, plan=plan, source_run_id=run_id)
