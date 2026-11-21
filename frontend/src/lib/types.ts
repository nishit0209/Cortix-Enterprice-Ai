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

/** apps/datasets Dataset.status: processing | ready | failed | archived | deleted. */
export type DatasetStatus = "processing" | "ready" | "failed" | "archived" | "deleted" | string;

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
  models?: Record<string, { model: string; breaker: string; resets_at: ISODate | null }>;
  /* W3 AI quota UX. */
  quota?: AiQuota | null;
  /** Where usage is counted: the Redis limiter, or the database (local runtime without Redis). */
  limiter_backend?: "redis" | "db";
  /** Local embedding model (documents search); never downloaded by the status call. */
  embeddings?: { available: boolean; model: string | null; cached?: boolean | null; loaded?: boolean | null } | null;
}

export type AiQuotaState = "ok" | "low" | "exhausted";

/** Which budget decided a low/exhausted `state` (`apps.ai.services.quota_state`). */
export type AiQuotaScope = "platform" | "org_requests" | "org_tokens" | "user";

export interface AiQuota {
  state: AiQuotaState;
  resets_at: ISODate | null;
  /** The org's daily REQUEST budget, whichever budget `scope` names. */
  used_today: number;
  limit_today: number;
  remaining_today?: number;
  /** The budget behind a low/exhausted state; null/absent when ok (or on older backends). */
  scope?: AiQuotaScope | null;
  /** The per-minute breaker is open (requests resume within a minute). */
  rate_limited?: boolean;
  /** The caller's own share of the daily budget, when a per-user limit applies. */
  user?: { used: number; limit: number } | null;
  /** The org's daily token budget (from `/ai/status.token_budget`). */
  tokens?: { used: number; limit: number } | null;
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
  daily: Array<{ day: string; model: string; requests: number; input_tokens: number; output_tokens: number; cached_tokens?: number; blocked: number; errors: number }>;
  by_feature?: Array<{ feature: string; requests: number; input_tokens: number; output_tokens: number }>;
  by_status?: Record<string, number>;
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
  /** apps/bi BiQueryAudit.verdict. */
  verdict: "ok" | "rejected" | "error" | "blocked" | string;
  reject_code: string | null;
  executed_sql: string | null;
  row_count: number | null;
  duration_ms: number | null;
  purpose?: string;
  message_id?: UUID | null;
  conversation_id?: UUID | null;
  dataset_id?: UUID;
  truncated?: boolean;
}

/* ---------- Saved queries + dashboards (W3) ---------- */

export interface SavedQuery {
  id: UUID;
  name: string;
  description: string;
  dataset_id: UUID;
  question: string;
  sql: string;
  chart: ChartHint | null;
  is_shared: boolean;
  owner_id: UUID;
  created_at: ISODate;
  updated_at: ISODate;
}

export interface DashboardSummary {
  id: UUID;
  title: string;
  description: string;
  is_default: boolean;
  widget_count: number;
  updated_at: ISODate;
  /** Shared dashboards are visible to every member with dashboards.view; only the owner edits. */
  is_shared?: boolean;
  owner_id?: UUID;
  created_at?: ISODate;
}

export type WidgetKind = "chart" | "table" | "kpi" | "text";

