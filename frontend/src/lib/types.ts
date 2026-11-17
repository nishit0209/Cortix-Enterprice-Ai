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
}

/* ---------- AI (W3) ---------- */

export interface AiStatus {
  policy: AIDataPolicy;
  tier: string;
  configured: boolean;
  org_budget: { limit: number; used: number };
  breaker: "closed" | "open_minute" | "open_day";
  resets_at: ISODate | null;
}

export interface Insight {
  kind: string;
  title: string;
  text: string;
  dataset_id: UUID | null;
}
