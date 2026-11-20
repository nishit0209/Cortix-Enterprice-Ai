"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { motion, useReducedMotion } from "framer-motion";
import { Boxes, BrainCircuit, FlaskConical, Plus, Rocket, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { StatCard } from "@/components/ui/stat-card";
import { FeatureStatusBadge } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorMessage } from "@/lib/errors";
import { algorithmLabel } from "@/lib/ml/algorithms";
import { servingVersion } from "@/lib/ml/mappers";
import type { MlModel, MlProject } from "@/lib/ml/types";
import { useDeleteMlProject, useMlModels, useMlProjects } from "@/lib/queries/ml";
import { formatNumber, formatRelative } from "@/lib/utils";
import { fmtMetric } from "./ml-charts";
import { MlError, StageBadge, TaskBadge } from "./common";

export function MlWorkspace() {
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const projects = useMlProjects(slug);
  const models = useMlModels(slug);
  const canManage = can("ml.manage");
  const [tab, setTab] = React.useState("projects");

  const prodCount = (models.data ?? []).filter((m) => m.production_version !== null).length;
  const experiments = (projects.data ?? []).reduce((a, p) => a + (p.experiment_count ?? 0), 0);
  const hasExperimentCounts = (projects.data ?? []).some((p) => p.experiment_count !== null);

  return (
    <div className="space-y-6">
      <PageHeader
        title="ML Workspace"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Explore a dataset, get model recommendations with reasons, train on CPU, compare experiments, then register, promote and serve models with drift monitoring."
        actions={
          canManage && (
            <Button asChild>
              <Link href={`/o/${slug}/ml/new`}>
                <Plus /> New project
              </Link>
            </Button>
          )
        }
      />

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="ML summary">
        <StatCard label="Projects" icon={<FlaskConical />} loading={projects.isLoading} value={projects.data ? formatNumber(projects.data.length) : "—"} />
        <StatCard
          label="Experiments"
          icon={<BrainCircuit />}
          accent="teal"
          loading={projects.isLoading}
          value={projects.data && hasExperimentCounts ? formatNumber(experiments) : "—"}
          hint={projects.data && !hasExperimentCounts ? "Open a project to see its runs" : undefined}
        />
        <StatCard label="Registered models" icon={<Boxes />} accent="accent" loading={models.isLoading} value={models.data ? formatNumber(models.data.length) : "—"} />
        <StatCard label="In production" icon={<Rocket />} accent="success" loading={models.isLoading} value={models.data ? formatNumber(prodCount) : "—"} />
      </section>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList aria-label="ML workspace sections">
          <TabsTrigger value="projects">
            <FlaskConical /> Projects
          </TabsTrigger>
          <TabsTrigger value="models">
            <Boxes /> Model registry
          </TabsTrigger>
        </TabsList>
        <TabsContent value="projects">
          {projects.isError ? (
            <Card>
              <MlError error={projects.error} what="The ML workspace" onRetry={() => void projects.refetch()} />
            </Card>
          ) : projects.isLoading ? (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-40 rounded-card" />
              ))}
            </div>
          ) : (projects.data ?? []).length === 0 ? (
            <Card>
              <EmptyState
                icon={<FlaskConical />}
                title="No ML projects yet"
                description="A project ties a ready dataset to a target column. NEXORA profiles it, recommends models and trains them on this server."
                action={
                  canManage ? (
                    <Button onClick={() => router.push(`/o/${slug}/ml/new`)}>
                      <Plus /> New project
                    </Button>
                  ) : undefined
                }
              />
            </Card>
          ) : (
            <ProjectGrid projects={projects.data!} canManage={canManage} />
          )}
        </TabsContent>
        <TabsContent value="models">
          <ModelsTable models={models.data} loading={models.isLoading} error={models.isError ? models.error : null} onRetry={() => void models.refetch()} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function ProjectGrid({ projects, canManage }: { projects: MlProject[]; canManage: boolean }) {
  const { slug } = useOrgContext();
  const reduce = useReducedMotion();
  const del = useDeleteMlProject(slug);
  const [pending, setPending] = React.useState<MlProject | null>(null);
  return (
    <>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {projects.map((p, i) => (
          <motion.div key={p.id} initial={reduce ? false : { opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2, delay: Math.min(i, 8) * 0.03 }}>
            <Card interactive className="group relative flex h-full flex-col p-5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <Link href={`/o/${slug}/ml/projects/${p.id}`} className="block truncate font-semibold text-fg after:absolute after:inset-0 focus-ring rounded-[4px]">
                    {p.name}
                  </Link>
                  <p className="mt-0.5 truncate text-xs text-fg-muted">
                    {p.dataset_name ?? "Dataset"} {p.target_column ? <>→ <span className="font-mono">{p.target_column}</span></> : null}
                  </p>
                </div>
                <TaskBadge task={p.task_type} />
              </div>
              {p.description && <p className="mt-3 line-clamp-2 text-sm text-fg-muted">{p.description}</p>}
              <dl className="mt-auto grid grid-cols-3 gap-2 pt-4 text-xs">
                <div>
                  <dt className="text-fg-subtle">Features</dt>
                  <dd className="tabular font-medium text-fg">{formatNumber(p.features.length)}</dd>
                </div>
                <div>
                  <dt className="text-fg-subtle">Experiments</dt>
                  <dd className="tabular font-medium text-fg">{p.experiment_count === null ? "—" : formatNumber(p.experiment_count)}</dd>
                </div>
                <div>
                  <dt className="text-fg-subtle">{p.best_metric ? `Best ${p.best_metric.name}` : "Updated"}</dt>
                  <dd className="tabular font-medium text-fg">{p.best_metric ? fmtMetric(p.best_metric.value) : formatRelative(p.updated_at ?? p.created_at)}</dd>
                </div>
              </dl>
              {canManage && (
                <Button
                  variant="ghost"
                  size="icon-xs"
                  className="absolute right-2 bottom-2 z-10 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                  aria-label={`Delete ${p.name}`}
                  onClick={() => setPending(p)}
                >
                  <Trash2 />
                </Button>
              )}
            </Card>
          </motion.div>
        ))}
      </div>
      <ConfirmDialog
        open={!!pending}
        onOpenChange={(o) => !o && setPending(null)}
        title={`Delete "${pending?.name ?? ""}"?`}
        description="Its experiments are deleted too. Registered models keep working."
        confirmLabel="Delete project"
        confirmText={pending?.name}
        loading={del.isPending}
        onConfirm={async () => {
          if (!pending) return;
          try {
            await del.mutateAsync(pending.id);
            toast.success("Project deleted");
            setPending(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </>
  );
}

function ModelsTable({ models, loading, error, onRetry }: { models: MlModel[] | undefined; loading: boolean; error: unknown; onRetry: () => void }) {
  const { slug } = useOrgContext();
  const router = useRouter();
  const columns = React.useMemo<DataTableColumn<MlModel>[]>(
    () => [
      {
        id: "name",
        accessorKey: "name",
        header: "Model",
        cell: ({ row }) => (
          <Link href={`/o/${slug}/ml/models/${row.original.id}`} className="font-medium hover:underline" onClick={(e) => e.stopPropagation()}>
            {row.original.name}
          </Link>
        ),
      },
      { id: "project", accessorFn: (m) => m.project_name ?? "", header: "Project", cell: ({ row }) => row.original.project_name ?? "—" },
      { id: "task", accessorFn: (m) => m.task_type ?? "", header: "Task", cell: ({ row }) => <TaskBadge task={row.original.task_type} /> },
      {
        id: "serving",
        accessorFn: (m) => servingVersion(m)?.version ?? 0,
        header: "Serving",
        cell: ({ row }) => {
          const v = servingVersion(row.original);
          return v ? (
            <span className="flex items-center gap-2">
              <span className="tabular font-mono text-xs">v{v.version}</span>
              <StageBadge stage={v.stage} />
            </span>
          ) : (
            "—"
          );
        },
      },
      {
        id: "algorithm",
        accessorFn: (m) => servingVersion(m)?.algorithm ?? "",
        header: "Algorithm",
        cell: ({ row }) => {
          const a = servingVersion(row.original)?.algorithm;
          return a ? algorithmLabel(a) : "—";
        },
      },
      { id: "versions", accessorFn: (m) => m.latest_version ?? 0, header: "Latest", meta: { align: "right" }, cell: ({ row }) => (row.original.latest_version ? `v${row.original.latest_version}` : "—") },
    ],
    [slug],
  );
  if (error) {
    return (
      <Card>
        <MlError error={error} what="The model registry" onRetry={onRetry} />
      </Card>
    );
  }
  return (
    <DataTable
      caption="Registered models"
      columns={columns}
      data={models}
      loading={loading}
      getRowId={(m) => m.id}
      onRowClick={(m) => router.push(`/o/${slug}/ml/models/${m.id}`)}
      emptyState={
        <EmptyState compact icon={<Boxes />} title="No registered models" description="Register the best experiment of a project to version it here." />
      }
    />
  );
}
