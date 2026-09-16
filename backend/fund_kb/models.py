"""All 24 design tables plus LoginSession; no PostgreSQL-specific SQL types.

JSON updates replace the entire value. Published content immutability, ACL and
reviewer separation remain service-layer obligations, as in the design package.
"""
from __future__ import annotations

import secrets
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import declared_attr

from .db import (
    Base,
    BoundedString,
    EmptyText,
    ExactKey,
    JSONDocument,
    JSONShape,
    UTCDateTime,
    UTCNow,
    UUIDString,
    utcnow,
)


def new_id():
    return str(uuid4())


def identifier(*args, **kwargs):
    return Column(UUIDString(), *args, **kwargs)


def primary_id():
    return identifier(primary_key=True, default=new_id)


def string(length, **kwargs):
    return Column(BoundedString(length), **kwargs)


def exact_key(length, **kwargs):
    return Column(ExactKey(length), **kwargs)


def timestamp(*, nullable=False, default_now=False, update=False):
    kwargs = {"nullable": nullable}
    if default_now:
        kwargs.update(default=utcnow, server_default=UTCNow())
    if update:
        kwargs["onupdate"] = utcnow
    return Column(UTCDateTime(), **kwargs)


def flag(name, default=False):
    return Column(Boolean(create_constraint=True, name=f"{name}_bool"), nullable=False,
                  default=default, server_default=true() if default else false())


def json_column(kind="any", *, nullable=False, default=None, string_items=False):
    kwargs = {"nullable": nullable}
    if default is not None:
        kwargs["default"] = default
    # Defaults intentionally run in the application; Oracle CLOB/MySQL LONGTEXT
    # default syntax is not portable. Raw SQL importers must supply JSON values.
    return Column(JSONDocument(kind, string_items=string_items), **kwargs)


def empty_text():
    return Column(EmptyText(), nullable=True, default="")


def choices(column, values):
    encoded = ",".join(repr(value) for value in values.split())
    return CheckConstraint(f"{column} IN ({encoded})", name=f"{column}_values")


def hash_check(column):
    stripped = column
    for char in "0123456789abcdef":
        stripped = f"replace({stripped},'{char}','')"
    return CheckConstraint(f"length({column}) = 64 AND coalesce(length({stripped}),0) = 0",
                           name=f"{column}_hex")


def json_check(column, kind="any"):
    return CheckConstraint(JSONShape(column, kind), name=f"{column}_json")


class RevisionMixin:
    revision = Column(BigInteger, nullable=False, default=1, server_default="1")

    @declared_attr.directive
    def __mapper_args__(cls):
        # ORM flush adds WHERE revision=<old> and increments the revision. Bulk
        # UPDATE bypasses this: services must include their own revision predicate.
        return {"version_id_col": cls.revision}


class User(Base):
    __tablename__ = "users"
    id = primary_id()
    external_subject = exact_key(512, nullable=False, unique=True)
    display_name = string(200, nullable=False)
    active = flag("active", True)
    created_at = timestamp(default_now=True)


class Space(RevisionMixin, Base):
    __tablename__ = "spaces"
    id = primary_id()
    name = string(200, nullable=False)
    created_at = timestamp(default_now=True)
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("revision > 0", name="revision_positive"),
    )


class SpaceMember(Base):
    __tablename__ = "space_members"
    space_id = identifier(ForeignKey("spaces.id"), primary_key=True)
    user_id = identifier(ForeignKey("users.id"), primary_key=True)
    role = string(16, primary_key=True)
    __table_args__ = (choices("role", "reader editor reviewer publisher admin"), Index("ix_members_user", "user_id"))


class Resource(RevisionMixin, Base):
    __tablename__ = "resources"
    id = primary_id()
    space_id = identifier(ForeignKey("spaces.id"), nullable=False)
    kind = string(16, nullable=False)
    name = string(300, nullable=False)
    category = string(200, nullable=False, default="未分类", server_default="未分类")
    tags = json_column("array", default=list, string_items=True)
    owner_id = identifier(ForeignKey("users.id"), nullable=False)
    restricted = flag("restricted")
    classification = string(16, nullable=False, default="INTERNAL", server_default="INTERNAL")
    access_epoch = Column(BigInteger, nullable=False, default=1, server_default="1")
    active_release_id = identifier(nullable=True)
    suspended = flag("suspended")
    deleted_at = timestamp(nullable=True)
    retain_until = timestamp(nullable=True)
    legal_hold = flag("legal_hold")
    created_at = timestamp(default_now=True)
    updated_at = timestamp(default_now=True, update=True)
    __table_args__ = (
        UniqueConstraint("space_id", "id", name="uq_resource_space_id"),
        ForeignKeyConstraint(["id", "active_release_id"], ["releases.resource_id", "releases.id"],
                             name="fk_resource_release_owner", use_alter=True),
        choices("kind", "document knowledge template"),
        choices("classification", "PUBLIC INTERNAL CONFIDENTIAL RESTRICTED"),
        CheckConstraint("length(name) BETWEEN 1 AND 300", name="name_length"),
        CheckConstraint("revision > 0 AND access_epoch > 0", name="revisions_positive"),
        json_check("tags", "array"),
        Index("resources_space_live", "space_id", "kind", "deleted_at", "created_at", "id"),
        Index("ix_resource_owner", "owner_id"),
        Index("ix_resource_active_release", "active_release_id"),
    )


