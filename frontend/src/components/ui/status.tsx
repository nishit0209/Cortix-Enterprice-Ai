import * as React from "react";
import { CircleDashed, FlaskConical, Hammer, Sparkles } from "lucide-react";
import { Badge, type BadgeProps } from "./badge";
import { cn, titleCase } from "@/lib/utils";

type Tone = NonNullable<BadgeProps["tone"]>;

const STATUS_TONES: Record<string, { tone: Tone; pulse?: boolean; label?: string }> = {
  queued: { tone: "neutral" },
  pending: { tone: "neutral" },
  draft: { tone: "neutral" },
  invited: { tone: "info" },
  running: { tone: "info", pulse: true },
  ingesting: { tone: "info", pulse: true },
  profiling: { tone: "info", pulse: true },
  processing: { tone: "info", pulse: true },
  scanning: { tone: "info", pulse: true },
  loading: { tone: "info", pulse: true },
  retrying: { tone: "warning", pulse: true },
  succeeded: { tone: "success" },
  success: { tone: "success" },
  ready: { tone: "success" },
  active: { tone: "success" },
  passed: { tone: "success" },
  accepted: { tone: "success" },
  uploaded: { tone: "success" },
  warning: { tone: "warning" },
  suspended: { tone: "warning" },
  paused: { tone: "warning" },
  expired: { tone: "warning" },
  failed: { tone: "danger" },
  error: { tone: "danger" },
  rejected: { tone: "danger" },
  cancelled: { tone: "neutral", label: "Cancelled" },
  revoked: { tone: "neutral" },
  archived: { tone: "neutral" },
  retired: { tone: "neutral" },
  disabled: { tone: "neutral" },
  deleted: { tone: "danger" },
};

/** Run/job/resource status pill (queued, running, succeeded, failed, retrying, ...). */
export function StatusBadge({ status, className }: { status: string | null | undefined; className?: string }) {
  const key = (status ?? "unknown").toLowerCase();
  const cfg = STATUS_TONES[key] ?? { tone: "neutral" as Tone };
  return (
    <Badge tone={cfg.tone} dot pulse={cfg.pulse} className={className}>
      {cfg.label ?? titleCase(key)}
    </Badge>
  );
}

export type FeatureStatus = "working" | "prototype" | "mock" | "planned";

const FEATURE: Record<FeatureStatus, { tone: Tone; label: string; icon: React.ReactNode; hint: string }> = {
  working: { tone: "success", label: "Working", icon: <Sparkles aria-hidden />, hint: "Backed by real services and data." },
  prototype: { tone: "info", label: "Prototype", icon: <FlaskConical aria-hidden />, hint: "Functional, but limited in scope." },
  mock: { tone: "warning", label: "Mock", icon: <Hammer aria-hidden />, hint: "Shows sample behavior only." },
  planned: { tone: "outline", label: "Planned", icon: <CircleDashed aria-hidden />, hint: "Not built yet. No data is shown." },
};

/** Honest module maturity badge (UI/UX spec principle 5). */
export function FeatureStatusBadge({ status, className }: { status: FeatureStatus; className?: string }) {
  const f = FEATURE[status];
  return (
    <Badge tone={f.tone} className={cn("gap-1", className)} title={f.hint}>
      {f.icon}
      {f.label}
    </Badge>
  );
}

const CLASSIFICATION: Record<string, { tone: Tone; hint: string }> = {
  synthetic: { tone: "teal", hint: "Attested synthetic data. Eligible for AI under every policy." },
  public: { tone: "info", hint: "Attested public data. Eligible for AI under every policy." },
  internal: { tone: "neutral", hint: "Internal data. Not sent to Gemini under synthetic_only." },
  confidential: { tone: "danger", hint: "Confidential data. Never sent to Gemini values-wise." },
};

export function ClassificationBadge({ value, className }: { value: string | null | undefined; className?: string }) {
  const key = (value ?? "internal").toLowerCase();
  const c = CLASSIFICATION[key] ?? CLASSIFICATION.internal;
  return (
    <Badge tone={c.tone} className={className} title={c.hint}>
      {titleCase(key)}
    </Badge>
  );
}

export const POLICY_LABELS: Record<string, string> = {
  synthetic_only: "Synthetic only",
  schema_only: "Schema only",
  aggregates_only: "Aggregates only",
  full: "Full",
};
