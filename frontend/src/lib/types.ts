/**
 * TypeScript mirrors of contracts/api/CONTRACT.md (binding API shapes).
 * Keep field names snake_case exactly as the API sends them.
 */

export type UUID = string;
export type ISODate = string;

export interface Paginated<T> {
  items: T[];
  next_cursor: string | null;
  previous_cursor: string | null;
}

export interface ProblemFieldError {
  field: string | null;
  code: string;
  message: string;
}

export interface ProblemDetails {
  type?: string;
  title?: string;
  status: number;
  code: string;
  detail?: string | null;
  request_id?: string | null;
  errors?: ProblemFieldError[];
  [extra: string]: unknown;
}

/* ---------- auth / orgs (W1) ---------- */

export interface User {
  id: UUID;
  email: string;
  full_name: string;
  is_platform_admin: boolean;
  timezone: string;
}

export type OrgRoleKey = "owner" | "admin" | "data_engineer" | "analyst" | "data_scientist" | "viewer" | "auditor" | string;

export interface OrgSummary {
  id: UUID;
  slug: string;
  name: string;
  role: OrgRoleKey;
  status: string;
}

export interface MeResponse {
  user: User;
  orgs: OrgSummary[];
}

export type AIDataPolicy = "synthetic_only" | "schema_only" | "aggregates_only" | "full";

export interface Org {
  id: UUID;
  slug: string;
  name: string;
  plan: string;
  status: string;
  region: string;
  ai_data_policy: AIDataPolicy;
  created_at: ISODate;
  role: OrgRoleKey;
  permissions: string[];
}

export interface SignupRequest {
  email: string;
  password: string;
  full_name: string;
  org_name: string;
}

