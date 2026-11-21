"use client";

import * as React from "react";
import { useOrgContext } from "@/components/shell/org-context";
import { Checkbox } from "@/components/ui/checkbox";
import { Input, Textarea } from "@/components/ui/input";
import type { JsonSchemaProp, StepSchema } from "@/lib/automation/catalog";
import { useDatasets } from "@/lib/queries/datasets";
import { useSavedQueries } from "@/lib/queries/dashboards";
import { useDocuments } from "@/lib/queries/documents";
import { useMlModels, useMlProjects } from "@/lib/queries/ml";
import { usePipelines } from "@/lib/queries/pipelines";
import { NativeSelect } from "@/components/mdm/common";

type Option = { value: string; label: string };

const REF_KINDS = new Set(["dataset", "pipeline", "model", "project", "document", "saved_query"]);

function RefOptions({ kind, children }: { kind: string; children: (o: { options: Option[]; loading: boolean }) => React.ReactNode }) {
  const { slug } = useOrgContext();
  switch (kind) {
    case "dataset":
      return <DatasetOpts slug={slug}>{children}</DatasetOpts>;
    case "pipeline":
      return <PipelineOpts slug={slug}>{children}</PipelineOpts>;
    case "model":
      return <ModelOpts slug={slug}>{children}</ModelOpts>;
    case "project":
      return <ProjectOpts slug={slug}>{children}</ProjectOpts>;
    case "document":
      return <DocumentOpts slug={slug}>{children}</DocumentOpts>;
    default:
      return <SavedQueryOpts slug={slug}>{children}</SavedQueryOpts>;
  }
}

type OptsProps = { slug: string; children: (o: { options: Option[]; loading: boolean }) => React.ReactNode };
function DatasetOpts({ slug, children }: OptsProps) {
  const q = useDatasets(slug);
  return <>{children({ options: (q.data ?? []).map((d) => ({ value: d.id, label: d.name })), loading: q.isLoading })}</>;
}
function PipelineOpts({ slug, children }: OptsProps) {
  const q = usePipelines(slug);
  return <>{children({ options: (q.data ?? []).map((d) => ({ value: d.id, label: d.name })), loading: q.isLoading })}</>;
}
function ModelOpts({ slug, children }: OptsProps) {
  const q = useMlModels(slug);
  return <>{children({ options: (q.data ?? []).map((d) => ({ value: d.id, label: d.name })), loading: q.isLoading })}</>;
}
function ProjectOpts({ slug, children }: OptsProps) {
  const q = useMlProjects(slug);
  return <>{children({ options: (q.data ?? []).map((d) => ({ value: d.id, label: d.name })), loading: q.isLoading })}</>;
}
function DocumentOpts({ slug, children }: OptsProps) {
  const q = useDocuments(slug);
  return <>{children({ options: (q.data ?? []).map((d) => ({ value: d.id, label: d.title || d.filename })), loading: q.isLoading })}</>;
}
function SavedQueryOpts({ slug, children }: OptsProps) {
  const q = useSavedQueries(slug);
  return <>{children({ options: (q.data ?? []).map((d) => ({ value: d.id, label: d.name })), loading: q.isLoading })}</>;
}
const typeOf = (p: JsonSchemaProp) => (Array.isArray(p.type) ? (p.type.find((t) => t !== "null") ?? "string") : (p.type ?? "string"));

