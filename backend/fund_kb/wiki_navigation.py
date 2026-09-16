"""Read-only inline navigation over a caller's current, authorized wiki_catalog.

This is discovery, not evidence or model reading. Call the ordinary scoped reader
before sending any page to a model. No bodies, answers or permission decisions
are cached; neither catalog entries nor persistent graph/business state change.
"""
from __future__ import annotations

import copy
import json
import time
import weakref
from collections import OrderedDict, defaultdict
from threading import RLock
from typing import NamedTuple

from sqlalchemy import select

from . import models as m
from . import services as svc
from .ingestion import block_text, text_sha256
from .wiki import _norm, parse_wikilinks

_BATCH_SIZE = 400  # SQL bind batches, never a catalog/result limit.
_CACHE_MAX_SCOPES = 8
_CACHE_MAX_BYTES = 4 * 1024 * 1024
_CACHE_TTL = 60.0
_LOCK = RLock()
_CACHE = OrderedDict()
_CACHE_BYTES = 0
_PAGE_FIELDS = {
    "id", "resource_id", "version_id", "_metadata_signature", "kind", "title", "name",
    "aliases", "block_count", "source_hash", "content_sha256", "revision", "access_epoch",
    "state", "legal_status", "is_canonical",
}
# The content fields retained by services.check_frozen_hash, excluding blocks,
# source blob identity/hash and relations, which are supplied from batch reads.
_HASH_FIELDS = (
    "author_id", "title", "knowledge_type", "applicability", "required_facts", "legal_status",
    "valid_from", "valid_to", "origin", "base_version_id", "source_url",
)


class _Entry(NamedTuple):
    created: float
    signature: str
    payload: bytes


def clear_navigation_cache():
    """Drop all process-local navigation entries (also useful for isolated tests)."""
    global _CACHE_BYTES
    with _LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0


def _drop(key):
    global _CACHE_BYTES
    _CACHE_BYTES -= len(_CACHE.pop(key).payload)


def _cached(key, signature):
    with _LOCK:
        now = time.monotonic()
        for candidate, entry in list(_CACHE.items()):
            if candidate[0]() is None or now - entry.created >= _CACHE_TTL:
                _drop(candidate)
        entry = _CACHE.get(key)
        if entry and entry.signature != signature:
            _drop(key)
            entry = None
        if entry:
            _CACHE.move_to_end(key)
            return json.loads(entry.payload)
    return None


def _remember(key, signature, mentions, warnings):
    global _CACHE_BYTES
    payload = json.dumps({"mentions": mentions, "warnings": sorted(warnings)},
                         separators=(",", ":")).encode()
    with _LOCK:
        if key in _CACHE:
            _drop(key)
        if len(payload) > _CACHE_MAX_BYTES or _CACHE_MAX_SCOPES <= 0:
            return  # A large graph still returns in full, just without caching.
        while _CACHE and (len(_CACHE) >= _CACHE_MAX_SCOPES
                          or _CACHE_BYTES + len(payload) > _CACHE_MAX_BYTES):
            _drop(next(iter(_CACHE)))
        _CACHE[key] = _Entry(time.monotonic(), signature, payload)
        _CACHE_BYTES += len(payload)


def _names(pages):
    names = defaultdict(set)
    for pid, page in pages.items():
        for name in [page.get("title"), page.get("name"), page.get("canonical_key"),
                     *page.get("aliases", [])]:
            if isinstance(name, str) and _norm(name):
                names[text_sha256(_norm(name))].add(pid)
    return names


def _resolve(pages, mentions, invalid, warnings):
    """Re-resolve even a cache hit, using only the CURRENT catalog's names/IDs."""
    names, resources, roots = _names(pages), defaultdict(set), {}
    for pid, page in pages.items():
        resources[page["resource_id"]].add(pid)

    def root(pid):
        path = set()
        while pid not in roots:
            if pid not in pages or pid in invalid or pid in path:
                target = None
                break
            path.add(pid)
            page = pages[pid]
            target = page.get("canonical_page_id")
            if page.get("canonical_available") is False:
                target = None
                break
            if not target and page.get("canonical_resource_id"):
                candidates = resources.get(page["canonical_resource_id"], set())
                if len(candidates) != 1:
                    target = None
                    break
                target = next(iter(candidates))
            if not target or target == pid:
                target = pid
                break
            pid = target
        else:
            target = roots[pid]
        for item in path:
            roots[item] = target
        return target

    links = {pid: [] for pid in pages if pid not in invalid}
    ambiguous = {}
    for pid, tokens in mentions.items():
        if pid not in links or pages[pid]["kind"] != "knowledge":
            continue
        linked, groups = set(), set()
        for token in tokens:
            matched = names.get(token, set())
            targets = {root(candidate) for candidate in matched}
            if not targets or None in targets:
                warnings.add("INLINE_NAVIGATION_TARGET_UNAVAILABLE")
                # An invalid/absent competitor cannot turn a duplicate name
                # into a definite link to the remaining candidate.
                continue
            if len(targets) > 1:
                groups.add(tuple(sorted(targets)))
                warnings.add("INLINE_NAVIGATION_AMBIGUOUS")
            else:
                linked.update(targets - {pid})
        links[pid] = sorted(linked)
        if groups:
            # No raw link label: it might name a private or unavailable object.
            ambiguous[pid] = [{"candidate_page_ids": list(group)} for group in sorted(groups)]
    return links, ambiguous


