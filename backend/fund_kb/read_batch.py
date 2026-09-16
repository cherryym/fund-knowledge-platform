"""One-operation facts/structure snapshot for Wiki reads, never an ACL result cache.

The ordinary authorization and integrity algorithms still run. Repeated lookups
use freshly selected rows from this scope, which is discarded before returning.
Mutations/transaction boundaries disable the snapshot; workers and writes outside
these explicit read scopes retain their normal fresh-query behavior.
"""
from contextlib import contextmanager
from functools import wraps

from sqlalchemy import event, select
from sqlalchemy.orm import load_only

from . import models as m

SLOT = "wiki_read_facts_v1"
CHUNK = 400  # Portable IN bind batches, not a result limit.


def current(db):
    value = db.info.get(SLOT)
    return value if value and value.valid and not (db.new or db.dirty or db.deleted) else None


class ReadFacts:
    def __init__(self, db, user, space_id):
        self.valid = True
        self.actor_id = user.id
        self.user_active = dict(db.execute(select(m.User.id, m.User.active)).all())
        self.spaces = set(db.scalars(select(m.Space.id)))
        self.governance = dict(db.execute(select(m.RuntimePolicy.name, m.RuntimePolicy.config)
            .where(m.RuntimePolicy.name.like("space-governance:%"))).all())
        self.members = {}
        for sid, role in db.execute(select(m.SpaceMember.space_id, m.SpaceMember.role)
                .where(m.SpaceMember.user_id == user.id)):
            self.members.setdefault(sid, set()).add(role)
        self.grants = {}
        for rid, permission in db.execute(select(m.ResourceGrant.resource_id, m.ResourceGrant.permission)
                .where(m.ResourceGrant.user_id == user.id)):
            self.grants.setdefault(rid, set()).add(permission)
        self.resources = list(db.scalars(select(m.Resource).where(m.Resource.space_id == space_id)
            .execution_options(populate_existing=True)))
        self.versions = []
        ids = [r.id for r in self.resources]
        for start in range(0, len(ids), CHUNK):
            self.versions.extend(db.scalars(select(m.ResourceVersion)
                .where(m.ResourceVersion.resource_id.in_(ids[start:start + CHUNK]))
                .execution_options(populate_existing=True)))
        self.by_resource = {}
        for version in self.versions:
            self.by_resource.setdefault(version.resource_id, []).append(version)
        for rows in self.by_resource.values():
            rows.sort(key=lambda v: v.version_no, reverse=True)
        self.version_ids = {v.id for v in self.versions}
        self.blocks = {vid: [] for vid in self.version_ids}
        self.links = {vid: [] for vid in self.version_ids}
        self.relations = {vid: [] for vid in self.version_ids}
        self.releases = {vid: [] for vid in self.version_ids}
        ids = sorted(self.version_ids)
        for start in range(0, len(ids), CHUNK):
            batch = ids[start:start + CHUNK]
            for row in db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id.in_(batch))
                    .order_by(m.ContentBlock.version_id, m.ContentBlock.ordinal)
                    .execution_options(populate_existing=True)):
                self.blocks[row.version_id].append(row)
            for row in db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id.in_(batch))
                    .execution_options(populate_existing=True)):
                self.links[row.from_version_id].append(row)
            for row in db.scalars(select(m.RelationEdge).where(m.RelationEdge.source_version_id.in_(batch))
                    .execution_options(populate_existing=True)):
                self.relations[row.source_version_id].append(row)
            for row in db.scalars(select(m.Release).options(load_only(m.Release.id, m.Release.resource_id,
                    m.Release.version_id, m.Release.state, m.Release.activated_at))
                    .where(m.Release.version_id.in_(batch)).execution_options(populate_existing=True)):
                self.releases[row.version_id].append(row)
        blob_ids = sorted({v.source_blob_id for v in self.versions if v.source_blob_id})
        self.blobs = []  # Strong references avoid repeated ORM reloads of the same source.
        for start in range(0, len(blob_ids), CHUNK):
            self.blobs.extend(db.scalars(select(m.Blob).where(m.Blob.id.in_(blob_ids[start:start + CHUNK]))
                .execution_options(populate_existing=True)))


@contextmanager
def read_scope(db, user, space_id):
    previous = db.info.get(SLOT)
    if current(db) is not None and previous.actor_id == user.id:
        yield previous
        return
    if not user or db.new or db.dirty or db.deleted:
        yield None
        return
    facts = ReadFacts(db, user, space_id)
    db.info[SLOT] = facts

    def invalidate(*_):
        facts.valid = False
        db.info.pop("wiki_checked_content_hash", None)
        db.info.pop("wiki_policy_rows", None)

    def statement(state):
        if not state.is_select:
            invalidate()

    events = [("after_flush", invalidate), ("after_commit", invalidate),
              ("after_rollback", invalidate), ("do_orm_execute", statement)]
    for name, callback in events:
        event.listen(db, name, callback)
    try:
        yield facts
    finally:
        for name, callback in events:
            event.remove(db, name, callback)
        facts.valid = False
        if previous is None:
            db.info.pop(SLOT, None)
        else:
            db.info[SLOT] = previous
        # These pre-existing computation caches must not outlive a logical read,
        # including callers that reuse a Session for multiple reads.
        db.info.pop("wiki_checked_content_hash", None)
        db.info.pop("wiki_policy_rows", None)
        db.info.pop("wiki_receipt_provenance_ids_v1", None)


def batched_read(*, resource_scope=False):
    def decorate(function):
        @wraps(function)
        def wrapped(db, user, scope_id, *args, **kwargs):
            from . import services as svc
            # Authorize the scope before collecting any scoped content facts.
            if resource_scope:
                resource = svc.resource_access(db, user, scope_id)
                space_id = resource.space_id
            else:
                svc.space_access(db, user, scope_id)
                space_id = scope_id
            with read_scope(db, user, space_id):
                return function(db, user, scope_id, *args, **kwargs)
        return wrapped
    return decorate


def blocks_for(db, version_id):
    facts = current(db)
    if facts is not None and version_id in facts.blocks:
        return facts.blocks[version_id]
    return list(db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == version_id)
        .order_by(m.ContentBlock.ordinal).execution_options(populate_existing=True)))


def links_for(db, version_id):
    facts = current(db)
    if facts is not None and version_id in facts.links:
        return facts.links[version_id]
    return list(db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == version_id)
        .execution_options(populate_existing=True)))


def relations_for(db, version_id):
    facts = current(db)
    if facts is not None and version_id in facts.relations:
        return facts.relations[version_id]
    return list(db.scalars(select(m.RelationEdge).where(m.RelationEdge.source_version_id == version_id)
        .execution_options(populate_existing=True)))


def versions_for(db, resource_id):
    facts = current(db)
    if facts is not None and resource_id in facts.by_resource:
        return facts.by_resource[resource_id]
    return list(db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == resource_id)
        .order_by(m.ResourceVersion.version_no.desc()).execution_options(populate_existing=True)))