export interface SignupResponse {
  user: User;
  org: Org;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface AiPolicyConsent {
  valid: boolean;
  version: string | number;
  granted_by: string | null;
  granted_at: ISODate | null;
}

export interface AiPolicyState {
  policy: AIDataPolicy;
  gemini_tier: "free" | "paid" | string;
  consent: AiPolicyConsent | null;
  allowed_values: AIDataPolicy[];
  consent_text_version: string | number;
}

export interface ConsentText {
  version: string | number;
  text: string;
}

export interface GrantConsentRequest {
  policy: "schema_only";
  consent_text_version: string | number;
  acknowledged: true;
  password: string;
}

export interface MemberUser {
  id: UUID;
  email: string;
  full_name: string;
}

export interface Member {
  id: UUID;
  user: MemberUser;
  role: OrgRoleKey;
  status: string;
  joined_at: ISODate;
}

export interface Invitation {
  id: UUID;
  email: string;
  role: OrgRoleKey;
  expires_at: ISODate;
  status: string;
}

export interface Role {
  id: UUID;
  key: OrgRoleKey;
  name: string;
  is_system: boolean;
  permissions: string[];
}

export interface AuditEvent {
  id: UUID;
  occurred_at: ISODate;
  actor_type: string;
  actor_id: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  metadata: Record<string, unknown> | null;
  request_id?: string | null;
  ip?: string | null;
  changes?: Record<string, unknown> | null;
}

/** /orgs/{org}/activity: recent audit events, enriched with the actor's email. */
export interface ActivityEvent extends AuditEvent {
  actor_email?: string | null;
  actor_name?: string | null;
}

/* ---------- jobs ---------- */

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface JobAccepted {
  job_id: UUID;
  status_url: string;
  events_url: string;
}

export interface Job {
  id: UUID;
  type: string;
  status: JobStatus;
  progress: number | null;
  message: string | null;
  result: Record<string, unknown> | null;
  error: { code: string; message: string; detail?: unknown } | null;
  created_at: ISODate;
  finished_at: ISODate | null;
}

export interface JobEventMap {
  status: { status: JobStatus | null; progress: number | null; message: string | null };
  log: { level: "info" | "warning" | "error"; message: string; step_key?: string };
  result: { result?: Record<string, unknown> | null; result_ref?: Record<string, unknown> | null };
  error: { code: string; message?: string; detail?: unknown };
  done: { status: "succeeded" | "failed" | "cancelled" };
}

/* ---------- data engineering (W2) ---------- */

export type Classification = "synthetic" | "public" | "internal" | "confidential";

export interface Upload {
  id: UUID;
  filename: string;
  size_bytes: number;
  status: string;
}

export interface DatasetVersion {
  id: UUID;
  version: number;
  row_count: number | null;
  status: string;
  wh_table: string | null;
  created_at: ISODate;
}

export type DatasetStatus = "pending" | "ingesting" | "profiling" | "ready" | "failed" | string;

export interface Dataset {
  id: UUID;
  name: string;
  slug: string;
  description: string | null;
  classification: Classification;
  status: DatasetStatus;
  row_count: number | null;
  column_count: number | null;
  quality_score: number | null;
  current_version: DatasetVersion | null;
  created_at: ISODate;
  updated_at: ISODate;
  tags: string[];
}

export interface CreateDatasetRequest {
  name: string;
  upload_id: UUID;
  classification?: Classification;
}

export interface CreateDatasetResponse {
  dataset: Dataset;
  job_id: UUID;
  events_url: string;
}

export interface HistogramBin {
  bin?: string | number;
  label?: string;
  start?: number;
  end?: number;
  count: number;
}

export interface ColumnStats {
  null_pct: number | null;
  distinct: number | null;
  min: unknown;
  max: unknown;
  mean: number | null;
  p50: number | null;
  top_values: Array<{ value: unknown; count: number }> | Record<string, number> | null;
  histogram: HistogramBin[] | { bins: Array<number | string>; counts: number[] } | null;
}

export interface DatasetColumn {
  name: string;
  source_name: string;
  data_type: string;
  semantic_type: string | null;
  nullable: boolean;
  is_pii: boolean;
  stats: ColumnStats | null;
}

export interface DatasetPreview {
  columns: Array<{ name: string; type: string; masked: boolean }>;
  rows: unknown[][];
}

export interface QualityRule {
  id: UUID;
  column: string | null;
  rule_type: string;
  severity: "info" | "warning" | "error" | "critical" | string;
  passed: boolean;
  failed_count: number;
}

export interface QualityReport {
  score: number | null;
  dimensions: {
    completeness: number | null;
    uniqueness: number | null;
    validity: number | null;
    consistency: number | null;
  };
  rules: QualityRule[];
  duplicates: {
    exact_rows: number;
    key_duplicates: number;
    fuzzy_clusters: unknown[];
  };
}

export interface DataStats {
  datasets: number;
  rows_total: number;
  avg_quality: number | null;
  pipelines_active: number;
  runs_7d: { succeeded: number; failed: number };
  storage_bytes: number;
  /** Optional: previous-period values for KPI deltas when the backend provides them. */
  previous?: Partial<Pick<DataStats, "datasets" | "rows_total" | "avg_quality" | "pipelines_active">> | null;
}

/* ---------- AI (W3) ---------- */

export interface AiStatus {
  policy: AIDataPolicy;
  tier: string;
  configured: boolean;
  org_budget: { limit: number; used: number };
  breaker: "closed" | "open_minute" | "open_day";
  resets_at: ISODate | null;
  /* W1 as-built additions (optional so older backends still type-check). */
  stored_policy?: AIDataPolicy;
  consent_valid?: boolean;
  policy_downgrade_reason?: string | null;
  token_budget?: { limit: number; used: number } | null;
  user_budget?: { limit: number; used: number } | null;
  budget_resets_at?: ISODate | null;
  limiter_available?: boolean;
}

export interface Insight {
  kind: string;
  title: string;
  text: string;
  dataset_id: UUID | null;
}

/** GET /orgs/{org}/ai/usage?days= (W1 as-built). */
export interface AiUsage {
  range: { from: string; to: string; days: number; timezone: string };
  totals: { requests: number; input_tokens: number; output_tokens: number; cached_tokens: number; blocked: number; errors: number };
  daily: Array<{ day: string; model: string; requests: number; input_tokens: number; output_tokens: number; blocked: number; errors: number }>;
}

/* ---------- Conversational BI (W3) ---------- */

export type ChatStage = "authorizing" | "resolving" | "generating" | "validating" | "executing" | "explaining";
export type SqlSource = "cache_exact" | "cache_semantic" | "builder" | "llm" | "manual" | "none" | string;
export type ChartType = "bar" | "line" | "area" | "pie" | "scatter" | "table" | "kpi";

export interface ChartHint {
  type: ChartType | string;
  x?: string | null;
  y?: string | string[] | null;
  series?: string | null;
}

/** Result columns arrive as `{name,type,masked}` objects (or bare names from older payloads). */
export interface ResultColumn {
  name: string;
  type?: string;
  masked?: boolean;
}

export interface ChatResult {
  columns: Array<ResultColumn | string>;
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
}

export interface ChatInsight {
  kind: string;
  text: string;
}

export interface Conversation {
  id: UUID;
  title: string;
  dataset_id: UUID;
  created_at: ISODate;
  last_message_at: ISODate | null;
}

export interface ChatMessage {
  id: UUID;
  role: "user" | "assistant";
  content: string;
  status: string;
  error_code: string | null;
  created_at: ISODate;
  sql: { raw: string | null; executed: string | null; source: SqlSource } | null;
  result: ChatResult | null;
  chart: ChartHint | null;
  insights: ChatInsight[];
  assumptions: string[];
  /** Optional extras some backends send. */
  error_detail?: string | null;
  suggestions?: string[];
  feedback?: "up" | "down" | null;
  duration_ms?: number | null;
}

/** Chat SSE events (CONTRACT.md W3). */
export interface ChatEventMap {
  status: { stage: ChatStage; source?: SqlSource };
  sql: { raw_sql: string | null; executed_sql: string | null; source: SqlSource; query_audit_id?: string | null; assumptions?: string[] };
  result: ChatResult & { chart?: ChartHint | null; chart_spec?: ChartHint | null };
  insight: ChatInsight;
  token: { text: string };
  done: { message_id: UUID; ai_requests_used?: number };
  error: { code: string; detail?: string | null; resets_at?: string | null; reject_code?: string | null; executed_sql?: string | null; title?: string };
}

export interface ManualQueryResult {
  executed_sql: string;
  columns: Array<ResultColumn | string>;
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  duration_ms?: number;
}

export interface QueryHistoryItem {
  id: UUID;
  occurred_at: ISODate;
  user_email: string | null;
  dataset: { id: UUID; name: string } | string | null;
  question: string | null;
  source: SqlSource;
  verdict: "allowed" | "rejected" | "error" | "timeout" | "blocked_by_policy" | string;
  reject_code: string | null;
  executed_sql: string | null;
  row_count: number | null;
  duration_ms: number | null;
}

/* ---------- Data sources (W2) ---------- */

export interface DataSourceConfig {
  host: string;
  port: number;
  dbname: string;
  sslmode: string;
  schema?: string;
}

export interface DataSource {
  id: UUID;
  name: string;
  type: "postgresql" | string;
  config: DataSourceConfig;
  username?: string;
  status: "active" | "error" | "disabled" | string;
  verified_readonly?: boolean;
  last_tested_at?: ISODate | null;
  last_test_result?: { ok?: boolean; code?: string; message?: string; checks?: SourceCheck[] } | null;
  created_at: ISODate;
  updated_at?: ISODate;
}

export interface SourceCheck {
  key: string;
  ok: boolean;
  message?: string;
}

export interface CreateDataSourceRequest {
  name: string;
  type: "postgresql";
  config: DataSourceConfig;
  username: string;
  password: string;
}

export interface IntrospectColumn {
  name: string;
  type: string;
  nullable?: boolean;
}

export interface IntrospectTable {
  name: string;
  schema: string;
  row_estimate?: number | null;
  columns: IntrospectColumn[];
}

export interface IntrospectSchema {
  name: string;
  tables: IntrospectTable[];
}

/* ---------- Pipelines (W2) ---------- */

export interface PipelineDagNode {
  id: string;
  type: string;
  config: Record<string, unknown>;
  position?: { x: number; y: number };
  label?: string;
}

export interface PipelineDag {
  nodes: PipelineDagNode[];
  edges: Array<{ from: string; to: string }>;
  settings?: Record<string, unknown>;
}

export interface PipelineSchedule {
  cron: string;
  timezone: string;
  is_active?: boolean;
  next_run_at?: ISODate | null;
}

export interface PipelineRunStep {
  step_key: string;
  step_type?: string;
  status: string;
  started_at?: ISODate | null;
  finished_at?: ISODate | null;
  rows_in?: number | null;
  rows_out?: number | null;
  error?: { code?: string; message?: string } | string | null;
  metrics?: Record<string, unknown> | null;
}

export interface PipelineRun {
  id: UUID;
  pipeline_id?: UUID;
  status: string;
  trigger?: string;
  attempt: number;
  started_at: ISODate | null;
  finished_at: ISODate | null;
  duration_ms: number | null;
  rows_in: number | null;
  rows_out: number | null;
  rows_rejected: number | null;
  error_code: string | null;
  error_message: string | null;
  quality_summary: { score?: number | null; rules_passed?: number; rules_failed?: number; blocking_failures?: number } | null;
  steps: PipelineRunStep[];
  created_at?: ISODate;
  job_id?: UUID | null;
}

export interface Pipeline {
  id: UUID;
  name: string;
  description?: string;
  status: string;
  dag: PipelineDag | null;
  schedule: PipelineSchedule | null;
  last_run: PipelineRun | null;
  version?: number | null;
  created_at?: ISODate;
  updated_at?: ISODate;
}

export interface RunLogLine {
  ts: ISODate;
  level: "debug" | "info" | "warning" | "error" | string;
  step_key?: string | null;
  message: string;
}

/* ---------- Lineage (W2) ---------- */

export interface LineageNode {
  id: string;
  type: string;
  label: string;
}

export interface LineageGraph {
  nodes: LineageNode[];
  edges: Array<{ from: string; to: string; label?: string | null }>;
}

/* ---------- Semantic layer (W2) ---------- */

export type SemanticRole = "dimension" | "measure" | "time" | "id" | "attribute";

export interface SemanticColumn {
  id?: UUID;
  column_name: string;
  display_name: string;
  description: string;
  synonyms: string[];
  role: SemanticRole | string;
  default_agg?: string | null;
  exposed_to_ai?: boolean;
}

export interface SemanticTable {
  id?: UUID;
  alias: string;
  display_name: string;
  description: string;
  columns: SemanticColumn[];
}

export interface MetricDefinition {
  id?: UUID;
  key: string;
  name: string;
  expression: string;
  description: string;
  synonyms: string[];
}

export interface SemanticModel {
  id: UUID;
  dataset_id: UUID;
  name: string;
  version?: number;
  status: "draft" | "published" | string;
  tables: SemanticTable[];
  metrics: MetricDefinition[];
}

/* ---------- Documents (AI/ML) ---------- */

export interface DocumentItem {
  id: UUID;
  title: string;
  filename: string;
  mime?: string;
  size_bytes: number;
  classification: Classification;
  status: string;
  page_count: number | null;
  doc_type?: string | null;
  languages?: string[] | null;
  summary?: string | null;
  summary_source?: "extractive" | "gemini" | null;
  error?: string | null;
  created_at: ISODate;
  last_job_id?: UUID | null;
}

export interface DocumentPage {
  page_no: number;
  text: string;
  ocr_used?: boolean;
  ocr_confidence?: number | null;
  lang?: string | null;
}

export interface DocumentChunk {
  id: UUID;
  ordinal: number;
  page_from: number;
  page_to: number;
  section_path?: string | null;
  text: string;
  token_count?: number;
}

export interface DocumentEntity {
  id: UUID;
  page_no: number | null;
  entity_type: string;
  value: string;
  normalized_value?: string | null;
  confidence?: number | null;
  source?: string | null;
}

export interface DocumentTable {
  id: UUID;
  page_no: number;
  table_index: number;
  data: { columns?: string[]; rows?: unknown[][] } | unknown[][];
}

export interface DocumentSearchHit {
  chunk_id: UUID;
  document_id: UUID;
  document_title?: string;
  page_from: number;
  page_to?: number;
  text: string;
  score?: number;
}

export interface DocumentCitation {
  document_id: UUID;
  document_title?: string;
  page: number;
  chunk_id?: UUID;
  text?: string;
}

export interface DocumentAnswer {
  answer: string;
  mode: "generative" | "extractive" | string;
  citations: DocumentCitation[];
  reason?: string | null;
  not_found?: boolean;
}