def _source_hash(version, blocks, citations, relations):
    """Match check_frozen_hash without its per-page/per-block SQL queries."""
    by_block = defaultdict(list)
    for row in citations:
        by_block[row["from_block_id"]].append({"version_id": row["to_version_id"],
            "block_id": row["to_block_id"], "purpose": row["purpose"]})
    content = {field: version[field] for field in _HASH_FIELDS}
    content["blocks"] = [{"block_id": b["block_id"], "ordinal": b["ordinal"],
        "block_type": b["block_type"], "data": b["data"], "locator": b["locator"] or {},
        "citations": sorted(by_block[b["block_id"]],
                            key=lambda c: (c["version_id"], c["block_id"], c["purpose"]))} for b in blocks]
    content.update(source_blob_id=version["source_blob_id"], source_sha256=version["source_sha256"])
    content["relations"] = sorted([{
        "target_resource_id": e["target_resource_id"], "relation_type": e["relation_type"],
        "conditions": e["conditions"] or {},
        **({"evidence_version_id": e["evidence_version_id"], "evidence_block_id": e["evidence_block_id"]}
           if e["evidence_version_id"] else {}),
    } for e in relations], key=svc.digest)
    return svc.digest(content)


def _load(db, space_id, pages, names):
    by_version = defaultdict(list)
    for pid, page in pages.items():
        if page["kind"] == "knowledge":
            by_version[page["version_id"]].append(pid)
    mentions, invalid, warnings = {}, set(), set()
    loaded_blocks = loaded_characters = 0
    version_ids = sorted(by_version)
    for start in range(0, len(version_ids), _BATCH_SIZE):
        ids = version_ids[start:start + _BATCH_SIZE]
        v, r = m.ResourceVersion, m.Resource
        versions = {row["version_id"]: row for row in db.execute(select(
            v.id.label("version_id"), v.resource_id, v.content_sha256, v.source_blob_id,
            *(getattr(v, field) for field in _HASH_FIELDS), m.Blob.sha256.label("source_sha256"))
            .join(r, r.id == v.resource_id).outerjoin(m.Blob, m.Blob.id == v.source_blob_id)
            .where(v.id.in_(ids), r.space_id == space_id, r.kind == "knowledge",
                   r.deleted_at.is_(None), r.suspended.is_(False))).mappings()}
        admitted = {}
        for vid in ids:
            version = versions.get(vid)
            for pid in by_version[vid]:
                if (not version or version["resource_id"] != pages[pid]["resource_id"]
                        or version["title"] != pages[pid]["title"]):
                    invalid.add(pid)
                    warnings.add("INLINE_NAVIGATION_CATALOG_CHANGED")
                else:
                    admitted.setdefault(vid, []).append(pid)
        if not admitted:
            continue
        # Select scalar values: do not reuse potentially stale ORM body objects.
        blocks, citations, relations = defaultdict(list), defaultdict(list), defaultdict(list)
        b = m.ContentBlock
        for row in db.execute(select(b.version_id, b.block_id, b.ordinal, b.block_type,
                b.data, b.locator, b.search_text, b.content_sha256)
                .where(b.version_id.in_(admitted)).order_by(b.version_id, b.ordinal, b.block_id)).mappings():
            blocks[row["version_id"]].append(row)
        frozen = [vid for vid in admitted if versions[vid]["content_sha256"]
                  or any(pages[pid].get("source_hash") or pages[pid].get("content_sha256")
                         for pid in admitted[vid])]
        if frozen:
            e = m.EvidenceLink
            for row in db.execute(select(e.from_version_id, e.from_block_id, e.to_version_id,
                    e.to_block_id, e.purpose).where(e.from_version_id.in_(frozen))).mappings():
                citations[row["from_version_id"]].append(row)
            e = m.RelationEdge
            for row in db.execute(select(e.source_version_id, e.target_resource_id, e.relation_type,
                    e.conditions, e.evidence_version_id, e.evidence_block_id)
                    .where(e.source_version_id.in_(frozen))).mappings():
                relations[row["source_version_id"]].append(row)
        for vid, pids in admitted.items():
            rows, version, tokens, bad = blocks[vid], versions[vid], set(), False
            for row in rows:
                loaded_blocks += 1
                text = row["search_text"]
                try:
                    canonical = block_text(row)
                except (AttributeError, TypeError, ValueError):
                    loaded_characters += len(text)
                    bad = True
                    warnings.add("INLINE_NAVIGATION_BLOCK_INVALID")
                    continue
                # Count all fetched blocks, even rejected ones. Normally the
                # two text forms agree; count the longer if they do not.
                loaded_characters += max(len(text), len(canonical))
                if text != canonical or text_sha256(canonical) != row["content_sha256"]:
                    bad = True
                    warnings.add("INLINE_NAVIGATION_BLOCK_HASH_MISMATCH")
                else:
                    tokens.update(text_sha256(_norm(link["title"])) for link in parse_wikilinks(canonical))
            if not rows:
                bad = True
                warnings.add("INLINE_NAVIGATION_BODY_UNAVAILABLE")
            if version["source_blob_id"] and not version["source_sha256"]:
                bad = True
                warnings.add("INLINE_NAVIGATION_SOURCE_UNAVAILABLE")
            current_hash = None
            if vid in frozen:
                current_hash = _source_hash(version, rows, citations[vid], relations[vid])
                if version["content_sha256"] and current_hash != version["content_sha256"]:
                    bad = True
                    warnings.add("INLINE_NAVIGATION_SOURCE_HASH_MISMATCH")
            else:
                warnings.add("INLINE_NAVIGATION_SOURCE_HASH_UNFROZEN")
            for pid in pids:
                expected = pages[pid].get("source_hash") or pages[pid].get("content_sha256")
                page_bad = bad
                if pages[pid].get("block_count") != len(rows):
                    page_bad = True
                    warnings.add("INLINE_NAVIGATION_BLOCK_COUNT_MISMATCH")
                if expected and expected != current_hash:
                    page_bad = True
                    warnings.add("INLINE_NAVIGATION_SOURCE_HASH_MISMATCH")
                if page_bad:
                    invalid.add(pid)
                else:
                    # Cache only hashes of CURRENT authorized names. Unknown
                    # labels are represented solely by a fixed warning code.
                    mentions[pid] = sorted(tokens & names.keys())
                    if tokens - names.keys():
                        warnings.add("INLINE_NAVIGATION_TARGET_UNAVAILABLE")
    return mentions, invalid, warnings, loaded_blocks, loaded_characters


