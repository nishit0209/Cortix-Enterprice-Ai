"use client";

import * as React from "react";
import { Bot, Pencil, Plus, ShieldAlert, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { CATEGORY_LABELS, type StepKind } from "@/lib/automation/catalog";
import { formatScopes, parseScopes, type AgentConfig } from "@/lib/automation/graph";
import { errorMessage } from "@/lib/errors";
import { useAgents, useDeleteAgent, useSaveAgent, useStepCatalog } from "@/lib/queries/automation";
import { formatRelative } from "@/lib/utils";
import { ModuleError, NativeSelect } from "@/components/mdm/common";

export function AgentsPanel() {
  const { slug, can } = useOrgContext();
  const canManage = can("automation.manage");
  const agents = useAgents(slug);
  const catalog = useStepCatalog(slug);
  const del = useDeleteAgent(slug);
  const [editing, setEditing] = React.useState<AgentConfig | "new" | null>(null);
  const [pending, setPending] = React.useState<AgentConfig | null>(null);
  const steps = catalog.data?.steps ?? [];
  const label = (k: string) => steps.find((s) => s.kind === k)?.label ?? k;

  if (agents.isError) return <Card><ModuleError error={agents.error} what="Agents" onRetry={() => void agents.refetch()} /></Card>;
  return (
    <Card>
      <CardHeader
        title="Agents"
        description="An agent may only call the tools on its allowlist, with your scopes. Every tool call is an audited step; high-impact tools always wait for approval."
        actions={
          canManage && (
            <Button size="sm" onClick={() => setEditing("new")}>
              <Plus /> New agent
            </Button>
          )
        }
      />
      <CardContent>
        {agents.isLoading ? (
          <Skeleton className="h-32" />
        ) : !(agents.data ?? []).length ? (
          <EmptyState compact icon={<Bot />} title="No agents configured" description="Create an agent with a narrow tool allowlist, for example to refresh pipelines and notify on failures." />
        ) : (
          <ul className="divide-y divide-border">
            {agents.data!.map((a) => (
              <li key={a.id} className="flex flex-wrap items-start justify-between gap-3 py-3">
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{a.name}</span>
                    <Badge tone="outline">{a.model_tier}</Badge>
                    <span className="text-xs text-fg-subtle">max {a.max_steps} steps</span>
                    {!a.is_active && <Badge tone="neutral">Inactive</Badge>}
                  </div>
                  {a.description && <p className="text-xs text-fg-muted">{a.description}</p>}
                  <div className="flex flex-wrap gap-1">
                    {a.allowed_tools.map((t) => (
                      <Badge key={t} tone={steps.find((s) => s.kind === t)?.impact === "high" ? "warning" : "accent"}>
                        {label(t)}
                      </Badge>
                    ))}
                  </div>
                  <p className="text-[11px] text-fg-subtle">
                    Approval required for: {a.approval_required_for.join(", ") || "high"} · updated {formatRelative(a.updated_at)}
                  </p>
                </div>
                {canManage && (
                  <div className="flex gap-1">
                    <Button variant="ghost" size="icon-sm" aria-label={`Edit ${a.name}`} onClick={() => setEditing(a)}>
                      <Pencil />
                    </Button>
                    <Button variant="ghost" size="icon-sm" aria-label={`Delete ${a.name}`} onClick={() => setPending(a)}>
                      <Trash2 />
                    </Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
      <AgentDialog agent={editing} onClose={() => setEditing(null)} steps={steps} />
      <ConfirmDialog
        open={!!pending}
        onOpenChange={(o) => !o && setPending(null)}
        title={`Delete agent "${pending?.name ?? ""}"?`}
        confirmLabel="Delete agent"
        loading={del.isPending}
        onConfirm={async () => {
          if (!pending) return;
          try {
            await del.mutateAsync(pending.id);
            setPending(null);
            toast.success("Agent deleted");
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </Card>
  );
}

function AgentDialog({ agent, onClose, steps }: { agent: AgentConfig | "new" | null; onClose: () => void; steps: StepKind[] }) {
  const { slug } = useOrgContext();
  const save = useSaveAgent(slug);
  const existing = agent && agent !== "new" ? agent : null;
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [tier, setTier] = React.useState("fast");
  const [tools, setTools] = React.useState<string[]>([]);
  const [scopes, setScopes] = React.useState<Record<string, string>>({});
  const [approveTools, setApproveTools] = React.useState<string[]>([]);
  const [maxSteps, setMaxSteps] = React.useState("8");
  const [lowNeedsApproval, setLowNeedsApproval] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!agent) return;
    setName(existing?.name ?? "");
    setDescription(existing?.description ?? "");
    setTier(existing?.model_tier ?? "fast");
    setTools(existing?.allowed_tools ?? []);
    setScopes(Object.fromEntries(Object.entries(existing?.tool_scopes ?? {}).map(([t, s]) => [t, formatScopes(s)])));
    setApproveTools((existing?.approval_required_for ?? []).filter((r) => r !== "low" && r !== "high"));
    setMaxSteps(String(existing?.max_steps ?? 8));
    setLowNeedsApproval(!!existing?.approval_required_for.includes("low"));
    setError(null);
  }, [agent, existing]);

  const byCategory = React.useMemo(() => {
    const m = new Map<string, StepKind[]>();
    for (const s of steps) m.set(s.category, [...(m.get(s.category) ?? []), s]);
    return [...m.entries()];
  }, [steps]);

  const submit = async () => {
    setError(null);
    if (!name.trim()) return setError("Name the agent.");
    if (!tools.length) return setError("Allow at least one tool.");
    const n = Number(maxSteps);
    if (!Number.isInteger(n) || n < 1 || n > 50) return setError("Max steps must be 1-50.");
    const tool_scopes: Record<string, Record<string, string[]>> = {};
    for (const t of tools) {
      const { scopes: parsed, error: e } = parseScopes(scopes[t] ?? "");
      if (e) return setError(`${t}: ${e}`);
      if (Object.keys(parsed).length) tool_scopes[t] = parsed;
    }
    try {
      await save.mutateAsync({
        id: existing?.id,
        name: name.trim(),
        description: description.trim() || undefined,
        model_tier: tier,
        allowed_tools: tools,
        tool_scopes,
        max_steps: n,
        approval_required_for: [...(lowNeedsApproval ? ["low"] : []), "high", ...approveTools.filter((t) => tools.includes(t))],
      });
      toast.success(existing ? "Agent updated" : "Agent created");
      onClose();
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  return (
    <Dialog open={!!agent} onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="lg">
        <DialogHeader title={existing ? `Edit ${existing.name}` : "New agent"} description="Planning may use Gemini through the AI data policy, with step metadata only: never row values on the free tier." />
        <DialogBody className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <Field id="ag-name" label="Name">
              <Input id="ag-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
            </Field>
            <Field id="ag-tier" label="Model tier">
              <NativeSelect id="ag-tier" value={tier} onChange={(e) => setTier(e.target.value)}>
                <option value="fast">Fast</option>
                <option value="reasoning">Reasoning</option>
              </NativeSelect>
            </Field>
            <Field id="ag-steps" label="Max steps per task">
              <Input id="ag-steps" type="number" min={1} max={50} value={maxSteps} onChange={(e) => setMaxSteps(e.target.value)} />
            </Field>
          </div>
          <Field id="ag-desc" label="Purpose">
            <Input id="ag-desc" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} />
          </Field>
          <fieldset>
            <legend className="mb-2 text-xs font-medium">Tools allowlist</legend>
            <div className="grid gap-3 sm:grid-cols-2">
              {byCategory.map(([cat, list]) => (
                <div key={cat}>
                  <p className="mb-1 text-[11px] font-medium text-fg-subtle uppercase">{CATEGORY_LABELS[cat as keyof typeof CATEGORY_LABELS] ?? cat}</p>
                  {list.map((s) => (
                    <label key={s.kind} className="flex items-center gap-2 py-0.5 text-sm">
                      <Checkbox checked={tools.includes(s.kind)} onCheckedChange={(c) => setTools((t) => (c === true ? [...t, s.kind] : t.filter((x) => x !== s.kind)))} />
                      {s.label}
                      {s.impact === "high" && <ShieldAlert className="size-3.5 text-warning" aria-label="High impact: always needs approval" />}
                      {!s.registered && <span className="text-[10px] text-fg-subtle">(not registered here)</span>}
                    </label>
                  ))}
                </div>
              ))}
            </div>
          </fieldset>
          {tools.length > 0 && (
            <fieldset className="space-y-2">
              <legend className="mb-1 text-xs font-medium">Per-tool limits</legend>
              <p className="text-[11px] text-fg-subtle">
                Optional. Restrict config values, e.g. <code>pipeline_id = &lt;id&gt;, &lt;id&gt;</code>. Steps always run with the permissions of the person the agent acts for.
              </p>
              {tools.map((t) => (
                <div key={t} className="grid items-center gap-2 sm:grid-cols-[10rem_1fr_auto]">
                  <span className="truncate font-mono text-xs">{t}</span>
                  <Input value={scopes[t] ?? ""} onChange={(e) => setScopes((s) => ({ ...s, [t]: e.target.value }))} placeholder="key = value1, value2; key2 = value" aria-label={`Scope for ${t}`} />
                  <label className="flex items-center gap-1.5 text-xs">
                    <Checkbox
                      checked={approveTools.includes(t) || steps.find((s) => s.kind === t)?.impact === "high"}
                      disabled={steps.find((s) => s.kind === t)?.impact === "high"}
                      onCheckedChange={(c) => setApproveTools((a) => (c === true ? [...a, t] : a.filter((x) => x !== t)))}
                    />
                    Approve
                  </label>
                </div>
              ))}
            </fieldset>
          )}
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={lowNeedsApproval} onCheckedChange={(c) => setLowNeedsApproval(c === true)} />
            Also require approval for low-impact tools (high impact always does)
          </label>
          {error && (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={submit} loading={save.isPending}>
            {existing ? "Save" : "Create agent"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
