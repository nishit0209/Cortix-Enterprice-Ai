"use client";

import * as React from "react";
import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  BUILTIN_ATTRIBUTES,
  LIST_OPS,
  MAX_CONDITIONS,
  NULL_OPS,
  OPS,
  emptyCondition,
  splitList,
  type ConditionRow,
  type Op,
  type ValueSource,
} from "@/lib/governance/conditions";
import { cn } from "@/lib/utils";

/**
 * Structured row-policy condition builder: column · operator · value | $user.attribute, ANDed. Never free SQL.
 */
export function ConditionBuilder({
  rows,
  onChange,
  columns,
  attributes,
  errors,
}: {
  rows: ConditionRow[];
  onChange: (rows: ConditionRow[]) => void;
  columns: Array<{ name: string; dtype: string }>;
  attributes: string[];
  errors: Record<number, string>;
}) {
  const set = (i: number, patch: Partial<ConditionRow>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const attrOptions = Array.from(new Set([...attributes, ...BUILTIN_ATTRIBUTES])).sort();
  const listId = React.useId().replace(/:/g, "");
  return (
    <div className="space-y-2">
      <datalist id={`${listId}-attrs`}>
        {attrOptions.map((a) => (
          <option key={a} value={a} />
        ))}
      </datalist>
      <ol className="space-y-2" aria-label="Conditions">
        {rows.map((r, i) => (
          <li key={i} className={cn("rounded-control border p-2.5", errors[i] ? "border-danger/50" : "border-border")}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="w-10 shrink-0 text-xs font-semibold uppercase text-fg-subtle">{i === 0 ? "Where" : "and"}</span>
              <Select value={r.column || undefined} onValueChange={(v) => set(i, { column: v })}>
                <SelectTrigger className="w-44" aria-label={`Condition ${i + 1} column`}>
                  <SelectValue placeholder="Column" />
                </SelectTrigger>
                <SelectContent>
                  {columns.map((c) => (
                    <SelectItem key={c.name} value={c.name} description={c.dtype}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={r.op} onValueChange={(v) => set(i, { op: v as Op })}>
                <SelectTrigger className="w-40" aria-label={`Condition ${i + 1} operator`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {OPS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!NULL_OPS.has(r.op) && (
                <>
                  <Select value={r.source} onValueChange={(v) => set(i, { source: v as ValueSource })}>
                    <SelectTrigger className="w-36" aria-label={`Condition ${i + 1} value source`}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="literal">{LIST_OPS.has(r.op) ? "These values" : "This value"}</SelectItem>
                      <SelectItem value="user_attribute">User attribute</SelectItem>
                    </SelectContent>
                  </Select>
                  {r.source === "user_attribute" ? (
                    <span className="flex min-w-40 flex-1 items-center rounded-control border border-border bg-surface pl-2 text-sm focus-within:border-accent">
                      <span className="font-mono text-xs text-fg-subtle">$user.</span>
                      <input
                        className="h-8 min-w-0 flex-1 bg-transparent px-1 font-mono text-[13px] text-fg outline-none"
                        aria-label={`Condition ${i + 1} attribute`}
                        list={`${listId}-attrs`}
                        value={r.attribute}
                        onChange={(e) => set(i, { attribute: e.target.value.trim() })}
                        placeholder="region"
                      />
                    </span>
                  ) : LIST_OPS.has(r.op) ? (
                    <ListInput label={`Condition ${i + 1} values`} list={r.list} onChange={(list) => set(i, { list })} />
                  ) : (
                    <Input className="min-w-40 flex-1" aria-label={`Condition ${i + 1} value`} value={r.value} onChange={(e) => set(i, { value: e.target.value })} placeholder="Value" />
                  )}
                </>
              )}
              <Button variant="ghost" size="icon-xs" className="ml-auto" aria-label={`Remove condition ${i + 1}`} onClick={() => onChange(rows.filter((_, j) => j !== i))}>
                <Trash2 />
              </Button>
            </div>
            {errors[i] && (
              <p role="alert" className="mt-1.5 pl-12 text-xs text-danger">
                {errors[i]}
              </p>
            )}
          </li>
        ))}
      </ol>
      {errors[-1] && <p className="text-xs text-danger">{errors[-1]}</p>}
      <Button variant="secondary" size="xs" onClick={() => onChange([...rows, emptyCondition()])} disabled={rows.length >= MAX_CONDITIONS}>
        <Plus /> Add condition
      </Button>
    </div>
  );
}

/** Comma/newline separated values; keeps the raw text while typing so separators aren't swallowed. */
function ListInput({ label, list, onChange }: { label: string; list: string[]; onChange: (list: string[]) => void }) {
  const [text, setText] = React.useState(() => list.join(", "));
  return (
    <Input
      className="min-w-40 flex-1"
      aria-label={label}
      placeholder="Comma-separated values"
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onChange(splitList(e.target.value));
      }}
    />
  );
}