def load_inline_navigation(db, user_id, space_id, pages) -> dict:
    """Return directed inline links within a CURRENT authorized catalog mapping.

    ``links`` has valid catalog page IDs as keys, with unique target page IDs;
    invalid pages are absent on both ends. ``ambiguous`` groups alternatives as
    ``{pid: [{"candidate_page_ids": [...]}]}``, never choosing a duplicate name.
    Targets may be knowledge or documents; only knowledge bodies are loaded.
    ``knowledge_pages`` counts supplied knowledge pages, including rejected ones.
    Body counters describe this function's actual cold I/O, including rejected
    blocks (characters as block_text, or the longer inconsistent search_text),
    not model input. Cache hits load zero body blocks/characters.

    The caller must rebuild wiki_catalog with current ACLs for each request.
    A hit reuses navigation structure, never authorization or source evidence.
    Unfrozen drafts receive per-block checks and an explicit hash warning; frozen
    versions also receive the complete services.check_frozen_hash equivalent.
    """
    snapshot = {pid: copy.deepcopy({key: value for key, value in page.items()
        if key in _PAGE_FIELDS or key.startswith("canonical_")}) for pid, page in pages.items()}
    signature = svc.digest(["inline-navigation-v1", user_id, space_id, snapshot])
    knowledge_pages = sum(page["kind"] == "knowledge" for page in snapshot.values())
    # Include database identity without retaining an engine/session/credential.
    bind = db.get_bind()
    key = (weakref.ref(getattr(bind, "engine", bind)), svc.digest([user_id, space_id]))
    cacheable = bool(knowledge_pages) and not (db.new or db.dirty or db.deleted)
    cached = _cached(key, signature) if cacheable else None
    invalid, loaded_blocks, loaded_characters = set(), 0, 0
    if cached is not None:
        mentions, warnings = cached["mentions"], set(cached["warnings"])
    else:
        # A read must never flush the caller's pending business mutations.
        with db.no_autoflush:
            mentions, invalid, warnings, loaded_blocks, loaded_characters = _load(
                db, space_id, snapshot, _names(snapshot))
        if cacheable and not invalid:
            _remember(key, signature, mentions, warnings)
    links, ambiguous = _resolve(snapshot, mentions, invalid, warnings)
    return {"links": links, "ambiguous": ambiguous, "invalid_pages": sorted(invalid),
        "warnings": sorted(warnings), "cache_hit": cached is not None,
        "body_blocks_loaded": loaded_blocks, "body_characters_loaded": loaded_characters,
        "knowledge_pages": knowledge_pages, "cache_signature": signature}