export interface WidgetLayout {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface DashboardWidget {
  id: UUID;
  title: string;
  kind: WidgetKind;
  saved_query_id?: UUID | null;
  chart?: ChartHint | null;
  layout: WidgetLayout;
  text?: string | null;
  dataset_id?: UUID | null;
}

export interface Dashboard extends DashboardSummary {
  widgets: DashboardWidget[];
}

export interface WidgetData {
  columns: Array<ResultColumn | string>;
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  chart: ChartHint | null;
  refreshed_at: ISODate;
  query_audit_id?: UUID;
  dataset_id?: UUID;
}

export interface CreateWidgetRequest {
  title: string;
  kind: WidgetKind;
  saved_query_id?: UUID;
  message_id?: UUID;
  chart?: ChartHint | null;
  layout: WidgetLayout;
  text?: string;
}

/* ---------- Notifications (W3) ---------- */

export interface AppNotification {
  id: UUID;
  kind: string;
  title: string;
  body: string;
  link: string | null;
  read_at: ISODate | null;
  created_at: ISODate;
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
  last_error?: { code?: string; message?: string } | null;
  has_password?: boolean;
  introspected_at?: ISODate | null;
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
  kind?: string;
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

/** Backend DAG wire format (`apps/pipelines/dag.py` DagIn): linear chain, unknown keys rejected. */
export interface PipelineDag {
  nodes: PipelineDagNode[];
  edges: Array<{ from: string; to: string }>;
}

/** POST /pipelines/validate → per-node issues (node id or null for graph-level problems). */
export interface PipelineValidation {
  valid: boolean;
  errors: Array<{ node: string | null; field: string; code: string; message: string }>;
  order: string[];
  schemas: Record<string, Array<{ name: string; type: string }> | null>;
}

/** POST /pipelines/{id}/preview (synchronous, read-only). */
export interface PipelinePreview {
  ok: boolean;
  columns: Array<{ name: string; type?: string }>;
  rows: unknown[][];
  row_count: number;
  source_rows?: number;
  rows_rejected?: number;
  steps: PipelineRunStep[];
  error?: { code: string; message: string; node: string | null };
}

export interface PipelineSchedule {
  cron: string;
  timezone: string;
  enabled?: boolean;
  next_run_at?: ISODate | null;
  last_enqueued_at?: ISODate | null;
  last_run_id?: UUID | null;
}

/** One step metric of a run (backend `engine.StepMetric`). */
export interface PipelineRunStep {
  id: string;
  type: string;
  status: string;
  started_at?: ISODate | null;
  finished_at?: ISODate | null;
  rows_in?: number | null;
  rows_out?: number | null;
  rows_rejected?: number | null;
  duration_ms?: number | null;
  error?: { code?: string; message?: string } | string | null;
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
  /** Only the run detail carries steps/attempts/logs; list rows are summaries. */
  steps?: PipelineRunStep[];
  number?: number;
  max_attempts?: number;
  version?: number;
  parent_run_id?: UUID | null;
  next_attempt_at?: ISODate | null;
  output_dataset_id?: UUID | null;
  logs?: RunLogLine[];
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
  output_dataset_id?: UUID | null;
  created_at?: ISODate;
  updated_at?: ISODate;
}

export interface RunLogLine {
  ts: ISODate;
  level: "debug" | "info" | "warning" | "error" | string;
  step?: string | null;
  attempt?: number;
  message: string;
  context?: Record<string, unknown> | null;
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

/* ---------- Semantic layer (W2, `apps/semantic`) ---------- */

/** Backend roles (`apps/semantic/generation.py` ROLES). A detected PII column keeps role `pii`. */
export type SemanticRole = "dimension" | "measure" | "time" | "id" | "pii";

export interface SemanticColumn {
  name: string;
  type?: string;
  data_type?: string;
  semantic_type?: string | null;
  role: SemanticRole | string;
  display_name: string;
  description: string;
  synonyms: string[];
  is_pii?: boolean;
  default_agg?: string | null;
  exposed_to_ai?: boolean;
  ordinal?: number;
  edited_fields?: string[];
}

export interface SemanticTable {
  name: string;
  display_name: string;
  description: string;
  synonyms: string[];
  edited_fields?: string[];
  columns: SemanticColumn[];
}

export interface MetricDefinition {
  id?: UUID;
  key: string;
  name: string;
  expression: string;
  description: string;
  synonyms: string[];
  table?: string;
  is_default?: boolean;
  is_active?: boolean;
  status?: "valid" | "invalid" | string;
  error?: string | null;
}

/** GET /datasets/{id}/semantic-model (generated on first access). */
export interface SemanticModel {
  id: UUID;
  dataset_id: UUID;
  dataset_version_id: UUID | null;
  version: number | null;
  name: string;
  description: string;
  status: "ready" | "failed" | string;
  last_error?: { code?: string; message?: string } | null;
  generated_at?: ISODate | null;
  updated_at?: ISODate | null;
  tables: SemanticTable[];
  metrics: MetricDefinition[];
}

/* ---------- Documents (AI/ML, `apps/documents`) ---------- */

export interface DocumentItem {
  id: UUID;
  title: string;
  filename: string;
  mime?: string;
  file_kind?: string;
  size_bytes: number;
  sha256?: string;
  classification: Classification;
  classification_attested_at?: ISODate | null;
  doc_type?: string | null;
  status: "processing" | "ready" | "failed" | string;
  page_count: number | null;
  language?: string | null;
  chunk_count?: number | null;
  ocr_pages?: number | null;
  ocr_unavailable_pages?: number | null;
  error?: string | null;
  error_code?: string | null;
  warnings?: string[] | null;
  last_job_id?: UUID | null;
  created_at: ISODate;
  updated_at?: ISODate;
  processed_at?: ISODate | null;
  /* detail only */
  languages?: Record<string, number> | string[] | null;
  doc_type_scores?: Record<string, number> | null;
  summary?: string | null;
  /** A signed `/api/v1/files/...` URL for the original file, when the backend exposes one. */
  download_url?: string | null;
}

export interface DocumentPage {
  id: UUID;
  number: number;
  text: string;
  method: "text" | "ocr" | "ocr_unavailable" | "ocr_failed" | "empty" | string;
  ocr_confidence?: number | null;
  char_count?: number;
  language?: string | null;
  label?: string | null;
}

export interface DocumentChunk {
  id: UUID;
  ordinal: number;
  page: number | null;
  page_to: number | null;
  section?: string | null;
  text: string;
  token_count?: number;
  has_embedding?: boolean;
}

export interface DocumentEntity {
  id: UUID;
  kind: string;
  value: string;
  normalized?: string | null;
  is_pii?: boolean;
  confidence?: number | null;
  page: number | null;
  chunk_id?: UUID | null;
}

export interface DocumentTable {
  id: UUID;
  index: number;
  page: number | null;
  title?: string | null;
  header: string[];
  rows: unknown[][];
  n_rows?: number;
  n_cols?: number;
  truncated?: boolean;
}

export interface DocumentSearchHit {
  chunk_id: UUID;
  document_id: UUID;
  document_title?: string;
  page: number | null;
  page_to?: number | null;
  section?: string | null;
  text: string;
  score?: number;
  highlights?: Array<[number, number]>;
}

export interface DocumentCitation {
  document_id: UUID;
  document_title?: string;
  page: number;
  chunk_id?: UUID;
  /** The verified quote (backend `quote`). */
  text?: string;
  source_id?: string;
}

/** POST /documents/ask. `mode: "extractive"` with a `reason` when the policy (or AI availability) forbids generation. */
export interface DocumentAnswer {
  question?: string;
  answer: string;
  mode: "generative" | "extractive" | string;
  citations: Array<DocumentCitation & { quote?: string }>;
  passages?: DocumentSearchHit[];
  confidence?: number | null;
  unanswerable?: boolean;
  reason?: string | null;
  message?: string | null;
  not_found?: boolean;
}
