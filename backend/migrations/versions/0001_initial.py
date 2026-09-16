"""Frozen initial schema: 24 design tables and login_sessions.

No live ORM metadata import: future model changes require new migrations.
"""
import sqlalchemy as sa
from alembic import op

from fund_kb import db

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Frozen revision; changing runtime models does not modify this schema.
    op.create_table('outbox',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('event_type', db.BoundedString(100), nullable=False),
    sa.Column('aggregate_id', db.UUIDString(), nullable=False),
    sa.Column('payload', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('dispatched_at', db.UTCDateTime(), nullable=True),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.CheckConstraint(db.JSONShape('payload', 'object'), name=op.f('ck_outbox_payload_json')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outbox'))
    )
    op.create_index('outbox_pending', 'outbox', ['dispatched_at', 'created_at', 'id'], unique=False)
    op.create_table('spaces',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('name', db.BoundedString(200), nullable=False),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('revision', sa.BigInteger(), server_default='1', nullable=False),
    sa.CheckConstraint('length(name) BETWEEN 1 AND 200', name=op.f('ck_spaces_name_length')),
    sa.CheckConstraint('revision > 0', name=op.f('ck_spaces_revision_positive')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_spaces'))
    )
    op.create_table('users',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('external_subject', db.ExactKey(512), nullable=False),
    sa.Column('display_name', db.BoundedString(200), nullable=False),
    sa.Column('active', sa.Boolean(create_constraint=True, name='active_bool'), server_default=sa.text('1'), nullable=False),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('external_subject', name=op.f('uq_users_external_subject'))
    )
    op.create_table('audit_events',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('actor_id', db.UUIDString(), nullable=True),
    sa.Column('action', db.BoundedString(100), nullable=False),
    sa.Column('object_type', db.BoundedString(64), nullable=False),
    sa.Column('object_id', db.UUIDString(), nullable=True),
    sa.Column('trace_id', db.BoundedString(128), nullable=False),
    sa.Column('outcome', db.BoundedString(16), nullable=False),
    sa.Column('details', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.CheckConstraint("outcome IN ('SUCCESS','DENIED','FAILED')", name=op.f('ck_audit_events_outcome_values')),
    sa.CheckConstraint(db.JSONShape('details', 'object'), name=op.f('ck_audit_events_details_json')),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name=op.f('fk_audit_events_actor_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    op.create_index('audit_lookup', 'audit_events', ['object_type', 'object_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_audit_events_actor_id'), 'audit_events', ['actor_id'], unique=False)
    op.create_table('blobs',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('space_id', db.UUIDString(), nullable=False),
    sa.Column('object_key', db.ExactKey(512), nullable=False),
    sa.Column('sha256', db.BoundedString(64), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('mime_type', db.BoundedString(200), nullable=False),
    sa.Column('scan_state', db.BoundedString(16), nullable=False),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.CheckConstraint("length(sha256) = 64 AND coalesce(length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256,'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),'8',''),'9',''),'a',''),'b',''),'c',''),'d',''),'e',''),'f','')),0) = 0", name=op.f('ck_blobs_sha256_hex')),
    sa.CheckConstraint("scan_state IN ('QUARANTINED','CLEAN','REJECTED')", name=op.f('ck_blobs_scan_state_values')),
    sa.CheckConstraint('size_bytes >= 0', name=op.f('ck_blobs_size_nonnegative')),
    sa.ForeignKeyConstraint(['space_id'], ['spaces.id'], name=op.f('fk_blobs_space_id_spaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_blobs')),
    sa.UniqueConstraint('object_key', name=op.f('uq_blobs_object_key'))
    )
    op.create_index(op.f('ix_blobs_space_id'), 'blobs', ['space_id'], unique=False)
    op.create_table('consultation_threads',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('space_id', db.UUIDString(), nullable=False),
    sa.Column('owner_id', db.UUIDString(), nullable=False),
    sa.Column('title', db.BoundedString(500), nullable=False),
    sa.Column('deleted_at', db.UTCDateTime(), nullable=True),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_consultation_threads_owner_id_users')),
    sa.ForeignKeyConstraint(['space_id'], ['spaces.id'], name=op.f('fk_consultation_threads_space_id_spaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_consultation_threads'))
    )
    op.create_index(op.f('ix_consultation_threads_space_id'), 'consultation_threads', ['space_id'], unique=False)
    op.create_index('threads_owner', 'consultation_threads', ['owner_id', 'deleted_at', 'created_at', 'id'], unique=False)
    op.create_table('idempotency_records',
    sa.Column('actor_id', db.UUIDString(), nullable=False),
    sa.Column('http_method', db.ExactKey(16), nullable=False),
    sa.Column('route', db.ExactKey(512), nullable=False),
    sa.Column('key', db.ExactKey(128), nullable=False),
    sa.Column('request_sha256', db.BoundedString(64), nullable=False),
    sa.Column('state', db.BoundedString(16), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=True),
    sa.Column('response', db.JSONDocument('any', string_items=False), nullable=True),
    sa.Column('expires_at', db.UTCDateTime(), nullable=False),
    sa.CheckConstraint("length(request_sha256) = 64 AND coalesce(length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(request_sha256,'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),'8',''),'9',''),'a',''),'b',''),'c',''),'d',''),'e',''),'f','')),0) = 0", name=op.f('ck_idempotency_records_request_sha256_hex')),
    sa.CheckConstraint("state IN ('STARTED','COMPLETED')", name=op.f('ck_idempotency_records_state_values')),
    sa.CheckConstraint(db.JSONShape('response', 'any'), name=op.f('ck_idempotency_records_response_json')),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name=op.f('fk_idempotency_records_actor_id_users')),
    sa.PrimaryKeyConstraint('actor_id', 'http_method', 'route', 'key', name=op.f('pk_idempotency_records'))
    )
    op.create_index('ix_idempotency_expiry', 'idempotency_records', ['expires_at'], unique=False)
    op.create_table('login_sessions',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('user_id', db.UUIDString(), nullable=False),
    sa.Column('token_hash', db.BoundedString(64), nullable=True),
    sa.Column('csrf_token', db.BoundedString(128), nullable=False),
    sa.Column('expires_at', db.UTCDateTime(), nullable=False),
    sa.Column('revoked_at', db.UTCDateTime(), nullable=True),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('last_seen_at', db.UTCDateTime(), nullable=True),
    sa.CheckConstraint("length(token_hash) = 64 AND coalesce(length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(token_hash,'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),'8',''),'9',''),'a',''),'b',''),'c',''),'d',''),'e',''),'f','')),0) = 0", name=op.f('ck_login_sessions_token_hash_hex')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_login_sessions_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_login_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_login_sessions_token_hash'))
    )
    op.create_index(op.f('ix_login_sessions_user_id'), 'login_sessions', ['user_id'], unique=False)
    op.create_index('ix_session_expiry', 'login_sessions', ['expires_at'], unique=False)
    op.create_table('resources',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('space_id', db.UUIDString(), nullable=False),
    sa.Column('kind', db.BoundedString(16), nullable=False),
    sa.Column('name', db.BoundedString(300), nullable=False),
    sa.Column('category', db.BoundedString(200), server_default='未分类', nullable=False),
    sa.Column('tags', db.JSONDocument('array', string_items=True), nullable=False),
    sa.Column('owner_id', db.UUIDString(), nullable=False),
    sa.Column('restricted', sa.Boolean(create_constraint=True, name='restricted_bool'), server_default=sa.text('0'), nullable=False),
    sa.Column('classification', db.BoundedString(16), server_default='INTERNAL', nullable=False),
    sa.Column('access_epoch', sa.BigInteger(), server_default='1', nullable=False),
    sa.Column('active_release_id', db.UUIDString(), nullable=True),
    sa.Column('suspended', sa.Boolean(create_constraint=True, name='suspended_bool'), server_default=sa.text('0'), nullable=False),
    sa.Column('deleted_at', db.UTCDateTime(), nullable=True),
    sa.Column('retain_until', db.UTCDateTime(), nullable=True),
    sa.Column('legal_hold', sa.Boolean(create_constraint=True, name='legal_hold_bool'), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('updated_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('revision', sa.BigInteger(), server_default='1', nullable=False),
    sa.CheckConstraint("classification IN ('PUBLIC','INTERNAL','CONFIDENTIAL','RESTRICTED')", name=op.f('ck_resources_classification_values')),
    sa.CheckConstraint("kind IN ('document','knowledge','template')", name=op.f('ck_resources_kind_values')),
    sa.CheckConstraint('length(name) BETWEEN 1 AND 300', name=op.f('ck_resources_name_length')),
    sa.CheckConstraint('revision > 0 AND access_epoch > 0', name=op.f('ck_resources_revisions_positive')),
    sa.CheckConstraint(db.JSONShape('tags', 'array'), name=op.f('ck_resources_tags_json')),
    sa.ForeignKeyConstraint(['id', 'active_release_id'], ['releases.resource_id', 'releases.id'], name='fk_resource_release_owner', use_alter=True),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_resources_owner_id_users')),
    sa.ForeignKeyConstraint(['space_id'], ['spaces.id'], name=op.f('fk_resources_space_id_spaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resources')),
    sa.UniqueConstraint('space_id', 'id', name='uq_resource_space_id')
    )
    op.create_index('ix_resource_active_release', 'resources', ['active_release_id'], unique=False)
    op.create_index('ix_resource_owner', 'resources', ['owner_id'], unique=False)
    op.create_index('resources_space_live', 'resources', ['space_id', 'kind', 'deleted_at', 'created_at', 'id'], unique=False)
    op.create_table('runtime_policies',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('name', db.ExactKey(200), nullable=False),
    sa.Column('config', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('updated_by', db.UUIDString(), nullable=False),
    sa.Column('updated_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('revision', sa.BigInteger(), server_default='1', nullable=False),
    sa.CheckConstraint('revision > 0', name=op.f('ck_runtime_policies_revision_positive')),
    sa.CheckConstraint(db.JSONShape('config', 'object'), name=op.f('ck_runtime_policies_config_json')),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_runtime_policies_updated_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_runtime_policies')),
    sa.UniqueConstraint('name', name=op.f('uq_runtime_policies_name'))
    )
    op.create_index(op.f('ix_runtime_policies_updated_by'), 'runtime_policies', ['updated_by'], unique=False)
    op.create_table('space_members',
    sa.Column('space_id', db.UUIDString(), nullable=False),
    sa.Column('user_id', db.UUIDString(), nullable=False),
    sa.Column('role', db.BoundedString(16), nullable=False),
    sa.CheckConstraint("role IN ('reader','editor','reviewer','publisher','admin')", name=op.f('ck_space_members_role_values')),
    sa.ForeignKeyConstraint(['space_id'], ['spaces.id'], name=op.f('fk_space_members_space_id_spaces')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_space_members_user_id_users')),
    sa.PrimaryKeyConstraint('space_id', 'user_id', 'role', name=op.f('pk_space_members'))
    )
    op.create_index('ix_members_user', 'space_members', ['user_id'], unique=False)
    op.create_table('consultation_runs',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('thread_id', db.UUIDString(), nullable=False),
    sa.Column('parent_run_id', db.UUIDString(), nullable=True),
    sa.Column('state', db.BoundedString(16), nullable=False),
    sa.Column('mode', db.BoundedString(8), nullable=False),
    sa.Column('request', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('response', db.JSONDocument('object', string_items=False), nullable=True),
    sa.Column('evidence_snapshot', db.JSONDocument('array', string_items=False), nullable=False),
    sa.Column('policy_snapshot', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('model_snapshot', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('invalidated_at', db.UTCDateTime(), nullable=True),
    sa.Column('error_code', db.BoundedString(100), nullable=True),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('completed_at', db.UTCDateTime(), nullable=True),
    sa.CheckConstraint("mode IN ('auto','answer','solution')", name=op.f('ck_consultation_runs_mode_values')),
    sa.CheckConstraint("state <> 'COMPLETED' OR response IS NOT NULL", name=op.f('ck_consultation_runs_completed_response')),
    sa.CheckConstraint("state IN ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED')", name=op.f('ck_consultation_runs_state_values')),
    sa.CheckConstraint(db.JSONShape('evidence_snapshot', 'array'), name=op.f('ck_consultation_runs_evidence_snapshot_json')),
    sa.CheckConstraint(db.JSONShape('model_snapshot', 'object'), name=op.f('ck_consultation_runs_model_snapshot_json')),
    sa.CheckConstraint(db.JSONShape('policy_snapshot', 'object'), name=op.f('ck_consultation_runs_policy_snapshot_json')),
    sa.CheckConstraint(db.JSONShape('request', 'object'), name=op.f('ck_consultation_runs_request_json')),
    sa.CheckConstraint(db.JSONShape('response', 'object'), name=op.f('ck_consultation_runs_response_json')),
    sa.ForeignKeyConstraint(['thread_id', 'parent_run_id'], ['consultation_runs.thread_id', 'consultation_runs.id'], name=op.f('fk_consultation_runs_thread_id_consultation_runs')),
    sa.ForeignKeyConstraint(['thread_id'], ['consultation_threads.id'], name=op.f('fk_consultation_runs_thread_id_consultation_threads')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_consultation_runs')),
    sa.UniqueConstraint('thread_id', 'id', name='uq_run_thread_id')
    )
    op.create_index('ix_run_parent', 'consultation_runs', ['thread_id', 'parent_run_id'], unique=False)
    op.create_index('runs_thread', 'consultation_runs', ['thread_id', 'created_at', 'id'], unique=False)
    op.create_table('resource_grants',
    sa.Column('resource_id', db.UUIDString(), nullable=False),
    sa.Column('user_id', db.UUIDString(), nullable=False),
    sa.Column('permission', db.BoundedString(16), nullable=False),
    sa.CheckConstraint("permission IN ('read','download','edit','review','publish','manage')", name=op.f('ck_resource_grants_permission_values')),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], name=op.f('fk_resource_grants_resource_id_resources')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_resource_grants_user_id_users')),
    sa.PrimaryKeyConstraint('resource_id', 'user_id', 'permission', name=op.f('pk_resource_grants'))
    )
    op.create_index('ix_grants_user', 'resource_grants', ['user_id'], unique=False)
    op.create_table('resource_versions',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('resource_id', db.UUIDString(), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('base_version_id', db.UUIDString(), nullable=True),
    sa.Column('state', db.BoundedString(16), server_default='DRAFT', nullable=False),
    sa.Column('author_id', db.UUIDString(), nullable=False),
    sa.Column('title', db.BoundedString(500), nullable=False),
    sa.Column('knowledge_type', db.BoundedString(32), server_default='source', nullable=False),
    sa.Column('origin', db.BoundedString(16), nullable=False),
    sa.Column('source_blob_id', db.UUIDString(), nullable=True),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('source_verified', sa.Boolean(create_constraint=True, name='source_verified_bool'), server_default=sa.text('0'), nullable=False),
    sa.Column('legal_status', db.BoundedString(24), server_default='UNKNOWN', nullable=False),
    sa.Column('valid_from', sa.Date(), nullable=True),
    sa.Column('valid_to', sa.Date(), nullable=True),
    sa.Column('applicability', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('required_facts', db.JSONDocument('array', string_items=False), nullable=False),
    sa.Column('change_kind', db.BoundedString(16), server_default='UPDATE', nullable=False),
    sa.Column('change_reason', db.EmptyText(), nullable=True),
    sa.Column('content_sha256', db.BoundedString(64), nullable=True),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('draft_resource_id', db.UUIDString(), sa.Computed("CASE WHEN state = 'DRAFT' THEN resource_id ELSE NULL END", ), nullable=True),
    sa.Column('draft_author_id', db.UUIDString(), sa.Computed("CASE WHEN state = 'DRAFT' THEN author_id ELSE NULL END", ), nullable=True),
    sa.Column('revision', sa.BigInteger(), server_default='1', nullable=False),
    sa.CheckConstraint("change_kind IN ('UPDATE','CORRECTION')", name=op.f('ck_resource_versions_change_kind_values')),
    sa.CheckConstraint("knowledge_type IN ('source','faq','rule','sop','scenario','case','term','solution_template')", name=op.f('ck_resource_versions_knowledge_type_values')),
    sa.CheckConstraint("legal_status IN ('UNKNOWN','NOT_APPLICABLE','FUTURE','EFFECTIVE','PARTIAL','REPEALED')", name=op.f('ck_resource_versions_legal_status_values')),
    sa.CheckConstraint("length(content_sha256) = 64 AND coalesce(length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(content_sha256,'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),'8',''),'9',''),'a',''),'b',''),'c',''),'d',''),'e',''),'f','')),0) = 0", name=op.f('ck_resource_versions_content_sha256_hex')),
    sa.CheckConstraint("origin IN ('UPLOAD','HUMAN','AI_DRAFT','COPY')", name=op.f('ck_resource_versions_origin_values')),
    sa.CheckConstraint("state = 'DRAFT' OR content_sha256 IS NOT NULL", name=op.f('ck_resource_versions_submitted_hash')),
    sa.CheckConstraint("state IN ('DRAFT','IN_REVIEW','APPROVED','REJECTED')", name=op.f('ck_resource_versions_state_values')),
    sa.CheckConstraint('valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from', name=op.f('ck_resource_versions_valid_interval')),
    sa.CheckConstraint('version_no > 0 AND revision > 0', name=op.f('ck_resource_versions_numbers_positive')),
    sa.CheckConstraint(db.JSONShape('applicability', 'object'), name=op.f('ck_resource_versions_applicability_json')),
    sa.CheckConstraint(db.JSONShape('required_facts', 'array'), name=op.f('ck_resource_versions_required_facts_json')),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], name=op.f('fk_resource_versions_author_id_users')),
    sa.ForeignKeyConstraint(['resource_id', 'base_version_id'], ['resource_versions.resource_id', 'resource_versions.id'], name='fk_version_base_owner'),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], name=op.f('fk_resource_versions_resource_id_resources')),
    sa.ForeignKeyConstraint(['source_blob_id'], ['blobs.id'], name=op.f('fk_resource_versions_source_blob_id_blobs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resource_versions')),
    sa.UniqueConstraint('draft_resource_id', 'draft_author_id', name='one_author_draft'),
    sa.UniqueConstraint('resource_id', 'id', name='uq_version_resource_id'),
    sa.UniqueConstraint('resource_id', 'version_no', name='uq_version_number')
    )
    op.create_index(op.f('ix_resource_versions_author_id'), 'resource_versions', ['author_id'], unique=False)
    op.create_index(op.f('ix_resource_versions_source_blob_id'), 'resource_versions', ['source_blob_id'], unique=False)
    op.create_index('ix_version_base', 'resource_versions', ['resource_id', 'base_version_id'], unique=False)
    op.create_index('versions_resource', 'resource_versions', ['resource_id', 'version_no'], unique=False)
    op.create_table('content_blocks',
    sa.Column('version_id', db.UUIDString(), nullable=False),
    sa.Column('block_id', db.UUIDString(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('block_type', db.BoundedString(16), nullable=False),
    sa.Column('data', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('search_text', db.EmptyText(), nullable=True),
    sa.Column('locator', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('content_sha256', db.BoundedString(64), nullable=False),
    sa.CheckConstraint("block_type IN ('heading','paragraph','list','table','step','warning','formula','image','attachment')", name=op.f('ck_content_blocks_block_type_values')),
    sa.CheckConstraint("length(content_sha256) = 64 AND coalesce(length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(content_sha256,'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),'8',''),'9',''),'a',''),'b',''),'c',''),'d',''),'e',''),'f','')),0) = 0", name=op.f('ck_content_blocks_content_sha256_hex')),
    sa.CheckConstraint('ordinal >= 0', name=op.f('ck_content_blocks_ordinal_nonnegative')),
    sa.CheckConstraint(db.JSONShape('data', 'object'), name=op.f('ck_content_blocks_data_json')),
    sa.CheckConstraint(db.JSONShape('locator', 'object'), name=op.f('ck_content_blocks_locator_json')),
    sa.ForeignKeyConstraint(['version_id'], ['resource_versions.id'], name=op.f('fk_content_blocks_version_id_resource_versions')),
    sa.PrimaryKeyConstraint('version_id', 'block_id', name=op.f('pk_content_blocks')),
    sa.UniqueConstraint('version_id', 'ordinal', name='uq_block_ordinal')
    )
    op.create_table('feedback',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('run_id', db.UUIDString(), nullable=False),
    sa.Column('user_id', db.UUIDString(), nullable=False),
    sa.Column('kind', db.BoundedString(16), nullable=False),
    sa.Column('comment', db.EmptyText(), nullable=True),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.CheckConstraint("kind IN ('HELPFUL','WRONG','MISSING','OUTDATED','OTHER')", name=op.f('ck_feedback_kind_values')),
    sa.ForeignKeyConstraint(['run_id'], ['consultation_runs.id'], name=op.f('fk_feedback_run_id_consultation_runs')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_feedback_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_feedback'))
    )
    op.create_index(op.f('ix_feedback_run_id'), 'feedback', ['run_id'], unique=False)
    op.create_index(op.f('ix_feedback_user_id'), 'feedback', ['user_id'], unique=False)
    op.create_table('issue_cases',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('space_id', db.UUIDString(), nullable=False),
    sa.Column('run_id', db.UUIDString(), nullable=True),
    sa.Column('creator_id', db.UUIDString(), nullable=False),
    sa.Column('assignee_id', db.UUIDString(), nullable=True),
    sa.Column('title', db.BoundedString(500), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('state', db.BoundedString(16), nullable=False),
    sa.Column('resolution', db.EmptyText(), nullable=True),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('updated_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('revision', sa.BigInteger(), server_default='1', nullable=False),
    sa.CheckConstraint("state IN ('OPEN','ASSIGNED','RESOLVED','CLOSED')", name=op.f('ck_issue_cases_state_values')),
    sa.CheckConstraint('revision > 0', name=op.f('ck_issue_cases_revision_positive')),
    sa.ForeignKeyConstraint(['assignee_id'], ['users.id'], name=op.f('fk_issue_cases_assignee_id_users')),
    sa.ForeignKeyConstraint(['creator_id'], ['users.id'], name=op.f('fk_issue_cases_creator_id_users')),
    sa.ForeignKeyConstraint(['run_id'], ['consultation_runs.id'], name=op.f('fk_issue_cases_run_id_consultation_runs')),
    sa.ForeignKeyConstraint(['space_id'], ['spaces.id'], name=op.f('fk_issue_cases_space_id_spaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_issue_cases'))
    )
    op.create_index('issues_space_state', 'issue_cases', ['space_id', 'state', 'created_at', 'id'], unique=False)
    op.create_index(op.f('ix_issue_cases_assignee_id'), 'issue_cases', ['assignee_id'], unique=False)
    op.create_index(op.f('ix_issue_cases_creator_id'), 'issue_cases', ['creator_id'], unique=False)
    op.create_index(op.f('ix_issue_cases_run_id'), 'issue_cases', ['run_id'], unique=False)
    op.create_table('jobs',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('kind', db.BoundedString(16), nullable=False),
    sa.Column('owner_id', db.UUIDString(), nullable=False),
    sa.Column('resource_id', db.UUIDString(), nullable=True),
    sa.Column('version_id', db.UUIDString(), nullable=True),
    sa.Column('run_id', db.UUIDString(), nullable=True),
    sa.Column('state', db.BoundedString(16), nullable=False),
    sa.Column('stage', db.EmptyText(), nullable=True),
    sa.Column('dedupe_key', db.ExactKey(512), nullable=False),
    sa.Column('payload', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('result', db.JSONDocument('object', string_items=False), nullable=True),
    sa.Column('error_code', db.BoundedString(100), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('lease_until', db.UTCDateTime(), nullable=True),
    sa.Column('cancel_requested', sa.Boolean(create_constraint=True, name='cancel_requested_bool'), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('updated_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.CheckConstraint("kind IN ('SCAN_PARSE','COMPILE','PUBLISH','ANSWER','EXPORT','INVALIDATE','PURGE')", name=op.f('ck_jobs_kind_values')),
    sa.CheckConstraint("state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name=op.f('ck_jobs_state_values')),
    sa.CheckConstraint('attempts >= 0', name=op.f('ck_jobs_attempts_nonnegative')),
    sa.CheckConstraint(db.JSONShape('payload', 'object'), name=op.f('ck_jobs_payload_json')),
    sa.CheckConstraint(db.JSONShape('result', 'object'), name=op.f('ck_jobs_result_json')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_jobs_owner_id_users')),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], name=op.f('fk_jobs_resource_id_resources')),
    sa.ForeignKeyConstraint(['run_id'], ['consultation_runs.id'], name=op.f('fk_jobs_run_id_consultation_runs')),
    sa.ForeignKeyConstraint(['version_id'], ['resource_versions.id'], name=op.f('fk_jobs_version_id_resource_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs')),
    sa.UniqueConstraint('dedupe_key', name=op.f('uq_jobs_dedupe_key'))
    )
    op.create_index(op.f('ix_jobs_owner_id'), 'jobs', ['owner_id'], unique=False)
    op.create_index(op.f('ix_jobs_resource_id'), 'jobs', ['resource_id'], unique=False)
    op.create_index(op.f('ix_jobs_run_id'), 'jobs', ['run_id'], unique=False)
    op.create_index(op.f('ix_jobs_version_id'), 'jobs', ['version_id'], unique=False)
    op.create_index('jobs_recovery', 'jobs', ['state', 'lease_until'], unique=False)
    op.create_table('releases',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('resource_id', db.UUIDString(), nullable=False),
    sa.Column('version_id', db.UUIDString(), nullable=False),
    sa.Column('state', db.BoundedString(16), nullable=False),
    sa.Column('manifest', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('publisher_id', db.UUIDString(), nullable=False),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('activated_at', db.UTCDateTime(), nullable=True),
    sa.Column('active_resource_id', db.UUIDString(), sa.Computed("CASE WHEN state = 'ACTIVE' THEN resource_id ELSE NULL END", ), nullable=True),
    sa.CheckConstraint("state IN ('PREPARING','ACTIVE','SUPERSEDED','FAILED')", name=op.f('ck_releases_state_values')),
    sa.CheckConstraint(db.JSONShape('manifest', 'object'), name=op.f('ck_releases_manifest_json')),
    sa.ForeignKeyConstraint(['publisher_id'], ['users.id'], name=op.f('fk_releases_publisher_id_users')),
    sa.ForeignKeyConstraint(['resource_id', 'version_id'], ['resource_versions.resource_id', 'resource_versions.id'], name=op.f('fk_releases_resource_id_resource_versions')),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], name=op.f('fk_releases_resource_id_resources')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_releases')),
    sa.UniqueConstraint('active_resource_id', name='one_active_release'),
    sa.UniqueConstraint('resource_id', 'id', name='uq_release_resource_id')
    )
    op.create_index('ix_release_version', 'releases', ['resource_id', 'version_id'], unique=False)
    op.create_index(op.f('ix_releases_publisher_id'), 'releases', ['publisher_id'], unique=False)
    op.create_table('review_decisions',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('version_id', db.UUIDString(), nullable=False),
    sa.Column('reviewer_id', db.UUIDString(), nullable=False),
    sa.Column('decision', db.BoundedString(8), nullable=False),
    sa.Column('reviewed_sha256', db.BoundedString(64), nullable=False),
    sa.Column('comment', db.EmptyText(), nullable=True),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.CheckConstraint("decision IN ('APPROVE','REJECT')", name=op.f('ck_review_decisions_decision_values')),
    sa.CheckConstraint("length(reviewed_sha256) = 64 AND coalesce(length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(reviewed_sha256,'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),'8',''),'9',''),'a',''),'b',''),'c',''),'d',''),'e',''),'f','')),0) = 0", name=op.f('ck_review_decisions_reviewed_sha256_hex')),
    sa.ForeignKeyConstraint(['reviewer_id'], ['users.id'], name=op.f('fk_review_decisions_reviewer_id_users')),
    sa.ForeignKeyConstraint(['version_id'], ['resource_versions.id'], name=op.f('fk_review_decisions_version_id_resource_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_decisions')),
    sa.UniqueConstraint('version_id', 'reviewer_id', name='uq_review_person')
    )
    op.create_index(op.f('ix_review_decisions_reviewer_id'), 'review_decisions', ['reviewer_id'], unique=False)
    op.create_table('uploads',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('version_id', db.UUIDString(), nullable=False),
    sa.Column('user_id', db.UUIDString(), nullable=False),
    sa.Column('filename', db.BoundedString(500), nullable=False),
    sa.Column('declared_size', sa.BigInteger(), nullable=False),
    sa.Column('part_size', sa.Integer(), server_default='8388608', nullable=False),
    sa.Column('part_count', sa.Integer(), nullable=False),
    sa.Column('expected_sha256', db.BoundedString(64), nullable=True),
    sa.Column('state', db.BoundedString(16), nullable=False),
    sa.Column('expires_at', db.UTCDateTime(), nullable=False),
    sa.Column('created_at', db.UTCDateTime(), server_default=db.UTCNow(), nullable=False),
    sa.Column('open_version_id', db.UUIDString(), sa.Computed("CASE WHEN state = 'OPEN' THEN version_id ELSE NULL END", ), nullable=True),
    sa.CheckConstraint("length(expected_sha256) = 64 AND coalesce(length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(expected_sha256,'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),'8',''),'9',''),'a',''),'b',''),'c',''),'d',''),'e',''),'f','')),0) = 0", name=op.f('ck_uploads_expected_sha256_hex')),
    sa.CheckConstraint("state IN ('OPEN','SEALED','CANCELLED','EXPIRED')", name=op.f('ck_uploads_state_values')),
    sa.CheckConstraint('declared_size BETWEEN 1 AND 104857600', name=op.f('ck_uploads_size_range')),
    sa.CheckConstraint('part_count BETWEEN 1 AND 13 AND part_size > 0', name=op.f('ck_uploads_parts_range')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_uploads_user_id_users')),
    sa.ForeignKeyConstraint(['version_id'], ['resource_versions.id'], name=op.f('fk_uploads_version_id_resource_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_uploads')),
    sa.UniqueConstraint('open_version_id', name='one_open_upload')
    )
    op.create_index(op.f('ix_uploads_user_id'), 'uploads', ['user_id'], unique=False)
    op.create_index(op.f('ix_uploads_version_id'), 'uploads', ['version_id'], unique=False)
    op.create_table('evidence_links',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('from_version_id', db.UUIDString(), nullable=False),
    sa.Column('from_block_id', db.UUIDString(), nullable=False),
    sa.Column('to_version_id', db.UUIDString(), nullable=False),
    sa.Column('to_block_id', db.UUIDString(), nullable=False),
    sa.Column('purpose', db.BoundedString(24), nullable=False),
    sa.CheckConstraint("purpose IN ('RULE','INTERNAL_OPINION','FACT','CASE','CALCULATION')", name=op.f('ck_evidence_links_purpose_values')),
    sa.CheckConstraint('from_version_id <> to_version_id OR from_block_id <> to_block_id', name=op.f('ck_evidence_links_no_self_link')),
    sa.ForeignKeyConstraint(['from_version_id', 'from_block_id'], ['content_blocks.version_id', 'content_blocks.block_id'], name=op.f('fk_evidence_links_from_version_id_content_blocks')),
    sa.ForeignKeyConstraint(['to_version_id', 'to_block_id'], ['content_blocks.version_id', 'content_blocks.block_id'], name=op.f('fk_evidence_links_to_version_id_content_blocks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_evidence_links')),
    sa.UniqueConstraint('from_version_id', 'from_block_id', 'to_version_id', 'to_block_id', 'purpose', name='uq_evidence_edge')
    )
    op.create_index('evidence_incoming', 'evidence_links', ['to_version_id', 'to_block_id'], unique=False)
    op.create_table('relation_edges',
    sa.Column('id', db.UUIDString(), nullable=False),
    sa.Column('source_version_id', db.UUIDString(), nullable=False),
    sa.Column('target_resource_id', db.UUIDString(), nullable=False),
    sa.Column('relation_type', db.BoundedString(24), nullable=False),
    sa.Column('conditions', db.JSONDocument('object', string_items=False), nullable=False),
    sa.Column('evidence_version_id', db.UUIDString(), nullable=True),
    sa.Column('evidence_block_id', db.UUIDString(), nullable=True),
    sa.CheckConstraint("relation_type IN ('CITES','EXPLAINS','APPLIES_TO','REQUIRES','EXCEPTION_OF','DEPENDS_ON','SUPERSEDES')", name=op.f('ck_relation_edges_relation_type_values')),
    sa.CheckConstraint('(evidence_version_id IS NULL AND evidence_block_id IS NULL) OR (evidence_version_id IS NOT NULL AND evidence_block_id IS NOT NULL)', name=op.f('ck_relation_edges_evidence_pair')),
    sa.CheckConstraint(db.JSONShape('conditions', 'object'), name=op.f('ck_relation_edges_conditions_json')),
    sa.ForeignKeyConstraint(['evidence_version_id', 'evidence_block_id'], ['content_blocks.version_id', 'content_blocks.block_id'], name=op.f('fk_relation_edges_evidence_version_id_content_blocks')),
    sa.ForeignKeyConstraint(['source_version_id'], ['resource_versions.id'], name=op.f('fk_relation_edges_source_version_id_resource_versions')),
    sa.ForeignKeyConstraint(['target_resource_id'], ['resources.id'], name=op.f('fk_relation_edges_target_resource_id_resources')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_relation_edges'))
    )
    op.create_index(op.f('ix_relation_edges_source_version_id'), 'relation_edges', ['source_version_id'], unique=False)
    op.create_index('ix_relation_evidence', 'relation_edges', ['evidence_version_id', 'evidence_block_id'], unique=False)
    op.create_index('relations_target', 'relation_edges', ['target_resource_id'], unique=False)
    op.create_table('run_evidence',
    sa.Column('run_id', db.UUIDString(), nullable=False),
    sa.Column('version_id', db.UUIDString(), nullable=False),
    sa.Column('block_id', db.UUIDString(), nullable=False),
    sa.Column('resource_access_epoch', sa.BigInteger(), nullable=False),
    sa.CheckConstraint('resource_access_epoch > 0', name=op.f('ck_run_evidence_epoch_positive')),
    sa.ForeignKeyConstraint(['run_id'], ['consultation_runs.id'], name=op.f('fk_run_evidence_run_id_consultation_runs')),
    sa.ForeignKeyConstraint(['version_id', 'block_id'], ['content_blocks.version_id', 'content_blocks.block_id'], name=op.f('fk_run_evidence_version_id_content_blocks')),
    sa.PrimaryKeyConstraint('run_id', 'version_id', 'block_id', name=op.f('pk_run_evidence'))
    )
    op.create_index('run_evidence_version', 'run_evidence', ['version_id', 'block_id'], unique=False)
    op.create_table('upload_parts',
    sa.Column('upload_id', db.UUIDString(), nullable=False),
    sa.Column('part_no', sa.Integer(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', db.BoundedString(64), nullable=False),
    sa.Column('object_key', db.ExactKey(512), nullable=False),
    sa.CheckConstraint("length(sha256) = 64 AND coalesce(length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256,'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),'8',''),'9',''),'a',''),'b',''),'c',''),'d',''),'e',''),'f','')),0) = 0", name=op.f('ck_upload_parts_sha256_hex')),
    sa.CheckConstraint('part_no > 0 AND size_bytes > 0', name=op.f('ck_upload_parts_part_positive')),
    sa.ForeignKeyConstraint(['upload_id'], ['uploads.id'], name=op.f('fk_upload_parts_upload_id_uploads')),
    sa.PrimaryKeyConstraint('upload_id', 'part_no', name=op.f('pk_upload_parts')),
    sa.UniqueConstraint('object_key', name=op.f('uq_upload_parts_object_key'))
    )
    if op.get_context().dialect.name != "sqlite":
        op.create_foreign_key("fk_resource_release_owner", "resources", "releases",
                              ["id", "active_release_id"], ["resource_id", "id"])


def downgrade():
    if op.get_context().dialect.name != "sqlite":
        op.drop_constraint("fk_resource_release_owner", "resources", type_="foreignkey")
    # Frozen revision; changing runtime models does not modify this schema.
    op.drop_table('upload_parts')
    op.drop_index('run_evidence_version', table_name='run_evidence')
    op.drop_table('run_evidence')
    op.drop_index('relations_target', table_name='relation_edges')
    op.drop_index('ix_relation_evidence', table_name='relation_edges')
    op.drop_index(op.f('ix_relation_edges_source_version_id'), table_name='relation_edges')
    op.drop_table('relation_edges')
    op.drop_index('evidence_incoming', table_name='evidence_links')
    op.drop_table('evidence_links')
    op.drop_index(op.f('ix_uploads_version_id'), table_name='uploads')
    op.drop_index(op.f('ix_uploads_user_id'), table_name='uploads')
    op.drop_table('uploads')
    op.drop_index(op.f('ix_review_decisions_reviewer_id'), table_name='review_decisions')
    op.drop_table('review_decisions')
    op.drop_index(op.f('ix_releases_publisher_id'), table_name='releases')
    op.drop_index('ix_release_version', table_name='releases')
    op.drop_table('releases')
    op.drop_index('jobs_recovery', table_name='jobs')
    op.drop_index(op.f('ix_jobs_version_id'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_run_id'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_resource_id'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_owner_id'), table_name='jobs')
    op.drop_table('jobs')
    op.drop_index(op.f('ix_issue_cases_run_id'), table_name='issue_cases')
    op.drop_index(op.f('ix_issue_cases_creator_id'), table_name='issue_cases')
    op.drop_index(op.f('ix_issue_cases_assignee_id'), table_name='issue_cases')
    op.drop_index('issues_space_state', table_name='issue_cases')
    op.drop_table('issue_cases')
    op.drop_index(op.f('ix_feedback_user_id'), table_name='feedback')
    op.drop_index(op.f('ix_feedback_run_id'), table_name='feedback')
    op.drop_table('feedback')
    op.drop_table('content_blocks')
    op.drop_index('versions_resource', table_name='resource_versions')
    op.drop_index('ix_version_base', table_name='resource_versions')
    op.drop_index(op.f('ix_resource_versions_source_blob_id'), table_name='resource_versions')
    op.drop_index(op.f('ix_resource_versions_author_id'), table_name='resource_versions')
    op.drop_table('resource_versions')
    op.drop_index('ix_grants_user', table_name='resource_grants')
    op.drop_table('resource_grants')
    op.drop_index('runs_thread', table_name='consultation_runs')
    op.drop_index('ix_run_parent', table_name='consultation_runs')
    op.drop_table('consultation_runs')
    op.drop_index('ix_members_user', table_name='space_members')
    op.drop_table('space_members')
    op.drop_index(op.f('ix_runtime_policies_updated_by'), table_name='runtime_policies')
    op.drop_table('runtime_policies')
    op.drop_index('resources_space_live', table_name='resources')
    op.drop_index('ix_resource_owner', table_name='resources')
    op.drop_index('ix_resource_active_release', table_name='resources')
    op.drop_table('resources')
    op.drop_index('ix_session_expiry', table_name='login_sessions')
    op.drop_index(op.f('ix_login_sessions_user_id'), table_name='login_sessions')
    op.drop_table('login_sessions')
    op.drop_index('ix_idempotency_expiry', table_name='idempotency_records')
    op.drop_table('idempotency_records')
    op.drop_index('threads_owner', table_name='consultation_threads')
    op.drop_index(op.f('ix_consultation_threads_space_id'), table_name='consultation_threads')
    op.drop_table('consultation_threads')
    op.drop_index(op.f('ix_blobs_space_id'), table_name='blobs')
    op.drop_table('blobs')
    op.drop_index(op.f('ix_audit_events_actor_id'), table_name='audit_events')
    op.drop_index('audit_lookup', table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_table('users')
    op.drop_table('spaces')
    op.drop_index('outbox_pending', table_name='outbox')
    op.drop_table('outbox')
