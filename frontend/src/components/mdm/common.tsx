"use client";

import * as React from "react";
import { CircleDashed, Lock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { controlBase } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status";
import { isApiError } from "@/lib/api";
import { isUnavailableEndpoint } from "@/lib/errors";
import { compareRecords, featureScores, statEntries } from "@/lib/mdm";
import { useDatasetColumns } from "@/lib/queries/datasets";
import { cn, formatDateTime, formatNumber, formatPercent, formatRelative, titleCase } from "@/lib/utils";

/** Error state that explains 404/405/501 as "not deployed" and 403 as a missing permission. */
export function ModuleError({ error, what, onRetry, compact }: { error: unknown; what: string; onRetry?: () => void; compact?: boolean }) {
  if (isApiError(error) && error.status === 403) {
    return <EmptyState compact={compact} icon={<Lock />} title={`You can't view ${what.toLowerCase()}`} description="Your role doesn't include the permission for this. Ask an organization admin." />;
  }
  if (isUnavailableEndpoint(error)) {
    return (
      <EmptyState
        compact={compact}
        icon={<CircleDashed />}
        title={`${what} isn't available on this server`}
        description="The service behind this view isn't deployed yet. Nothing is simulated; it fills in once the backend is running."
      />
    );
  }
  return <ErrorState compact={compact} error={error} onRetry={onRetry} />;
}

export function NativeSelect({ className, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(controlBase, "h-9 px-2.5 text-sm", className)} {...props} />;
}

/** A 0..1 score as a bar + percentage. */
export function ScoreBar({ value, className, label }: { value: number | null | undefined; className?: string; label?: string }) {
  if (value === null || value === undefined || !Number.isFinite(value)) return <span className="text-fg-subtle">—</span>;
  const pct = Math.max(0, Math.min(1, value)) * 100;
  const tone = value >= 0.9 ? "bg-success" : value >= 0.7 ? "bg-warning" : "bg-danger";
  return (
    <span className={cn("flex items-center gap-2", className)} title={label}>
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-surface-2" aria-hidden>
        <span className={cn("block h-full rounded-full", tone)} style={{ width: `${pct}%` }} />
      </span>
      <span className="tabular text-xs font-medium">{formatPercent(value, 0)}</span>
    </span>
  );
}

/** Per-feature similarity of a match (email, phone, name, ...). */
export function FeatureBars({ features }: { features: unknown }) {
  const rows = featureScores(features);
  if (!rows.length) return null;
  return (
    <dl className="grid gap-1.5 sm:grid-cols-2">
      {rows.map((f) => (
        <div key={f.name} className="flex items-center justify-between gap-3 text-xs">
          <dt className="text-fg-muted">{titleCase(f.name.replace(/_/g, " "))}</dt>
          <dd>
            <ScoreBar value={f.value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Side-by-side comparison of two source records; differing values are highlighted. */
export function CompareRecords({
  a,
  b,
  labelA,
  labelB,
  priority,
}: {
  a: Record<string, unknown> | undefined;
  b: Record<string, unknown> | undefined;
  labelA: React.ReactNode;
  labelB: React.ReactNode;
  priority?: string[];
}) {
  const rows = compareRecords(a, b, priority);
  if (!rows.length) return <p className="text-sm text-fg-muted">The records' attributes aren't available (they may be restricted by policy).</p>;
  return (
    <div className="overflow-x-auto rounded-control border border-border">
      <table className="w-full text-sm">
        <thead className="bg-surface-2 text-xs text-fg-muted">
          <tr>
            <th className="w-36 px-3 py-2 text-left font-medium">Field</th>
            <th className="px-3 py-2 text-left font-medium">{labelA}</th>
            <th className="px-3 py-2 text-left font-medium">{labelB}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-t border-border align-top">
              <th scope="row" className="px-3 py-1.5 text-left text-xs font-medium text-fg-muted">
                {r.key.replace(/_/g, " ")}
              </th>
              {[r.a, r.b].map((v, i) => (
                <td
                  key={i}
                  className={cn("px-3 py-1.5 break-words", r.state === "different" && "bg-warning-soft/60 text-fg", r.state === "same" && "text-fg-muted")}
                >
                  {v || <span className="text-fg-subtle">—</span>}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="border-t border-border px-3 py-1.5 text-[11px] text-fg-subtle">Highlighted rows differ between the two records.</p>
    </div>
  );
}

export function RunStats({ stats }: { stats: unknown }) {
  const entries = statEntries(stats);
  if (!entries.length) return null;
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
      {entries.slice(0, 18).map((e) => (
        <div key={e.label} className="flex justify-between gap-2">
          <dt className="truncate text-fg-muted">{e.label}</dt>
          <dd className="tabular font-medium">{typeof e.value === "number" ? formatNumber(e.value) : String(e.value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export interface RunRow {
  id: string;
  kind?: string;
  status: string;
  stats: unknown;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export function RunList({ runs, loading, empty }: { runs: RunRow[] | undefined; loading: boolean; empty: React.ReactNode }) {
  if (loading && !runs) return <Skeleton className="h-32 rounded-card" />;
  if (!runs?.length) return <>{empty}</>;
  return (
    <ol className="space-y-2">
      {runs.map((r) => (
        <li key={r.id} className="rounded-control border border-border p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <StatusBadge status={r.status} />
            {r.kind && <Badge tone="outline">{titleCase(r.kind)}</Badge>}
            <span className="text-fg-muted" title={formatDateTime(r.created_at)}>
              Started {formatRelative(r.started_at ?? r.created_at)}
            </span>
            {r.finished_at && <span className="text-fg-subtle">· finished {formatRelative(r.finished_at)}</span>}
          </div>
          {r.error && <p className="mt-2 text-xs text-danger">{r.error}</p>}
          <div className="mt-2">
            <RunStats stats={r.stats} />
          </div>
        </li>
      ))}
    </ol>
  );
}

export interface MappingField {
  key: string;
  label: string;
  hint?: string;
  multi?: boolean;
}

/** One column picker per field, fed by the dataset's columns. PII columns are flagged (policy is enforced server-side). */
export function ColumnMapping({
  org,
  datasetId,
  fields,
  value,
  onChange,
}: {
  org: string;
  datasetId: string | null;
  fields: MappingField[];
  value: Record<string, string | string[]>;
  onChange: (next: Record<string, string | string[]>) => void;
}) {
  const cols = useDatasetColumns(org, datasetId ?? "", !!datasetId);
  if (!datasetId) return <p className="text-sm text-fg-muted">Choose a dataset to map its columns.</p>;
  if (cols.isLoading) return <Skeleton className="h-40" />;
  if (cols.isError) return <ModuleError compact error={cols.error} what="Dataset columns" onRetry={() => void cols.refetch()} />;
  const columns = cols.data ?? [];
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {fields.map((f) => {
        const id = `map-${f.key}`;
        const current = value[f.key];
        if (f.multi) {
          const selected = Array.isArray(current) ? current : current ? [current] : [];
          return (
            <fieldset key={f.key} className="sm:col-span-2">
              <legend className="mb-1 text-xs font-medium text-fg">{f.label}</legend>
              <div className="flex max-h-28 flex-wrap gap-1.5 overflow-y-auto">
                {columns.map((c) => {
                  const on = selected.includes(c.name);
                  return (
                    <button
                      key={c.name}
                      type="button"
                      aria-pressed={on}
                      onClick={() => onChange({ ...value, [f.key]: on ? selected.filter((x) => x !== c.name) : [...selected, c.name] })}
                      className={cn("rounded-full border px-2 py-0.5 text-xs focus-ring", on ? "border-accent bg-accent-soft text-accent-soft-fg" : "border-border text-fg-muted hover:bg-surface-2")}
                    >
                      {c.name}
                    </button>
                  );
                })}
              </div>
              {f.hint && <p className="mt-1 text-[11px] text-fg-subtle">{f.hint}</p>}
            </fieldset>
          );
        }
        return (
          <div key={f.key}>
            <label htmlFor={id} className="mb-1 block text-xs font-medium text-fg">
              {f.label}
            </label>
            <NativeSelect
              id={id}
              value={typeof current === "string" ? current : ""}
              onChange={(e) => {
                const next = { ...value };
                if (e.target.value) next[f.key] = e.target.value;
                else delete next[f.key];
                onChange(next);
              }}
            >
              <option value="">Not mapped</option>
              {columns.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.name} ({c.data_type}){c.is_pii ? " · PII" : ""}
                </option>
              ))}
            </NativeSelect>
            {f.hint && <p className="mt-1 text-[11px] text-fg-subtle">{f.hint}</p>}
          </div>
        );
      })}
    </div>
  );
}

/** Columns a 400 `column_not_allowed` names (masked/denied by policy). */
export function excludedByPolicy(error: unknown): string[] {
  if (!isApiError(error)) return [];
  const body = error.body as { excluded_by_policy?: unknown } | null;
  return Array.isArray(body?.excluded_by_policy) ? body.excluded_by_policy.map(String) : [];
}
