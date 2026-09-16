"""Authorized metadata catalog and lazy, whole-page reading.

Catalog entries are discovery metadata, NEVER body/evidence admission. Their
names, source lineage and relation endpoints pass current ACL checks without
loading paragraph/table text. Selected pages subsequently pass the existing
full scan/hash/provenance checks. No vector database or corpus-size cutoff.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from datetime import datetime

from sqlalchemy import func, select

from . import models as m
from . import services as svc
from .reference_evidence import _latest_versions, reference_evidence


class CatalogAuthority:
    """One-read-transaction metadata checks; never reuse across requests."""

    def __init__(self, db, user):
        self.db, self.user = db, user
        self.resources, self.versions, self.checked = {}, {}, {}
        self.provenance, self.edges, self.citations, self.attachments = {}, {}, {}, {}
        self.base_resources, self.base_versions = {}, {}
        self.busy = None

    def resource(self, rid):
        if rid not in self.resources:
            self.resources[rid] = self.db.get(m.Resource, rid)
        return self.resources[rid]

    def version(self, vid):
        if vid not in self.versions:
            self.versions[vid] = self.db.get(m.ResourceVersion, vid)
        return self.versions[vid]

    def snapshot(self, snap, path=(), *, blob=False):
        vid = snap.get("version_id")
        v = self.version(vid) if vid else None
        r = self.resource(v.resource_id) if v else None
        if (not r or r.id != snap.get("resource_id") or r.access_epoch != snap.get("access_epoch")
                or (v.content_sha256 is not None and v.content_sha256 != snap.get("content_sha256"))):
            svc.fail(404, "WIKI_METADATA_CHANGED", "资料目录或来源已变更")
        self.check(vid, path)
        if blob:
            b = self.db.get(m.Blob, v.source_blob_id) if v.source_blob_id else None
            if not b or b.scan_state != "CLEAN" or b.sha256 != snap.get("source_blob_sha256"):
                svc.fail(404, "WIKI_METADATA_CHANGED", "来源原件不可用")

    def check_resource(self, rid, path=()):
        if rid not in self.base_resources:
            self.base_resources[rid] = svc._resource_acl(self.db, self.user, self.resource(rid))
        r = self.base_resources[rid]
        if r.suspended:
            svc.fail(404, "NOT_FOUND", "资料已停用或不可访问")
        if r.kind != "knowledge":
            return r
        marker = "resource:" + r.id
        if marker in path or len(path) >= 16:
            svc.fail(409, "DEPENDENCY_CYCLE", "知识来源依赖成环")
        from . import wiki
        if r.id not in self.provenance:
            p = wiki._policy(self.db, f"wiki-provenance:{r.id}")
            self.provenance[r.id] = (p, wiki._provenance_sources(self.db, r))
        policy, sources = self.provenance[r.id]
        if sources is None:
            return r
        next_path = (*path, marker)
        if policy and policy.config.get("source_mode") == "unverified_draft":
            snaps = policy.config.get("source_snapshot", [])
            if not snaps or not set(sources).issubset({s.get("version_id") for s in snaps}):
                svc.fail(404, "WIKI_PROVENANCE_UNAVAILABLE", "冻结来源记录不可用")
            for snap in snaps:
                self.snapshot(snap, next_path, blob=True)
            for snap in policy.config.get("reference_snapshot", []):
                self.snapshot(snap, next_path)
        else:
            for vid in sources:
                self.check(vid, next_path)
        return r

    def check(self, vid, path=()):
        if vid in path or len([x for x in path if not x.startswith("resource:")]) >= 8:
            svc.fail(409, "DEPENDENCY_CYCLE", "资料依赖成环或超过八层")
        # Path-specific memo does not hide cycles through a previously visited subtree.
        if (vid, path) in self.checked:
            return self.checked[(vid, path)]
        v = self.version(vid)
        if not v:
            svc.fail(404, "NOT_FOUND", "版本不可访问")
        r = self.check_resource(v.resource_id, path)
        if vid not in self.base_versions:
            svc._version_acl(self.db, self.user, v, r)
            b = self.db.get(m.Blob, v.source_blob_id) if v.source_blob_id else None
            if (r.kind == "document" and not b) or (b and b.scan_state != "CLEAN"):
                svc.fail(404, "SOURCE_NOT_CLEAN", "原件尚未通过扫描")
            if self.busy is None:
                self.busy = set(self.db.scalars(select(m.Upload.version_id).where(m.Upload.state == "OPEN"))) | set(
                    self.db.scalars(select(m.Job.version_id).where(m.Job.state.in_(["QUEUED", "RUNNING"]))))
            if vid in self.busy:
                svc.fail(404, "WORK_IN_PROGRESS", "资料正在处理")
            self.base_versions[vid] = b
        b = self.base_versions[vid]
        if vid not in self.edges:
            self.edges[vid] = list(self.db.scalars(select(m.RelationEdge).where(m.RelationEdge.source_version_id == vid)))
        if vid not in self.citations:
            self.citations[vid] = list(self.db.execute(select(m.EvidenceLink.to_version_id, m.EvidenceLink.to_block_id)
                .where(m.EvidenceLink.from_version_id == vid)))
        dependencies = {row[0] for row in self.citations[vid]}
        # Only attachment references are read here, never paragraph/table data or
        # search_text. Attachment authority cannot be omitted from the catalog.
        if vid not in self.attachments:
            self.attachments[vid] = list(self.db.scalars(select(m.ContentBlock.data).where(
                m.ContentBlock.version_id == vid, m.ContentBlock.block_type.in_(["image", "attachment"]))))
        dependencies.update(data["version_id"] for data in self.attachments[vid] if data.get("version_id"))
        next_path = (*path, vid)
        for edge in self.edges[vid]:
            target = self.check_resource(edge.target_resource_id, next_path)
            if edge.evidence_version_id:
                dependencies.add(edge.evidence_version_id)
            if edge.relation_type == "DEPENDS_ON":
                latest = self.db.scalar(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == target.id)
                    .order_by(m.ResourceVersion.version_no.desc()).limit(1))
                if not latest:
                    svc.fail(404, "NOT_FOUND", "依赖尚无版本")
                dependencies.add(latest.id)
        children = [self.check(dep, next_path) for dep in sorted(dependencies)]
        stamp = svc.digest([v.id, v.revision, v.content_sha256, v.title, v.state, v.legal_status,
            svc.primitive(v.valid_from), svc.primitive(v.valid_to), v.applicability,
            r.id, r.revision, r.access_epoch, b.sha256 if b else None, children])
        self.checked[(vid, path)] = stamp
        return stamp


def _formal_candidate(db, resource, context):
    """Mirror formal metadata selection; body-read repeats central eligibility."""
    day, cutoff = svc.effective_date(context), context.get("knowledge_cutoff")
    if isinstance(cutoff, str):
        cutoff = datetime.fromisoformat(cutoff)
    from .wiki import unverified_wiki
    if unverified_wiki(db, resource):
        return None
    for v in db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == resource.id)
            .order_by(m.ResourceVersion.version_no.desc())):
        if v.state != "APPROVED" or not svc.is_released(db, v, cutoff):
            continue
        if v.valid_from and day < v.valid_from or svc.match_applicability(v.applicability or {}, context) is False:
            continue
        if (v.valid_to and day >= v.valid_to) or svc.match_applicability(v.applicability or {}, context) is None:
            return None
        if v.legal_status in {"UNKNOWN", "PARTIAL"} or (v.legal_status == "FUTURE" and not v.valid_from) \
                or (v.legal_status == "REPEALED" and not v.valid_to):
            return None
        if not v.content_sha256 or (v.knowledge_type == "source" and not v.source_verified):
            return None
        from .source_authority import formal_excluded
        if formal_excluded(db, v, context):
            return None
        return v
    return None


def build_catalog(db, user, space_id, context=None, *, scope="reference"):
    from .projection_read import active, memo, projection_read
    with projection_read(db):
        if active(db):
            key = svc.digest([user if isinstance(user, str) else user.id, space_id, context or {}, scope,
                              svc.primitive(svc.effective_date(context or {}))])
            return copy.deepcopy(memo(db, "authorized_catalog", key,
                lambda: _build_catalog(db, user, space_id, context or {}, scope)))
        return _build_catalog(db, user, space_id, context or {}, scope)


def _build_catalog(db, user, space_id, context, scope):
    if isinstance(user, str):
        user = db.get(m.User, user)
    svc.space_access(db, user, space_id)
    access = CatalogAuthority(db, user)
    resources = list(db.scalars(select(m.Resource).where(m.Resource.space_id == space_id,
        m.Resource.kind.in_(["document", "knowledge"]), m.Resource.deleted_at.is_(None), m.Resource.suspended.is_(False))))
    latest = _latest_versions(db, resources)
    access.resources.update({r.id: r for r in resources})
    access.versions.update({v.id: v for v in latest.values()})
    # Batch adjacency metadata. This removes per-page SQL while retaining every
    # edge and anchor. 400 is the SQL bind batch size, not a result limit.
    version_ids = list(access.versions)
    for start in range(0, len(version_ids), 400):
        ids = version_ids[start:start + 400]
        for vid in ids:
            access.edges[vid], access.citations[vid], access.attachments[vid] = [], [], []
        for edge in db.scalars(select(m.RelationEdge).where(m.RelationEdge.source_version_id.in_(ids))):
            access.edges[edge.source_version_id].append(edge)
        for fv, tv, tb in db.execute(select(m.EvidenceLink.from_version_id, m.EvidenceLink.to_version_id,
                m.EvidenceLink.to_block_id).where(m.EvidenceLink.from_version_id.in_(ids))):
            access.citations[fv].append((tv, tb))
        for vid, data in db.execute(select(m.ContentBlock.version_id, m.ContentBlock.data).where(
                m.ContentBlock.version_id.in_(ids), m.ContentBlock.block_type.in_(["image", "attachment"]))):
            access.attachments[vid].append(data)
    # Count block identifiers in SQL, not characters or full text. Empty pages
    # have no readable body; no page/row-number cutoff is applied.
    counts = {}
    for start in range(0, len(resources), 400):
        ids = [r.id for r in resources[start:start + 400]]
        counts.update(dict(db.execute(select(m.ContentBlock.version_id, func.count(m.ContentBlock.block_id))
            .join(m.ResourceVersion, m.ResourceVersion.id == m.ContentBlock.version_id)
            .where(m.ResourceVersion.resource_id.in_(ids)).group_by(m.ContentBlock.version_id)).all()))
    entries = []
    for r in resources:
        v = latest.get(r.id) if scope == "reference" else _formal_candidate(db, r, context)
        if not v or not counts.get(v.id):
            continue
        try:
            stamp = access.check(v.id)
        except svc.APIError:
            continue
        entries.append({"resource_id": r.id, "version_id": v.id, "title": v.title, "kind": r.kind,
            "state": v.state, "legal_status": v.legal_status, "category": r.category or "",
            "knowledge_type": v.knowledge_type, "aliases": [t[6:] for t in r.tags or [] if t.startswith("alias:")],
            "applicability": v.applicability or {}, "valid_from": svc.primitive(v.valid_from), "valid_to": svc.primitive(v.valid_to),
            "block_count": counts[v.id], "characters": None, "records": [], "links": [], "relations": [],
            "body_loaded": False, "_metadata_signature": stamp})
    pages = {}
    for index, page in enumerate(sorted(entries, key=lambda p: (p["kind"] != "knowledge", p["title"], p["version_id"])), 1):
        page["id"] = f"W{index}"
        pages[page["id"]] = page
    by_version = {p["version_id"]: p for p in pages.values()}
    by_resource = {p["resource_id"]: p for p in pages.values()}
    for start in range(0, len(pages), 400):
        names = [f"wiki-compilation:{p['version_id']}" for p in list(pages.values())[start:start + 400]]
        for policy in db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.in_(names))):
            page = by_version.get(policy.name.removeprefix("wiki-compilation:"))
            if page:
                for key in ("compilation_type", "compilation_spec_version", "structure_status", "batch_scope_status"):
                    if key in policy.config:
                        page[key] = policy.config[key]
                if policy.config.get("compiled_content_sha256") != access.version(page["version_id"]).content_sha256:
                    page["structure_status"] = "CHANGED_SINCE_COMPILATION"
    try:
        from .wiki_maintenance import catalog_metadata
    except ImportError:
        catalog_metadata = None
    if catalog_metadata:
        metadata = catalog_metadata(db, user, space_id, set(by_resource))
        for rid, item in metadata.items():
            if rid in by_resource:
                page = by_resource[rid]
                page["aliases"] = sorted(set(item.get("aliases", []) +
                    ([item["canonical_key"]] if item.get("canonical_key") else [])))
                canonical = by_resource.get(item.get("canonical_resource_id"))
                if canonical and canonical["id"] != page["id"]:
                    page["canonical_page_id"] = canonical["id"]
    assembly = _RelationAssembly()
    for page in pages.values():
        vid = page["version_id"]
        for target_vid, block_id in access.citations.get(vid, []):
            target = by_version.get(target_vid)
            if target:
                _add_relation(pages, page, target, "CITES", "registered", {}, [{"version_id": target_vid, "block_id": block_id}], version_map=by_version, assembly=assembly)
        for edge in access.edges.get(vid, []):
            target = by_resource.get(edge.target_resource_id)
            if target:
                anchors = [{"version_id": edge.evidence_version_id, "block_id": edge.evidence_block_id}] if edge.evidence_version_id else []
                _add_relation(pages, page, target, edge.relation_type, "registered", edge.conditions or {}, anchors, version_map=by_version, assembly=assembly)
        # Imported notes may have document-level provenance rather than block links.
        policy, sources = access.provenance.get(page["resource_id"], (None, None))
        for source_vid in sources or []:
            target = by_version.get(source_vid)
            if target:
                _add_relation(pages, page, target, "CITES", "provenance", {}, [{"version_id": source_vid}], version_map=by_version, assembly=assembly)
    for policy in db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like(f"wiki-semantic:{space_id}:%"))):
        config = policy.config
        if config.get("space_id") != space_id:
            continue
        try:
            for snap in config.get("source_snapshot", []):
                access.snapshot(snap, blob=True)
            for snap in config.get("reference_snapshot", []):
                access.snapshot(snap)
            if not config.get("source_snapshot"):
                continue
        except (svc.APIError, KeyError, TypeError):
            continue
        for proposal in config.get("proposals", []):
            endpoints = proposal.get("endpoints", [])
            if len(endpoints) != 2:
                continue
            a, b = [by_resource.get(s.get("resource_id")) for s in endpoints]
            if not a or not b or any(page["version_id"] != snap.get("version_id") for page, snap in zip((a, b), endpoints)):
                continue
            try:
                for snap in endpoints:
                    access.snapshot(snap)
            except svc.APIError:
                continue
            _add_relation(pages, a, b, proposal["relation_type"], "proposed", {},
                proposal.get("anchors", []), proposal.get("explanation", ""), version_map=by_version, assembly=assembly)
    for page in pages.values():
        page["links"] = sorted(set(page["links"]))
    from .source_authority import annotate_catalog
    return annotate_catalog(db, user, space_id, context, pages)


class _RelationAssembly:
    """Call-local pure construction indexes, never an authority/cache boundary.

    Rows are shared between endpoints exactly as before. Use endpoint buckets,
    then Python equality for ALL original fields: hashing JSON conditions would
    incorrectly distinguish equal numeric/bool values or dictionary key order.
    """
    def __init__(self):
        self.edges = {}
        self.anchor_digests = {}

    def candidates(self, page, row):
        key = (id(page), row["source"], row["target"])
        if key not in self.edges:
            self.edges[key] = [edge for edge in page["relations"]
                if edge["source"] == row["source"] and edge["target"] == row["target"]]
        return self.edges[key]

    def known_anchors(self, row):
        key = id(row)
        if key not in self.anchor_digests:
            self.anchor_digests[key] = {svc.digest(anchor) for anchor in row["anchors"]}
        return self.anchor_digests[key]


def _add_relation(pages, source, target, relation_type, origin, conditions, anchors, explanation="", *, version_map, assembly=None):
    if source["id"] == target["id"]:
        return
    # A type is a navigation aid, not a proof that the rule is true/applicable.
    row = {"source": source["id"], "target": target["id"], "type": relation_type,
        "verification_status": "PROPOSED" if origin == "proposed" else "REGISTERED_NOT_BUSINESS_VERIFIED",
        "origin": origin, "conditions": copy.deepcopy(conditions), "explanation": explanation,
        "anchors": [{k: s[k] for k in ("version_id", "block_id") if k in s} for s in anchors],
        "source_pages": sorted({version_map[s["version_id"]]["id"] for s in anchors if s.get("version_id") in version_map})}
    for page, other in ((source, target), (target, source)):
        # Group one logical edge; retain every source anchor inside that edge.
        candidates = assembly.candidates(page, row) if assembly is not None else page["relations"]
        same = next((e for e in candidates if all(e[k] == row[k] for k in
            ("source", "target", "type", "origin", "conditions", "explanation"))), None)
        if same:
            if assembly is None:
                known = {svc.digest(a) for a in same["anchors"]}
                same["anchors"].extend(a for a in row["anchors"] if svc.digest(a) not in known)
            else:
                known = assembly.known_anchors(same)
                incoming = [(anchor, svc.digest(anchor)) for anchor in row["anchors"]]
                # Update AFTER selection: preserve duplicate anchors inside a
                # single incoming row, matching the original generator rule.
                same["anchors"].extend(anchor for anchor, key in incoming if key not in known)
                known.update(key for _, key in incoming)
            same["source_pages"] = sorted(set(same["source_pages"] + row["source_pages"]))
        else:
            page["relations"].append(row)
            if assembly is not None:
                candidates.append(row)
        page["links"].append(other["id"])


def catalog_signature(pages):
    return svc.digest([{k: v for k, v in p.items() if k not in {"records", "body_loaded", "characters"}}
        for p in pages.values()])


def read_whole_pages(db, user, space_id, pages, requested, context=None, *, scope="reference", next_evidence=1):
    """The ONLY transition from catalog discovery to source-backed full text."""
    wanted = list(dict.fromkeys(pid for pid in requested if pid in pages))
    versions = {pages[pid]["version_id"] for pid in wanted}
    if scope == "reference":
        records = reference_evidence(db, user, space_id, context or {}, reading=True, version_ids=versions)
    else:
        records = svc.eligible_evidence(db, user, space_id, context or {}, version_ids=versions)
    grouped = defaultdict(list)
    for row in records:
        grouped[row["version_id"]].append(row)
    result, unavailable = [], []
    for pid in wanted:
        page = pages[pid]
        rows = grouped.get(page["version_id"], [])
        if not rows or len(rows) != page["block_count"]:
            unavailable.append(pid)
            continue
        for row in sorted(rows, key=lambda r: (r["ordinal"], r["block_id"])):
            row["evidence_id"] = f"E{next_evidence}"
            next_evidence += 1
        page["records"] = sorted(rows, key=lambda r: (r["ordinal"], r["block_id"]))
        page["characters"] = sum(len(r["text"]) for r in rows)
        page["body_loaded"] = True
        result.extend(page["records"])
    # Discover inline links only in actually selected full pages. The catalog
    # never has to scan every body just to reconstruct these links.
    from .wiki import _norm, parse_wikilinks
    names = defaultdict(set)
    for page in pages.values():
        for name in [page["title"], *page.get("aliases", [])]:
            names[_norm(name)].add(page["id"])
    for pid in wanted:
        if not pages[pid].get("body_loaded"):
            continue
        links = set(pages[pid]["links"])
        for row in pages[pid]["records"]:
            for link in parse_wikilinks(row["text"]):
                links.update(names.get(_norm(link["title"]), set()))
        pages[pid]["links"] = sorted(links - {pid})
    return result, unavailable, next_evidence


def related_reads(pages, requested):
    """One-hop conditions/dependencies and incoming exceptions, plus sources.

    No recursive graph-wide expansion. Further hops are explicit model READs.
    Proposed edges request comparison, never assert business validity.
    """
    result = list(requested)
    for pid in requested:
        canonical = pages[pid].get("canonical_page_id")
        if canonical and canonical in pages:
            result.append(canonical)
        for edge in pages[pid].get("relations", []):
            if edge["source"] == pid and edge["type"] in {"APPLIES_TO", "REQUIRES", "DEPENDS_ON"}:
                result.append(edge["target"])
            if edge["target"] == pid and edge["type"] == "EXCEPTION_OF":
                result.append(edge["source"])
    for pid in list(dict.fromkeys(result)):
        for edge in pages[pid].get("relations", []):
            if edge["source"] == pid and edge["type"] == "CITES":
                result.append(edge["target"])
    return list(dict.fromkeys(result))
