"use client";

import * as React from "react";
import { EyeOff, KeyRound, Pencil, Plus, Rows3, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Switch } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field, Label } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { errorMessage } from "@/lib/errors";
import { emptyCondition, parsePredicate, serializeConditions, summarizePredicate, validateConditions, type ConditionRow } from "@/lib/governance/conditions";
import {
  useColumnPolicies,
  useDatasetAcl,
  useGovMutations,
  useRowPolicies,
  type ColumnAction,
  type ColumnPolicy,
  type Privilege,
  type RowPolicy,
} from "@/lib/queries/governance";
import type { Member, Role } from "@/lib/types";
import { formatRelative } from "@/lib/utils";
import { ConditionBuilder } from "./condition-builder";
import { PrincipalPicker, principalComplete, principalLabel, type Principal } from "./principal";

export interface Directory {
  roles: Role[];
  members: Member[];
  columns: Array<{ name: string; dtype: string }>;
  attributes: string[];
}

const PRIVILEGES: Array<{ value: Privilege; label: string; hint: string }> = [
  { value: "read", label: "Read", hint: "See the dataset and its preview." },
  { value: "query", label: "Query", hint: "Ask questions and run SQL." },
  { value: "export", label: "Export", hint: "Download results." },
  { value: "admin", label: "Admin", hint: "Manage the dataset." },
];

export const COLUMN_ACTIONS: Array<{ value: ColumnAction; label: string; hint: string }> = [
  { value: "deny", label: "Deny", hint: "The column is removed for this principal." },
  { value: "mask_null", label: "Mask: empty", hint: "Values read as NULL." },
  { value: "mask_hash", label: "Mask: hash", hint: "Salted SHA-256; joins still work." },
  { value: "mask_partial", label: "Mask: partial", hint: "Show only the last N characters." },
];

/* ---------------- ACL ---------------- */

