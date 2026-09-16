"""Pure reading navigation for CURRENT, caller-checked, reranked section units.

No text is rewritten, no source is read, and no semantic/authority verdict is
made here. The reader must revalidate ACL, versions and hashes before reading
whole Wiki pages and complete source sections around the returned anchors.

Reranker scores take precedence over retrieval scores (including negative
logits); the scales are never added. Without reranking, a conservative body-only
lexical check rejects obviously unrelated retrieval. It is NOT semantic QA.
Section diversity and exact provenance reduce repeated reading, never impose a
per-document cap. ``max_seed_units`` bounds only the initial units; real context
relations can expand past it, and all remaining pages/units stay discoverable.

``graph_plan`` is a navigation hint, not an authority or a second seed selector.
Its edges must exist in pages, and its locators cannot replace reranked locators.
Reverse CITES uses only frozen initial source anchors, never graph-added anchors.
PROPOSED links can navigate to independently admitted candidates, but cannot
establish a source chain or contribute independent evidence votes.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from collections.abc import Mapping
from copy import deepcopy

_FORWARD = {"CITES", "REQUIRES", "DEPENDS_ON", "APPLIES_TO", "EXCEPTION_OF",
            "CONFLICTS_WITH", "SUPERSEDES", "PRECEDES", "PART_OF"}
_REVERSE = {"EXCEPTION_OF", "CONFLICTS_WITH", "SUPERSEDES", "PRECEDES"}
_WORDS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[._/-][a-z0-9]+)*", re.IGNORECASE)
_STOP = re.compile(r"如何|怎么|什么|哪些|是否|请问|应当|需要|进行|处理|流程|规则|方法|"
                   r"\b(?:how|what|which|the|and|with|for|from|does|should|please)\b", re.IGNORECASE)
_EDGE_ID = ("source", "target", "type", "origin", "verification_status")
_ADVISORY = {"READ_REVALIDATION_REQUIRED", "BUSINESS_ACCURACY_NOT_EVALUATED",
             "PROPOSED_RELATIONS_NAVIGATION_ONLY", "GRAPH_REQUESTS_DEFERRED",
             "PROPOSED_RELATIONS_DEFERRED", "LEXICAL_FALLBACK_NOT_SEMANTIC_VALIDATION"}
_COVERAGE = {"EMPTY_QUESTION", "NO_READ_CANDIDATES", "SOURCE_ANCHORS_MISSING",
            "WIKI_SOURCE_CHAIN_MISSING", "SEED_UNITS_DEFERRED", "CROSS_DOMAIN_UNITS_DEFERRED",
            "QUERY_CANDIDATES_DEFERRED"}


def _id(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value):
    return isinstance(value, (list, tuple)) and all(_id(item) for item in value)


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _features(text):
    """Ranking fallback only: no page title, category, role or business glossary."""
    words = _WORDS.findall(_STOP.sub(" ", text.casefold()))
    return {part for word in words for part in
            ([word] if not "\u3400" <= word[0] <= "\u9fff" else
             [word[i:i + 2] for i in range(len(word) - 1)]) if len(part) > 1}


def _freeze(value):
    if isinstance(value, Mapping):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _json_value(value):
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_json_value(item) for item in value)
    return value is None or isinstance(value, (str, bool)) or _number(value)


def _catalog(pages, warnings):
    if not isinstance(pages, Mapping):
        warnings.add("INVALID_CATALOG_ENTRY")
        return {}
    result = {}
    for pid, page in pages.items():
        if (not _id(pid) or not isinstance(page, Mapping) or page.get("id", pid) != pid
                or not all(_id(page.get(key)) for key in ("version_id", "resource_id"))):
            warnings.add("INVALID_CATALOG_ENTRY")
        else:
            result[pid] = page
    return result


def _identity(row, page):
    return (all(row.get(key) == page[key] for key in ("version_id", "resource_id"))
            and all(key not in row or row[key] == page.get(key) for key in ("space_id", "kind")))


def _checked_locators(row, page):
    """Check supplied verification/locator metadata, never claim to redo DB QA.

    The required unit contract already promises caller-checked text and blocks.
    When optional verification or precise spans are supplied, contradictions
    fail closed. full_text_verified=False is normal for a section excerpt and
    is deliberately NOT interpreted as an invalid unit.
    """
    if ("verification" in row and row["verification"] != "CURRENT_DB_BLOCK_SLICES"
            or any(key in row and row[key] is not True for key in ("verified", "identity_verified"))):
        return False
    if "source_spans" not in row:
        return True
    spans = row["source_spans"]
    if not isinstance(spans, (list, tuple)) or not spans:
        return False
    blocks, cursor, previous = [], 0, None
    for span in spans:
        if (not isinstance(span, Mapping) or not _json_value(span) or not _id(span.get("block_id"))
                or not isinstance(span.get("content_sha256"), str)
                or not re.fullmatch(r"[a-fA-F0-9]{64}", span["content_sha256"])
                or any(key in span and span[key] != row.get(key, page.get(key))
                       for key in ("page_id", "version_id", "resource_id", "space_id"))):
            return False
        start, end, left, right, ordinal = [span.get(key) for key in
                                          ("start", "end", "text_start", "text_end", "ordinal")]
        if (any(type(value) is not int for value in (start, end, left, right, ordinal))
                or ordinal < 0 or not 0 <= start < end or not cursor <= left < right <= len(row["text"])
                or end - start != right - left or row["text"][cursor:left].strip()
                or previous is not None and (ordinal < previous[0] or ordinal == previous[0] and start < previous[1])):
            return False
        blocks.append(span["block_id"])
        cursor, previous = right, (ordinal, end)
    return (not row["text"][cursor:].strip()
            and list(dict.fromkeys(blocks)) == list(dict.fromkeys(row["block_ids"])))


def _units(rows, catalog, warnings):
    """Reject conflicting identities; merge identical passages without score votes."""
    if not isinstance(rows, (list, tuple)):
        warnings.add("INVALID_UNITS")
        return [], 0, 0
    candidates, identities, conflicting = [], {}, set()
    rejected = 0
    for index, row in enumerate(rows):
        if (not isinstance(row, Mapping) or not _id(row.get("page_id"))
                or row["page_id"] not in catalog):
            warnings.add("UNIT_NOT_ADMITTED")
            rejected += 1
            continue
        page = catalog[row["page_id"]]
        if not _identity(row, page):
            warnings.add("UNIT_IDENTITY_MISMATCH")
            rejected += 1
            continue
        if (not _id(row.get("unit_id")) or not _id(row.get("text"))
                or not _strings(row.get("block_ids")) or not _strings(row.get("section_path"))
                or not _strings(row.get("channels")) or not _number(row.get("score"))
                or (row.get("rerank_score") is not None and not _number(row["rerank_score"]))
                or ("matched_queries" in row and not _strings(row["matched_queries"]))
                or (row.get("query_rank") is not None
                    and (type(row["query_rank"]) is not int or row["query_rank"] < 1))
                or (page.get("kind") != "knowledge" and not row["block_ids"])):
            warnings.add("INVALID_UNIT")
            rejected += 1
            continue
        if not _checked_locators(row, page):
            warnings.add("UNIT_VERIFICATION_MISMATCH")
            rejected += 1
            continue
        item = {key: row[key] for key in ("unit_id", "page_id", "version_id", "resource_id", "text", "score")}
        item.update(block_ids=tuple(dict.fromkeys(row["block_ids"])), section_path=tuple(row["section_path"]),
                    rerank_score=row.get("rerank_score"), index=index,
                    matched_queries=tuple(dict.fromkeys(row.get("matched_queries", ()))))
        query_rank = row.get("query_rank")
        item["query_priorities"] = {query: (query_rank is None, query_rank or 0, _rank(item))
                                    for query in item["matched_queries"]}
        item["query_appearances"] = {query: index for query in item["matched_queries"]}
        identity = (item["page_id"], item["unit_id"])
        passage = (item["page_id"], item["version_id"], item["resource_id"],
                   frozenset(item["block_ids"]), item["section_path"], item["text"],
                   _freeze(row.get("source_spans")))
        if identity in identities and identities[identity] != passage:
            conflicting.add(identity)
            warnings.add("UNIT_ID_CONFLICT")
        identities[identity] = passage
        candidates.append((identity, passage, item))
    unique, duplicates = {}, 0
    for identity, passage, item in candidates:
        if identity in conflicting:
            rejected += 1
        elif passage not in unique:
            unique[passage] = item
        else:
            duplicates += 1
            old = unique[passage]
            merged = dict(min((old, item), key=_rank))
            merged["matched_queries"] = tuple(dict.fromkeys(old["matched_queries"] + item["matched_queries"]))
            merged["query_priorities"] = {
                query: min(row["query_priorities"][query] for row in (old, item) if query in row["query_priorities"])
                for query in merged["matched_queries"]}
            merged["query_appearances"] = {
                query: min(row["query_appearances"][query] for row in (old, item) if query in row["query_appearances"])
                for query in merged["matched_queries"]}
            unique[passage] = merged
    return sorted(unique.values(), key=_rank), rejected, duplicates


def _rank(unit):
    rerank = unit["rerank_score"]
    return (rerank is None, -(unit["score"] if rerank is None else rerank), unit["index"])


def _relations(catalog, warnings):
    versions = defaultdict(list)
    for pid, page in catalog.items():
        versions[page["version_id"]].append(pid)
    edges, seen = [], set()
    for owner, page in catalog.items():
        rows = page.get("relations", ())
        if not isinstance(rows, (list, tuple)):
            warnings.add("INVALID_RELATION")
            continue
        for raw in rows:
            if (not isinstance(raw, Mapping) or not _json_value(raw)
                    or not all(_id(raw.get(key)) for key in ("source", "target", "type"))
                    or any(key in raw and not _id(raw[key]) for key in ("origin", "verification_status"))):
                warnings.add("INVALID_RELATION")
                continue
            source, target = raw["source"], raw["target"]
            if source not in catalog or target not in catalog or owner not in (source, target):
                warnings.add("RELATION_ENDPOINT_NOT_ADMITTED")
                continue
            if any(f"{end}_{key}" in raw and raw[f"{end}_{key}"] != catalog[pid].get(key)
                   for end, pid in (("source", source), ("target", target))
                   for key in ("version_id", "resource_id", "space_id")):
                warnings.add("RELATION_IDENTITY_MISMATCH")
                continue
            locators, valid_anchors, invalid = defaultdict(set), [], False
            anchors = raw.get("anchors", ())
            source_pages = raw.get("source_pages", ())
            if not _strings(source_pages) or any(pid not in catalog for pid in source_pages):
                warnings.add("RELATION_ENDPOINT_NOT_ADMITTED")
                continue
            if not isinstance(anchors, (list, tuple)):
                warnings.add("INVALID_ANCHOR")
                continue
            for anchor in anchors:
                if not isinstance(anchor, Mapping) or not _id(anchor.get("version_id")):
                    invalid = True
                    break
                matches = versions.get(anchor["version_id"], ())
                pid = anchor.get("page_id") if "page_id" in anchor else (matches[0] if len(matches) == 1 else None)
                if (not _id(pid) or pid not in matches
                        or any(key in anchor and anchor[key] != catalog[pid].get(key)
                               for key in ("resource_id", "space_id"))
                        or ("block_id" in anchor and not _id(anchor["block_id"]))):
                    invalid = True
                    break
                valid_anchors.append(deepcopy(dict(anchor)))
                if "block_id" in anchor:
                    locators[pid].add(anchor["block_id"])
            if invalid:
                warnings.add("ANCHOR_NOT_ADMITTED")
                continue
            # Keep actual direction/type/status/conditions. Missing status stays
            # missing; no REGISTERED/VERIFIED status or inferred edge is invented.
            public = {key: deepcopy(raw[key]) for key in (*_EDGE_ID, "conditions", "explanation") if key in raw}
            if "anchors" in raw:
                public["anchors"] = valid_anchors
            if "source_pages" in raw:
                public["source_pages"] = list(source_pages)
            public["navigation_only"] = True
            fingerprint = _freeze(public)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            proposed = (str(raw.get("origin", "")).casefold() == "proposed"
                        or str(raw.get("verification_status", "")).upper() == "PROPOSED")
            edges.append({"public": public, "locators": locators, "proposed": proposed})
    return edges


def _graph_hints(plan, catalog, edges, warnings):
    """Legacy hints cannot supply new relationships, identities or seed units."""
    if plan is None:
        return set(), set(), {}
    if not isinstance(plan, Mapping):
        warnings.add("INVALID_GRAPH_PLAN")
        return set(), set(), {}
    requests = plan.get("requested", ())
    if not _strings(requests):
        warnings.add("INVALID_GRAPH_PLAN")
        requests = ()
    if any(pid not in catalog for pid in requests):
        warnings.add("GRAPH_PAGE_NOT_ADMITTED")
    requested = set(requests) & catalog.keys()
    matched = set()
    rows = plan.get("used_edges", ())
    if not isinstance(rows, (list, tuple)):
        warnings.add("INVALID_GRAPH_PLAN")
        rows = ()
    for row in rows:
        matches = {index for index, edge in enumerate(edges) if isinstance(row, Mapping)
                   and all((key in row) == (key in edge["public"]) and row.get(key) == edge["public"].get(key)
                           for key in _EDGE_ID)
                   and all(key not in row or row[key] == edge["public"].get(key)
                           for key in ("conditions", "explanation"))}
        if not matches:
            warnings.add("GRAPH_EDGE_NOT_REGISTERED")
        matched.update(matches)
    locators = plan.get("anchors", {})
    if not isinstance(locators, Mapping):
        warnings.add("INVALID_GRAPH_PLAN")
        locators = {}
    clean = {}
    for pid, blocks in locators.items():
        if pid not in catalog:
            warnings.add("GRAPH_PAGE_NOT_ADMITTED")
        elif not _strings(blocks):
            warnings.add("GRAPH_ANCHOR_NOT_ADMITTED")
        else:
            clean[pid] = set(blocks)
    return requested, matched, clean


def plan_evidence_reads(question, pages, units, *, graph_plan=None, max_seed_units=24,
                        conservative_graph=False, explicit_pages=()) -> dict:
    """Return the seven query_graph-compatible fields; perform no IO or mutation.

    pages is the current authorized page-id mapping. Units carry unit_id,
    page_id, version_id, resource_id, text, block_ids, section_path, numeric score,
    optional numeric rerank_score and channels. Optional matched_queries lists
    each originating SEARCH; query_rank is its positive, one-based rerank rank.
    Each query gets its best unit before general filling; exact duplicate units
    merge query memberships/ranks without summing scores across queries.
    Optional space/kind identities must agree too. Scores rank navigation only; reranking is trusted as upstream
    relevance evidence, never re-vetoed using title roles or query word coverage.
    Invalid units/edges are rejected with fixed codes (no external IDs or text).
    selected_* and unselected_unit_count describe first-batch units; requested_*
    includes graph expansion. selected_query_count counts queries whose BEST
    candidate is planned, not merely queries mentioned by some selected unit.
    """
    if type(max_seed_units) is not int or max_seed_units < 0:
        raise ValueError("max_seed_units must be a non-negative integer")
    warnings = {"READ_REVALIDATION_REQUIRED", "BUSINESS_ACCURACY_NOT_EVALUATED"}
    catalog = _catalog(pages, warnings)
    admitted, rejected_count, duplicate_count = _units(units, catalog, warnings)
    edges = _relations(catalog, warnings)
    old_requested, hinted_edges, old_anchors = _graph_hints(graph_plan, catalog, edges, warnings)
    query = _features(question) if isinstance(question, str) and question.strip() else set()
    empty_question = not isinstance(question, str) or not question.strip()
    if empty_question:
        warnings.add("EMPTY_QUESTION")
    eligible = [unit for unit in admitted if not empty_question
                and (unit["rerank_score"] is not None or query & _features(unit["text"])
                     or any(_features(search) & _features(unit["text"]) for search in unit["matched_queries"]))]
    if not empty_question and len(eligible) < len(admitted):
        warnings.add("CROSS_DOMAIN_UNITS_DEFERRED")
    if any(unit["rerank_score"] is None for unit in eligible):
        warnings.add("LEXICAL_FALLBACK_NOT_SEMANTIC_VALIDATION")
    eligible_pages = {unit["page_id"] for unit in eligible}
    by_page, outgoing, incoming = defaultdict(list), defaultdict(list), defaultdict(list)
    for unit in eligible:
        by_page[unit["page_id"]].append(unit)
    for index, edge in enumerate(edges):
        outgoing[edge["public"]["source"]].append(index)
        incoming[edge["public"]["target"]].append(index)

    def wiki_footprint(pid):
        evidence, contexts = set(), set()
        for index in outgoing[pid] + incoming[pid]:
            edge = edges[index]
            raw = edge["public"]
            if edge["proposed"]:
                continue
            if raw["source"] == pid and raw["type"] == "CITES":
                target = raw["target"]
                if catalog[target].get("kind") != "knowledge":
                    evidence.update((catalog[target]["version_id"], bid)
                                    for bid in edge["locators"].get(target, ()) or (None,))
            elif raw["type"] in _FORWARD | _REVERSE:
                contexts.add((raw["type"], raw["target"] if raw["source"] == pid else raw["source"],
                              raw["source"] == pid, _freeze(raw.get("conditions"))))
        return evidence, contexts

    footprints = {pid: wiki_footprint(pid) for pid in catalog if catalog[pid].get("kind") == "knowledge"}
    requested, selected, reasons, anchors = [], set(), defaultdict(list), defaultdict(set)
    pending, used = deque(), set()
    seed_units, seed_pages, sections = [], set(), set()
    wiki_evidence, wiki_contexts, chosen_wikis = set(), set(), set()

    def choose(pid, reason):
        if reason not in reasons[pid]:
            reasons[pid].append(reason)
        if pid not in selected:
            selected.add(pid)
            requested.append(pid)
            pending.append(pid)

    def new_wiki(pid):
        evidence, contexts = footprints[pid]
        return pid not in chosen_wikis and (not evidence or bool(evidence - wiki_evidence)
                                            or bool(contexts - wiki_contexts))

    def note_wiki(pid):
        evidence, contexts = footprints[pid]
        chosen_wikis.add(pid)
        wiki_evidence.update(evidence)
        wiki_contexts.update(contexts)

    def add_seed(unit, *, query_best=False):
        pid = unit["page_id"]
        if unit in seed_units:
            return True
        if catalog[pid].get("kind") == "knowledge":
            if pid in chosen_wikis:
                # Whole-page reading already includes another unit on this Wiki.
                return True
            if not query_best and not new_wiki(pid):
                return False
        elif not set(unit["block_ids"]) - anchors[pid]:
            return True
        if len(seed_units) >= max_seed_units:
            return False
        seed_units.append(unit)
        seed_pages.add(pid)
        sections.add((pid, unit["section_path"] or unit["block_ids"]))
        if catalog[pid].get("kind") == "knowledge":
            note_wiki(pid)
        choose(pid, "RERANKED_UNIT_SEED" if unit["rerank_score"] is not None else "RETRIEVAL_UNIT_SEED")
        if query_best:
            choose(pid, "MATCHED_QUERY_BEST_SECTION")
        anchors[pid].update(unit["block_ids"])
        return True

    query_appearances = {}
    for unit in admitted:
        for search, index in unit["query_appearances"].items():
            query_appearances[search] = min(query_appearances.get(search, index), index)
    query_order = sorted(query_appearances, key=query_appearances.get)
    covered_queries = set()
    for search in query_order:
        candidates = sorted((unit for unit in eligible if search in unit["matched_queries"]),
                            key=lambda unit: unit["query_priorities"][search])
        # Best per-query sections may share asset terms, one manual, or the same
        # cited original. Those similarities cannot veto a distinct query's best.
        if candidates and add_seed(candidates[0], query_best=True):
            covered_queries.add(search)

    # Diversify sections within each score family, then fill with additional
    # anchored units. Never let an unreranked role/term hit displace reranked units.
    for reranked in (True, False):
        family = [unit for unit in eligible if (unit["rerank_score"] is not None) == reranked]
        for distinct_section in (True, False):
            for unit in family:
                if len(seed_units) >= max_seed_units:
                    break
                pid = unit["page_id"]
                section = (pid, unit["section_path"] or unit["block_ids"])
                if unit in seed_units or (distinct_section and section in sections):
                    continue
                add_seed(unit)

    for pid in explicit_pages:
        if pid in catalog:
            choose(pid, "MODEL_REQUESTED_READ")
            seed_pages.add(pid)
        else:
            warnings.add("EXPLICIT_PAGE_NOT_ADMITTED")
    initial_anchors = {pid: frozenset(anchors[pid]) for pid in seed_pages}

    def use(index, neighbor, reason):
        edge = edges[index]
        choose(neighbor, "PROPOSED_NAVIGATION" if edge["proposed"] else reason)
        used.add(index)
        if index in hinted_edges:
            choose(neighbor, "GRAPH_PLAN_REAL_RELATION")
        if edge["proposed"]:
            warnings.add("PROPOSED_RELATIONS_NAVIGATION_ONLY")
        if catalog[neighbor].get("kind") == "knowledge":
            note_wiki(neighbor)
        for pid, blocks in edge["locators"].items():
            choose(pid, "PROPOSED_ANCHOR_NAVIGATION" if edge["proposed"] else "RELATION_SOURCE_ANCHOR")
            anchors[pid].update(blocks)
        if catalog[neighbor].get("kind") != "knowledge" and not anchors[neighbor] and by_page[neighbor]:
            # An unlocated context edge can still use a CURRENT checked unit.
            # This is graph expansion, not another legacy role-selected seed.
            candidates = by_page[neighbor]
            best = [candidates[0]]
            for search in dict.fromkeys(search for candidate in candidates for search in candidate["matched_queries"]):
                best.append(min((candidate for candidate in candidates if search in candidate["matched_queries"]),
                                key=lambda candidate: candidate["query_priorities"][search]))
            for candidate in best:
                anchors[neighbor].update(candidate["block_ids"])
            choose(neighbor, "CONTEXT_VERIFIED_UNIT_ANCHORS")

    while pending:
        pid = pending.popleft()
        for index in outgoing[pid]:
            raw = edges[index]["public"]
            neighbor = raw["target"]
            if raw["type"] not in _FORWARD and not (index in hinted_edges and neighbor in old_requested):
                continue
            if edges[index]["proposed"] and neighbor not in (seed_pages if conservative_graph else eligible_pages):
                warnings.add("PROPOSED_RELATIONS_DEFERRED")
                continue
            use(index, neighbor, "CITES_SOURCE" if raw["type"] == "CITES" else raw["type"] + "_CONTEXT")
        # Highest-ranked Wiki wins duplicate navigation; catalog order is the
        # deterministic fallback when no Wiki unit was retrieved.
        reverse = sorted(incoming[pid], key=lambda index:
                         _rank(by_page[edges[index]["public"]["source"]][0])
                         if by_page[edges[index]["public"]["source"]] else (True, math.inf, index))
        for index in reverse:
            edge, raw = edges[index], edges[index]["public"]
            neighbor, kind = raw["source"], raw["type"]
            if kind == "CITES":
                if conservative_graph and neighbor not in seed_pages:
                    # Generic backlinks remain searchable, but do not cause
                    # automatic expansion of a whole source-document hub.
                    continue
                if (edge["proposed"] or catalog[neighbor].get("kind") != "knowledge"
                        or not (initial_anchors.get(pid, frozenset()) & edge["locators"].get(pid, set()))
                        or (neighbor not in selected and not new_wiki(neighbor))):
                    continue
                use(index, neighbor, "REVERSE_CITES_EXACT")
            elif kind in _REVERSE or (index in hinted_edges and neighbor in old_requested):
                if edge["proposed"] and neighbor not in (seed_pages if conservative_graph else eligible_pages):
                    warnings.add("PROPOSED_RELATIONS_DEFERRED")
                    continue
                use(index, neighbor, "INCOMING_" + kind)

    # Validate legacy locators against this actual reading route. In particular,
    # a real but low-ranked unit cannot launder its anchor through an old plan.
    for pid, blocks in old_anchors.items():
        if blocks - anchors[pid]:
            warnings.add("GRAPH_ANCHOR_NOT_ADMITTED")
    if old_requested - selected:
        warnings.add("GRAPH_REQUESTS_DEFERRED")
    sources = {pid for pid in selected if catalog[pid].get("kind") != "knowledge"}
    wikis = selected - sources
    backed = {edges[index]["public"]["source"] for index in used
              if edges[index]["public"]["type"] == "CITES" and not edges[index]["proposed"]
              and edges[index]["public"]["target"] in sources
              and edges[index]["locators"].get(edges[index]["public"]["target"])}
    deferred_units = [unit for unit in admitted if unit not in seed_units]
    if set(query_order) - covered_queries:
        warnings.add("QUERY_CANDIDATES_DEFERRED")
    if any(unit not in seed_units for unit in eligible):
        warnings.add("SEED_UNITS_DEFERRED")
    if wikis - backed:
        warnings.add("WIKI_SOURCE_CHAIN_MISSING")
    if any(not anchors[pid] for pid in sources):
        warnings.add("SOURCE_ANCHORS_MISSING")
    if not requested:
        warnings.add("NO_READ_CANDIDATES")
    deferred = [pid for pid in catalog if pid not in selected]
    coverage, advisory = warnings & _COVERAGE, warnings & _ADVISORY
    blocking = warnings - coverage - advisory
    source_versions = {(catalog[pid]["resource_id"], catalog[pid]["version_id"]) for pid in sources}
    source_resources = {catalog[pid]["resource_id"] for pid in sources}
    page_sections = {pid: sum(section[0] == pid for section in sections) for pid in seed_pages}
    return {"requested": requested, "anchors": {pid: sorted(anchors[pid]) for pid in requested},
            "reasons": dict(reasons), "used_edges": [edges[index]["public"] for index in sorted(used)],
            "deferred_pages": deferred, "warnings": sorted(warnings), "stats": {
                "catalog_count": len(catalog), "hit_count": len({unit["page_id"] for unit in admitted}),
                "unit_count": len(units) if isinstance(units, (list, tuple)) else 0,
                "admitted_unit_count": len(admitted), "rejected_unit_count": rejected_count,
                "duplicate_unit_count": duplicate_count, "seed_unit_count": len(seed_units),
                "eligible_unit_count": len(eligible), "selected_unit_count": len(seed_units),
                "unselected_unit_count": len(deferred_units),
                "selected_page_count": len(seed_pages), "selected_section_count": len(sections),
                "page_section_counts": {pid: page_sections[pid] for pid in requested if pid in page_sections},
                "diversity_status": "INPUT_SECTIONS_NOT_BUSINESS_COVERAGE",
                "matched_query_count": len(query_order), "selected_query_count": len(covered_queries),
                "unselected_query_count": len(set(query_order) - covered_queries),
                "seed_unit_ids": [unit["unit_id"] for unit in seed_units],
                "deferred_unit_ids": [unit["unit_id"] for unit in deferred_units],
                "seed_count": len(seed_pages), "seed_limit": max_seed_units, "required_count": 0,
                "graph_count": len(selected - seed_pages), "requested_count": len(selected),
                "source_count": len(sources), "wiki_count": len(wikis),
                "seed_source_count": len(seed_pages & sources), "seed_wiki_count": len(seed_pages & wikis),
                "graph_edge_count": len(used), "deferred_count": len(deferred), "visited_count": len(selected),
                "anchor_count": sum(len(anchors[pid]) for pid in selected),
                "source_resource_count": len(source_resources), "source_version_count": len(source_versions),
                "source_count_status": "PROVENANCE_NOT_FACT_VOTES",
                "advisory_warnings": sorted(advisory), "coverage_warnings": sorted(coverage),
                "blocking_warnings": sorted(blocking), "coverage_status": "NOT_VERIFIED",
                "business_accuracy": "NOT_EVALUATED", "escalation_required": bool(coverage or blocking),
                "recommendation": "expanded" if selected - seed_pages or coverage or blocking else "direct",
                "wiki_read_mode": "whole_page", "source_read_mode": "complete_sections_from_all_anchors",
                "full_text_reachable": True}}
