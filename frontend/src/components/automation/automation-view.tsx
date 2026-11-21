"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Bot, History, Inbox, Plus, Workflow as WorkflowIcon, Zap } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
import { Pager } from "@/components/ui/pager";
import { StatCard } from "@/components/ui/stat-card";
import { FeatureStatusBadge, StatusBadge } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { Workflow, WorkflowRun } from "@/lib/automation/graph";
import { errorMessage } from "@/lib/errors";
import { useAgents, useApprovals, useCreateWorkflow, useWorkflowRuns, useWorkflows } from "@/lib/queries/automation";
import { formatNumber, formatRelative, titleCase } from "@/lib/utils";
import { ModuleError } from "@/components/mdm/common";
import { AgentsPanel } from "./agents";
import { ApprovalsInbox } from "./approvals";

const TABS = ["workflows", "runs", "approvals", "agents"] as const;

export function AutomationView() {
  const { slug, can } = useOrgContext();
  const params = useSearchParams();
  const initial = params.get("tab");
  const [tab, setTab] = React.useState<string>(initial && (TABS as readonly string[]).includes(initial) ? initial : "workflows");
  const canManage = can("automation.manage");
  const workflows = useWorkflows(slug);
  const approvals = useApprovals(slug, "pending");
  const agents = useAgents(slug, canManage);
  const runs = useWorkflowRuns(slug, null);
  const [creating, setCreating] = React.useState(false);
  const active = (workflows.data ?? []).filter((w) => w.status === "active").length;
  const waiting = (runs.items ?? []).filter((r) => r.status === "waiting_approval").length;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Automation"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Workflows chain registered steps (pipelines, saved queries, Customer/Product 360, ML, documents) behind schedule, webhook or event triggers, with human approvals and governed agents."
        actions={
          canManage && (
            <Button onClick={() => setCreating(true)}>
              <Plus /> New workflow
            </Button>
          )
        }
      />
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Automation summary">
        <StatCard label="Workflows" icon={<WorkflowIcon />} loading={workflows.isLoading} value={workflows.data ? formatNumber(workflows.data.length) : "—"} hint={workflows.data ? `${formatNumber(active)} active` : undefined} />
        <StatCard
          label="Pending approvals"
          icon={<Inbox />}
          accent="warning"
          loading={approvals.isLoading}
          value={approvals.items ? `${formatNumber(approvals.items.length)}${approvals.hasNext ? "+" : ""}` : "—"}
          hint={waiting ? `${formatNumber(waiting)} run(s) waiting` : undefined}
        />
        <StatCard label="Recent runs" icon={<History />} accent="teal" loading={runs.isLoading} value={runs.items ? formatNumber(runs.items.length) : "—"} hint={runs.items?.[0] ? `Last ${formatRelative(runs.items[0].created_at)}` : undefined} />
        <StatCard label="Agents" icon={<Bot />} accent="accent" loading={canManage && agents.isLoading} value={canManage && agents.data ? formatNumber(agents.data.length) : "—"} />
      </section>
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList aria-label="Automation sections">
          <TabsTrigger value="workflows">
            <WorkflowIcon /> Workflows
          </TabsTrigger>
          <TabsTrigger value="runs">
            <History /> Runs
          </TabsTrigger>
          <TabsTrigger value="approvals">
            <Inbox /> Approvals
          </TabsTrigger>
          {canManage && (
            <TabsTrigger value="agents">
              <Bot /> Agents
            </TabsTrigger>
          )}
        </TabsList>
        <TabsContent value="workflows">
          <WorkflowsTable workflows={workflows.data} loading={workflows.isLoading} error={workflows.isError ? workflows.error : null} onRetry={() => void workflows.refetch()} onCreate={canManage ? () => setCreating(true) : undefined} />
        </TabsContent>
        <TabsContent value="runs">
          <RunsTable workflowId={null} />
        </TabsContent>
        <TabsContent value="approvals">
          <ApprovalsInbox />
        </TabsContent>
        {canManage && (
          <TabsContent value="agents">
            <AgentsPanel />
          </TabsContent>
        )}
      </Tabs>
      <CreateWorkflowDialog open={creating} onOpenChange={setCreating} />
    </div>
  );
}