function FieldInput({ name, prop, value, onChange, disabled }: { name: string; prop: JsonSchemaProp; value: unknown; onChange: (v: unknown) => void; disabled?: boolean }) {
  const id = `cfg-${name}`;
  const t = typeOf(prop);
  const refKind = t === "string" && /_id$/.test(name) ? name.replace(/_id$/, "") : "";
  if (prop.enum) {
    return (
      <NativeSelect id={id} value={value === undefined || value === null ? "" : String(value)} onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)} disabled={disabled}>
        <option value="">Choose…</option>
        {prop.enum.map((v) => (
          <option key={String(v)} value={String(v)}>
            {String(v)}
          </option>
        ))}
      </NativeSelect>
    );
  }
  if (REF_KINDS.has(refKind)) {
    return (
      <RefOptions kind={refKind}>
        {(res) => (
          <NativeSelect id={id} value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value || undefined)} disabled={disabled || res.loading}>
            <option value="">{res.loading ? "Loading…" : res.options.length ? "Choose…" : "None available"}</option>
            {res.options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
            {typeof value === "string" && value && !res.options.some((o) => o.value === value) && <option value={value}>{value}</option>}
          </NativeSelect>
        )}
      </RefOptions>
    );
  }
  if (t === "boolean") {
    return <Checkbox id={id} checked={value === true} onCheckedChange={(c) => onChange(c === true)} disabled={disabled} />;
  }
  if (t === "integer" || t === "number") {
    return (
      <Input
        id={id}
        type="number"
        min={prop.minimum}
        max={prop.maximum}
        step={t === "integer" ? 1 : "any"}
        value={typeof value === "number" ? value : ""}
        onChange={(e) => onChange(e.target.value === "" ? undefined : t === "integer" ? Math.trunc(Number(e.target.value)) : Number(e.target.value))}
        disabled={disabled}
      />
    );
  }
  if (t === "array") {
    const list = Array.isArray(value) ? value.map(String) : [];
    return <Input id={id} value={list.join(", ")} placeholder="comma, separated" onChange={(e) => onChange(e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} disabled={disabled} />;
  }
  if (t === "object") {
    return <JsonInput id={id} value={value} onChange={onChange} disabled={disabled} />;
  }
  return <Input id={id} value={typeof value === "string" ? value : value === undefined || value === null ? "" : String(value)} onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)} disabled={disabled} />;
}

export function JsonInput({ id, value, onChange, disabled, rows = 4 }: { id: string; value: unknown; onChange: (v: unknown) => void; disabled?: boolean; rows?: number }) {
  const [text, setText] = React.useState(() => (value === undefined ? "" : JSON.stringify(value, null, 2)));
  const [bad, setBad] = React.useState(false);
  return (
    <>
      <Textarea
        id={id}
        rows={rows}
        className="font-mono text-xs"
        value={text}
        aria-invalid={bad}
        disabled={disabled}
        onChange={(e) => {
          setText(e.target.value);
          if (!e.target.value.trim()) {
            setBad(false);
            return onChange(undefined);
          }
          try {
            onChange(JSON.parse(e.target.value));
            setBad(false);
          } catch {
            setBad(true);
          }
        }}
      />
      {bad && <p className="mt-1 text-[11px] text-danger">Not valid JSON yet.</p>}
    </>
  );
}

/** A form generated from a step's JSON schema. */
export function ConfigForm({ schema, value, onChange, disabled }: { schema: StepSchema; value: Record<string, unknown>; onChange: (next: Record<string, unknown>) => void; disabled?: boolean }) {
  const props = Object.entries(schema.properties ?? {});
  if (!props.length) return <p className="text-xs text-fg-muted">This step has no settings.</p>;
  const required = new Set(schema.required ?? []);
  return (
    <div className="space-y-3">
      {props.map(([name, prop]) => (
        <div key={name}>
          <label htmlFor={`cfg-${name}`} className="mb-1 block text-xs font-medium">
            {prop.title ?? name.replace(/_/g, " ")}
            {required.has(name) && <span className="text-danger"> *</span>}
          </label>
          <FieldInput
            name={name}
            prop={prop}
            value={value[name]}
            disabled={disabled}
            onChange={(v) => {
              const next = { ...value };
              if (v === undefined) delete next[name];
              else next[name] = v;
              onChange(next);
            }}
          />
          {prop.description && <p className="mt-1 text-[11px] text-fg-subtle">{prop.description}</p>}
        </div>
      ))}
    </div>
  );
}
