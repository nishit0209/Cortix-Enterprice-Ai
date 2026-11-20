"use client";

import * as React from "react";
import { Plus, Trash2, TriangleAlert, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/field";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { AGG_FUNCS, CAST_TYPES, CATEGORY_META, FILTER_OPS, STEP_BY_TYPE, type FieldSpec } from "@/lib/pipelines/catalog";
import type { SchemaColumn } from "@/lib/pipelines/dag";
import type { Dataset } from "@/lib/types";
import { cn } from "@/lib/utils";
import { STEP_ICONS } from "./step-node";

type Config = Record<string, unknown>;

export interface InspectorProps {
  nodeId: string;
  stepType: string;
  config: Config;
  onChange: (config: Config) => void;
  onDelete: () => void;
  input: SchemaColumn[] | null;
  rightInput?: SchemaColumn[] | null;
  output: SchemaColumn[] | null;
  issues: string[];
  datasets: Dataset[];
  readOnly?: boolean;
}

/** Per-node config side panel: a typed form for the step, its issues and its inferred output schema. */
export function Inspector({ nodeId, stepType, config, onChange, onDelete, input, rightInput, output, issues, datasets, readOnly }: InspectorProps) {
  const spec = STEP_BY_TYPE[stepType];
  if (!spec) return <p className="p-4 text-sm text-fg-muted">Unknown step type “{stepType}”.</p>;
  const color = CATEGORY_META[spec.category].color;
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-start gap-3 border-b border-border p-4">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-control [&_svg]:size-4" style={{ background: `color-mix(in oklab, ${color} 14%, transparent)`, color }} aria-hidden>
          {STEP_ICONS[stepType]}
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-fg">{spec.label}</h2>
          <p className="text-xs text-fg-muted">{spec.description}</p>
          <p className="mt-1 font-mono text-[11px] text-fg-subtle">{nodeId}</p>
        </div>
        {!readOnly && (
          <Button variant="danger-ghost" size="icon-xs" onClick={onDelete} aria-label="Delete step">
            <Trash2 />
          </Button>
        )}
      </div>
      <div className="scrollbar-thin min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {issues.length > 0 && (
          <ul className="space-y-1 rounded-control border border-danger/30 bg-danger-soft/60 px-3 py-2 text-xs text-danger" role="alert">
            {issues.map((i) => (
              <li key={i} className="flex gap-1.5">
                <TriangleAlert className="mt-0.5 size-3 shrink-0" aria-hidden /> {i}
              </li>
            ))}
          </ul>
        )}
        <fieldset disabled={readOnly} className="space-y-4">
          {spec.fields
            .filter((f) => !f.showIf || f.showIf.in.includes(String(config[f.showIf.key] ?? "")))
            .map((f) => (
              <FieldEditor key={f.key} field={f} value={config[f.key]} onChange={(v) => set(f.key, v)} input={input} rightInput={rightInput} datasets={datasets} id={`${nodeId}-${f.key}`} />
            ))}
        </fieldset>
        <div className="space-y-1.5 border-t border-border pt-4">
          <div className="text-[11px] font-semibold uppercase tracking-[0.08em] text-fg-subtle">Output schema</div>
          {output ? (
            output.length === 0 ? (
              <p className="text-xs text-fg-muted">No columns.</p>
            ) : (
              <ul className="flex flex-wrap gap-1">
                {output.map((c) => (
                  <li key={c.name}>
                    <Badge tone="outline" className="font-mono" title={c.type ?? "type decided at run time"}>
                      {c.name}
                    </Badge>
                  </li>
                ))}
              </ul>
            )
          ) : (
            <p className="text-xs text-fg-muted">Connect an input (and pick the source dataset) to see the columns.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function names(cols: SchemaColumn[] | null | undefined): string[] | null {
  return cols ? cols.map((c) => c.name) : null;
}

function FieldEditor({
  field,
  value,
  onChange,
  input,
  rightInput,
  datasets,
  id,
}: {
  field: FieldSpec;
  value: unknown;
  onChange: (v: unknown) => void;
  input: SchemaColumn[] | null;
  rightInput?: SchemaColumn[] | null;
  datasets: Dataset[];
  id: string;
}) {
  const cols = names(input);
  const label = (
    <div className="flex items-center justify-between">
      <Label htmlFor={id}>
        {field.label}
        {field.required && <span className="text-danger"> *</span>}
      </Label>
    </div>
  );
  const hint = field.hint ? <p className="text-xs text-fg-muted">{field.hint}</p> : null;

  switch (field.kind) {
    case "dataset":
      return (
        <div className="space-y-1.5">
          {label}
          <Select value={String(value ?? "") || undefined} onValueChange={onChange}>
            <SelectTrigger id={id}>
              <SelectValue placeholder="Choose a dataset" />
            </SelectTrigger>
            <SelectContent>
              {datasets
                .filter((d) => String(d.status) === "ready")
                .map((d) => (
                  <SelectItem key={d.id} value={d.id}>
                    {d.name}
                  </SelectItem>
                ))}
            </SelectContent>
          </Select>
        </div>
      );
    case "select":
      return (
        <div className="space-y-1.5">
          {label}
          <Select value={String(value ?? "") || undefined} onValueChange={onChange}>
            <SelectTrigger id={id}>
              <SelectValue placeholder="Choose…" />
            </SelectTrigger>
            <SelectContent>
              {(field.options ?? []).map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      );
    case "text":
    case "number":
      return (
        <div className="space-y-1.5">
          {label}
          <Input id={id} type={field.kind === "number" ? "number" : "text"} value={String(value ?? "")} placeholder={field.placeholder} onChange={(e) => onChange(e.target.value)} />
          {hint}
        </div>
      );
    case "expression":
      return (
        <div className="space-y-1.5">
          {label}
          <Textarea id={id} value={String(value ?? "")} placeholder={field.placeholder} onChange={(e) => onChange(e.target.value)} className="min-h-16 font-mono text-xs" spellCheck={false} />
          <p className="text-xs text-fg-muted">Columns, literals, arithmetic, comparisons, CASE, COALESCE, LOWER/UPPER/TRIM. Validated on save.</p>
        </div>
      );
    case "column":
      return (
        <div className="space-y-1.5">
          {label}
          <ColumnSelect id={id} options={cols} value={String(value ?? "")} onChange={onChange} />
        </div>
      );
    case "columns":
      return (
        <div className="space-y-1.5">
          {label}
          <MultiColumn id={id} options={cols} value={Array.isArray(value) ? (value as string[]) : []} onChange={onChange} />
          {hint}
        </div>
      );
    case "rename_map": {
      const map = (value && typeof value === "object" ? value : {}) as Record<string, string>;
      const rows = Object.entries(map);
      return (
        <div className="space-y-1.5">
          {label}
          <RowList
            rows={rows}
            onAdd={() => {
              const next = (cols ?? []).find((c) => !(c in map)) ?? `column_${rows.length + 1}`;
              onChange({ ...map, [next]: "" });
            }}
            addLabel="Add rename"
            render={([from, to], i) => (
              <>
                <ColumnSelect
                  options={cols}
                  value={from}
                  aria-label={`Column ${i + 1}`}
                  onChange={(nf) => {
                    const entries = rows.map(([k, v]) => (k === from ? [nf, v] : [k, v]));
                    onChange(Object.fromEntries(entries));
                  }}
                />
                <span className="text-fg-subtle">→</span>
                <Input aria-label={`New name ${i + 1}`} value={to} placeholder="new_name" onChange={(e) => onChange({ ...map, [from]: e.target.value })} className="font-mono text-xs" />
              </>
            )}
            onRemove={(i) => onChange(Object.fromEntries(rows.filter((_, j) => j !== i)))}
          />
        </div>
      );
    }
    case "casts": {
      const rows = (Array.isArray(value) ? value : []) as Array<{ column: string; to: string }>;
      return (
        <div className="space-y-1.5">
          {label}
          <RowList
            rows={rows}
            addLabel="Add cast"
            onAdd={() => onChange([...rows, { column: cols?.[0] ?? "", to: "text" }])}
            onRemove={(i) => onChange(rows.filter((_, j) => j !== i))}
            render={(r, i) => (
              <>
                <ColumnSelect options={cols} value={r.column} aria-label={`Cast column ${i + 1}`} onChange={(c) => onChange(rows.map((x, j) => (j === i ? { ...x, column: c } : x)))} />
                <SmallSelect value={r.to} options={CAST_TYPES} aria-label="Target type" onChange={(t) => onChange(rows.map((x, j) => (j === i ? { ...x, to: t } : x)))} />
              </>
            )}
          />
        </div>
      );
    }
    case "conditions": {
      const rows = (Array.isArray(value) ? value : []) as Array<{ column: string; op: string; value?: unknown }>;
      return (
        <div className="space-y-1.5">
          {label}
          <RowList
            rows={rows}
            addLabel="Add condition"
            stacked
            onAdd={() => onChange([...rows, { column: cols?.[0] ?? "", op: "=", value: "" }])}
            onRemove={(i) => onChange(rows.filter((_, j) => j !== i))}
            render={(r, i) => {
              const upd = (patch: Partial<typeof r>) => onChange(rows.map((x, j) => (j === i ? { ...x, ...patch } : x)));
              const list = r.op === "in" || r.op === "not_in";
              return (
                <>
                  <ColumnSelect options={cols} value={r.column} aria-label={`Condition ${i + 1} column`} onChange={(c) => upd({ column: c })} />
                  <SmallSelect value={r.op} options={FILTER_OPS} aria-label="Operator" onChange={(op) => upd({ op, value: op === "in" || op === "not_in" ? [] : "" })} />
                  {r.op !== "is_null" && r.op !== "not_null" && (
                    <Input
                      aria-label={`Condition ${i + 1} value`}
                      value={list ? (Array.isArray(r.value) ? (r.value as unknown[]).join(", ") : "") : String(r.value ?? "")}
                      placeholder={list ? "a, b, c" : "value"}
                      onChange={(e) => upd({ value: list ? e.target.value.split(",").map((s) => s.trim()).filter(Boolean) : e.target.value })}
                    />
                  )}
                </>
              );
            }}
          />
        </div>
      );
    }
    case "aggregations": {
      const rows = (Array.isArray(value) ? value : []) as Array<{ column: string; fn: string; as?: string }>;
      return (
        <div className="space-y-1.5">
          {label}
          <RowList
            rows={rows}
            addLabel="Add aggregation"
            stacked
            onAdd={() => onChange([...rows, { column: cols?.[0] ?? "", fn: "sum", as: "" }])}
            onRemove={(i) => onChange(rows.filter((_, j) => j !== i))}
            render={(r, i) => {
              const upd = (patch: Partial<typeof r>) => onChange(rows.map((x, j) => (j === i ? { ...x, ...patch } : x)));
              return (
                <>
                  <SmallSelect value={r.fn} options={AGG_FUNCS} aria-label="Function" onChange={(fn) => upd({ fn })} />
                  <ColumnSelect options={cols ? ["*", ...cols] : null} value={r.column} aria-label={`Aggregation ${i + 1} column`} onChange={(c) => upd({ column: c })} />
                  <Input aria-label="Output name" value={r.as ?? ""} placeholder={`${r.fn}_${r.column || "col"}`} onChange={(e) => upd({ as: e.target.value })} className="font-mono text-xs" />
                </>
              );
            }}
          />
        </div>
      );
    }
    case "sorts": {
      const rows = (Array.isArray(value) ? value : []) as Array<{ column: string; desc?: boolean }>;
      return (
        <div className="space-y-1.5">
          {label}
          <RowList
            rows={rows}
            addLabel="Add sort key"
            onAdd={() => onChange([...rows, { column: cols?.[0] ?? "", desc: false }])}
            onRemove={(i) => onChange(rows.filter((_, j) => j !== i))}
            render={(r, i) => (
              <>
                <ColumnSelect options={cols} value={r.column} aria-label={`Sort ${i + 1} column`} onChange={(c) => onChange(rows.map((x, j) => (j === i ? { ...x, column: c } : x)))} />
                <SmallSelect
                  value={r.desc ? "desc" : "asc"}
                  options={[
                    { value: "asc", label: "Ascending" },
                    { value: "desc", label: "Descending" },
                  ]}
                  aria-label="Direction"
                  onChange={(d) => onChange(rows.map((x, j) => (j === i ? { ...x, desc: d === "desc" } : x)))}
                />
              </>
            )}
          />
        </div>
      );
    }
    case "join_keys": {
      const rows = (Array.isArray(value) ? value : []) as Array<{ left: string; right: string }>;
      const rcols = names(rightInput);
      return (
        <div className="space-y-1.5">
          {label}
          <RowList
            rows={rows}
            addLabel="Add key pair"
            onAdd={() => onChange([...rows, { left: cols?.[0] ?? "", right: rcols?.find((c) => c === cols?.[0]) ?? rcols?.[0] ?? "" }])}
            onRemove={(i) => onChange(rows.filter((_, j) => j !== i))}
            render={(r, i) => (
              <>
                <ColumnSelect options={cols} value={r.left} aria-label={`Left key ${i + 1}`} onChange={(c) => onChange(rows.map((x, j) => (j === i ? { ...x, left: c } : x)))} />
                <span className="text-fg-subtle">=</span>
                <ColumnSelect options={rcols} value={r.right} aria-label={`Right key ${i + 1}`} onChange={(c) => onChange(rows.map((x, j) => (j === i ? { ...x, right: c } : x)))} />
              </>
            )}
          />
        </div>
      );
    }
    default:
      return null;
  }
}

function RowList<T>({
  rows,
  render,
  onAdd,
  onRemove,
  addLabel,
  stacked,
}: {
  rows: T[];
  render: (row: T, i: number) => React.ReactNode;
  onAdd: () => void;
  onRemove: (i: number) => void;
  addLabel: string;
  stacked?: boolean;
}) {
  return (
    <div className="space-y-2">
      {rows.map((r, i) => (
        <div key={i} className={cn("flex items-center gap-1.5", stacked && "flex-wrap rounded-control border border-border p-2 [&>*]:min-w-0 [&>*]:flex-1")}>
          {render(r, i)}
          <Button type="button" variant="ghost" size="icon-xs" onClick={() => onRemove(i)} aria-label="Remove" className="!flex-none">
            <X />
          </Button>
        </div>
      ))}
      <Button type="button" variant="secondary" size="xs" onClick={onAdd}>
        <Plus /> {addLabel}
      </Button>
    </div>
  );
}

function SmallSelect({
  value,
  options,
  onChange,
  "aria-label": ariaLabel,
}: {
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (v: string) => void;
  "aria-label": string;
}) {
  return (
    <Select value={value || undefined} onValueChange={onChange}>
      <SelectTrigger className="h-8 min-w-0 text-xs" aria-label={ariaLabel}>
        <SelectValue placeholder="…" />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** Column picker from the inferred input schema; free text when the schema is unknown. */
function ColumnSelect({
  options,
  value,
  onChange,
  id,
  "aria-label": ariaLabel,
}: {
  options: string[] | null;
  value: string;
  onChange: (v: string) => void;
  id?: string;
  "aria-label"?: string;
}) {
  if (!options) {
    return <Input id={id} aria-label={ariaLabel} value={value} placeholder="column" onChange={(e) => onChange(e.target.value)} className="h-8 font-mono text-xs" />;
  }
  const opts = value && !options.includes(value) ? [value, ...options] : options;
  return (
    <Select value={value || undefined} onValueChange={onChange}>
      <SelectTrigger id={id} className="h-8 min-w-0 font-mono text-xs" aria-label={ariaLabel}>
        <SelectValue placeholder="column" />
      </SelectTrigger>
      <SelectContent>
        {opts.map((c) => (
          <SelectItem key={c} value={c} className={cn(!options.includes(c) && "text-danger")}>
            {c}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function MultiColumn({ options, value, onChange, id }: { options: string[] | null; value: string[]; onChange: (v: string[]) => void; id: string }) {
  if (!options) {
    return (
      <Input
        id={id}
        value={value.join(", ")}
        placeholder="col_a, col_b"
        onChange={(e) => onChange(e.target.value.split(",").map((s) => s.trim()).filter(Boolean))}
        className="font-mono text-xs"
      />
    );
  }
  const all = Array.from(new Set([...options, ...value]));
  return (
    <div id={id} className="flex flex-wrap gap-1" role="group">
      {all.map((c) => {
        const on = value.includes(c);
        const missing = !options.includes(c);
        return (
          <button
            key={c}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(on ? value.filter((v) => v !== c) : [...value, c])}
            className={cn(
              "rounded-full border px-2 py-0.5 font-mono text-[11px] transition-colors focus-ring",
              missing ? "border-danger/50 text-danger" : on ? "border-accent/40 bg-accent-soft text-accent-soft-fg" : "border-border text-fg-muted hover:border-border-strong hover:text-fg",
            )}
          >
            {c}
          </button>
        );
      })}
      {all.length > 1 && (
        <button type="button" onClick={() => onChange(value.length === options.length ? [] : [...options])} className="px-1 text-[11px] text-accent hover:underline focus-ring">
          {value.length === options.length ? "Clear" : "All"}
        </button>
      )}
    </div>
  );
}