function WorkflowsTable({ workflows, loading, error, onRetry, onCreate }: { workflows: Workflow[] | undefined; loading: boolean; error: unknown; onRetry: () => void; onCreate?: () => void }) {
  const { slug } = useOrgContext();
  const router = useRouter();
  const columns = React.useMemo<DataTableColumn<Workflow>[]>(
    () => [
      {
        id: "name",
        accessorFn: (w) => w.name,
        header: "Workflow",
        cell: ({ row }) => (
          <Link href={`/o/${slug}/automation/workflows/${row.original.id}`} className="font-medium hover:underline" onClick={(e) => e.stopPropagation()}>
            {row.original.name}
          </Link>
        ),
      },
      { id: "status", accessorFn: (w) => w.status, header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
      { id: "version", accessorFn: (w) => w.current_version ?? 0, header: "Published", cell: ({ row }) => (row.original.current_version ? `v${row.original.current_version}` : "—") },
      {
        id: "last",
        accessorFn: (w) => w.last_run?.at ?? "",
        header: "Last run",
        cell: ({ row }) =>
          row.original.last_run ? (
            <span className="flex items-center gap-2">
              <StatusBadge status={row.original.last_run.status} /> <span className="text-xs text-fg-muted">{formatRelative(row.original.last_run.at)}</span>
            </span>
          ) : (
            "—"
          ),
      },
      { id: "updated", accessorFn: (w) => w.updated_at ?? "", header: "Updated", cell: ({ row }) => formatRelative(row.original.updated_at) },
    ],
    [slug],
  );
  if (error) return <Card><ModuleError error={error} what="Workflows" onRetry={onRetry} /></Card>;
  return (
    <DataTable
      caption="Workflows"
      columns={columns}
      data={workflows}
      loading={loading}
      getRowId={(w) => w.id}
      searchable
      searchPlaceholder="Search workflows"
      onRowClick={(w) => router.push(`/o/${slug}/automation/workflows/${w.id}`)}
      emptyState={
        <EmptyState
          compact
          icon={<Zap />}
          title="No workflows yet"
          description="For example: when a dataset finishes ingesting, run a pipeline, re-score customers and notify the team."
          action={
            onCreate ? (
              <Button onClick={onCreate}>
                <Plus /> New workflow
              </Button>
            ) : undefined
          }
        />
      }
    />
  );
}

export function RunsTable({ workflowId }: { workflowId: string | null }) {
  const { slug } = useOrgContext();
  const router = useRouter();
  const runs = useWorkflowRuns(slug, workflowId);
  const columns = React.useMemo<DataTableColumn<WorkflowRun>[]>(
    () => [
      {
        id: "run",
        accessorFn: (r) => r.created_at ?? "",
        header: "Run",
        cell: ({ row }) => (
          <Link href={`/o/${slug}/automation/runs/${row.original.id}`} className="font-medium hover:underline" onClick={(e) => e.stopPropagation()}>
            {formatRelative(row.original.created_at)}
          </Link>
        ),
      },
      ...(workflowId
        ? []
        : [{ id: "workflow", accessorFn: (r: WorkflowRun) => r.workflow_name ?? "", header: "Workflow", cell: ({ row }: { row: { original: WorkflowRun } }) => row.original.workflow_name ?? "—" } as DataTableColumn<WorkflowRun>]),
      { id: "status", accessorFn: (r) => r.status, header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
      { id: "trigger", accessorFn: (r) => r.trigger_type ?? "", header: "Trigger", cell: ({ row }) => (row.original.trigger_type ? titleCase(row.original.trigger_type) : "—") },
      { id: "version", accessorFn: (r) => r.version ?? 0, header: "Version", cell: ({ row }) => (row.original.version ? `v${row.original.version}` : "—") },
      { id: "error", accessorFn: (r) => r.error ?? "", header: "Error", cell: ({ row }) => <span className="line-clamp-1 text-xs text-danger">{row.original.error ?? ""}</span> },
    ],
    [slug, workflowId],
  );
  if (runs.isError) return <Card><ModuleError error={runs.error} what="Workflow runs" onRetry={() => void runs.refetch()} /></Card>;
  return (
    <div>
      <DataTable
        caption="Workflow runs"
        columns={columns}
        data={runs.items}
        loading={runs.isLoading}
        getRowId={(r) => r.id}
        columnToggle={false}
        onRowClick={(r) => router.push(`/o/${slug}/automation/runs/${r.id}`)}
        emptyState={<EmptyState compact icon={<History />} title="No runs yet" description="Runs appear when a trigger fires or someone runs a workflow." />}
      />
      <Pager page={runs.page} hasPrev={runs.hasPrev} hasNext={runs.hasNext} onPrev={runs.goPrev} onNext={runs.goNext} loading={runs.isFetching} />
    </div>
  );
}

function CreateWorkflowDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { slug } = useOrgContext();
  const router = useRouter();
  const create = useCreateWorkflow(slug);
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (!open) {
      setName("");
      setDescription("");
      setError(null);
    }
  }, [open]);
  const submit = async () => {
    if (!name.trim()) return setError("Name the workflow.");
    try {
      const wf = await create.mutateAsync({ name: name.trim(), description: description.trim() || undefined });
      toast.success("Workflow created");
      onOpenChange(false);
      if (wf.id) router.push(`/o/${slug}/automation/workflows/${wf.id}`);
    } catch (e) {
      setError(errorMessage(e));
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <DialogHeader title="New workflow" description="You'll add steps and triggers in the builder." />
        <DialogBody className="space-y-3">
          <Field id="wf-name" label="Name" error={error ?? undefined}>
            <Input id="wf-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} autoFocus />
          </Field>
          <Field id="wf-desc" label="Description">
            <Input id="wf-desc" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={1000} />
          </Field>
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending}>
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