class ResourceGrant(Base):
    __tablename__ = "resource_grants"
    resource_id = identifier(ForeignKey("resources.id"), primary_key=True)
    user_id = identifier(ForeignKey("users.id"), primary_key=True)
    permission = string(16, primary_key=True)
    __table_args__ = (choices("permission", "read download edit review publish manage"),
                      Index("ix_grants_user", "user_id"))


class Blob(Base):
    __tablename__ = "blobs"
    id = primary_id()
    space_id = identifier(ForeignKey("spaces.id"), nullable=False, index=True)
    object_key = exact_key(512, nullable=False, unique=True)
    sha256 = string(64, nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    mime_type = string(200, nullable=False)
    scan_state = string(16, nullable=False)
    created_at = timestamp(default_now=True)
    __table_args__ = (hash_check("sha256"), CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
                      choices("scan_state", "QUARANTINED CLEAN REJECTED"))


class ResourceVersion(RevisionMixin, Base):
    __tablename__ = "resource_versions"
    id = primary_id()
    resource_id = identifier(ForeignKey("resources.id"), nullable=False)
    version_no = Column(Integer, nullable=False)
    base_version_id = identifier(nullable=True)
    state = string(16, nullable=False, default="DRAFT", server_default="DRAFT")
    author_id = identifier(ForeignKey("users.id"), nullable=False, index=True)
    title = string(500, nullable=False)
    knowledge_type = string(32, nullable=False, default="source", server_default="source")
    origin = string(16, nullable=False)
    source_blob_id = identifier(ForeignKey("blobs.id"), nullable=True, index=True)
    source_url = Column(Text, nullable=True)
    source_verified = flag("source_verified")
    legal_status = string(24, nullable=False, default="UNKNOWN", server_default="UNKNOWN")
    valid_from = Column(Date, nullable=True)
    valid_to = Column(Date, nullable=True)
    applicability = json_column("object", default=dict)
    required_facts = json_column("array", default=list)
    change_kind = string(16, nullable=False, default="UPDATE", server_default="UPDATE")
    change_reason = empty_text()
    content_sha256 = string(64, nullable=True)
    created_at = timestamp(default_now=True)
    draft_resource_id = identifier(Computed("CASE WHEN state = 'DRAFT' THEN resource_id ELSE NULL END"))
    draft_author_id = identifier(Computed("CASE WHEN state = 'DRAFT' THEN author_id ELSE NULL END"))
    __table_args__ = (
        UniqueConstraint("resource_id", "version_no", name="uq_version_number"),
        UniqueConstraint("resource_id", "id", name="uq_version_resource_id"),
        UniqueConstraint("draft_resource_id", "draft_author_id", name="one_author_draft"),
        ForeignKeyConstraint(["resource_id", "base_version_id"], ["resource_versions.resource_id", "resource_versions.id"],
                             name="fk_version_base_owner"),
        CheckConstraint("version_no > 0 AND revision > 0", name="numbers_positive"),
        CheckConstraint("valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from", name="valid_interval"),
        CheckConstraint("state = 'DRAFT' OR content_sha256 IS NOT NULL", name="submitted_hash"),
        choices("state", "DRAFT IN_REVIEW APPROVED REJECTED"),
        choices("knowledge_type", "source faq rule sop scenario case term solution_template"),
        choices("origin", "UPLOAD HUMAN AI_DRAFT COPY"),
        choices("legal_status", "UNKNOWN NOT_APPLICABLE FUTURE EFFECTIVE PARTIAL REPEALED"),
        choices("change_kind", "UPDATE CORRECTION"),
        hash_check("content_sha256"), json_check("applicability", "object"), json_check("required_facts", "array"),
        Index("versions_resource", "resource_id", "version_no"),
        Index("ix_version_base", "resource_id", "base_version_id"),
        {"implicit_returning": False},
    )


class ContentBlock(Base):
    __tablename__ = "content_blocks"
    version_id = identifier(ForeignKey("resource_versions.id"), primary_key=True)
    block_id = identifier(primary_key=True, default=new_id)
    ordinal = Column(Integer, nullable=False)
    block_type = string(16, nullable=False)
    data = json_column("object")
    search_text = empty_text()
    locator = json_column("object", default=dict)
    content_sha256 = string(64, nullable=False)
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal", name="uq_block_ordinal"),
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        choices("block_type", "heading paragraph list table step warning formula image attachment"),
        hash_check("content_sha256"), json_check("data", "object"), json_check("locator", "object"),
    )


