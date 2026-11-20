/**
 * Plain-language copy for chat / SQL errors: AI policy and quota codes plus the SQL guard reject codes
 * (backend/nexora/contracts.py GUARD_REJECT_CODES, SECURITY_ARCHITECTURE §5.1).
 */

export type ChatErrorKind = "policy" | "quota" | "not_configured" | "guard" | "timeout" | "ai" | "generic";

export interface ChatErrorCopy {
  kind: ChatErrorKind;
  title: string;
  description: string;
  /** Whether the card should show the SQL that was rejected or failed. */
  showSql: boolean;
}

/** Mirror of GUARD_REJECT_CODES (frozen W2 contract). */
export const GUARD_REJECT_CODES = [
  "parse_error", "too_large", "multiple_statements", "not_select", "forbidden_clause", "forbidden_function",
  "forbidden_cast", "table_not_allowed", "column_not_allowed", "unknown_table", "unknown_column",
  "ambiguous_column", "system_object", "recursive_cte", "set_returning_function", "type_error",
  "invalid_sql", "cost_exceeded", "plan_relation_not_allowed", "execution_error", "timeout",
] as const;

const GUARD_COPY: Record<string, string> = {
  parse_error: "The SQL couldn't be parsed. Check for a typo or an unfinished clause.",
  too_large: "The query text is longer than the allowed size.",
  multiple_statements: "Only one statement can run at a time. Remove the extra statements after the first `;`.",
  not_select: "Only read-only SELECT queries are allowed. Writes and DDL are blocked.",
  forbidden_clause: "The query uses a clause that is not allowed (for example FOR UPDATE, INTO or COPY).",
  forbidden_function: "The query calls a function that isn't on the allow-list.",
  forbidden_cast: "The query uses a type cast that isn't allowed.",
  table_not_allowed: "The query references a table you don't have access to in this dataset.",
  column_not_allowed: "The query references a column your access policy denies.",
  unknown_table: "The query references a table that doesn't exist in this dataset.",
  unknown_column: "The query references a column that doesn't exist in this dataset.",
  ambiguous_column: "A column name is ambiguous. Qualify it with its table name.",
  system_object: "System catalogs and internal schemas can't be queried.",
  recursive_cte: "Recursive CTEs are not allowed.",
  set_returning_function: "Set-returning functions (such as generate_series) are not allowed.",
  type_error: "A value or comparison has the wrong type for its column.",
  invalid_sql: "The SQL is not valid for this dataset.",
  cost_exceeded: "The query is too expensive to run. Add filters or aggregate to scan less data.",
  plan_relation_not_allowed: "The query plan touches a relation outside your allowed tables.",
  execution_error: "The database reported an error while running the query.",
  timeout: "The query ran longer than the statement timeout. Add filters or aggregate first.",
};

export function isGuardCode(code: string | null | undefined): boolean {
  return !!code && (GUARD_REJECT_CODES as readonly string[]).includes(code);
}

export function guardMessage(code: string | null | undefined): string | null {
  return code ? (GUARD_COPY[code] ?? null) : null;
}

export function chatErrorCopy(code: string, detail?: string | null, rejectCode?: string | null): ChatErrorCopy {
  const guard = isGuardCode(code) ? code : isGuardCode(rejectCode) ? rejectCode! : code === "sql_rejected" && isGuardCode(detail) ? detail! : null;
  switch (code) {
    case "ai_policy_blocked":
      return {
        kind: "policy",
        title: "Blocked by your AI data policy",
        description:
          "Answering this needs Gemini, and your organization's AI data policy does not allow sending this dataset to it. Cached and builder answers still work for supported questions.",
        showSql: false,
      };
    case "ai_policy_consent_required":
      return {
        kind: "policy",
        title: "Consent required",
        description: "This needs the schema_only policy, which requires a valid consent from an owner or admin.",
        showSql: false,
      };
    case "ai_quota_exhausted":
      return {
        kind: "quota",
        title: "AI budget used up",
        description: "Your organization's Gemini budget is exhausted. Cached and builder answers keep working until it resets.",
        showSql: false,
      };
    case "ai_not_configured":
      return {
        kind: "not_configured",
        title: "Gemini isn't configured",
        description:
          "This deployment has no Gemini API key, so questions that need the AI can't be answered. Questions the deterministic builder or cache understands still work.",
        showSql: false,
      };
    case "ai_unavailable":
    case "ai_limiter_unavailable":
      return {
        kind: "ai",
        title: "AI is temporarily unavailable",
        description: "Gemini or the quota limiter is unreachable, or the circuit breaker is open. Try again shortly.",
        showSql: false,
      };
    case "ai_invalid_output":
      return {
        kind: "ai",
        title: "The AI answer couldn't be used",
        description: "Gemini's SQL failed validation after a repair attempt. Rephrase the question or write the SQL yourself.",
        showSql: true,
      };
    case "query_timeout":
      return { kind: "timeout", title: "The query timed out", description: GUARD_COPY.timeout, showSql: true };
    default:
      break;
  }
  if (guard) {
    return {
      kind: guard === "timeout" ? "timeout" : "guard",
      title: guard === "execution_error" ? "The query failed" : "SQL rejected by the guard",
      description: GUARD_COPY[guard] ?? "The query violates a security rule.",
      showSql: true,
    };
  }
  if (code === "sql_rejected") {
    return { kind: "guard", title: "SQL rejected by the guard", description: detail || "The query violates a security rule.", showSql: true };
  }
  if (code === "clarification_needed") {
    return { kind: "generic", title: "Can you clarify?", description: detail || "The question is ambiguous for this dataset.", showSql: false };
  }
  return {
    kind: "generic",
    title: "Couldn't answer that",
    description: detail || "Something went wrong while answering. Try again or rephrase the question.",
    showSql: true,
  };
}

export const SOURCE_LABELS: Record<string, string> = {
  cache_exact: "Cache",
  cache_semantic: "Semantic cache",
  builder: "Builder",
  llm: "Gemini",
  manual: "Manual",
  dashboard: "Dashboard",
  none: "None",
};

export function sourceTone(source: string | null | undefined): "teal" | "info" | "accent" | "neutral" | "warning" {
  switch (source) {
    case "cache_exact":
    case "cache_semantic":
      return "teal";
    case "builder":
      return "info";
    case "llm":
      return "accent";
    case "manual":
      return "warning";
    case "dashboard":
      return "info";
    default:
      return "neutral";
  }
}

/** Guard verdicts of the query audit log (`apps/bi` BiQueryAudit.verdict: ok | rejected | error | blocked). */
const VERDICT_LABELS: Record<string, string> = {
  ok: "Allowed",
  allowed: "Allowed",
  rejected: "Rejected",
  error: "Error",
  blocked: "Blocked by policy",
  blocked_by_policy: "Blocked by policy",
  timeout: "Timed out",
};

export function verdictLabel(verdict: string): string {
  return VERDICT_LABELS[verdict] ?? verdict.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function verdictTone(verdict: string): "success" | "danger" | "warning" | "neutral" {
  if (verdict === "ok" || verdict === "allowed") return "success";
  if (verdict === "rejected" || verdict === "error") return "danger";
  if (verdict === "blocked" || verdict === "blocked_by_policy" || verdict === "timeout") return "warning";
  return "neutral";
}

/** True for a query the guard let through. */
export function isAllowedVerdict(verdict: string | null | undefined): boolean {
  return verdict === "ok" || verdict === "allowed";
}
