export type Kind = "document" | "knowledge" | "template";
export type Json = Record<string, unknown>;
export type Page<T> = { items: T[]; next_cursor: string | null };
export type Space = {
  id: string;
  name: string;
  revision: number;
  roles?: string[];
  kind?: "personal" | "team" | "legacy";
  owner_id?: string | null;
  governed?: boolean;
};
export type Me = {
  id: string;
  display_name: string;
  csrf_token: string;
  spaces: Space[];
  is_admin?: boolean;
};
export type DemoUser = { id: string; display_name: string; roles?: string[] };
export type Member = {
  user_id: string;
  roles: string[];
  display_name?: string;
};
export type Resource = {
  id: string;
  space_id: string;
  kind: Kind;
  name: string;
  category: string;
  tags: string[];
  owner_id: string;
  revision: number;
  access_epoch: number;
  restricted: boolean;
  classification: string;
  suspended: boolean;
  deleted_at: string | null;
  active_release_id: string | null;
  active_version_id: string | null;
  updated_at?: string;
  file_extension?: string | null;
  mime_type?: string | null;
  latest_version_no?: number | null;
  latest_state?: string | null;
  owner_name?: string;
  latest_version_id?: string | null;
  latest_version_revision?: number | null;
  version_no?: number;
};
export type CitationRef = {
  version_id: string;
  block_id: string;
  purpose: "RULE" | "INTERNAL_OPINION" | "FACT" | "CASE" | "CALCULATION";
};
export type Block = {
  block_id: string;
  ordinal: number;
  block_type:
    | "heading"
    | "paragraph"
    | "list"
    | "table"
    | "step"
    | "warning"
    | "formula"
    | "image"
    | "attachment";
  data: Json;
  locator: Json;
  citations: CitationRef[];
};
export type Content = {
  title: string;
  knowledge_type: string;
  applicability: Json;
  required_facts: string[];
  legal_status: string;
  valid_from: string | null;
  valid_to: string | null;
  source_url?: string | null;
  blocks: Block[];
};
export type Version = Content & {
  id: string;
  resource_id: string;
  version_no: number;
  state: "DRAFT" | "IN_REVIEW" | "APPROVED" | "REJECTED";
  revision: number;
  author_id: string;
  source_verified: boolean;
  content_sha256: string | null;
  source_filename?: string | null;
  mime_type?: string | null;
  origin?: "UPLOAD" | "HUMAN" | "AI_DRAFT" | "COPY";
  base_version_id?: string | null;
};
export type VersionSummary = Pick<Version, "id" | "version_no" | "state" | "revision">;
export type Review = {
  id: string;
  version_id: string;
  reviewer_id: string;
  decision: string;
  reviewed_sha256: string;
  comment: string;
  created_at: string;
};
export type Permissions = {
  restricted: boolean;
  classification: string;
  grants: { user_id: string; permission: string }[];
};
export type Relation = {
  target_resource_id: string;
  relation_type: string;
  conditions: Json;
  evidence_version_id?: string;
  evidence_block_id?: string;
};
export type Job = {
  id: string;
  kind: string;
  task?: "VECTOR_INDEX";
  state: string;
  stage: string;
  attempts: number;
  error_code: string | null;
  result: Json | null;
};
export type Part = { part_no: number; size_bytes: number; sha256: string };
export type Upload = {
  id: string;
  version_id: string;
  state: string;
  part_size: number;
  part_count: number;
  completed_parts: Part[];
  expires_at: string;
};
export type Thread = {
  id: string;
  space_id: string;
  title: string;
  created_at: string;
};
export type Evidence = {
  id: string;
  resource_id: string;
  version_id: string;
  block_id: string;
  source_title: string;
  excerpt: string;
  locator: Json;
  content_sha256: string;
};
export type Solution = {
  goal: string;
  preconditions: string[];
  materials: string[];
  steps: {
    id: string;
    action: string;
    owner_role: string;
    inputs: string[];
    output: string;
    verification: string;
    evidence_ids: string[];
    depends_on: string[];
  }[];
  branches: { condition: string; action: string }[];
  completion_checks: string[];
  escalation: string[];
};
export type AnswerScope = "reference" | "formal";
export type Answer = {
  format?: "wiki_markdown";
  narrative_markdown?: string;
  server_notice?: string;
  grounding_status?: "SOURCE_LINKED" | "UNVERIFIED" | "NO_LOCAL_SOURCES";
  quality_warnings?: {code: string; message: string}[];
  run_id: string;
  status: string;
  mode: string;
  summary: string;
  scope: Record<string, string | null>;
  facts: Json[];
  missing_facts: Json[];
  claims: { id: string; text: string; evidence_ids: string[] }[];
  analysis?: {interpretation: string; checks: {title: string; reason: string; evidence_ids: string[]}[];
    branches: {condition: string; action: string; evidence_ids: string[]}[]};
  citations: Evidence[];
  solution: Solution | null;
  limitations: string[];
  required_sources: string[];
  review_status: string;
  generated_at: string;
};
export type Run = {
  id: string;
  thread_id: string;
  job_id: string;
  state: string;
  invalidated: boolean;
  answer: Answer | null;
  error_code: string | null;
  phase?: string | null;
  attempt?: number;
  question_analysis?: {source: "model_prior_knowledge_unverified"; local_sources_loaded: number; plan: {
    interpretation: string; initial_assessment: string; search_queries: string[]; focus_terms: string[];
    decision_points?: string[]; missing_facts?: string[]}};
  failure_diagnostic?: {code:string;message:string;phase:string;category?:string;schema_errors:{path:string;rule:string}[];retryable:boolean;next_step:string};
  question?: string;
  context?: Record<string, string>;
  mode?: string;
  answer_scope?: AnswerScope | null;
  // Server-derived provenance; never supplied by the request composer.
  answer_scope_origin?: "explicit" | "inherited" | "legacy_default" | "historical_unknown";
  // Recomputed for the current caller on GET, not a historical evidence snapshot.
  evidence_diagnostic?: {
    scope: AnswerScope;
    basis: "current_access";
    observed_at: string;
    message: string;
    note: string;
    visible_resource_count?: number;
    eligible_resource_count?: number;
    excluded_resource_count?: number;
    reasons: { code: string; label: string; count?: number }[];
    next_step: string;
  };
  created_at?: string;
  model_snapshot?: (Record<string, unknown> & {
    retrieval_selection?: { profile_id: string; fingerprint: string; model: string; dimensions: number };
    public_preview?: {
      state: "pending"; notice: string; text: string; run_id: string;
      attempt: number; revision: number; source_check: "current_access"; final_validation: "pending";
    };
    model_id?: string;
    brand?: string;
    execution_mode?: string | null;
    model_invoked?: boolean | null;
    planning_model_invoked?: boolean;
    planning_cache?: {hit: boolean; saved_model_requests: number; final_answer_reused: boolean;
      fresh_source_checks: boolean; source_run_id?: string; age_ms?: number};
    answer_model_invoked?: boolean;
    model_request_count?: number;
    retrieval_runtime?: {state: string; phase: string; self_tested: boolean; process_id: number; runtime_id: string; error_code?: string | null};
    validation_status?: "pending" | "passed" | "failed" | "review_required";
    context_completion?: {status: string; reference_count: number; resolved_count: number; gap_count: number;
      structural_groups_added: number; additional_searches?: number; professional_completeness: string;
      direction_count?: number; direction_source_read_count?: number; direction_gap_count?: number;
      reading_coverage?: {status: string; direction_count: number; source_read_count: number; gap_count: number;
        professional_completeness: string; directions: {id: string; query: string; status: string;
          source_pages: string[]; wiki_pages: string[]; evidence_ids: string[]; semantic_support: string}[]};
      references?: {text: string; status: string; source_page_id: string; target_page_id?: string; reason?: string}[];
      group_rerank?: {status: string; model?: string; dropped_pages: number; cache_hit: boolean; ordered_pages?: string[];
        ranking_basis?: string; scored_sources?: number; reused_source_scores?: number}};
    wiki_reading?: {catalog_pages: number; loaded_pages: number; loaded_blocks: number; loaded_characters: number;
      page_titles: string[]; full_text_loaded: boolean; scoped_source_pages?: number; full_source_pages?: number;
      source_sections?: number};
    reading_progress?: {stage: string; round?: number; current_batch?: number; total_batches?: number;
      completed_batches?: number; total_characters?: number; sent_characters?: number; loaded_blocks?: number;
      requested_pages?: number; updated_at?: string};
    hybrid_retrieval?: {strategy: "wiki_rag_rrf"; source_bodies_loaded_for_discovery: number; full_catalog_available: boolean;
      queries: {query: string; mode: string; catalog_pages: number; indexed_catalog_pages: number;
        total_candidates: number; returned: number; warnings: string[]; timing_ms: number}[]};
    last_request?: {
      phase: "planning" | "synthesis" | "wiki_index" | "wiki_notes";
      state: "waiting" | "received" | "failed";
      attempt: number;
      started_at?: string;
      duration_ms?: number;
      response_chars?: number;
      finish_reason?: "stop" | "length" | "unknown";
      error_code?: string;
      limits?: {total_seconds: number | null; read_idle_seconds: number | null; connect_seconds: number; max_output_tokens: number};
      usage?: {prompt_tokens?: number; completion_tokens?: number; total_tokens?: number; reasoning_tokens?: number};
    } | null;
    evidence_count?: number | null;
  }) | null;
};
export type SystemStatus = {
  answer_scopes?: AnswerScope[] | null;
  retrieval_mode?: "wiki" | "hybrid";
  database: Json;
  auth_mode: string;
  app_env: string;
  model: {
    provider: string;
    configured: boolean;
    live_model_verified: boolean;
  };
  vector: Json;
  dispatcher_ready: boolean;
  baseline_operations: number;
};
export type IssueCase = {
  id: string;
  space_id: string;
  title: string;
  description: string;
  state: string;
  resolution: string;
  assignee_id: string | null;
  revision: number;
};
export type ModelPolicy = {
  provider_ref: string;
  generation_model: string;
  extraction_model: string;
  enable_vector: boolean;
  prompt_version: string;
  evaluation_id: string;
};
export type SearchHit = {
  resource_id: string;
  version_id: string;
  block_id: string;
  title: string;
  excerpt: string;
  locator: Json;
};
export type AuditEvent = {
  id: string;
  action: string;
  object_type: string;
  outcome: string;
  created_at: string;
  trace_id: string;
};