class EvidenceLink(Base):
    __tablename__ = "evidence_links"
    id = primary_id()
    from_version_id = identifier(nullable=False)
    from_block_id = identifier(nullable=False)
    to_version_id = identifier(nullable=False)
    to_block_id = identifier(nullable=False)
    purpose = string(24, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["from_version_id", "from_block_id"], ["content_blocks.version_id", "content_blocks.block_id"]),
        ForeignKeyConstraint(["to_version_id", "to_block_id"], ["content_blocks.version_id", "content_blocks.block_id"]),
        UniqueConstraint("from_version_id", "from_block_id", "to_version_id", "to_block_id", "purpose", name="uq_evidence_edge"),
        CheckConstraint("from_version_id <> to_version_id OR from_block_id <> to_block_id", name="no_self_link"),
        choices("purpose", "RULE INTERNAL_OPINION FACT CASE CALCULATION"),
        Index("evidence_incoming", "to_version_id", "to_block_id"),
    )


class RelationEdge(Base):
    __tablename__ = "relation_edges"
    id = primary_id()
    source_version_id = identifier(ForeignKey("resource_versions.id"), nullable=False, index=True)
    target_resource_id = identifier(ForeignKey("resources.id"), nullable=False)
    relation_type = string(24, nullable=False)
    conditions = json_column("object", default=dict)
    evidence_version_id = identifier(nullable=True)
    evidence_block_id = identifier(nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(["evidence_version_id", "evidence_block_id"], ["content_blocks.version_id", "content_blocks.block_id"]),
        CheckConstraint("(evidence_version_id IS NULL AND evidence_block_id IS NULL) OR "
                        "(evidence_version_id IS NOT NULL AND evidence_block_id IS NOT NULL)", name="evidence_pair"),
        choices("relation_type", "CITES EXPLAINS APPLIES_TO REQUIRES EXCEPTION_OF DEPENDS_ON SUPERSEDES"),
        json_check("conditions", "object"), Index("relations_target", "target_resource_id"),
        Index("ix_relation_evidence", "evidence_version_id", "evidence_block_id"),
    )


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    id = primary_id()
    version_id = identifier(ForeignKey("resource_versions.id"), nullable=False)
    reviewer_id = identifier(ForeignKey("users.id"), nullable=False, index=True)
    decision = string(8, nullable=False)
    reviewed_sha256 = string(64, nullable=False)
    comment = empty_text()
    created_at = timestamp(default_now=True)
    __table_args__ = (UniqueConstraint("version_id", "reviewer_id", name="uq_review_person"),
                      choices("decision", "APPROVE REJECT"), hash_check("reviewed_sha256"))


class Release(Base):
    __tablename__ = "releases"
    id = primary_id()
    resource_id = identifier(ForeignKey("resources.id"), nullable=False)
    version_id = identifier(nullable=False)
    state = string(16, nullable=False)
    manifest = json_column("object", default=dict)
    publisher_id = identifier(ForeignKey("users.id"), nullable=False, index=True)
    created_at = timestamp(default_now=True)
    activated_at = timestamp(nullable=True)
    active_resource_id = identifier(Computed("CASE WHEN state = 'ACTIVE' THEN resource_id ELSE NULL END"))
    __table_args__ = (
        UniqueConstraint("resource_id", "id", name="uq_release_resource_id"),
        UniqueConstraint("active_resource_id", name="one_active_release"),
        ForeignKeyConstraint(["resource_id", "version_id"], ["resource_versions.resource_id", "resource_versions.id"]),
        choices("state", "PREPARING ACTIVE SUPERSEDED FAILED"), json_check("manifest", "object"),
        Index("ix_release_version", "resource_id", "version_id"),
        {"implicit_returning": False},
    )


