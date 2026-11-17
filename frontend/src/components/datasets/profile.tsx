"use client";

import * as React from "react";
import { Fingerprint, Hash, KeyRound, ListTree, Type } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { HistogramChart } from "@/components/dashboard/charts";
import type { ColumnStats, DatasetColumn } from "@/lib/types";
import { cn, formatNumber, formatPercent } from "@/lib/utils";

/** Normalise the two histogram encodings the profiler may return into `{label, count}[]`. */
export function normalizeHistogram(h: ColumnStats["histogram"]): Array<{ label: string; count: number }> {
  if (!h) return [];
  if (Array.isArray(h)) {
    return h.map((b, i) => ({
      label:
        b.label ??
        (b.bin !== undefined
          ? String(b.bin)
          : b.start !== undefined && b.end !== undefined
            ? `${fmt(b.start)}–${fmt(b.end)}`
            : String(i)),
      count: Number(b.count) || 0,
    }));
  }
  if (Array.isArray(h.counts)) {
    const bins = h.bins ?? [];
    return h.counts.map((c, i) => {
      const lo = bins[i];
      const hi = bins[i + 1];
      const label = lo !== undefined && hi !== undefined ? `${fmt(lo)}–${fmt(hi)}` : lo !== undefined ? String(lo) : String(i);
      return { label, count: Number(c) || 0 };
    });
  }
  return [];
}

export function normalizeTopValues(t: ColumnStats["top_values"]): Array<{ value: string; count: number }> {
  if (!t) return [];
  if (Array.isArray(t)) return t.map((x) => ({ value: display(x.value), count: Number(x.count) || 0 }));
  return Object.entries(t).map(([value, count]) => ({ value, count: Number(count) || 0 }));
}

function fmt(v: unknown): string {
  if (typeof v === "number") return Math.abs(v) >= 1000 ? formatNumber(v, { compact: true }) : String(Number(v.toFixed(2)));
  return String(v);
}

function display(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "number") return fmt(v);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function typeIcon(t: string) {
  const s = t.toLowerCase();
  if (/(int|float|double|decimal|numeric|number)/.test(s)) return Hash;
  if (/(date|time)/.test(s)) return ListTree;
  if (/(bool)/.test(s)) return KeyRound;
  return Type;
}

/** Null % arrives as 0–1 or 0–100. */
function nullPct(v: number | null | undefined): number | null {
  if (v === null || v === undefined) return null;
  return v <= 1 ? v * 100 : v;
}

export function ColumnProfileCard({ column }: { column: DatasetColumn }) {
  const s = column.stats;
  const hist = normalizeHistogram(s?.histogram ?? null);
  const top = normalizeTopValues(s?.top_values ?? null).slice(0, 4);
  const topMax = Math.max(1, ...top.map((t) => t.count));
  const nulls = nullPct(s?.null_pct);
  const TypeIcon = typeIcon(column.data_type);

  return (
    <Card className="flex flex-col p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <TypeIcon className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
            <h3 className="truncate font-mono text-sm font-medium text-fg" title={column.name}>
              {column.name}
            </h3>
          </div>
          {column.source_name && column.source_name !== column.name && (
            <p className="mt-0.5 truncate text-xs text-fg-subtle" title={column.source_name}>
              from “{column.source_name}”
            </p>
          )}
        </div>
        {column.is_pii && (
          <Badge tone="danger" title="Personally identifiable information detected">
            <Fingerprint /> PII
          </Badge>
        )}
      </div>
      <div className="mt-2.5 flex flex-wrap gap-1.5">
        <Badge tone="outline" className="font-mono">
          {column.data_type}
        </Badge>
        {column.semantic_type && <Badge tone="accent">{column.semantic_type.replace(/_/g, " ")}</Badge>}
        {!column.nullable && <Badge tone="neutral">required</Badge>}
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <Stat label="Null">
          <span className={cn(nulls !== null && nulls > 20 && "text-warning")}>{formatPercent(nulls, nulls !== null && nulls < 10 ? 1 : 0)}</span>
        </Stat>
        <Stat label="Distinct">{formatNumber(s?.distinct ?? null)}</Stat>
        {s?.min !== undefined && s?.min !== null && <Stat label="Min">{display(s.min)}</Stat>}
        {s?.max !== undefined && s?.max !== null && <Stat label="Max">{display(s.max)}</Stat>}
        {s?.mean !== undefined && s?.mean !== null && <Stat label="Mean">{fmt(s.mean)}</Stat>}
        {s?.p50 !== undefined && s?.p50 !== null && <Stat label="Median">{fmt(s.p50)}</Stat>}
      </dl>

      {nulls !== null && (
        <div className="mt-3 h-1 overflow-hidden rounded-full bg-surface-3" aria-hidden>
          <div className="h-full bg-teal" style={{ width: `${Math.max(0, 100 - nulls)}%` }} />
        </div>
      )}

      <div className="mt-4 flex-1">
        {hist.length > 1 ? (
          <div className="h-16" aria-label={`Distribution of ${column.name}`} role="img">
            <HistogramChart data={hist} />
          </div>
        ) : top.length > 0 ? (
          <ul className="space-y-1.5" aria-label={`Most frequent values of ${column.name}`}>
            {top.map((t) => (
              <li key={t.value} className="relative overflow-hidden rounded-[4px] px-2 py-1 text-xs">
                <span className="absolute inset-y-0 left-0 bg-accent-soft" style={{ width: `${(t.count / topMax) * 100}%` }} aria-hidden />
                <span className="relative flex justify-between gap-2">
                  <span className="truncate text-fg">{t.value}</span>
                  <span className="tabular text-fg-muted">{formatNumber(t.count)}</span>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-fg-subtle">No distribution available.</p>
        )}
      </div>
    </Card>
  );
}

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-fg-subtle">{label}</dt>
      <dd className="tabular truncate font-medium text-fg">{children}</dd>
    </div>
  );
}
