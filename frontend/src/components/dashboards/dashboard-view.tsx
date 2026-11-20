"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Check, LayoutDashboard, Move, Pencil, Plus, RefreshCw, Sparkles, Star } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { isApiError } from "@/lib/api";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { canManageDashboards } from "@/lib/permissions";
import { dashKeys, useDashboard, useDeleteWidget, useSaveLayout, useUpdateDashboard } from "@/lib/queries/dashboards";
import type { DashboardWidget } from "@/lib/types";
import { DashboardGrid } from "./dashboard-grid";
import { DashboardFormDialog, WidgetDialog } from "./dialogs";

/** /o/{org}/dashboards/{id}: the widget board with edit mode (drag, resize, add, edit, remove). */
export function DashboardView() {
  const { id } = useParams<{ id: string }>();
  const { slug, can, me } = useOrgContext();
  const qc = useQueryClient();
  const q = useDashboard(slug, id);
  const saveLayout = useSaveLayout(slug, id);
  const update = useUpdateDashboard(slug);
  const delWidget = useDeleteWidget(slug, id);
  const [editing, setEditing] = React.useState(false);
  const [widgetDialog, setWidgetDialog] = React.useState<{ open: boolean; widget: DashboardWidget | null }>({ open: false, widget: null });
  const [removing, setRemoving] = React.useState<DashboardWidget | null>(null);
  const [renaming, setRenaming] = React.useState(false);
  useSetBreadcrumb(q.data?.title);

  const d = q.data;
  const owner = !!d && (!d.owner_id || d.owner_id === me.user.id);
  const canEdit = owner && canManageDashboards(can);

  if (q.isLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-10 w-72" />
        <div className="grid gap-4 md:grid-cols-2">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-64 rounded-card" />
          ))}
        </div>
      </div>
    );
  }
  if (q.isError || !d) {
    return (
      <Card>
        {isApiError(q.error) && q.error.isNotFound ? (
          <EmptyState
            icon={<LayoutDashboard />}
            title="Dashboard not found"
            description="It may have been deleted, or it isn't shared with you."
            action={
              <Button variant="secondary" asChild>
                <Link href={`/o/${slug}/dashboards`}>
                  <ArrowLeft /> All dashboards
                </Link>
              </Button>
            }
          />
        ) : isUnavailableEndpoint(q.error) ? (
          <EmptyState icon={<LayoutDashboard />} title="Dashboards aren't available" description="The dashboard service did not respond. Try again shortly." />
        ) : (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        )}
      </Card>
    );
  }

  const refreshAll = () => void qc.invalidateQueries({ queryKey: dashKeys.widgetDataAll(slug, id) });

  return (
    <div className="space-y-6">
      <Link href={`/o/${slug}/dashboards`} className="inline-flex items-center gap-1.5 rounded-[4px] text-sm text-fg-muted hover:text-fg focus-ring">
        <ArrowLeft className="size-4" /> Dashboards
      </Link>
      <PageHeader
        title={d.title}
        description={d.description || undefined}
        badge={
          <span className="flex gap-1.5">
            {d.is_default && owner && (
              <Badge tone="accent">
                <Star /> Home
              </Badge>
            )}
            {d.is_shared && <Badge tone="info">Shared</Badge>}
          </span>
        }
        actions={
          <>
            {d.widgets.some((w) => w.kind !== "text") && !editing && (
              <Button variant="secondary" onClick={refreshAll}>
                <RefreshCw /> Refresh all
              </Button>
            )}
            {canEdit && !editing && !d.is_default && (
              <Button
                variant="ghost"
                loading={update.isPending}
                onClick={async () => {
                  try {
                    await update.mutateAsync({ id: d.id, is_default: true });
                    toast.success("Shown on your home dashboard");
                  } catch (e) {
                    toast.error(errorMessage(e));
                  }
                }}
              >
                <Star /> Show on home
              </Button>
            )}
            {canEdit && (
              <>
                {editing && (
                  <>
                    <Button variant="ghost" onClick={() => setRenaming(true)}>
                      <Pencil /> Rename
                    </Button>
                    <Button variant="secondary" onClick={() => setWidgetDialog({ open: true, widget: null })}>
                      <Plus /> Add widget
                    </Button>
                  </>
                )}
                <Button variant={editing ? "primary" : "secondary"} onClick={() => setEditing((e) => !e)} aria-pressed={editing}>
                  {editing ? (
                    <>
                      <Check /> Done
                    </>
                  ) : (
                    <>
                      <Move /> Edit layout
                    </>
                  )}
                </Button>
              </>
            )}
          </>
        }
      />
      {editing && (
        <p className="text-xs text-fg-muted" role="note">
          Drag a widget by its title to move it, drag its corner to resize it. With the keyboard, focus a widget and use the arrow keys (Shift to resize). Changes save automatically.
          {saveLayout.isPending ? " Saving…" : ""}
        </p>
      )}
      {d.widgets.length === 0 ? (
        <Card>
          <EmptyState
            icon={<LayoutDashboard />}
            title="This dashboard is empty"
            description="Pin an answer from Ask AI (“Pin to dashboard” under any answer), or add a widget from a saved query or a text note."
            action={
              canEdit ? (
                <Button onClick={() => setWidgetDialog({ open: true, widget: null })}>
                  <Plus /> Add widget
                </Button>
              ) : undefined
            }
            secondaryAction={
              can("bi.chat") ? (
                <Button variant="secondary" asChild>
                  <Link href={`/o/${slug}/ask`}>
                    <Sparkles /> Ask a question
                  </Link>
                </Button>
              ) : undefined
            }
          />
        </Card>
      ) : (
        <DashboardGrid
          org={slug}
          dashboardId={d.id}
          widgets={d.widgets}
          editing={editing}
          canEdit={canEdit}
          onCommit={(items) =>
            saveLayout.mutate(items, {
              onError: (e) => toast.error(`Layout not saved: ${errorMessage(e)}`),
            })
          }
          onEditWidget={canEdit ? (w) => setWidgetDialog({ open: true, widget: w }) : undefined}
          onRemoveWidget={canEdit ? (w) => setRemoving(w) : undefined}
        />
      )}
      <WidgetDialog
        org={slug}
        dashboardId={d.id}
        existing={d.widgets}
        widget={widgetDialog.widget}
        open={widgetDialog.open}
        onOpenChange={(open) => setWidgetDialog((s) => ({ ...s, open }))}
      />
      <DashboardFormDialog org={slug} open={renaming} onOpenChange={setRenaming} dashboard={d} />
      <ConfirmDialog
        open={!!removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={`Remove “${removing?.title ?? ""}”?`}
        description="The widget is removed from this dashboard. Its saved query stays available."
        confirmLabel="Remove widget"
        loading={delWidget.isPending}
        onConfirm={async () => {
          if (!removing) return;
          try {
            await delWidget.mutateAsync(removing.id);
            toast.success("Widget removed");
            setRemoving(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </div>
  );
}

/** The caller's home dashboard (is_default) rendered read-only on the org home page. */
export function HomeDashboardSection({ dashboardId }: { dashboardId: string }) {
  const { slug } = useOrgContext();
  const q = useDashboard(slug, dashboardId);
  if (q.isLoading) return <Skeleton className="h-64 rounded-card" />;
  if (q.isError || !q.data) return <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />;
  const d = q.data;
  return (
    <section aria-labelledby="home-dashboard" className="space-y-3">
      <div className="flex items-center gap-2">
        <LayoutDashboard className="size-4 text-fg-subtle" aria-hidden />
        <h2 id="home-dashboard" className="text-sm font-semibold text-fg">
          {d.title}
        </h2>
        <Button variant="link" size="xs" asChild className="ml-auto">
          <Link href={`/o/${slug}/dashboards/${d.id}`}>Open dashboard</Link>
        </Button>
      </div>
      {d.widgets.length === 0 ? (
        <Card>
          <EmptyState
            compact
            icon={<LayoutDashboard />}
            title="Your home dashboard is empty"
            description="Pin answers from Ask AI to see them here."
            action={
              <Button size="sm" variant="secondary" asChild>
                <Link href={`/o/${slug}/ask`}>
                  <Sparkles /> Ask a question
                </Link>
              </Button>
            }
          />
        </Card>
      ) : (
        <DashboardGrid org={slug} dashboardId={d.id} widgets={d.widgets} />
      )}
    </section>
  );
}