class Upload(Base):
    __tablename__ = "uploads"
    id = primary_id()
    version_id = identifier(ForeignKey("resource_versions.id"), nullable=False, index=True)
    user_id = identifier(ForeignKey("users.id"), nullable=False, index=True)
    filename = string(500, nullable=False)
    declared_size = Column(BigInteger, nullable=False)
    part_size = Column(Integer, nullable=False, default=8388608, server_default="8388608")
    part_count = Column(Integer, nullable=False)
    expected_sha256 = string(64, nullable=True)
    state = string(16, nullable=False)
    expires_at = timestamp()
    created_at = timestamp(default_now=True)
    open_version_id = identifier(Computed("CASE WHEN state = 'OPEN' THEN version_id ELSE NULL END"))
    __table_args__ = (
        UniqueConstraint("open_version_id", name="one_open_upload"),
        CheckConstraint("declared_size BETWEEN 1 AND 104857600", name="size_range"),
        CheckConstraint("part_count BETWEEN 1 AND 13 AND part_size > 0", name="parts_range"),
        choices("state", "OPEN SEALED CANCELLED EXPIRED"), hash_check("expected_sha256"),
        {"implicit_returning": False},
    )


class UploadPart(Base):
    __tablename__ = "upload_parts"
    upload_id = identifier(ForeignKey("uploads.id"), primary_key=True)
    part_no = Column(Integer, primary_key=True)
    size_bytes = Column(BigInteger, nullable=False)
    sha256 = string(64, nullable=False)
    object_key = exact_key(512, nullable=False, unique=True)
    __table_args__ = (CheckConstraint("part_no > 0 AND size_bytes > 0", name="part_positive"), hash_check("sha256"))


class ConsultationThread(Base):
    __tablename__ = "consultation_threads"
    id = primary_id()
    space_id = identifier(ForeignKey("spaces.id"), nullable=False, index=True)
    owner_id = identifier(ForeignKey("users.id"), nullable=False)
    title = string(500, nullable=False)
    deleted_at = timestamp(nullable=True)
    created_at = timestamp(default_now=True)
    __table_args__ = (Index("threads_owner", "owner_id", "deleted_at", "created_at", "id"),)


class ConsultationRun(Base):
    __tablename__ = "consultation_runs"
    id = primary_id()
    thread_id = identifier(ForeignKey("consultation_threads.id"), nullable=False)
    parent_run_id = identifier(nullable=True)
    state = string(16, nullable=False)
    mode = string(8, nullable=False)
    request = json_column("object")
    response = json_column("object", nullable=True)
    evidence_snapshot = json_column("array", default=list)
    policy_snapshot = json_column("object", default=dict)
    model_snapshot = json_column("object", default=dict)
    invalidated_at = timestamp(nullable=True)
    error_code = string(100, nullable=True)
    created_at = timestamp(default_now=True)
    completed_at = timestamp(nullable=True)
    __table_args__ = (
        UniqueConstraint("thread_id", "id", name="uq_run_thread_id"),
        ForeignKeyConstraint(["thread_id", "parent_run_id"], ["consultation_runs.thread_id", "consultation_runs.id"]),
        choices("state", "QUEUED RUNNING COMPLETED FAILED CANCELLED"), choices("mode", "auto answer solution"),
        CheckConstraint("state <> 'COMPLETED' OR response IS NOT NULL", name="completed_response"),
        json_check("request", "object"), json_check("response", "object"), json_check("evidence_snapshot", "array"),
        json_check("policy_snapshot", "object"), json_check("model_snapshot", "object"),
        Index("runs_thread", "thread_id", "created_at", "id"), Index("ix_run_parent", "thread_id", "parent_run_id"),
    )


class RunEvidence(Base):
    __tablename__ = "run_evidence"
    run_id = identifier(ForeignKey("consultation_runs.id"), primary_key=True)
    version_id = identifier(primary_key=True)
    block_id = identifier(primary_key=True)
    resource_access_epoch = Column(BigInteger, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["version_id", "block_id"], ["content_blocks.version_id", "content_blocks.block_id"]),
        CheckConstraint("resource_access_epoch > 0", name="epoch_positive"),
        Index("run_evidence_version", "version_id", "block_id"),
    )


class Feedback(Base):
    __tablename__ = "feedback"
    id = primary_id()
    run_id = identifier(ForeignKey("consultation_runs.id"), nullable=False, index=True)
    user_id = identifier(ForeignKey("users.id"), nullable=False, index=True)
    kind = string(16, nullable=False)
    comment = empty_text()
    created_at = timestamp(default_now=True)
    __table_args__ = (choices("kind", "HELPFUL WRONG MISSING OUTDATED OTHER"),)