export function AclSection({ datasetId, dir }: { datasetId: string; dir: Directory }) {
  const { slug } = useOrgContext();
  const acl = useDatasetAcl(slug, datasetId);
  const m = useGovMutations<{ principal_type: string; principal_id: string; privilege: Privilege }>(slug, datasetId, "acl");
  const [principal, setPrincipal] = React.useState<Principal>({ type: "role", id: null });
  const [privilege, setPrivilege] = React.useState<Privilege>("query");
  const rows = acl.data ?? [];

  const add = async () => {
    if (!principal.id || principal.type === "everyone") return;
    try {
      await m.create.mutateAsync({ principal_type: principal.type, principal_id: principal.id, privilege });
      toast.success("Grant added");
      setPrincipal({ type: principal.type, id: null });
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  return (
    <Card>
      <CardHeader
        icon={<KeyRound />}
        title="Access grants"
        description={
          rows.length === 0
            ? "No grants: every member whose role allows the action can use this dataset. Adding the first grant restricts it to the listed principals (owners and admins always keep access)."
            : "Only these principals (plus owners and admins) can use this dataset, at the privilege granted."
        }
      />
      <CardContent className="space-y-4">
        {acl.isError ? (
          <ErrorState compact error={acl.error} onRetry={() => void acl.refetch()} />
        ) : acl.isLoading ? (
          <Skeleton className="h-16" />
        ) : rows.length > 0 ? (
          <ul className="divide-y divide-border rounded-control border border-border">
            {rows.map((g) => (
              <li key={g.id} className="flex items-center gap-3 px-3 py-2 text-sm">
                <span className="min-w-0 flex-1 truncate text-fg">{principalLabel(g, dir.roles, dir.members)}</span>
                <Badge tone="accent">{g.privilege}</Badge>
                <span className="hidden text-xs text-fg-subtle sm:inline">{formatRelative(g.created_at)}</span>
                <Button variant="ghost" size="icon-xs" aria-label="Remove grant" loading={m.remove.isPending && m.remove.variables === g.id} onClick={() => void m.remove.mutateAsync(g.id).catch((e) => toast.error(errorMessage(e)))}>
                  <Trash2 />
                </Button>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="grid gap-2 rounded-control border border-dashed border-border p-3 lg:grid-cols-[minmax(0,1fr)_160px_auto] lg:items-center">
          <PrincipalPicker value={principal} onChange={setPrincipal} roles={dir.roles} members={dir.members} allowEveryone={false} idPrefix="acl" />
          <Select value={privilege} onValueChange={(v) => setPrivilege(v as Privilege)}>
            <SelectTrigger aria-label="Privilege">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PRIVILEGES.map((p) => (
                <SelectItem key={p.value} value={p.value} description={p.hint}>
                  {p.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button size="sm" onClick={() => void add()} disabled={!principal.id} loading={m.create.isPending}>
            <Plus /> Grant
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/* ---------------- column policies ---------------- */

interface ColumnBody {
  column_name: string;
  principal_type: string;
  principal_id: string | null;
  action: ColumnAction;
  params: { keep_last?: number };
  exempt_role_ids: string[];
}

export function ColumnPoliciesSection({ datasetId, dir }: { datasetId: string; dir: Directory }) {
  const { slug } = useOrgContext();
  const q = useColumnPolicies(slug, datasetId);
  const m = useGovMutations<ColumnBody>(slug, datasetId, "column-policies");
  const [editing, setEditing] = React.useState<ColumnPolicy | "new" | null>(null);
  const [deleting, setDeleting] = React.useState<ColumnPolicy | null>(null);
  const rows = q.data ?? [];
  return (
    <Card>
      <CardHeader
        icon={<EyeOff />}
        title="Column policies"
        description="Deny or mask a column per principal. The most restrictive rule wins; exempt roles bypass a rule."
        actions={
          <Button size="sm" variant="secondary" onClick={() => setEditing("new")}>
            <Plus /> Add policy
          </Button>
        }
      />
      <CardContent>
        {q.isError ? (
          <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
        ) : q.isLoading ? (
          <Skeleton className="h-16" />
        ) : rows.length === 0 ? (
          <EmptyState compact icon={<EyeOff />} title="No column policies" description="Every allowed column is visible unmasked." />
        ) : (
          <div className="scrollbar-thin overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead className="text-xs text-fg-muted">
                <tr className="border-b border-border">
                  <th className="py-2 pr-3 text-left font-medium">Column</th>
                  <th className="py-2 pr-3 text-left font-medium">Applies to</th>
                  <th className="py-2 pr-3 text-left font-medium">Action</th>
                  <th className="py-2 pr-3 text-left font-medium">Exempt</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.id} className="border-b border-border/60 last:border-0">
                    <td className="py-2 pr-3 font-mono text-[13px]">{p.column_name}</td>
                    <td className="py-2 pr-3">{principalLabel(p, dir.roles, dir.members)}</td>
                    <td className="py-2 pr-3">
                      <Badge tone={p.action === "deny" ? "danger" : "warning"}>
                        {COLUMN_ACTIONS.find((a) => a.value === p.action)?.label ?? p.action}
                        {p.action === "mask_partial" && p.params.keep_last !== undefined ? ` · last ${p.params.keep_last}` : ""}
                      </Badge>
                    </td>
                    <td className="py-2 pr-3 text-xs text-fg-muted">{p.exempt_role_ids.map((id) => dir.roles.find((r) => r.id === id)?.name ?? "role").join(", ") || "—"}</td>
                    <td className="py-2 text-right">
                      <Button variant="ghost" size="icon-xs" aria-label={`Edit policy on ${p.column_name}`} onClick={() => setEditing(p)}>
                        <Pencil />
                      </Button>
                      <Button variant="ghost" size="icon-xs" aria-label={`Delete policy on ${p.column_name}`} onClick={() => setDeleting(p)}>
                        <Trash2 />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
      <ColumnPolicyDialog
        open={!!editing}
        policy={editing === "new" ? null : editing}
        dir={dir}
        saving={m.create.isPending || m.update.isPending}
        onClose={() => setEditing(null)}
        onSave={async (body) => {
          try {
            if (editing && editing !== "new") await m.update.mutateAsync({ id: editing.id, body });
            else await m.create.mutateAsync(body);
            toast.success("Column policy saved");
            setEditing(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete the policy on ${deleting?.column_name ?? ""}?`}
        description="The column becomes visible to this principal again (subject to other policies)."
        confirmLabel="Delete policy"
        loading={m.remove.isPending}
        onConfirm={async () => {
          if (!deleting) return;
          try {
            await m.remove.mutateAsync(deleting.id);
            setDeleting(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </Card>
  );
}

function ExemptRoles({ value, onChange, roles }: { value: string[]; onChange: (v: string[]) => void; roles: Role[] }) {
  return (
    <div className="flex flex-wrap gap-1.5" role="group" aria-label="Exempt roles">
      {roles.map((r) => {
        const on = value.includes(r.id);
        return (
          <button
            key={r.id}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(on ? value.filter((x) => x !== r.id) : [...value, r.id])}
            className={on ? "rounded-full border border-accent bg-accent-soft px-2.5 py-1 text-xs text-accent-soft-fg focus-ring" : "rounded-full border border-border px-2.5 py-1 text-xs text-fg-muted hover:border-border-strong focus-ring"}
          >
            {r.name}
          </button>
        );
      })}
    </div>
  );
}

function ColumnPolicyDialog({
  open,
  policy,
  dir,
  saving,
  onClose,
  onSave,
}: {
  open: boolean;
  policy: ColumnPolicy | null;
  dir: Directory;
  saving: boolean;
  onClose: () => void;
  onSave: (body: ColumnBody) => Promise<void>;
}) {
  const [column, setColumn] = React.useState("");
  const [principal, setPrincipal] = React.useState<Principal>({ type: "everyone", id: null });
  const [action, setAction] = React.useState<ColumnAction>("mask_hash");
  const [keepLast, setKeepLast] = React.useState("4");
  const [exempt, setExempt] = React.useState<string[]>([]);
  React.useEffect(() => {
    if (!open) return;
    setColumn(policy?.column_name ?? "");
    setPrincipal(policy ? { type: policy.principal_type, id: policy.principal_id } : { type: "everyone", id: null });
    setAction(policy?.action ?? "mask_hash");
    setKeepLast(String(policy?.params.keep_last ?? 4));
    setExempt(policy?.exempt_role_ids ?? []);
  }, [open, policy]);
  const keep = Number(keepLast);
  const keepError = action === "mask_partial" && (!Number.isInteger(keep) || keep < 0 || keep > 64) ? "0 to 64." : undefined;
  const valid = !!column && principalComplete(principal) && !keepError;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="md">
        <DialogHeader title={policy ? "Edit column policy" : "Add column policy"} icon={<EyeOff />} />
        <DialogBody className="space-y-4">
          <Field id="cp-col" label="Column">
            <Select value={column || undefined} onValueChange={setColumn}>
              <SelectTrigger aria-label="Column">
                <SelectValue placeholder="Choose a column" />
              </SelectTrigger>
              <SelectContent>
                {dir.columns.map((c) => (
                  <SelectItem key={c.name} value={c.name} description={c.dtype}>
                    {c.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <div className="space-y-1.5">
            <Label>Applies to</Label>
            <PrincipalPicker value={principal} onChange={setPrincipal} roles={dir.roles} members={dir.members} allowEveryone idPrefix="cp" />
          </div>
          <div className="space-y-1.5">
            <Label>Action</Label>
            <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Action">
              {COLUMN_ACTIONS.map((a) => (
                <button
                  key={a.value}
                  type="button"
                  role="radio"
                  aria-checked={action === a.value}
                  onClick={() => setAction(a.value)}
                  className={action === a.value ? "rounded-control border border-accent bg-accent-soft/60 p-2.5 text-left focus-ring" : "rounded-control border border-border p-2.5 text-left hover:border-border-strong focus-ring"}
                >
                  <span className="block text-sm font-medium text-fg">{a.label}</span>
                  <span className="block text-xs text-fg-muted">{a.hint}</span>
                </button>
              ))}
            </div>
          </div>
          {action === "mask_partial" && (
            <Field id="cp-keep" label="Characters to keep" error={keepError}>
              <Input inputMode="numeric" value={keepLast} onChange={(e) => setKeepLast(e.target.value)} />
            </Field>
          )}
          <div className="space-y-1.5">
            <Label>Exempt roles</Label>
            <ExemptRoles value={exempt} onChange={setExempt} roles={dir.roles} />
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!valid}
            loading={saving}
            onClick={() =>
              void onSave({
                column_name: column,
                principal_type: principal.type,
                principal_id: principal.type === "everyone" ? null : principal.id,
                action,
                params: action === "mask_partial" ? { keep_last: keep } : {},
                exempt_role_ids: exempt,
              })
            }
          >
            Save policy
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ---------------- row policies ---------------- */

interface RowBody {
  name: string;
  principal_type: string;
  principal_id: string | null;
  predicate: ReturnType<typeof serializeConditions>;
  exempt_role_ids: string[];
  is_active: boolean;
}

export function RowPoliciesSection({ datasetId, dir }: { datasetId: string; dir: Directory }) {
  const { slug } = useOrgContext();
  const q = useRowPolicies(slug, datasetId);
  const m = useGovMutations<RowBody>(slug, datasetId, "row-policies");
  const [editing, setEditing] = React.useState<RowPolicy | "new" | null>(null);
  const [deleting, setDeleting] = React.useState<RowPolicy | null>(null);
  const rows = q.data ?? [];
  return (
    <Card>
      <CardHeader
        icon={<Rows3 />}
        title="Row policies"
        description="Limit which rows a principal sees. Conditions are ANDed and can reference user attributes ($user.region)."
        actions={
          <Button size="sm" variant="secondary" onClick={() => setEditing("new")}>
            <Plus /> Add row policy
          </Button>
        }
      />
      <CardContent>
        {q.isError ? (
          <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
        ) : q.isLoading ? (
          <Skeleton className="h-16" />
        ) : rows.length === 0 ? (
          <EmptyState compact icon={<Rows3 />} title="No row policies" description="Everyone with access sees every row." />
        ) : (
          <ul className="space-y-2">
            {rows.map((p) => (
              <li key={p.id} className="flex flex-wrap items-start gap-3 rounded-control border border-border p-3">
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-fg">{p.name}</span>
                    <Badge tone="outline">{principalLabel(p, dir.roles, dir.members)}</Badge>
                    {!p.is_active && <Badge>Inactive</Badge>}
                  </div>
                  <p className="font-mono text-xs text-fg-muted">{summarizePredicate(p.predicate)}</p>
                </div>
                <Switch
                  checked={p.is_active}
                  aria-label={`${p.name} active`}
                  onCheckedChange={(v) => void m.update.mutateAsync({ id: p.id, body: { is_active: v } }).catch((e) => toast.error(errorMessage(e)))}
                />
                <Button variant="ghost" size="icon-xs" aria-label={`Edit ${p.name}`} onClick={() => setEditing(p)}>
                  <Pencil />
                </Button>
                <Button variant="ghost" size="icon-xs" aria-label={`Delete ${p.name}`} onClick={() => setDeleting(p)}>
                  <Trash2 />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
      <RowPolicyDialog
        open={!!editing}
        policy={editing === "new" ? null : editing}
        dir={dir}
        saving={m.create.isPending || m.update.isPending}
        onClose={() => setEditing(null)}
        onSave={async (body) => {
          try {
            if (editing && editing !== "new") await m.update.mutateAsync({ id: editing.id, body });
            else await m.create.mutateAsync(body);
            toast.success("Row policy saved");
            setEditing(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete "${deleting?.name ?? ""}"?`}
        description="Rows it hid become visible to this principal (subject to other policies)."
        confirmLabel="Delete policy"
        loading={m.remove.isPending}
        onConfirm={async () => {
          if (!deleting) return;
          try {
            await m.remove.mutateAsync(deleting.id);
            setDeleting(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </Card>
  );
}

function RowPolicyDialog({
  open,
  policy,
  dir,
  saving,
  onClose,
  onSave,
}: {
  open: boolean;
  policy: RowPolicy | null;
  dir: Directory;
  saving: boolean;
  onClose: () => void;
  onSave: (body: RowBody) => Promise<void>;
}) {
  const [name, setName] = React.useState("");
  const [principal, setPrincipal] = React.useState<Principal>({ type: "everyone", id: null });
  const [rows, setRows] = React.useState<ConditionRow[]>([emptyCondition()]);
  const [exempt, setExempt] = React.useState<string[]>([]);
  const [showErrors, setShowErrors] = React.useState(false);
  React.useEffect(() => {
    if (!open) return;
    setName(policy?.name ?? "");
    setPrincipal(policy ? { type: policy.principal_type, id: policy.principal_id } : { type: "everyone", id: null });
    const parsed = policy ? parsePredicate(policy.predicate) : [];
    setRows(parsed.length ? parsed : [emptyCondition()]);
    setExempt(policy?.exempt_role_ids ?? []);
    setShowErrors(false);
  }, [open, policy]);
  const errors = validateConditions(
    rows,
    dir.columns.map((c) => c.name),
  );
  const types = Object.fromEntries(dir.columns.map((c) => [c.name, c.dtype]));
  const valid = !!name.trim() && principalComplete(principal) && Object.keys(errors).length === 0;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="xl">
        <DialogHeader title={policy ? "Edit row policy" : "Add row policy"} description="Structured conditions only; no SQL is ever accepted." icon={<Rows3 />} />
        <DialogBody className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <Field id="rp-name" label="Name">
              <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} placeholder="Own region only" />
            </Field>
            <div className="space-y-1.5">
              <Label>Applies to</Label>
              <PrincipalPicker value={principal} onChange={setPrincipal} roles={dir.roles} members={dir.members} allowEveryone idPrefix="rp" />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label>Rows visible when</Label>
            <ConditionBuilder rows={rows} onChange={setRows} columns={dir.columns} attributes={dir.attributes} errors={showErrors ? errors : {}} />
          </div>
          <div className="rounded-control bg-surface-2 px-3 py-2 font-mono text-xs text-fg-muted" aria-label="Predicate summary">
            {Object.keys(errors).length ? "Complete the conditions to see the summary." : summarizePredicate(serializeConditions(rows, types))}
          </div>
          <div className="space-y-1.5">
            <Label>Exempt roles</Label>
            <ExemptRoles value={exempt} onChange={setExempt} roles={dir.roles} />
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={saving}
            onClick={() => {
              setShowErrors(true);
              if (!valid) return;
              void onSave({
                name: name.trim(),
                principal_type: principal.type,
                principal_id: principal.type === "everyone" ? null : principal.id,
                predicate: serializeConditions(rows, types),
                exempt_role_ids: exempt,
                is_active: policy?.is_active ?? true,
              });
            }}
          >
            Save policy
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
