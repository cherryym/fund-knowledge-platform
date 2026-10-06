"""Interactive Wiki query: whole catalog -> complete pages -> linked reads -> Markdown.

Source/credential authority stays in the existing services. Model prose is not a
database command, and a READ can address only this request's registered pages.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from hashlib import sha256

from sqlalchemy import select

from . import models as m
from . import providers
from . import services as svc
from .answer_packing import fits_context, pack_text
from .answer_preview import previews
from .answer_timing import AnswerTimings, persist_timings
from .evidence_review import model_review_context, prepare_source_review, review_answer, review_warnings
from .planning_cache import planning_cache, planning_cache_key, validated_plan
from .query_context import compact_catalog, compact_evidence, graph_query_text
from .query_cost import model_cost_hint
from .query_path_cache import get_query_path, put_query_path
from .reference_evidence import evidence_signature, reference_evidence
from .retrieval_observation import observation_summary
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
    REASONING_PLANNING_INSTRUCTION,
    REASONING_PROMPT_VERSION,
    REASONING_SYNTHESIS_INSTRUCTION,
    REASONING_SYSTEM,
    REASONING_SYSTEM_SHA256,
    SELECTION_INSTRUCTION,
    SYNTHESIS_INSTRUCTION,
    SYSTEM,
    SYSTEM_SHA256,
    UNIVERSAL_PLANNING_INSTRUCTION,
    UNIVERSAL_PROMPT_VERSION,
    UNIVERSAL_SYNTHESIS_INSTRUCTION,
    UNIVERSAL_SYSTEM,
    UNIVERSAL_SYSTEM_SHA256,
    clean_search_queries,
    fallback_pages,
    full_read_requests,
    gap_requests,
    index_lines,
    library_catalog_instruction,
    move_gap_lines,
    display_title,
    name_page_ids,
    page_text,
    planning_read_requests,
    planning_search_requests,
    read_requests,
    requests_catalog,
    search_requests,
    section_read_requests,
    sort_gaps,
)
from .wiki_section_reader import outline_text, read_scoped_pages, source_anchors


# Reasoning strategy: an explicit READ of a source this small is read whole; a
# larger unlocated source contributes this many question-restricted units.
FULL_READ_BLOCKS = 120
# Whole-source reads are kept while the estimated reading still fits this share of one
# synthesis request; the remainder is read located, avoiding extra note-taking calls.
READ_BUDGET_SHARE = 0.75
READ_ESTIMATE_BLOCK_BYTES = 48
READ_ESTIMATE_SECTION_FACTOR = 3
LOCATED_UNITS_PER_READ = 3
# Question-restricted locating searches per run; later rounds keep outlines + READ_SECTION.
LOCATED_SEARCH_ROUNDS = 2
# A page too large for the synthesis request keeps only sections the reranker scores above this
# (Qwen3-Reranker logit: above 0 = judged relevant to the question).
SECTION_MIN_SCORE = 0.0
# Only pages the model asked for or among this many top-ranked pages are reduced (bounds reranking time).
REDUCE_TOP_RANKED = 6
# Reasoning: a search for an explicitly cited document/clause reranks only this many fused candidates
# (finding a named reference does not need the full discovery pool; every reference is still searched).
DEPENDENCY_RERANK_UNITS = 16
# Reasoning: unresolved explicit references listed in the synthesis prompt (the rest are counted). A tax law with
# hundreds of cross-references otherwise fills most of a 64 KiB request with the list alone.
CONTEXT_GAP_LINES = 20


def run_wiki_answer(dispatcher, job_id, attempt):
    timings = AnswerTimings()
    try:
        return _run_wiki_answer(dispatcher, job_id, attempt, timings=timings)
    finally:
        # Covers cancellation, stale leases, final validation and cleanup errors.
        previews.discard_job(job_id, attempt)
        persist_timings(dispatcher, job_id, attempt, timings)


def _run_wiki_answer(dispatcher, job_id, attempt, *, timings=None):
    from .jobs import JobError
    from .wiki_answer_content import build_narrative_answer

    timings = timings or AnswerTimings()
    path_started = time.monotonic()
    with dispatcher.session_factory.begin() as db:
        job = dispatcher._fence(db, job_id, attempt)
        actor, run, thread = dispatcher._run_context(db, job, validate_attachments=False)
        previous = (run.policy_snapshot or {}).get("question_analysis")
        previous_calls = int((run.model_snapshot or {}).get("model_request_count", 0))
        settings, _ = dispatcher._answer_policy(db, run)
        # Reasoning core = universal reading/citation path + library map before
        # planning, no question-type reading rules, explicit derivation and GAPs.
        reasoning = settings.wiki_query_strategy == "reasoning"
        universal = reasoning or settings.wiki_query_strategy == "universal"
        adaptive = universal or settings.wiki_query_strategy == "adaptive"
        system = (REASONING_SYSTEM if reasoning else UNIVERSAL_SYSTEM if universal
                  else ADAPTIVE_SYSTEM if adaptive else SYSTEM)
        prompt_version = (REASONING_PROMPT_VERSION if reasoning else UNIVERSAL_PROMPT_VERSION if universal
                          else ADAPTIVE_PROMPT_VERSION if adaptive else PROMPT_VERSION)
        system_sha = (REASONING_SYSTEM_SHA256 if reasoning else UNIVERSAL_SYSTEM_SHA256 if universal
                      else ADAPTIVE_SYSTEM_SHA256 if adaptive else SYSTEM_SHA256)
        request, context, space_id = dict(run.request), dict(run.request.get("context", {})), thread.space_id
        from .business_reading import business_profile, profile_instructions
        business = business_profile(db, space_id) if universal and not reasoning else None
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
        run.model_snapshot = {**{key: value for key, value in (run.model_snapshot or {}).items()
            if key != "evidence_review"}, "answer_engine": "wiki_reader",
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
    source_review = None
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

    def phase_effort(connection, phase):
        # Deeper synthesis only where the value is attested (Codex) or documented
        # (OpenAI API); planning and other providers keep their existing default.
        wanted = settings.wiki_synthesis_reasoning_effort
        if not reasoning or wanted is None or phase != "synthesis" or not connection:
            return None
        if connection.get("protocol") == "codex_app_server":
            engine = connection.get("_codex_engine")
            supports = getattr(engine, "supports_reasoning", None)
            return wanted if callable(supports) and supports(connection.get("model_id"), wanted) else None
        if connection.get("provider_id") == "openai" and connection.get("protocol") in {"responses", "openai"}:
            return wanted
        return None

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
            effort = phase_effort(connection, phase)
            dispatcher._audit(db, job, "answer.model_invocation_started", {"phase": phase, "limits": limits,
                "prompt_version": prompt_version, "system_prompt_sha256": system_sha,
                "format": "markdown", "records_sent": len(records), "sensitive_input_redacted": redacted,
                "local_sources_loaded": 0 if phase == "planning" else None,
                **({"reasoning_effort_requested": effort} if reasoning else {})})
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
                    json_mode=False, output_schema=None, timeout=None,
                    **({"reasoning_effort": effort} if effort else {}))
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
            elapsed = time.monotonic() - started
            model_elapsed_ms += elapsed * 1000
            timings.add("model_" + phase, elapsed)

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
            with timings.measure("local_model_preparation"):
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

    library = None
    if reasoning:
        # The planner sees what exists (metadata only) before deciding what to
        # read. Same authorized catalog, no bodies; no question-type reading rules.
        dispatcher._checkpoint(job_id, attempt, "LOADING_WIKI_CATALOG", {})
        with timings.measure("catalog_and_policy"), dispatcher.read_session_factory() as db:
            job = db.get(m.Job, job_id)
            actor, _, _ = dispatcher._run_context(db, job, read_only=True)
            pages = build_catalog(db, actor, space_id, context, scope=scope)
            connection = providers.resolve_connection(db, actor, space_id, choice["connection_id"], choice["model_id"],
                settings, require_transfer=True, expected_revision=revision) if choice else {}
            source_plan = {"policy_stamp": policy_stamp(db, space_id), "sources": [], "matched_rules": [], "warnings": []}
            from .source_metadata import load as load_source_metadata
            candidates = load_source_metadata(db, [p["version_id"] for p in pages.values() if p["kind"] == "document"])
        catalog_stamp = catalog_signature(pages)
        from .library_map import PLANNING_LEVELS, build_fitting
        planning_tail = f"\n问题：{question}\n用户背景：{json.dumps(context, ensure_ascii=False)}"
        # Source list with metadata first: planners select sources; knowledge pages
        # stay reachable through SEARCH/CATALOG and the dependency closure.
        library = build_fitting(pages, candidates, lambda text: fits_context(system,
            REASONING_PLANNING_INSTRUCTION + "\n" + text + planning_tail, connection,
            default_capacity=settings.provider_max_request_bytes), lambda text: text, levels=PLANNING_LEVELS)
        with dispatcher.session_factory.begin() as db:
            dispatcher._fence(db, job_id, attempt)
            current = db.get(m.ConsultationRun, run_id)
            current.model_snapshot = {**current.model_snapshot, "library_map": ({**library["stats"],
                "sha256": library["sha256"]} if library else {"status": "NOT_FITTED", "body_blocks_loaded": 0})}

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
                # A reasoning plan depends on the library map (W-ids, titles, metadata).
                planning_instruction=(REASONING_PLANNING_INSTRUCTION + "\n" + (library or {}).get("sha256", "")
                                      if reasoning else UNIVERSAL_PLANNING_INSTRUCTION),
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
        planning_instruction = (REASONING_PLANNING_INSTRUCTION if reasoning else
                                UNIVERSAL_PLANNING_INSTRUCTION if universal else PLANNING_INSTRUCTION)
        map_text = "\n" + library["text"] if library else ""
        preliminary, planning_finish = complete(planning_instruction + map_text +
            f"\n问题：{question}\n用户背景：{json.dumps(context, ensure_ascii=False)}", "planning")
        planning_complete = planning_finish == "stop"
        plan = {"interpretation": question, "initial_assessment": preliminary,
            "search_queries": list(dict.fromkeys([question, *planning_search_requests(preliminary)])),
            "focus_terms": [], "decision_points": [], "missing_facts": []}
        if reasoning:
            plan["search_queries"] = clean_search_queries(plan["search_queries"])
            plan["gaps"] = gap_requests(preliminary)
            plan["library_map_sha256"] = library["sha256"] if library else None
        with dispatcher.session_factory.begin() as db:
            job = dispatcher._fence(db, job_id, attempt)
            current = db.get(m.ConsultationRun, run_id)
            current.policy_snapshot = {**current.policy_snapshot, "question_analysis": {
                "source": "model_with_library_map_metadata" if library else "model_prior_knowledge_unverified",
                "local_sources_loaded": 0, "plan": plan,
                "prompt_version": prompt_version, "system_prompt_sha256": system_sha}}
            dispatcher._audit(db, job, "answer.planning_completed", {"plan_sha256": svc.digest(plan), "local_sources_loaded": 0,
                "prompt_version": prompt_version, "system_prompt_sha256": system_sha,
                **({"library_map_sha256": library["sha256"], "library_map_level": library["stats"]["level"]}
                   if library else {})})

    if not reasoning:
        dispatcher._checkpoint(job_id, attempt, "LOADING_WIKI_CATALOG", {})
        with timings.measure("catalog_and_policy"), dispatcher.read_session_factory() as db:
            job = db.get(m.Job, job_id)
            actor, _, _ = dispatcher._run_context(db, job, read_only=True)
            pages = build_catalog(db, actor, space_id, context, scope=scope)
            connection = providers.resolve_connection(db, actor, space_id, choice["connection_id"], choice["model_id"],
                settings, require_transfer=True, expected_revision=revision) if choice else {}
            source_plan = build_reading_plan(db, space_id, question, context, pages)
            from .business_reading import add_foundations
            add_foundations(db, business, pages, source_plan, context)
        catalog_stamp = catalog_signature(pages)
    # Planned READ lines (reasoning map) are explicit model reads: same scoped
    # reader, dependency closure and ACL/hash checks as any later READ.
    plan_reads = []
    if reasoning:
        plan_text = plan.get("initial_assessment", "")
        plan_reads = list(dict.fromkeys([*remember_commands(plan_text, pages),
                                         *planning_read_requests(plan_text, pages)]))
        manual_requested.update(plan_reads)
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

    def discover(queries, rerank_units=None):
        nonlocal graph_plan, query_path, navigation_links
        from .hybrid_retrieval import candidate_context, search_catalog
        selected, pending_queries = [], list(queries)
        while pending_queries:
            current_queries, pending_queries = pending_queries, []
            current_queries = list(dict.fromkeys(q.strip() for q in current_queries
                if isinstance(q, str) and q.strip() and q.strip() not in searched))
            prefetched = {}
            vector = dispatcher.vector_index
            if (universal and current_queries and vector is not None
                    and getattr(vector.settings, "retrieval_strategy", None) == "unit_rerank"
                    and callable(getattr(vector, "search_many", None))
                    and callable(getattr(vector, "rerank_many", None))):
                from .batch_retrieval import search_catalog_batch
                # Follow-up and dependency searches get the same fresh three-
                # boundary batch path as initial planning; no SQL transaction
                # stays open over inference, and no query direction is removed.
                with timings.measure("retrieval_and_navigation"):
                    batch_results, batch_receipt = search_catalog_batch(authority(), space_id, current_queries,
                        pages=pages, scope=scope, context=context, vector=vector,
                        limit=settings.hybrid_candidate_limit, checkpoint=authority,
                        session_factory=dispatcher.read_session_factory, inference_schedule="serial_equivalent",
                        rerank_units=rerank_units)
                prefetched = {q: {**result, "batch_execution": batch_receipt}
                              for q, result in zip(current_queries, batch_results, strict=True)}
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
                if query in prefetched:
                    result = prefetched[query]
                else:
                    with timings.measure("retrieval_and_navigation"), dispatcher.read_session_factory() as db:
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
                    "candidate_preview_stats": result.get("candidate_preview_stats", {}),
                    "retrieval_observations": [observation_summary(result["retrieval_trace"])] if "retrieval_trace" in result else []})
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
        path_key = svc.digest(["evidence-bundle-path-v3", actor.id, space_id, scope, question, context,
            svc.primitive(svc.effective_date(context)),
            catalog_stamp, source_plan["policy_stamp"], prompt_version, system_sha,
            dispatcher.vector_index.embedding.fingerprint if dispatcher.vector_index else None,
            *([plan.get("search_queries"), settings.reranker_mode, settings.reranker_model, settings.reranker_revision,
               settings.reranker_max_tokens, settings.reranker_dtype, settings.reranker_instruction,
               settings.retrieval_strategy, settings.retrieval_rerank_policy,
               settings.retrieval_unit_candidates, settings.retrieval_seed_units] if universal else [])])
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
                        "timing_ms": core_result["timing_ms"], "warnings": core_result["warnings"],
                        "retrieval_observations": core_result.get("retrieval_observations", [])}
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
                "retrieval_observations": result.get("retrieval_observations", []),
                **({"searches": result.get("searches", [])} if universal else {})})
        timings.add("retrieval_and_navigation", retrieval_ms / 1000)
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
        if reasoning:
            wanted = list(dict.fromkeys([*[pid for pid in plan_reads if pid in pages], *wanted]))
        discovered_anchors = {pid: set(ids) for pid, ids in graph_plan["anchors"].items() if pid in pages}
        query_path = {"strategy": "reasoning_core" if reasoning else "universal_wiki_rag" if universal else "adaptive_graph",
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
                "candidate_policy": settings.retrieval_rerank_policy,
                "reading_plan_source": "model_plan_with_library_map" if library else "model_public_plan"} if universal else {}),
            **({"planned_reads": [pid for pid in plan_reads if pid in pages],
                "library_map_level": (library or {}).get("stats", {}).get("level")} if reasoning else {})}
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
    located_attempted, prose_read_used, located_rounds = set(), False, 0
    budget_located = []

    def within_read_budget(candidates, expanded, anchors):
        """Reasoning: whole-source candidates (smallest first) that, together with what is read
        and this round's other reads (estimated from block sizes), fit READ_BUDGET_SHARE of one
        synthesis request. Sizes are metadata only; no body is loaded here."""
        if not candidates:
            return []
        from sqlalchemy import func
        others = [pid for pid in expanded if pid not in read_pages and pid not in candidates]
        versions = {pages[pid]["version_id"]: pid for pid in [*candidates, *others]}
        with dispatcher.read_session_factory() as db:
            stats = {versions[vid]: (count, chars or 0) for vid, count, chars in db.execute(
                select(m.ContentBlock.version_id, func.count(), func.sum(func.length(m.ContentBlock.search_text)))
                .where(m.ContentBlock.version_id.in_(list(versions))).group_by(m.ContentBlock.version_id))}

        def estimate(pid, blocks=None):
            count, chars = stats.get(pid, (0, 0))
            if blocks is not None and count:
                count, chars = min(count, blocks), min(chars, chars * blocks // count)
            return count * READ_ESTIMATE_BLOCK_BYTES + 3 * chars

        used = sum(len(row["text"].encode()) + READ_ESTIMATE_BLOCK_BYTES for row in read_records.values())
        used += sum(estimate(pid) if pages[pid]["kind"] == "knowledge" or pid in full_requested
                    else estimate(pid, len(anchors.get(pid, ())) * READ_ESTIMATE_SECTION_FACTOR) for pid in others)
        chosen = []
        for pid in sorted(candidates, key=estimate):
            if not fits(intro + "x" * int((used + estimate(pid)) / READ_BUDGET_SHARE) + REASONING_SYNTHESIS_INSTRUCTION):
                break
            chosen.append(pid)
            used += estimate(pid)
        return chosen

    wiki_located = set()

    def wiki_citation_targets(expanded):
        """Reasoning: sources reached only because a READ knowledge page cites them. They are read where they answer
        this question (question-restricted located units, as for large sources), not at every block the page cites -
        a concept page can cite thousands of blocks, and graph planning may already have anchored them all. The
        knowledge page itself is still read whole. Each source is located once per run."""
        knowledge = [pid for pid in expanded if pid in manual_requested and pages[pid]["kind"] == "knowledge"]
        cited = source_anchors(pages, knowledge) if knowledge else {}
        targets = {pid for pid in cited if pid in expanded and pages[pid]["kind"] == "document"
                   and pid not in manual_requested and pid not in full_requested and not section_requested.get(pid)
                   and pid not in wiki_located}
        wiki_located.update(targets)
        return targets

    context_trimmed, context_partial = set(), set()

    def reading_units(page):
        """Complete units a reduced read keeps or leaves whole: a source's read sections (each with the parent
        lead-in read before it), or a knowledge page's heading sections (text before its first heading leads)."""
        units, keys = [], []
        if page["kind"] == "knowledge":
            for row in page.get("records", []):
                if not units or row["text"].lstrip().startswith("#"):
                    units.append([])
                units[-1].append(row)
            return units
        # Read sections nest (a chapter includes its articles): units are the innermost ones, and a parent's own
        # heading/lead-in rows belong to the unit that follows them.
        sections = [section for section in page.get("read_sections") or [] if section.get("block_ids")]
        spans = [set(section["block_ids"]) for section in sections]
        member = {bid: section["section_id"] for section, span in zip(sections, spans)
                  if not any(other < span for other in spans) for bid in span}
        for row in page.get("records", []):
            sid = member.get(row["block_id"])
            if units and (keys[-1] == sid or keys[-1] is None and sid is not None or sid is None and keys[-1] is None):
                keys[-1] = keys[-1] or sid  # a lead-in joins the section it introduces
                units[-1].append(row)
            else:
                units.append([row])
                keys.append(sid)
        return units

    def unit_scores(page, units):
        """Reranker scores of each unit against the question (cached with the group scores), or None when the
        reranker is not available or returns anything but one finite number per unit."""
        vector = dispatcher.vector_index
        if vector is None or not callable(getattr(vector, "rerank", None)):
            return None
        model = [getattr(getattr(vector, "settings", None), key, None) for key in (
            "reranker_model", "reranker_revision", "reranker_dtype", "reranker_instruction", "reranker_max_tokens")]
        texts = [page["title"] + "\n" + "\n".join(row["text"] for row in unit) for unit in units]
        keys = ["section_score_v1:" + sha256(json.dumps({"query": question, "text": text, "model": model},
                                                        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                for text in texts]
        missing = list(dict.fromkeys(key for key in keys if key not in group_rank_cache))
        text_by_key = dict(zip(keys, texts))
        try:
            with timings.measure("context_reranking"):
                values = vector.rerank(question, [text_by_key[key] for key in missing]) if missing else []
        except Exception:  # noqa: BLE001 - without scores the page is not reduced; authority is rechecked by the caller
            return None
        if values is None or len(values) != len(missing) or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf"))
                for v in values):
            return None
        group_rank_cache.update(zip(missing, values))
        return [group_rank_cache[key] for key in keys]

    def reduced_page(page, units, chosen):
        rows = [row for index in sorted(chosen) for row in units[index]]
        ids = {row["block_id"] for row in rows}
        return {**page, "records": rows, "read_scope": "sections", "read_sections": [
            section for section in page.get("read_sections", []) if section.get("block_ids") and section["block_ids"][0] in ids]}

    def trim_to_one_request(prefix, instruction, extras, ordered, report):
        """Reasoning: when the read material exceeds one synthesis request, fill it by priority instead of note-taking
        passes. Conclusions rest on source text and knowledge pages are navigation, so sources come first - those the
        model asked for or the reranker scores as relevant - then knowledge pages, then sources scored as unrelated;
        within each tier pages the model asked for first, then reranker order, each root with its dependency bundle.
        A bundle that does not fit is taken page by page, and a page too large on its own contributes its most relevant
        complete sections (reranked against the question; sections scored as unrelated stay out), so a relevant large
        source is reduced rather than lost to smaller pages. What is not sent is listed by title so the model can READ
        it (a later round puts it first). Returns (body, dropped, partial) or (None, [], [])."""
        rank = {pid: index for index, pid in enumerate(ordered)}
        receipt = (report or {}).get("group_rerank") or {}
        groups = [[pid for pid in group if pid in read_pages] for group in receipt.get("groups") or [[pid] for pid in ordered]]
        scores = receipt.get("scores") or []
        root_score = ({group[0]: score for group, score in zip(receipt["groups"], scores) if group}
                      if receipt.get("groups") and len(scores) == len(receipt["groups"]) else {})

        def tier(root):
            if pages[root]["kind"] == "knowledge":
                return 1
            return 0 if root in manual_requested or root not in root_score or root_score[root] > 0 else 2

        groups = sorted((group for group in groups if group), key=lambda group: (
            tier(group[0]), not set(group) & manual_requested, rank.get(group[0], len(rank))))

        def text(keep, part):
            return "\n\n".join(page_text(part.get(pid) or pages[pid], related_pages=read_pages)
                               for pid in pages if pid in keep or pid in part)

        def listing(dropped, part):
            notes = []
            if part:
                notes.append("以下资料只放入了与本题最相关的完整小节，其余小节在库内；答复确需时用 READ_SECTION 或 READ 加编号补读，"
                             "不是资料缺口：" + "；".join(f"{pid}《{display_title(pages[pid]['title'])}》" for pid in part))
            if dropped:
                notes.append("以下资料在库内、与本题相关，但超出本次单次综合的容量未放入正文。答复确需其中内容时，"
                             "用 READ 或 READ_SECTION 加编号补读；它们不是本库缺少的资料，不要列为GAP或资料缺口："
                             + "；".join(f"{pid}《{display_title(pages[pid]['title'])}》" for pid in dropped))
            return "".join("\n" + note for note in notes)

        def fits_with(keep, part):
            dropped = [pid for pid in ordered if pid in read_pages and pid not in keep and pid not in part]
            return fits(prefix + text(keep, part) + extras + listing(dropped, part) + instruction)

        def reduce(pid, keep, part):
            if pid not in manual_requested and rank.get(pid, len(rank)) >= REDUCE_TOP_RANKED:
                return None
            page = pages[pid]
            units = reading_units(page)
            lead = [0] if page["kind"] == "knowledge" and units and not units[0][0]["text"].lstrip().startswith("#") else []
            candidates = [index for index in range(len(units)) if index not in lead]
            if len(candidates) < 2:
                return None
            scores = unit_scores(page, units)
            if scores is None:
                return None
            chosen = list(lead)
            for index in sorted(candidates, key=lambda i: (-scores[i], i)):
                if scores[index] <= SECTION_MIN_SCORE:
                    break
                if fits_with(keep, {**part, pid: reduced_page(page, units, [*chosen, index])}):
                    chosen.append(index)
            return reduced_page(page, units, chosen) if len(chosen) > len(lead) else None

        kept, partial = set(), {}
        for group in groups:
            members = [pid for pid in group if pid not in kept and pid not in partial]
            if not members:
                continue
            if fits_with(kept | set(members), partial):
                kept |= set(members)
                continue
            for pid in members:
                if fits_with(kept | {pid}, partial):
                    kept.add(pid)
                    continue
                reduced = reduce(pid, kept, partial)
                if reduced is not None:
                    partial[pid] = reduced
        if not kept and not partial:
            return None, [], []
        dropped = [pid for pid in ordered if pid in read_pages and pid not in kept and pid not in partial]
        return text(kept, partial) + extras + listing(dropped, partial), dropped, list(partial)

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
        if reasoning:
            wiki_targets = wiki_citation_targets(expanded)
            for pid in wiki_targets:  # replaced by this question's located units below
                anchors[pid] = set()
                discovered_anchors[pid] = set()
            # An explicit READ of a source without a located clause would otherwise
            # yield only an outline. Small sources are read whole; large ones get a
            # retrieval restricted to that source for this question. Once per page.
            unlocated = [pid for pid in expanded if pid in manual_requested and pages[pid]["kind"] == "document"
                         and pid not in full_requested and not section_requested.get(pid) and not anchors.get(pid)
                         and pid not in located_attempted]
            located_attempted.update(unlocated)
            small = [pid for pid in unlocated if (pages[pid].get("block_count") or 0) <= FULL_READ_BLOCKS]
            whole = within_read_budget(small, expanded, anchors)
            full_requested.update(whole)
            budget_located.extend(pid for pid in small if pid not in whole)
            large = {pid: pages[pid] for pid in unlocated if pid not in full_requested}
            # One question-restricted search per round for every page that needs locating (sources cited by a READ
            # concept page, and large explicit READs), instead of one search per kind.
            search_pages = {pid: pages[pid] for pid in wiki_targets}
            if large and located_rounds < LOCATED_SEARCH_ROUNDS:
                located_rounds += 1
                search_pages.update(large)
            if search_pages:
                from .universal_retrieval import search_many_catalog
                with timings.measure("retrieval_and_navigation"), dispatcher.read_session_factory() as db:
                    located = search_many_catalog(db, authority(), space_id, [question], pages=search_pages, scope=scope,
                        context=context, vector=dispatcher.vector_index, limit=settings.hybrid_candidate_limit,
                        checkpoint=authority, session_factory=dispatcher.read_session_factory)
                taken = defaultdict(int)
                for unit in located.get("units", []):
                    pid = unit.get("page_id")
                    if pid in search_pages and taken[pid] < LOCATED_UNITS_PER_READ:
                        taken[pid] += 1
                        anchors[pid].update(unit.get("block_ids", []))
                        discovered_anchors.setdefault(pid, set()).update(unit.get("block_ids", []))
            expanded = [pid for pid in expanded if pid not in wiki_located or anchors.get(pid)]
        fresh_ids = [pid for pid in expanded if pid not in unavailable_pages and read_signature(pid, anchors) not in seen_reads and (
            pid not in read_pages or pid in full_requested and not pages[pid].get("full_text_loaded")
            or set(section_requested.get(pid, ())) - {s["section_id"] for s in pages[pid].get("read_sections", [])}
            or set(reference_requested.get(pid, ())) - set(pages[pid].get("incoming_context", {}))
            or set(anchors.get(pid, ())) - {r["block_id"] for r in pages[pid].get("records", [])})]
        reading_progress("loading_sections", round=reading_round, requested_pages=len(fresh_ids),
                         current_batch=0, total_batches=0, completed_batches=0)
        with timings.measure("source_reading"), dispatcher.read_session_factory() as db:
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
            from .evidence_context import context_instructions, context_plan, order_context_groups
            from .evidence_coverage import coverage_instructions, reading_coverage
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
                followups.extend(discover(missing_queries, rerank_units=DEPENDENCY_RERANK_UNITS if reasoning else None))
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
            with timings.measure("context_reranking"):
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
        from .business_reading import primary_first
        ordered_pages = primary_first(ordered_pages, source_plan)
        source_review = prepare_source_review(list(read_records.values()), source_plan=source_plan, context=context)
        review_context = model_review_context(source_review)
        # Previously read material remains in direct synthesis whenever it fits.
        # For explicit very-large reads only NEW source sections need compiling;
        # do not restart a whole-handbook reading pass after every follow-up READ.
        body = compact_evidence(pages, ordered_pages, used_edges=graph_plan.get("used_edges", [])) \
            if adaptive else "\n\n".join(page_text(pages[pid], related_pages=read_pages) for pid in pages if pid in read_pages)
        page_body = body
        if context_report is not None:
            body += context_instructions(context_report, limit=CONTEXT_GAP_LINES if reasoning else None,
                                         order=ordered_pages)
            body += coverage_instructions(context_report["reading_coverage"])
        body += review_context
        if outlines:
            body += "\n" + outline_text(pages, outlines)
        if not body:
            body = "本次没有匹配或可读的本地正文。可以给明确标注的一般分析，不得伪造本地依据。"
        if unavailable_pages:
            body += "\n下列页当前未通过完整正文/版本校验，未加载也不可作依据：" + " ".join(sorted(unavailable_pages))
        prefix = intro + "\n本次已核对的完整Wiki/原文小节：\n"
        instruction = "\n" + (REASONING_SYNTHESIS_INSTRUCTION if reasoning else UNIVERSAL_SYNTHESIS_INSTRUCTION
                              if universal else ADAPTIVE_SYNTHESIS_INSTRUCTION if adaptive else SYNTHESIS_INSTRUCTION)
        if reasoning:
            instruction += library_catalog_instruction(page["title"] for page in pages.values() if page["kind"] == "document")
        if context_report and context_report["gap_count"] and reasoning:
            instruction += ("\n原文中有部分显式交叉引用未能精确定位；结论依赖这些引用时，在该结论处用业务语言注明需核对，"
                            "不报告数量，也不得宣称这些依赖已核验。")
        elif context_report and context_report["gap_count"]:
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
        context_trimmed.clear()  # describes the latest synthesis request only
        context_partial.clear()
        # Reasoning: a later round (after the model asked to READ more) is trimmed again from everything read, the new
        # pages first. Its asking reply is only READ lines, so carrying it as notes would lose what the first round sent.
        if reasoning and body.startswith(page_body) and not fits(prefix + body + instruction):
            trimmed_body, dropped, partial = trim_to_one_request(prefix, instruction, body[len(page_body):], ordered_pages,
                                                                 context_report)
            authority()  # section scoring ran native inference outside any transaction
            if trimmed_body is not None:
                body = trimmed_body
                context_trimmed.update(dropped)
                context_partial.update(partial)
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
                    body += context_instructions(context_report, limit=CONTEXT_GAP_LINES if reasoning else None,
                                                 order=ordered_pages)
                    body += coverage_instructions(context_report["reading_coverage"])
                body += review_context
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
        if reasoning and not requested and not prose_read_used and len(final) < 400 and "[E" not in final:
            # Some models state a follow-up read in prose instead of a READ line.
            # Only a short, uncited reply naming registered ids is treated so, once.
            # CJK characters count as word characters, so no \b before "W" in "补读W7".
            requested = [pid for pid in dict.fromkeys(w.upper() for w in re.findall(r"(?<![A-Za-z0-9])W\d+(?!\d)", final))
                         if pid in pages]
            prose_read_used = bool(requested)
            manual_requested.update(requested)
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
    coverage_gaps = None
    if reasoning:
        # GAP lines are model-declared missing materials/facts: shown once as a
        # readable section and kept on the run for library completion, never read as commands.
        # Only materials the library lacks are gaps; case documents (contract, custody agreement...) are listed apart.
        titles = [page["title"] for page in pages.values()]
        final, answer_gaps, case_materials = move_gap_lines(final, titles)
        # Presentation only: per-request W handles become titles; claims/E-citations untouched.
        final, named_page_ids = name_page_ids(final, pages)
        planning_gaps, planning_case = sort_gaps(plan.get("gaps", []), titles)
        coverage_gaps = {"planning": planning_gaps, "answer": answer_gaps,
                         "case_materials": list(dict.fromkeys([*planning_case, *case_materials])),
                         "source": "model_declared_unverified"}
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
    quality_review = review_answer(answer["narrative_markdown"], list(read_records.values()), source_review)
    answer["quality_warnings"].extend(review_warnings(quality_review))
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
            "source_authority_stamp": final_authority_stamp, "pipeline_timing": timings.snapshot(),
            "evidence_review": quality_review}
        if citation_trace is not None:
            current.model_snapshot["citation_integrity"] = citation_trace
        if coverage_gaps is not None:
            current.model_snapshot["coverage_gaps"] = coverage_gaps
            current.model_snapshot["presentation_rewrites"] = {"page_ids_named": named_page_ids}
            if context_trimmed or context_partial:
                current.model_snapshot["wiki_reading"] = {**(current.model_snapshot.get("wiki_reading") or {}),
                    "page_titles": [pages[pid]["title"] for pid in sorted(read_pages - context_trimmed)],
                    "trimmed_page_titles": [pages[pid]["title"] for pid in sorted(context_trimmed)],
                    "partial_page_titles": [pages[pid]["title"] for pid in sorted(context_partial)]}
            current.model_snapshot["read_budget"] = {"share": READ_BUDGET_SHARE, "located_by_budget": len(budget_located),
                                                     "wiki_citations_located": len(wiki_located),
                                                     "context_trimmed_pages": len(context_trimmed),
                                                     "context_partial_pages": len(context_partial)}
            if coverage_gaps["planning"] or coverage_gaps["answer"]:
                dispatcher._audit(db, job, "answer.coverage_gaps_recorded", {
                    "planning": len(coverage_gaps["planning"]), "answer": len(coverage_gaps["answer"]),
                    "gaps_sha256": svc.digest(coverage_gaps)})
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