class IssueCase(RevisionMixin, Base):
    __tablename__ = "issue_cases"
    id = primary_id()
    space_id = identifier(ForeignKey("spaces.id"), nullable=False)
    run_id = identifier(ForeignKey("consultation_runs.id"), nullable=True, index=True)
    creator_id = identifier(ForeignKey("users.id"), nullable=False, index=True)
    assignee_id = identifier(ForeignKey("users.id"), nullable=True, index=True)
    title = string(500, nullable=False)
    description = Column(Text, nullable=False)
    state = string(16, nullable=False)
    resolution = empty_text()
    created_at = timestamp(default_now=True)
    updated_at = timestamp(default_now=True, update=True)
    __table_args__ = (choices("state", "OPEN ASSIGNED RESOLVED CLOSED"),
                      CheckConstraint("revision > 0", name="revision_positive"),
                      Index("issues_space_state", "space_id", "state", "created_at", "id"))


class Job(Base):
    __tablename__ = "jobs"
    id = primary_id()
    kind = string(16, nullable=False)
    owner_id = identifier(ForeignKey("users.id"), nullable=False, index=True)
    resource_id = identifier(ForeignKey("resources.id"), nullable=True, index=True)
    version_id = identifier(ForeignKey("resource_versions.id"), nullable=True, index=True)
    run_id = identifier(ForeignKey("consultation_runs.id"), nullable=True, index=True)
    state = string(16, nullable=False)
    stage = empty_text()
    dedupe_key = exact_key(512, nullable=False, unique=True)
    payload = json_column("object", default=dict)
    result = json_column("object", nullable=True)
    error_code = string(100, nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    lease_until = timestamp(nullable=True)
    cancel_requested = flag("cancel_requested")
    created_at = timestamp(default_now=True)
    updated_at = timestamp(default_now=True, update=True)
    __table_args__ = (
        choices("kind", "SCAN_PARSE COMPILE PUBLISH ANSWER EXPORT INVALIDATE PURGE"),
        choices("state", "QUEUED RUNNING SUCCEEDED FAILED CANCELLED"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        json_check("payload", "object"), json_check("result", "object"), Index("jobs_recovery", "state", "lease_until"),
    )


class Outbox(Base):
    __tablename__ = "outbox"
    id = primary_id()
    event_type = string(100, nullable=False)
    aggregate_id = identifier(nullable=False)
    payload = json_column("object")
    dispatched_at = timestamp(nullable=True)
    created_at = timestamp(default_now=True)
    __table_args__ = (json_check("payload", "object"), Index("outbox_pending", "dispatched_at", "created_at", "id"))


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    actor_id = identifier(ForeignKey("users.id"), primary_key=True)
    http_method = exact_key(16, primary_key=True)
    route = exact_key(512, primary_key=True)
    key = exact_key(128, primary_key=True)
    request_sha256 = string(64, nullable=False)
    state = string(16, nullable=False)
    status_code = Column(Integer, nullable=True)
    response = json_column(nullable=True)
    expires_at = timestamp()
    __table_args__ = (hash_check("request_sha256"), choices("state", "STARTED COMPLETED"),
                      json_check("response"), Index("ix_idempotency_expiry", "expires_at"))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = primary_id()
    actor_id = identifier(ForeignKey("users.id"), nullable=True, index=True)
    action = string(100, nullable=False)
    object_type = string(64, nullable=False)
    object_id = identifier(nullable=True)
    trace_id = string(128, nullable=False)
    outcome = string(16, nullable=False)
    details = json_column("object", default=dict)
    created_at = timestamp(default_now=True)
    __table_args__ = (choices("outcome", "SUCCESS DENIED FAILED"), json_check("details", "object"),
                      Index("audit_lookup", "object_type", "object_id", "created_at"))


class RuntimePolicy(RevisionMixin, Base):
    __tablename__ = "runtime_policies"
    id = primary_id()
    name = exact_key(200, nullable=False, unique=True)
    config = json_column("object")
    updated_by = identifier(ForeignKey("users.id"), nullable=False, index=True)
    updated_at = timestamp(default_now=True, update=True)
    __table_args__ = (json_check("config", "object"), CheckConstraint("revision > 0", name="revision_positive"))


class LoginSession(Base):
    __tablename__ = "login_sessions"
    id = primary_id()
    user_id = identifier(ForeignKey("users.id"), nullable=False, index=True)
    token_hash = string(64, nullable=True, unique=True)
    csrf_token = string(128, nullable=False, default=lambda: secrets.token_urlsafe(32))
    expires_at = timestamp()
    revoked_at = timestamp(nullable=True)
    created_at = timestamp(default_now=True)
    last_seen_at = timestamp(nullable=True)
    __table_args__ = (hash_check("token_hash"), Index("ix_session_expiry", "expires_at"))
