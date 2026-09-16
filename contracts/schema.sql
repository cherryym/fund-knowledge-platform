-- fund_kb v2.0 reference initial migration; PostgreSQL. No production data.
-- UUIDs are allocated by application. UTC timestamptz, dates are business dates.
-- Run only on a new development database after reviewing this migration.
BEGIN;
CREATE SCHEMA fund_kb;
SET LOCAL search_path = fund_kb, public;

CREATE TABLE users (
    id uuid PRIMARY KEY,
    external_subject text NOT NULL UNIQUE,
    display_name text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE spaces (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE space_members (
    space_id uuid NOT NULL REFERENCES spaces(id),
    user_id uuid NOT NULL REFERENCES users(id),
    role text NOT NULL CHECK (role IN ('reader','editor','reviewer','publisher','admin')),
    PRIMARY KEY (space_id,user_id,role)
);
CREATE TABLE resources (
    id uuid PRIMARY KEY,
    space_id uuid NOT NULL REFERENCES spaces(id),
    kind text NOT NULL CHECK (kind IN ('document','knowledge','template')),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 300),
    category text NOT NULL DEFAULT '未分类',
    tags text[] NOT NULL DEFAULT '{}',
    owner_id uuid NOT NULL REFERENCES users(id),
    restricted boolean NOT NULL DEFAULT false,
    classification text NOT NULL DEFAULT 'INTERNAL'
        CHECK (classification IN ('PUBLIC','INTERNAL','CONFIDENTIAL','RESTRICTED')),
    access_epoch bigint NOT NULL DEFAULT 1,
    revision bigint NOT NULL DEFAULT 1,
    active_release_id uuid,
    suspended boolean NOT NULL DEFAULT false,
    deleted_at timestamptz,
    retain_until timestamptz,
    legal_hold boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (space_id,id)
);
CREATE TABLE resource_grants (
    resource_id uuid NOT NULL REFERENCES resources(id),
    user_id uuid NOT NULL REFERENCES users(id),
    permission text NOT NULL CHECK (permission IN ('read','download','edit','review','publish','manage')),
    PRIMARY KEY (resource_id,user_id,permission)
);
CREATE TABLE blobs (
    id uuid PRIMARY KEY,
    space_id uuid NOT NULL REFERENCES spaces(id),
    object_key text NOT NULL UNIQUE,
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    mime_type text NOT NULL,
    scan_state text NOT NULL CHECK (scan_state IN ('QUARANTINED','CLEAN','REJECTED')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE resource_versions (
    id uuid PRIMARY KEY,
    resource_id uuid NOT NULL REFERENCES resources(id),
    version_no integer NOT NULL CHECK (version_no > 0),
    base_version_id uuid,
    state text NOT NULL DEFAULT 'DRAFT'
        CHECK (state IN ('DRAFT','IN_REVIEW','APPROVED','REJECTED')),
    author_id uuid NOT NULL REFERENCES users(id),
    title text NOT NULL,
    knowledge_type text NOT NULL DEFAULT 'source'
        CHECK (knowledge_type IN ('source','faq','rule','sop','scenario','case','term','solution_template')),
    origin text NOT NULL CHECK (origin IN ('UPLOAD','HUMAN','AI_DRAFT','COPY')),
    source_blob_id uuid REFERENCES blobs(id),
    source_url text,
    source_verified boolean NOT NULL DEFAULT false,
    legal_status text NOT NULL DEFAULT 'UNKNOWN'
        CHECK (legal_status IN ('UNKNOWN','NOT_APPLICABLE','FUTURE','EFFECTIVE','PARTIAL','REPEALED')),
    valid_from date,
    valid_to date,
    applicability jsonb NOT NULL DEFAULT '{}',
    required_facts jsonb NOT NULL DEFAULT '[]',
    change_kind text NOT NULL DEFAULT 'UPDATE' CHECK (change_kind IN ('UPDATE','CORRECTION')),
    change_reason text NOT NULL DEFAULT '',
    content_sha256 text CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (resource_id,version_no),
    UNIQUE (resource_id,id),
    FOREIGN KEY (resource_id,base_version_id) REFERENCES resource_versions(resource_id,id),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from),
    CHECK (jsonb_typeof(applicability)='object'),
    CHECK (jsonb_typeof(required_facts)='array'),
    CHECK (state='DRAFT' OR content_sha256 IS NOT NULL)
);
-- Only one active draft per author/resource. Rejected revisions are copied.
CREATE UNIQUE INDEX one_author_draft ON resource_versions(resource_id,author_id) WHERE state='DRAFT';
CREATE TABLE content_blocks (
    version_id uuid NOT NULL REFERENCES resource_versions(id),
    block_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    block_type text NOT NULL CHECK (block_type IN
        ('heading','paragraph','list','table','step','warning','formula','image','attachment')),
    data jsonb NOT NULL,
    search_text text NOT NULL DEFAULT '',
    locator jsonb NOT NULL DEFAULT '{}',
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    PRIMARY KEY (version_id,block_id),
    UNIQUE (version_id,ordinal),
    CHECK (jsonb_typeof(data)='object'),
    CHECK (jsonb_typeof(locator)='object')
);
CREATE TABLE evidence_links (
    id uuid PRIMARY KEY,
    from_version_id uuid NOT NULL,
    from_block_id uuid NOT NULL,
    to_version_id uuid NOT NULL,
    to_block_id uuid NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('RULE','INTERNAL_OPINION','FACT','CASE','CALCULATION')),
    FOREIGN KEY (from_version_id,from_block_id) REFERENCES content_blocks(version_id,block_id),
    FOREIGN KEY (to_version_id,to_block_id) REFERENCES content_blocks(version_id,block_id),
    UNIQUE (from_version_id,from_block_id,to_version_id,to_block_id,purpose),
    CHECK (from_version_id <> to_version_id OR from_block_id <> to_block_id)
);
CREATE TABLE relation_edges (
    id uuid PRIMARY KEY,
    source_version_id uuid NOT NULL REFERENCES resource_versions(id),
    target_resource_id uuid NOT NULL REFERENCES resources(id),
    relation_type text NOT NULL CHECK (relation_type IN
        ('CITES','EXPLAINS','APPLIES_TO','REQUIRES','EXCEPTION_OF','DEPENDS_ON','SUPERSEDES')),
    conditions jsonb NOT NULL DEFAULT '{}',
    evidence_version_id uuid,
    evidence_block_id uuid,
    FOREIGN KEY (evidence_version_id,evidence_block_id) REFERENCES content_blocks(version_id,block_id),
    CHECK ((evidence_version_id IS NULL)=(evidence_block_id IS NULL))
);
CREATE TABLE review_decisions (
    id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES resource_versions(id),
    reviewer_id uuid NOT NULL REFERENCES users(id),
    decision text NOT NULL CHECK (decision IN ('APPROVE','REJECT')),
    reviewed_sha256 text NOT NULL CHECK (reviewed_sha256 ~ '^[a-f0-9]{64}$'),
    comment text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(version_id,reviewer_id)
);
CREATE TABLE releases (
    id uuid PRIMARY KEY,
    resource_id uuid NOT NULL REFERENCES resources(id),
    version_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('PREPARING','ACTIVE','SUPERSEDED','FAILED')),
    manifest jsonb NOT NULL DEFAULT '{}',
    publisher_id uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    UNIQUE(resource_id,id),
    FOREIGN KEY(resource_id,version_id) REFERENCES resource_versions(resource_id,id)
);
CREATE UNIQUE INDEX one_active_release ON releases(resource_id) WHERE state='ACTIVE';
ALTER TABLE resources ADD CONSTRAINT resource_release_ownership
    FOREIGN KEY(id,active_release_id) REFERENCES releases(resource_id,id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE uploads (
    id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES resource_versions(id),
    user_id uuid NOT NULL REFERENCES users(id),
    filename text NOT NULL,
    declared_size bigint NOT NULL CHECK (declared_size BETWEEN 1 AND 104857600),
    part_size integer NOT NULL DEFAULT 8388608,
    part_count integer NOT NULL CHECK (part_count BETWEEN 1 AND 13),
    expected_sha256 text CHECK (expected_sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL CHECK (state IN ('OPEN','SEALED','CANCELLED','EXPIRED')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_open_upload ON uploads(version_id) WHERE state='OPEN';
CREATE TABLE upload_parts (
    upload_id uuid NOT NULL REFERENCES uploads(id),
    part_no integer NOT NULL CHECK (part_no > 0),
    size_bytes bigint NOT NULL CHECK(size_bytes>0),
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    object_key text NOT NULL UNIQUE,
    PRIMARY KEY(upload_id,part_no)
);
CREATE TABLE consultation_threads (
    id uuid PRIMARY KEY,
    space_id uuid NOT NULL REFERENCES spaces(id),
    owner_id uuid NOT NULL REFERENCES users(id),
    title text NOT NULL,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE consultation_runs (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES consultation_threads(id),
    parent_run_id uuid,
    state text NOT NULL CHECK (state IN ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED')),
    mode text NOT NULL CHECK(mode IN ('auto','answer','solution')),
    request jsonb NOT NULL,
    response jsonb,
    evidence_snapshot jsonb NOT NULL DEFAULT '[]',
    policy_snapshot jsonb NOT NULL DEFAULT '{}',
    model_snapshot jsonb NOT NULL DEFAULT '{}',
    invalidated_at timestamptz,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE(thread_id,id),
    FOREIGN KEY(thread_id,parent_run_id) REFERENCES consultation_runs(thread_id,id),
    CHECK (state <> 'COMPLETED' OR response IS NOT NULL)
);
CREATE TABLE run_evidence (
    run_id uuid NOT NULL REFERENCES consultation_runs(id),
    version_id uuid NOT NULL,
    block_id uuid NOT NULL,
    resource_access_epoch bigint NOT NULL,
    PRIMARY KEY(run_id,version_id,block_id),
    FOREIGN KEY(version_id,block_id) REFERENCES content_blocks(version_id,block_id)
);
CREATE TABLE feedback (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES consultation_runs(id),
    user_id uuid NOT NULL REFERENCES users(id),
    kind text NOT NULL CHECK(kind IN ('HELPFUL','WRONG','MISSING','OUTDATED','OTHER')),
    comment text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE issue_cases (
    id uuid PRIMARY KEY,
    space_id uuid NOT NULL REFERENCES spaces(id),
    run_id uuid REFERENCES consultation_runs(id),
    creator_id uuid NOT NULL REFERENCES users(id),
    assignee_id uuid REFERENCES users(id),
    title text NOT NULL,
    description text NOT NULL,
    state text NOT NULL CHECK(state IN ('OPEN','ASSIGNED','RESOLVED','CLOSED')),
    resolution text NOT NULL DEFAULT '',
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    kind text NOT NULL CHECK(kind IN ('SCAN_PARSE','COMPILE','PUBLISH','ANSWER','EXPORT','INVALIDATE','PURGE')),
    owner_id uuid NOT NULL REFERENCES users(id),
    resource_id uuid REFERENCES resources(id),
    version_id uuid REFERENCES resource_versions(id),
    run_id uuid REFERENCES consultation_runs(id),
    state text NOT NULL CHECK(state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
    stage text NOT NULL DEFAULT '',
    dedupe_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL DEFAULT '{}',
    result jsonb,
    error_code text,
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
    lease_until timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE outbox (
    id uuid PRIMARY KEY,
    event_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    payload jsonb NOT NULL,
    dispatched_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE idempotency_records (
    actor_id uuid NOT NULL REFERENCES users(id),
    http_method text NOT NULL,
    route text NOT NULL,
    key text NOT NULL,
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL CHECK(state IN ('STARTED','COMPLETED')),
    status_code integer,
    response jsonb,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY(actor_id,http_method,route,key)
);
CREATE TABLE audit_events (
    id uuid PRIMARY KEY,
    actor_id uuid REFERENCES users(id),
    action text NOT NULL,
    object_type text NOT NULL,
    object_id uuid,
    trace_id text NOT NULL,
    outcome text NOT NULL CHECK(outcome IN ('SUCCESS','DENIED','FAILED')),
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE runtime_policies (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE,
    revision bigint NOT NULL DEFAULT 1,
    config jsonb NOT NULL,
    updated_by uuid NOT NULL REFERENCES users(id),
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- Policy config contains model/secret REFERENCE IDs, not secret values.
-- All cross-space references require explicit service-layer authorization.
-- Services must freeze blocks/edges/citations when versions leave DRAFT.
-- Approval requires reviewer != author and reviewed_sha256 == content_sha256.
-- Reject cyclic source dependencies before publication; no automatic cascading delete.
-- Resource restore MUST leave suspended=true. Read/write authorization on every endpoint.
-- No application credentials/roles created here; grant least privileges separately.
-- audit_events: runtime application role INSERT/SELECT only. WORM is an external deployment control.

CREATE INDEX resources_space_live ON resources(space_id,kind,created_at,id) WHERE deleted_at IS NULL;
CREATE INDEX resources_tags ON resources USING gin(tags);
CREATE INDEX versions_resource ON resource_versions(resource_id,version_no DESC);
CREATE INDEX evidence_incoming ON evidence_links(to_version_id,to_block_id);
CREATE INDEX relations_target ON relation_edges(target_resource_id);
CREATE INDEX threads_owner ON consultation_threads(owner_id,created_at,id) WHERE deleted_at IS NULL;
CREATE INDEX runs_thread ON consultation_runs(thread_id,created_at,id);
CREATE INDEX run_evidence_version ON run_evidence(version_id);
CREATE INDEX issues_space_state ON issue_cases(space_id,state,created_at,id);
CREATE INDEX jobs_recovery ON jobs(state,lease_until);
CREATE INDEX outbox_pending ON outbox(created_at) WHERE dispatched_at IS NULL;
CREATE INDEX audit_lookup ON audit_events(object_type,object_id,created_at);
COMMIT;
