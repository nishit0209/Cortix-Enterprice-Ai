"use client";

import * as React from "react";
import { Ban, Check, EyeOff, Plus, ScanEye, ShieldX, Trash2, UserCog } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ATTRIBUTE_RE, BUILTIN_ATTRIBUTES, splitList } from "@/lib/governance/conditions";
import { previewColumns, previewRowSummary } from "@/lib/governance/preview";
import { errorMessage } from "@/lib/errors";
import { useDatasetColumns } from "@/lib/queries/datasets";
import { usePolicyPreview, useSaveUserAttributes, useUserAttributes, type PolicyPreviewWithSample, type Privilege } from "@/lib/queries/governance";
import { ResultTable } from "@/components/ask/result-table";
import { Checkbox } from "@/components/ui/checkbox";
import type { Dataset, Member } from "@/lib/types";
import { formatNumber } from "@/lib/utils";
import type { Directory } from "./dataset-policies";

/* ---------------- policy preview ---------------- */

export function PolicyPreviewPanel({ datasets, dir, initialDataset }: { datasets: Dataset[]; dir: Directory; initialDataset: string | null }) {
  const { slug } = useOrgContext();
  const [datasetId, setDatasetId] = React.useState<string | null>(initialDataset);
  const [userId, setUserId] = React.useState<string | null>(null);
  const [privilege, setPrivilege] = React.useState<Privilege>("query");
  const [withSample, setWithSample] = React.useState(false);
  const preview = usePolicyPreview(slug);
  const cols = useDatasetColumns(slug, datasetId ?? "", !!datasetId);
  const [result, setResult] = React.useState<{ key: string; data: PolicyPreviewWithSample } | null>(null);
  const key = `${datasetId}:${userId}:${privilege}:${withSample}`;

  const run = async () => {
    if (!datasetId || !userId) return;
    try {
      setResult({ key, data: await preview.mutateAsync({ datasetId, userId, privilege, sample: withSample }) });
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const current = result?.key === key ? result.data : null;
  const access = current ? previewColumns((cols.data ?? []).map((c) => c.name), current) : [];
  const counts = { visible: 0, masked: 0, denied: 0 };
  for (const a of access) counts[a.status]++;
  const member = dir.members.find((m) => m.user.id === userId);

  return (
    <>
      <Card>
        <CardHeader icon={<ScanEye />} title="Preview as a member" description="Computes the effective policy for that member. Nothing leaves the org." />
        <CardContent className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_150px_auto] md:items-end">
          <Field id="pv-ds" label="Dataset">
            <Select value={datasetId ?? undefined} onValueChange={setDatasetId}>
              <SelectTrigger aria-label="Preview dataset">
                <SelectValue placeholder="Choose a dataset" />
              </SelectTrigger>
              <SelectContent>
                {datasets.map((d) => (
                  <SelectItem key={d.id} value={d.id}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field id="pv-user" label="Member">
            <Select value={userId ?? undefined} onValueChange={setUserId}>
              <SelectTrigger aria-label="Preview member">
                <SelectValue placeholder="Choose a member" />
              </SelectTrigger>
              <SelectContent>
                {dir.members.map((m) => (
                  <SelectItem key={m.user.id} value={m.user.id} description={`${m.user.email} · ${m.role}`}>
                    {m.user.full_name || m.user.email}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field id="pv-priv" label="Privilege">
            <Select value={privilege} onValueChange={(v) => setPrivilege(v as Privilege)}>
              <SelectTrigger aria-label="Preview privilege">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(["read", "query", "export", "admin"] as const).map((p) => (
                  <SelectItem key={p} value={p}>
                    {p}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Button onClick={() => void run()} disabled={!datasetId || !userId} loading={preview.isPending}>
            <ScanEye /> Preview
          </Button>
          <label className="flex items-center gap-2 text-xs text-fg-muted md:col-span-4">
            <Checkbox checked={withSample} onCheckedChange={(v) => setWithSample(v === true)} aria-label="Include sample rows" />
            Also run a 20-row sample as this member (through the query guard, limited by your own access too; audited)
          </label>
        </CardContent>
      </Card>

      {current && !current.access && (
        <Card>
          <EmptyState compact icon={<ShieldX />} title="No access" description={`${member?.user.email ?? "This member"} can't ${privilege} this dataset: an access grant is missing.`} />
        </Card>
      )}
      {current?.access && (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
          <Card>
            <CardHeader title="Columns" description={`${formatNumber(counts.visible)} visible · ${formatNumber(counts.masked)} masked · ${formatNumber(counts.denied)} denied`} />
            <CardContent>
              {cols.isLoading ? (
                <Skeleton className="h-24" />
              ) : (
                <ul className="grid gap-1.5 sm:grid-cols-2" aria-label="Column access">
                  {access.map((a) => (
                    <li key={a.column} className="flex items-center gap-2 rounded-control border border-border px-2.5 py-1.5 text-sm">
                      {a.status === "visible" ? <Check className="size-3.5 text-success" /> : a.status === "masked" ? <EyeOff className="size-3.5 text-warning" /> : <Ban className="size-3.5 text-danger" />}
                      <span className="truncate font-mono text-[13px] text-fg">{a.column}</span>
                      <Badge className="ml-auto" tone={a.status === "visible" ? "success" : a.status === "masked" ? "warning" : "danger"}>
                        {a.status === "masked" ? `masked · ${a.mask}` : a.status}
                      </Badge>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
          <Card className="self-start">
            <CardHeader title="Rows" />
            <CardContent className="space-y-3 text-sm">
              {previewRowSummary(current).map((r) => (
                <div key={r.table || "all"}>
                  {r.table && <div className="text-xs text-fg-subtle">{r.table}</div>}
                  <p className="font-mono text-xs text-fg">{r.summary}</p>
                </div>
              ))}
              {current.policy && (
                <dl className="grid grid-cols-2 gap-2 border-t border-border pt-3 text-xs">
                  <div>
                    <dt className="text-fg-subtle">Row cap</dt>
                    <dd className="tabular text-fg">{formatNumber(current.policy.row_cap)}</dd>
                  </div>
                  <div>
                    <dt className="text-fg-subtle">Timeout</dt>
                    <dd className="tabular text-fg">{current.policy.timeout_s ? `${current.policy.timeout_s} s` : "—"}</dd>
                  </div>
                </dl>
              )}
            </CardContent>
          </Card>
        </div>
      )}
      {current?.access && current.sample && (
        <Card>
          <CardHeader title="Sample rows as this member" description={current.sample.ok ? `${formatNumber(current.sample.row_count)} row(s)${current.sample.truncated ? " (truncated)" : ""}` : undefined} />
          <CardContent>
            {current.sample.ok ? (
              current.sample.rows.length ? (
                <ResultTable caption="Policy preview sample" columns={current.sample.columns} rows={current.sample.rows} />
              ) : (
                <p className="text-sm text-fg-muted">The member&apos;s row policy leaves no rows.</p>
              )
            ) : (
              <p className="text-sm text-fg-muted">{current.sample.detail}</p>
            )}
          </CardContent>
        </Card>
      )}
    </>
  );
}

/* ---------------- user attributes ---------------- */

export function UserAttributesPanel({ members, onKeys }: { members: Member[]; onKeys: (keys: string[]) => void }) {
  const { slug } = useOrgContext();
  const [userId, setUserId] = React.useState<string | null>(null);
  const q = useUserAttributes(slug, userId);
  const save = useSaveUserAttributes(slug, userId ?? "");
  const [draft, setDraft] = React.useState<Array<{ key: string; values: string }>>([]);

  React.useEffect(() => {
    if (q.data) {
      setDraft(Object.entries(q.data.attributes).map(([key, values]) => ({ key, values: values.join(", ") })));
      onKeys(Object.keys(q.data.attributes));
    }
  }, [q.data, onKeys]);

  const errors = draft.map((d, i) =>
    !ATTRIBUTE_RE.test(d.key)
      ? "Lowercase snake_case key."
      : BUILTIN_ATTRIBUTES.includes(d.key)
        ? `"${d.key}" is built in.`
        : draft.findIndex((x) => x.key === d.key) !== i
          ? "Duplicate key."
          : splitList(d.values).length === 0
            ? "Add at least one value."
            : null,
  );
  const valid = errors.every((e) => e === null);

  return (
    <Card>
      <CardHeader
        icon={<UserCog />}
        title="User attributes"
        description="Values bound into row policies as $user.<key>, e.g. $user.region = North, South. Built in: $user.id, $user.email, $user.role."
      />
      <CardContent className="space-y-4">
        <Select value={userId ?? undefined} onValueChange={setUserId}>
          <SelectTrigger className="max-w-sm" aria-label="Member">
            <SelectValue placeholder="Choose a member" />
          </SelectTrigger>
          <SelectContent>
            {members.map((m) => (
              <SelectItem key={m.user.id} value={m.user.id} description={m.user.email}>
                {m.user.full_name || m.user.email}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {!userId ? null : q.isError ? (
          <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
        ) : q.isLoading ? (
          <Skeleton className="h-20" />
        ) : (
          <div className="space-y-3">
            {draft.length === 0 && <p className="text-sm text-fg-muted">No attributes yet.</p>}
            {draft.map((d, i) => (
              <div key={i} className="grid gap-2 sm:grid-cols-[200px_minmax(0,1fr)_auto] sm:items-start">
                <Input aria-label={`Attribute ${i + 1} key`} className="font-mono" value={d.key} placeholder="region" onChange={(e) => setDraft((all) => all.map((x, j) => (j === i ? { ...x, key: e.target.value.trim() } : x)))} />
                <div>
                  <Input aria-label={`Attribute ${i + 1} values`} value={d.values} placeholder="North, South" onChange={(e) => setDraft((all) => all.map((x, j) => (j === i ? { ...x, values: e.target.value } : x)))} />
                  {errors[i] && <p className="mt-1 text-xs text-danger">{errors[i]}</p>}
                </div>
                <Button variant="ghost" size="icon-sm" aria-label={`Remove attribute ${i + 1}`} onClick={() => setDraft((all) => all.filter((_, j) => j !== i))}>
                  <Trash2 />
                </Button>
              </div>
            ))}
            <div className="flex gap-2">
              <Button variant="secondary" size="sm" onClick={() => setDraft((all) => [...all, { key: "", values: "" }])} disabled={draft.length >= 50}>
                <Plus /> Add attribute
              </Button>
              <Button
                size="sm"
                disabled={!valid}
                loading={save.isPending}
                onClick={async () => {
                  try {
                    await save.mutateAsync(Object.fromEntries(draft.map((d) => [d.key, splitList(d.values)])));
                    toast.success("Attributes saved");
                  } catch (e) {
                    toast.error(errorMessage(e));
                  }
                }}
              >
                Save attributes
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
