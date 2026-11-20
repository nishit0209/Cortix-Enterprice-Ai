"use client";

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { CalendarClock, History, MoreHorizontal, Pencil, Play, Plus, Trash2, Workflow } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger, Tooltip } from "@/components/ui/overlays";
import { PageHeader } from "@/components/ui/page-header";
import { FeatureStatusBadge, StatusBadge } from "@/components/ui/status";
import { describeCron } from "@/lib/pipelines/cron";
import { serializeDag } from "@/lib/pipelines/dag";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { runPipeline, useCreatePipeline, useDeletePipeline, usePipelines } from "@/lib/queries/pipelines";
import { formatMs } from "@/hooks/use-countdown";
import type { Pipeline } from "@/lib/types";
import { formatDateTime, formatNumber, formatRelative } from "@/lib/utils";

export function PipelinesView() {
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const params = useSearchParams();
  const q = usePipelines(slug);
  const [creating, setCreating] = React.useState(params.get("new") === "1");
  const [deleting, setDeleting] = React.useState<Pipeline | null>(null);
  const del = useDeletePipeline(slug);
  const manage = can("pipelines.manage");

  const columns = React.useMemo<DataTableColumn<Pipeline>[]>(
    () => [
      {
        id: "name",
        accessorKey: "name",
        header: "Pipeline",
        cell: ({ row }) => (
          <span className="flex items-center gap-2.5">
            <span className="flex size-7 items-center justify-center rounded-control bg-accent-soft text-accent-soft-fg">
              <Workflow className="size-3.5" aria-hidden />
            </span>
            <span className="min-w-0">
              <span className="block truncate font-medium">{row.original.name}</span>
              <span className="block text-xs text-fg-subtle">{row.original.dag?.nodes?.length ?? 0} steps</span>
            </span>
          </span>
        ),
      },
      { id: "status", accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
      {
        id: "last_run",
        accessorFn: (r) => r.last_run?.started_at ?? "",
        header: "Last run",
        cell: ({ row }) => {
          const r = row.original.last_run;
          if (!r) return <span className="text-fg-subtle">Never run</span>;
          return (
            <span className="flex items-center gap-2">
              <StatusBadge status={r.status} />
              <Tooltip content={formatDateTime(r.started_at)}>
                <span className="text-xs text-fg-muted">{formatRelative(r.started_at ?? r.created_at)}</span>
              </Tooltip>
            </span>
          );
        },
      },
      {
        id: "rows",
        accessorFn: (r) => r.last_run?.rows_out ?? -1,
        header: "Rows out",
        meta: { align: "right", hideOnMobile: true },
        cell: ({ row }) => formatNumber(row.original.last_run?.rows_out),
      },
      {
        id: "duration",
        accessorFn: (r) => r.last_run?.duration_ms ?? -1,
        header: "Duration",
        meta: { align: "right", hideOnMobile: true },
        cell: ({ row }) => <span className="tabular">{formatMs(row.original.last_run?.duration_ms)}</span>,
      },
      {
        id: "schedule",
        accessorFn: (r) => r.schedule?.cron ?? "",
        header: "Schedule",
        cell: ({ row }) =>
          row.original.schedule ? (
            <Badge tone={row.original.schedule.is_active === false ? "outline" : "info"}>
              <CalendarClock /> {describeCron(row.original.schedule.cron)}
            </Badge>
          ) : (
            <span className="text-fg-subtle">Manual</span>
          ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">Actions</span>,
        enableSorting: false,
        meta: { align: "right" },
        cell: ({ row }) => (
          <div onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon-xs" aria-label={`Actions for ${row.original.name}`}>
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => router.push(`/o/${slug}/data/pipelines/${row.original.id}`)}>
                  <Pencil /> Open builder
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => router.push(`/o/${slug}/data/pipelines/${row.original.id}/runs`)}>
                  <History /> Run history
                </DropdownMenuItem>
                {can("pipelines.run") && (
                  <DropdownMenuItem
                    onSelect={async () => {
                      try {
                        await runPipeline(slug, row.original.id);
                        toast.success("Run queued");
                        void q.refetch();
                      } catch (e) {
                        toast.error(errorMessage(e));
                      }
                    }}
                  >
                    <Play /> Run now
                  </DropdownMenuItem>
                )}
                {manage && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem destructive onSelect={() => setDeleting(row.original)}>
                      <Trash2 /> Delete
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ),
      },
    ],
    [can, manage, q, router, slug],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Pipelines"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Visual pipelines that clean, reshape and join datasets into new versions, with schedules, retries and streamed runs."
        actions={
          manage ? (
            <Button onClick={() => setCreating(true)}>
              <Plus /> New pipeline
            </Button>
          ) : undefined
        }
      />
      {q.isError && isUnavailableEndpoint(q.error) ? (
        <Card>
          <EmptyState icon={<Workflow />} title="Pipelines aren't available yet" description="The pipeline service hasn't been deployed." />
        </Card>
      ) : (
        <DataTable
          caption="Pipelines"
          columns={columns}
          data={q.data}
          loading={q.isLoading}
          error={q.isError ? q.error : undefined}
          onRetry={() => void q.refetch()}
          onRowClick={(p) => router.push(`/o/${slug}/data/pipelines/${p.id}`)}
          getRowId={(p) => p.id}
          searchPlaceholder="Search pipelines…"
          filters={[
            {
              columnId: "status",
              label: "Status",
              options: Array.from(new Set((q.data ?? []).map((p) => p.status))).map((s) => ({ value: s, label: s })),
            },
          ]}
          emptyState={
            <EmptyState
              icon={<Workflow />}
              title="No pipelines yet"
              description="Build a pipeline from a dataset source through cleaning and reshaping steps to a new dataset version."
              action={
                manage ? (
                  <Button onClick={() => setCreating(true)}>
                    <Plus /> New pipeline
                  </Button>
                ) : undefined
              }
            />
          }
        />
      )}
      <NewPipelineDialog org={slug} open={creating} onOpenChange={setCreating} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete “${deleting?.name ?? ""}”?`}
        description="The pipeline, its versions and schedule are removed. Datasets it produced are kept."
        confirmLabel="Delete pipeline"
        confirmText={deleting?.name}
        loading={del.isPending}
        onConfirm={async () => {
          if (!deleting) return;
          try {
            await del.mutateAsync(deleting.id);
            toast.success("Pipeline deleted");
            setDeleting(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </div>
  );
}

function NewPipelineDialog({ org, open, onOpenChange }: { org: string; open: boolean; onOpenChange: (o: boolean) => void }) {
  const router = useRouter();
  const create = useCreatePipeline(org);
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [error, setError] = React.useState<string | undefined>();
  React.useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
      setError(undefined);
    }
  }, [open]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader icon={<Workflow />} title="New pipeline" description="You'll design the steps on the canvas next." />
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (!name.trim()) {
              setError("Name the pipeline.");
              return;
            }
            try {
              const p = await create.mutateAsync({
                name: name.trim(),
                description: description.trim() || undefined,
                dag: serializeDag({
                  nodes: [
                    { id: "source_1", type: "source", config: { dataset_id: "" }, position: { x: 0, y: 80 } },
                    { id: "output_1", type: "destination", config: { dataset_name: "", mode: "new_version" }, position: { x: 560, y: 80 } },
                  ],
                  edges: [],
                }),
              });
              onOpenChange(false);
              router.push(`/o/${org}/data/pipelines/${p.id}`);
            } catch (err) {
              setError(errorMessage(err));
            }
          }}
          className="contents"
        >
          <DialogBody className="space-y-4">
            <Field id="pl-name" label="Name" error={error}>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Clean sales orders" autoFocus maxLength={120} />
            </Field>
            <Field id="pl-desc" label="Description" hint="Optional.">
              <Textarea value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending}>
              Create &amp; open builder
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
