"""Pure, query-adaptive navigation over a CURRENT authorized wiki_catalog.

``plan_graph_reads`` returns exactly requested, anchors, reasons, used_edges,
deferred_pages, warnings and stats. IDs outside ``pages`` are never admitted.
The caller reads whole Wiki pages and complete source sections around ALL the
returned anchors, rechecking ACL/version/hash in the normal reader. This module
does not read, trim, edit, cache or certify source text, and invokes no model.

``used_edges`` retains the actual direction/type/status of catalog edges. INLINE
and CANONICAL edges describe supplied navigation metadata, never inferred facts.
``stats.recommendation`` (direct/expanded) describes the reading route only;
coverage and business accuracy remain unverified even when no further expansion
is suggested. Every unselected authorized page remains in deferred_pages for READ.

warnings is a flat, backward-compatible code list. stats splits it into advisory,
coverage and blocking_warnings. Normal READ/business disclaimers are advisory;
coverage gaps request reading/search, not cache invalidation. Blocking warnings
identify incomplete required inputs/authority metadata. Cache validity still
belongs to the caller's current ACL/version/hash checks, never this pure plan.
Anchor warnings are attached to their actual edges: unused-edge problems use
CATALOG_UNUSED_* advisory codes; the original blocking codes apply only when
the offending edge is used by this reading plan.
role_counts/role_gaps describe navigation roles, NOT legal authority. Exact Wiki
CITES locators take priority over redundant/unrelated source retrieval locators;
mandatory anchors and genuinely additional question aspects remain intact.
The caller can supply space-separated atomic query words, preserving explicit
subquestion separators. Those boundaries are retained without loading a tokenizer.
Unsegmented strings have a conservative short-feature fallback. No corpus,
dictionary, database, service or model is read/called here.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict, deque
from collections.abc import Mapping

_DEFAULT_SEEDS = 8
_CONTEXT_TYPES = {"REQUIRES", "DEPENDS_ON", "APPLIES_TO", "EXCEPTION_OF"}
_CONTEXT_ROLES = {"condition", "prerequisite", "dependency", "exception", "scope", "applicability"}
_CONTEXT = re.compile(
    r"前提|条件|例外|除外|依赖|适用范围|适用对象|另按|另行适用|须先|需先|必须先|"
    r"\b(?:prerequisites?|requires?|depends? on|exceptions?|subject to|unless|provided that)\b", re.IGNORECASE)
_WIKILINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_BREAKS = re.compile(r"[，,。；;！？!?\n]")
_STOP = re.compile(
    r"有哪些|为什么|怎么样|如何|怎么|什么|是否|请问|分别|以及|同时|应该|需要|进行|有关|相关|"
    r"[的了是在和与及或等请吗呢]|"
    r"\b(?:the|a|an|and|or|of|to|in|for|on|is|are|how|what|which|please)\b", re.IGNORECASE)
_WORDS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[._/-][a-z0-9]+)*")
_FACETS = re.compile(r"[、，,；;。！？!?\n]|以及|同时|分别|[和与及]|\band\b", re.IGNORECASE)
_METHOD = re.compile(r"方法|标准|指引|准则|\b(?:method\w*|standard\w*|guidance)\b", re.IGNORECASE)
_PROCEDURE = re.compile(r"细则|流程|规程|程序|\b(?:procedure\w*|process\w*)\b", re.IGNORECASE)
_METHOD_QUESTION = re.compile(r"估值|定值|计量|测算|计算|方法|\b(?:valu\w*|measur\w*|calculat\w*|method\w*)\b",
                              re.IGNORECASE)
# Actions can explain WHAT to read, but cannot identify WHICH subject a rule
# concerns. This is a language filter, not a resource/asset/answer allowlist.
_ACTIONS = re.compile(
    r"过程中|过程|业务|估值|定值|处理|核算|会计|计量|测算|计算|操作|流程|方法|标准|指引|准则|"
    r"规则|规定|实务|事项|问题|情况|场景|具体|详细|说明|介绍|确定|确认|关于|对于|针对|涉及|应当|应如何|"
    r"\b(?:business|valu\w*|accounting|treatment|process\w*|handl\w*|measur\w*|calculat\w*|"
    r"method\w*|standard\w*|guidance|rule\w*|operation\w*|explain|describe)\b", re.IGNORECASE)
_DISCLAIMER = re.compile(r"免责声明|未核验|待核验|仅供参考|仅供导航|不构成(?:投资|法律|正式)|"
                         r"\b(?:disclaimer|unverified|not verified|for reference only)\b", re.IGNORECASE)


def _normal(text):
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _strings(values):
    if isinstance(values, str):
        return [values] if values else []
    return [v for v in (values or ()) if isinstance(v, str) and v]


def _text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _text_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _text_values(item)


def _discovery_text(text):
    """Ignore boilerplate for ranking only; never change the provided text."""
    return "\n".join(part for part in re.split(r"[。！？!?\n]", text) if not _DISCLAIMER.search(part))


def _terms(text):
    result = set()
    for word in _WORDS.findall(_STOP.sub(" ", _normal(text))):
        if "\u3400" <= word[0] <= "\u9fff":
            for size in (2, 3, 4):
                result.update(word[n:n + size] for n in range(len(word) - size + 1))
        else:
            result.add(word)
    return result


def _subject_terms(text):
    """Honor caller-supplied word boundaries, with no dictionary/model IO."""
    normal = _normal(text)
    atomic_input = bool(re.search(r"[\u3400-\u9fff]\s+[\u3400-\u9fff]", normal))
    cleaned = _STOP.sub(" ", _ACTIONS.sub(" ", normal))
    result = set()
    for word in _WORDS.findall(cleaned):
        if not atomic_input and "\u3400" <= word[0] <= "\u9fff" and len(word) > 3:
            result.update(word[n:n + 2] for n in range(len(word) - 1))
        elif len(word) > 1 and not word.isdecimal():
            result.add(word)
    return result


def _identity_matches(row, page):
    return all(key not in row or row[key] == page.get(key) for key in ("version_id", "resource_id"))


def _finite_score(value):
    try:
        number = float(value)
        return max(0.0, number) if math.isfinite(number) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _reading_role(page):
    if page.get("kind") == "knowledge":
        return "wiki"
    # These labels help select complementary reading, never rank legal effect.
    labels = " ".join(_text_values({key: page.get(key) for key in ("title", "role", "knowledge_type")}))
    if _METHOD.search(labels):
        return "method_reference"
    if _PROCEDURE.search(labels):
        return "procedure_reference"
    return "source"


class _Relevance:
    """Local IDF-weighted question features, without a domain/title allowlist."""

    def __init__(self, question, texts, classifications=None):
        self.question = _normal(question)
        self.subject_query = _subject_terms(question)
        self.query = _terms(question) | self.subject_query
        self.matches = {}
        for pid, text in texts.items():
            clean = _normal(_discovery_text(text))
            features = _terms(clean) & self.query
            features.update(term for term in self.subject_query if term in clean)
            self.matches[pid] = features
        counts = Counter(term for terms in self.matches.values() for term in terms)
        self.weights = {term: math.log1p((len(texts) + .5) / (counts[term] + .5)) for term in self.query}
        self.total = self.mass(self.query) or 1.0
        self.subject_total = self.mass(self.subject_query) or 1.0
        self.subject_matches = {pid: terms & self.subject_query for pid, terms in self.matches.items()}
        self.scores = {pid: .82 * self.mass(self.subject_matches[pid]) / self.subject_total
                       + .15 * self.mass(terms) / self.total
                       + .03 * self.mass(_terms((classifications or {}).get(pid, "")) & self.query) / self.total
                       for pid, terms in self.matches.items()}
        self.facets = []
        for part in _FACETS.split(question):
            features = _terms(part) & self.query
            if features and features not in self.facets:
                self.facets.append(features)
        self.strong = {pid: self._strong(terms) for pid, terms in self.matches.items()}

    def mass(self, terms):
        return math.fsum(self.weights[term] for term in sorted(terms))

    def _strong(self, terms):
        terms = terms & self.subject_query
        if not terms:
            return False
        if terms == self.subject_query:
            return True
        # Overlapping n-grams of one generic two-character word do not turn
        # that word into multiple independent topical signals.
        positions, latin = set(), 0
        longest = 0
        for term in terms:
            if "\u3400" <= term[0] <= "\u9fff":
                longest = max(longest, len(term))
                match = re.search(re.escape(term), self.question)
                if match:
                    positions.update(range(match.start(), match.end()))
            else:
                latin += 1
        coverage = self.mass(terms) / self.subject_total
        # Action-only facets ("valuation and accounting") cannot make a
        # commodity rule topical to a different asset via its shared manual.
        explicit_facet = any(bool(facet & self.subject_query) and facet & self.subject_query <= terms
                             for facet in self.facets)
        return (explicit_facet or (len(positions) >= 4 or latin >= 2) and coverage >= .2
                or longest >= 3 and coverage >= .18
                or latin == 1 and coverage >= .3)

    def covered_facets(self, terms):
        return {n for n, facet in enumerate(self.facets)
                if self.mass(terms & facet) >= .5 * self.mass(facet)}


def plan_graph_reads(question, pages, hits, *, inline_links=None, required_sources=()) -> dict:
    """Plan full reading units; inputs remain unchanged and no IO is performed.

    pages is the wiki_catalog page-id mapping; hits is its hybrid hit list (or
    the hybrid result envelope). required_sources accepts the curated sources
    list, optionally its {sources: [...]} envelope. Missing/stale identities
    produce fixed warning codes without disclosing inaccessible identifiers.
    Anchor IDs are supplied locators, NOT newly validated evidence identities.
    """
    warnings = {"READ_REVALIDATION_REQUIRED", "BUSINESS_ACCURACY_NOT_EVALUATED"}
    catalog = {pid: page for pid, page in pages.items()
               if isinstance(pid, str) and isinstance(page, Mapping) and page.get("id", pid) == pid}
    if len(catalog) != len(pages):
        warnings.add("INVALID_CATALOG_ENTRY")
    versions = defaultdict(list)
    for pid, page in catalog.items():
        if page.get("version_id"):
            versions[page["version_id"]].append(pid)
    by_version = {vid: ids[0] for vid, ids in versions.items() if len(ids) == 1}
    evidence_text = defaultdict(list)
    snippet_units = defaultdict(list)
    discovery_anchors = defaultdict(set)
    hit_data = {}
    hit_rows = hits.get("hits", ()) if isinstance(hits, Mapping) else hits
    for row in hit_rows or ():
        if not isinstance(row, Mapping) or row.get("page_id") not in catalog:
            warnings.add("HIT_NOT_ADMITTED")
            continue
        pid = row["page_id"]
        if not _identity_matches(row, catalog[pid]):
            warnings.add("HIT_IDENTITY_MISMATCH")
            continue
        hit = hit_data.setdefault(pid, {"score": 0.0, "channels": set()})
        hit["score"] = max(hit["score"], _finite_score(row.get("score", 0)))
        hit["channels"].update(_strings(row.get("channels")))
        discovery_anchors[pid].update(_strings(row.get("matched_block_ids")))
        for snippet in row.get("candidate_snippets", ()):
            if isinstance(snippet, str):
                evidence_text[pid].append(snippet)
                snippet_units[pid].append((snippet, set()))
                continue
            if not isinstance(snippet, Mapping) or not _identity_matches(snippet, catalog[pid]):
                warnings.add("SNIPPET_IDENTITY_MISMATCH")
                continue
            evidence_text[pid].extend(_text_values(snippet.get("text", "")))
            evidence_text[pid].extend(_text_values(snippet.get("section_path", ())))
            blocks = set(_strings(snippet.get("block_ids")))
            for span in snippet.get("source_spans", ()):
                if isinstance(span, Mapping) and isinstance(span.get("block_id"), str):
                    blocks.add(span["block_id"])
            discovery_anchors[pid].update(blocks)
            snippet_units[pid].append(("\n".join(_text_values(snippet.get("text", ""))), blocks))

    mandatory = {}
    mandatory_topics = defaultdict(set)
    required_rows = required_sources.get("sources", ()) if isinstance(required_sources, Mapping) else required_sources
    for row in required_rows or ():
        if not isinstance(row, Mapping) or row.get("page_id") not in catalog:
            warnings.add("REQUIRED_SOURCE_NOT_ADMITTED")
            continue
        pid = row["page_id"]
        if not _identity_matches(row, catalog[pid]):
            warnings.add("REQUIRED_SOURCE_IDENTITY_MISMATCH")
            continue
        mandatory.setdefault(pid, set()).update(_strings(row.get("anchor_block_ids")))
        mandatory_topics[pid].update(_strings(row.get("topic_block_ids"))
                                    if "topic_block_ids" in row else mandatory[pid])
        discovery_anchors[pid].update(mandatory_topics[pid])

    texts = {pid: "\n".join([
        *(_text_values({key: page.get(key) for key in ("title", "aliases")})),
        *evidence_text[pid]]) for pid, page in catalog.items()}
    # Shared taxonomy prefixes cannot authorize reverse graph expansion.
    classifications = {pid: " ".join(_text_values(page.get("category", ""))) for pid, page in catalog.items()}
    relevance = _Relevance(question, texts, classifications)
    reading_roles = {pid: _reading_role(page) for pid, page in catalog.items()}
    method_question = bool(_METHOD_QUESTION.search(question))
    method_terms = _terms(" ".join(match.group() for match in _METHOD_QUESTION.finditer(question)))
    method_candidates = {pid for pid in hit_data if method_question and reading_roles[pid] == "method_reference"
                         and relevance.matches[pid] & method_terms}
    # Index each supplied logical edge once. Catalog repeats it on both ends.
    edges, outgoing, incoming = {}, defaultdict(list), defaultdict(list)
    for page in catalog.values():
        for raw in page.get("relations", ()):
            if not isinstance(raw, Mapping):
                warnings.add("INVALID_RELATION")
                continue
            source, target, kind = raw.get("source"), raw.get("target"), raw.get("type")
            if source not in catalog or target not in catalog:
                warnings.add("RELATION_ENDPOINT_NOT_ADMITTED")
                continue
            if not isinstance(kind, str) or not kind:
                warnings.add("INVALID_RELATION")
                continue
            origin = raw.get("origin", "registered")
            status = raw.get("verification_status", "REGISTERED_NOT_BUSINESS_VERIFIED")
            proposed = str(origin).lower() == "proposed" or str(status).upper() == "PROPOSED"
            key = (source, target, kind, str(origin), str(status))
            if key not in edges:
                edges[key] = {"source": source, "target": target, "type": kind,
                    "origin": str(origin), "verification_status": str(status),
                    "navigation_only": True, "_proposed": proposed, "_anchors": defaultdict(set),
                    "_anchor_warnings": set()}
                outgoing[source].append(key)
                incoming[target].append(key)
            for anchor in raw.get("anchors", ()):
                if not isinstance(anchor, Mapping):
                    edges[key]["_anchor_warnings"].add("INVALID_ANCHOR")
                    continue
                apid = by_version.get(anchor.get("version_id"))
                if not apid or ("page_id" in anchor and anchor["page_id"] != apid):
                    edges[key]["_anchor_warnings"].add("ANCHOR_NOT_ADMITTED")
                    continue
                block = anchor.get("block_id")
                if isinstance(block, str) and block:
                    edges[key]["_anchors"][apid].add(block)

    citation_fanout = defaultdict(set)
    for edge in edges.values():
        if edge["type"] == "CITES" and not edge["_proposed"] and catalog[edge["source"]].get("kind") == "knowledge":
            for block in edge["_anchors"].get(edge["target"], ()):
                citation_fanout[(edge["target"], block)].add(edge["source"])

    requested, anchors, reasons, seeds = [], defaultdict(set), defaultdict(list), set()
    selected, pending, used, visited = set(), deque(), {}, set()

    def choose(pid, reason, *, seed=False):
        if reason not in reasons[pid]:
            reasons[pid].append(reason)
        if seed:
            seeds.add(pid)
        if pid not in selected:
            selected.add(pid)
            requested.append(pid)
            pending.append(pid)
            anchors[pid].update(discovery_anchors[pid])

    for pid, blocks in mandatory.items():
        choose(pid, "REQUIRED_SOURCE", seed=True)
        anchors[pid].update(blocks)

    # Eight is a discovery default, not a limit on mandatory reading or closure.
    seed_limit = max(_DEFAULT_SEEDS, 2 * len(relevance.facets))
    ranked = list(hit_data)
    if not ranked:
        ranked = [pid for pid in catalog if relevance.strong[pid]]
        if ranked:
            warnings.add("CATALOG_DISCOVERY_FALLBACK")
    eligible = [pid for pid in ranked if relevance.strong[pid] or pid in method_candidates]
    fallback = bool(ranked) and not eligible
    if fallback:
        eligible = ranked
        warnings.add("LOW_RELEVANCE_SEEDS_REQUIRE_REVIEW")
    top_score = max((item["score"] for item in hit_data.values()), default=0) or 1.0
    top_relevance = max(relevance.scores.values(), default=0) or 1.0

    def priority(pid, covered, roles):
        hit = hit_data.get(pid, {})
        return (.55 * relevance.scores[pid] / top_relevance
                + .25 * hit.get("score", 0) / top_score
                + .03 * min(2, len(hit.get("channels", ())))
                + .35 * relevance.mass(relevance.subject_matches[pid] - covered) / relevance.subject_total
                + (.3 if reading_roles[pid] not in roles else 0))

    covered = set().union(*(relevance.matches[pid] for pid in seeds)) if seeds else set()
    roles = {reading_roles[pid] for pid in seeds}
    remaining = set(eligible) - selected
    auto_seeds = 0
    while remaining:
        # The default is a ceiling before coverage escalation, never a quota.
        # Stop as soon as the remaining hits add neither a core subject/event
        # facet nor a needed reading role. Mandatory closure happens afterward.
        useful = set()
        for item in remaining:
            role = reading_roles[item]
            role_needed = (role == "wiki" and "wiki" not in roles
                           or role != "wiki" and not (roles - {"wiki"})
                           or role == "procedure_reference" and role not in roles
                           or item in method_candidates and "method_reference" not in roles)
            subject_gain = relevance.mass(relevance.subject_matches[item] - covered) / relevance.subject_total
            if not seeds or role_needed or relevance.strong[item] and subject_gain >= .06:
                useful.add(item)
        if not useful:
            break
        pid = min(useful, key=lambda item: (-priority(item, covered, roles), item))
        if auto_seeds >= seed_limit:
            choose(pid, "COVERAGE_ESCALATION", seed=True)
        new_features = relevance.matches[pid] - covered
        new_facets = relevance.covered_facets(relevance.matches[pid]) - relevance.covered_facets(covered)
        new_role = reading_roles[pid] not in roles
        reason = "HYBRID_SEED" if pid in hit_data else "CATALOG_RELEVANCE_SEED"
        choose(pid, reason, seed=True)
        if pid in method_candidates:
            choose(pid, "METHOD_REFERENCE_FOR_COMPARISON", seed=True)
        if new_role:
            choose(pid, "ROLE_DIVERSITY", seed=True)
        if new_features or new_facets:
            choose(pid, "QUESTION_COVERAGE", seed=True)
        covered.update(relevance.matches[pid])
        roles.add(reading_roles[pid])
        remaining.remove(pid)
        auto_seeds += 1

    inline = {}
    for source, targets in (inline_links or {}).items():
        if source not in catalog:
            warnings.add("INLINE_ENDPOINT_NOT_ADMITTED")
            continue
        inline[source] = []
        for target in _strings(targets):
            if target not in catalog:
                warnings.add("INLINE_ENDPOINT_NOT_ADMITTED")
            elif target != source and target not in inline[source]:
                inline[source].append(target)

    def contextual_links(pid):
        """Context must belong to this actual link, not another link nearby."""
        names = defaultdict(set)
        contextual = set()
        for target in inline.get(pid, ()):
            page = catalog[target]
            for name in [target, *(_text_values(page.get("title", ""))), *(_text_values(page.get("aliases", ())))]:
                names[_normal(name)].add(target)
            if any(page.get(key) in _CONTEXT_ROLES for key in ("node_role", "knowledge_type", "role")):
                contextual.add(target)
        bodies = [*evidence_text[pid]]
        bodies.extend(text for row in catalog[pid].get("records", ()) if isinstance(row, Mapping)
                      for text in _text_values(row.get("text", "")))
        for text in bodies:
            for clause in _BREAKS.split(text):
                links = list(_WIKILINK.finditer(clause))
                for n, match in enumerate(links):
                    start = links[n - 1].end() if n else 0
                    end = links[n + 1].start() if n + 1 < len(links) else len(clause)
                    context = clause[start:match.start()] + clause[match.end():end]
                    if _CONTEXT.search(context):
                        name = _normal(match.group(1).split("|", 1)[0].split("#", 1)[0])
                        contextual.update(names.get(name, ()))
        return contextual

    graph_anchors = defaultdict(set)

    def use_edge(key, target, reason):
        edge = edges[key]
        warnings.update(edge["_anchor_warnings"])
        choose(target, "PROPOSED_NAVIGATION" if edge["_proposed"] else reason)
        used[key] = {k: value for k, value in edge.items() if not k.startswith("_")}
        if edge["_proposed"]:
            warnings.add("PROPOSED_RELATIONS_NAVIGATION_ONLY")
        for apid, blocks in edge["_anchors"].items():
            choose(apid, "PROPOSED_ANCHOR_NAVIGATION" if edge["_proposed"] else "RELATION_SOURCE_ANCHOR")
            anchors[apid].update(blocks)
            graph_anchors[apid].update(blocks)

    def citation_scope(pid, *, prospective=False):
        blocks, features = set(), set()
        for key in incoming[pid]:
            edge = edges[key]
            source = edge["source"]
            if (edge["type"] == "CITES" and not edge["_proposed"]
                    and catalog[source].get("kind") == "knowledge"
                    and (source in selected or prospective and relevance.strong[source])
                    and edge["_anchors"].get(pid)):
                blocks.update(edge["_anchors"][pid])
                features.update(relevance.matches[source])
        return blocks, features

    def additional_topic_anchors(pid, cited_features):
        additional = set()
        for text, blocks in snippet_units[pid]:
            features = _terms(_discovery_text(text)) & relevance.query
            if relevance._strong(features) and relevance.mass(features - cited_features) / relevance.total >= .06:
                additional.update(blocks)
        return additional

    inline_signatures = set()
    while pending:
        pid = pending.popleft()
        if pid in visited:
            continue
        visited.add(pid)
        canonical = catalog[pid].get("canonical_page_id")
        if canonical:
            if canonical not in catalog:
                warnings.add("CANONICAL_ENDPOINT_NOT_ADMITTED")
            else:
                choose(canonical, "CANONICAL_NAVIGATION")
                used[(pid, canonical, "CANONICAL")] = {"source": pid, "target": canonical,
                    "type": "CANONICAL", "origin": "canonical_page_id", "verification_status": "NAVIGATION_ONLY",
                    "navigation_only": True}
        for key in outgoing[pid]:
            edge = edges[key]
            target, kind = edge["target"], edge["type"]
            if kind not in _CONTEXT_TYPES | {"CITES"}:
                continue
            if edge["_proposed"] and not relevance.strong[target]:
                continue
            use_edge(key, target, "CITES_SOURCE" if kind == "CITES" else kind + "_CONTEXT")
        reverse_anchors = discovery_anchors[pid]
        if catalog[pid].get("kind") == "document":
            cited_blocks, cited_features = citation_scope(pid, prospective=True)
            if cited_blocks:
                reverse_anchors = ((reverse_anchors & cited_blocks) | mandatory_topics[pid]
                                   | additional_topic_anchors(pid, cited_features))
        for key in incoming[pid]:
            edge = edges[key]
            source, kind = edge["source"], edge["type"]
            if kind == "EXCEPTION_OF":
                if not edge["_proposed"] or relevance.strong[source]:
                    use_edge(key, source, "INCOMING_EXCEPTION")
            elif kind == "CITES" and catalog[pid].get("kind") == "document" \
                    and catalog[source].get("kind") == "knowledge":
                # CRITICAL: only original retrieval/mandatory anchors may unlock
                # reverse CITES. Anchors collected during closure must not cause
                # a popular manual to fan out into all sibling Wiki pages.
                overlap = reverse_anchors & edge["_anchors"].get(pid, set())
                selective_overlap = {block for block in overlap if len(citation_fanout[(pid, block)]) <= 2}
                if edge["_proposed"]:
                    if relevance.strong[source]:
                        use_edge(key, source, "PROPOSED_NAVIGATION")
                elif selective_overlap or relevance.strong[source]:
                    use_edge(key, source, "REVERSE_CITES_EXACT" if selective_overlap else "REVERSE_CITES_RELEVANT")
                elif overlap:
                    if "DEFERRED_SHARED_SOURCE_ANCHOR" not in reasons[source]:
                        reasons[source].append("DEFERRED_SHARED_SOURCE_ANCHOR")
                    warnings.add("SHARED_SOURCE_ANCHORS_REQUIRE_TOPIC_MATCH")
        contexts = contextual_links(pid) if inline.get(pid) else set()
        for target in sorted(inline.get(pid, ()), key=lambda item: (-relevance.scores[item], item)):
            if relevance.strong[target] or target in contexts:
                signature = (catalog[target].get("kind"), frozenset(relevance.matches[target]))
                if target not in selected and target not in contexts and signature in inline_signatures:
                    warnings.add("INLINE_RELATED_CANDIDATES_DEFERRED")
                    reasons[target].append("DEFERRED_REDUNDANT_INLINE_NAVIGATION")
                    continue
                choose(target, "INLINE_CONTEXT_NAVIGATION" if target in contexts else "INLINE_RELEVANT_NAVIGATION")
                if target not in contexts:
                    inline_signatures.add(signature)
                used[(pid, target, "INLINE")] = {"source": pid, "target": target, "type": "INLINE",
                    "origin": "inline", "verification_status": "NAVIGATION_ONLY", "navigation_only": True}
                warnings.add("INLINE_LINKS_NAVIGATION_ONLY")

    deferred = [pid for pid in catalog if pid not in selected]
    for pid in deferred:
        reasons[pid].append("DEFERRED_LOW_RELEVANCE" if pid in hit_data and not relevance.strong[pid]
                            else "DEFERRED_FOR_LATER_READ")
    sources = {pid for pid in selected if catalog[pid].get("kind") == "document"}
    wikis = {pid for pid in selected if catalog[pid].get("kind") == "knowledge"}
    deferred_anchor_count = 0
    for pid in sources:
        cited_blocks, cited_features = citation_scope(pid)
        if cited_blocks:
            preferred = graph_anchors[pid] | mandatory.get(pid, set()) | additional_topic_anchors(pid, cited_features)
            omitted = anchors[pid] - preferred
            anchors[pid] = preferred
            choose(pid, "EXACT_CITES_ANCHORS_PREFERRED")
            if omitted:
                deferred_anchor_count += len(omitted)
                choose(pid, "RETRIEVAL_ANCHORS_DEFERRED")
    source_backed = {edge["source"] for key, edge in edges.items() if key in used
                     and edge["type"] == "CITES" and not edge["_proposed"] and edge["target"] in sources}
    if len(sources) == 1:
        warnings.add("SINGLE_DOCUMENT_COVERAGE_RISK")
    if sources and not wikis:
        warnings.add("DOCUMENT_ONLY_COVERAGE_RISK")
    if wikis - source_backed:
        warnings.add("WIKI_SOURCE_CHAIN_MISSING")
    if any(not anchors[pid] for pid in sources):
        warnings.add("SOURCE_ANCHORS_MISSING")
    if not requested:
        warnings.add("NO_READ_CANDIDATES")
    final_features = set().union(*(relevance.matches[pid] for pid in selected)) if selected else set()
    covered_facets = relevance.covered_facets(final_features)
    if not relevance.query or len(covered_facets) < len(relevance.facets):
        warnings.add("QUESTION_COVERAGE_REQUIRES_READ_OR_SEARCH")
    role_counts = dict(sorted(Counter(reading_roles[pid] for pid in selected).items()))
    role_counts["exact_cited_source"] = sum(bool(citation_scope(pid)[0]) for pid in sources)
    role_counts["required_source"] = len(mandatory)
    role_counts["context"] = sum(any(reason in {"INCOMING_EXCEPTION", "REQUIRES_CONTEXT", "DEPENDS_ON_CONTEXT",
                                             "APPLIES_TO_CONTEXT", "EXCEPTION_OF_CONTEXT"}
                                    for reason in reasons[pid]) for pid in selected)
    role_gaps = []
    if not wikis:
        role_gaps.append("related_wiki")
    if not sources:
        role_gaps.append("original_source")
    if wikis and not role_counts["exact_cited_source"]:
        role_gaps.append("exact_cited_source")
    if method_question and not role_counts.get("method_reference"):
        role_gaps.append("method_reference_for_comparison")
    if role_gaps:
        warnings.add("READING_ROLE_COVERAGE_GAPS")
    for key, edge in edges.items():
        if key not in used:
            warnings.update("CATALOG_UNUSED_" + code for code in edge["_anchor_warnings"])
    risk_codes = {"SINGLE_DOCUMENT_COVERAGE_RISK", "DOCUMENT_ONLY_COVERAGE_RISK", "WIKI_SOURCE_CHAIN_MISSING",
                  "SOURCE_ANCHORS_MISSING", "NO_READ_CANDIDATES", "LOW_RELEVANCE_SEEDS_REQUIRE_REVIEW",
                  "REQUIRED_SOURCE_NOT_ADMITTED", "REQUIRED_SOURCE_IDENTITY_MISMATCH",
                  "QUESTION_COVERAGE_REQUIRES_READ_OR_SEARCH", "ANCHOR_NOT_ADMITTED",
                  "INLINE_RELATED_CANDIDATES_DEFERRED", "READING_ROLE_COVERAGE_GAPS"}
    escalation = bool(warnings & risk_codes)
    blocking_codes = {"REQUIRED_SOURCE_NOT_ADMITTED", "REQUIRED_SOURCE_IDENTITY_MISMATCH", "INVALID_CATALOG_ENTRY",
                      "INVALID_RELATION", "RELATION_ENDPOINT_NOT_ADMITTED", "INVALID_ANCHOR", "ANCHOR_NOT_ADMITTED",
                      "CANONICAL_ENDPOINT_NOT_ADMITTED"}
    blocking = warnings & blocking_codes
    coverage = (warnings & risk_codes) - blocking
    advisory = warnings - blocking - coverage
    return {"requested": requested, "anchors": {pid: sorted(anchors[pid]) for pid in requested},
        "reasons": dict(reasons), "used_edges": list(used.values()), "deferred_pages": deferred,
        "warnings": sorted(warnings), "stats": {
            "catalog_count": len(catalog), "hit_count": len(hit_data), "seed_count": len(seeds),
            "seed_limit": seed_limit, "required_count": len(mandatory), "graph_count": len(selected - seeds),
            "requested_count": len(requested), "source_count": len(sources), "wiki_count": len(wikis),
            "seed_source_count": len(seeds & sources), "seed_wiki_count": len(seeds & wikis),
            "graph_edge_count": len(used), "deferred_count": len(deferred), "visited_count": len(visited),
            "anchor_count": sum(len(anchors[pid]) for pid in requested),
            "source_hit_anchors_deferred_count": deferred_anchor_count,
            "role_counts": role_counts, "role_gaps": role_gaps, "role_status": "NAVIGATION_ONLY",
            "advisory_warnings": sorted(advisory), "coverage_warnings": sorted(coverage),
            "blocking_warnings": sorted(blocking),
            "question_facet_count": len(relevance.facets), "covered_facet_count": len(covered_facets),
            "recommendation": "expanded" if selected - seeds or escalation else "direct",
            "escalation_required": escalation, "coverage_status": "NOT_VERIFIED",
            "business_accuracy": "NOT_EVALUATED", "wiki_read_mode": "whole_page",
            "source_read_mode": "complete_sections_from_all_anchors"}}
