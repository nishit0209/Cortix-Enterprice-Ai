"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { LayoutDashboard, MoreHorizontal, Pencil, Plus, Share2, Sparkles, Star, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/overlays";
import { PageHeader } from "@/components/ui/page-header";
import { FeatureStatusBadge } from "@/components/ui/status";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { canManageDashboards } from "@/lib/permissions";
import { useDashboards, useDeleteDashboard, useUpdateDashboard } from "@/lib/queries/dashboards";
import type { DashboardSummary } from "@/lib/types";
import { formatRelative } from "@/lib/utils";
import { DashboardFormDialog } from "./dialogs";

/** /o/{org}/dashboards: the caller's own and shared dashboards (create, rename, set default, share, delete). */
export function DashboardsView() {
  const { slug, can, me } = useOrgContext();
  const router = useRouter();
  const q = useDashboards(slug);
  const update = useUpdateDashboard(slug);
  const del = useDeleteDashboard(slug);
  const manage = canManageDashboards(can);
  const [form, setForm] = React.useState<{ open: boolean; dashboard: DashboardSummary | null }>({ open: false, dashboard: null });
  const [deleting, setDeleting] = React.useState<DashboardSummary | null>(null);
  const list = q.data ?? [];
  const mine = (d: DashboardSummary) => !d.owner_id || d.owner_id === me.user.id;

  const setDefault = async (d: DashboardSummary) => {
    try {
      await update.mutateAsync({ id: d.id, is_default: true });
      toast.success(`“${d.title}” is now your home dashboard`);
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };
  const toggleShare = async (d: DashboardSummary) => {
    try {
      await update.mutateAsync({ id: d.id, is_shared: !d.is_shared });
      toast.success(d.is_shared ? "No longer shared" : "Shared with the organization");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Dashboards"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Pinned answers and saved queries, re-run live on every view under each viewer's access policy."
        actions={
          manage ? (
            <Button onClick={() => setForm({ open: true, dashboard: null })}>
              <Plus /> New dashboard
            </Button>
          ) : undefined
        }
      />
      {q.isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-36 rounded-card" />
          ))}
        </div>
      ) : q.isError ? (
        <Card>
          {isUnavailableEndpoint(q.error) ? (
            <EmptyState icon={<LayoutDashboard />} title="Dashboards aren't available" description="The dashboard service did not respond. Try again shortly." />
          ) : (
            <ErrorState error={q.error} onRetry={() => void q.refetch()} />
          )}
        </Card>
      ) : list.length === 0 ? (
        <Card>
          <EmptyState
            icon={<LayoutDashboard />}
            title="No dashboards yet"
            description="Create a dashboard, then pin answers from Ask AI to it. Widgets always re-run their query, so they stay current."
            action={
              manage ? (
                <Button onClick={() => setForm({ open: true, dashboard: null })}>
                  <Plus /> New dashboard
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
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-label="Dashboards">
          {list.map((d) => (
            <li key={d.id}>
              <Card interactive className="relative flex h-full flex-col gap-3 p-5">
                <div className="flex items-start gap-3">
                  <span className="flex size-9 shrink-0 items-center justify-center rounded-card bg-accent-soft text-accent-soft-fg">
                    <LayoutDashboard className="size-4" aria-hidden />
                  </span>
                  <div className="min-w-0 flex-1">
                    <Link href={`/o/${slug}/dashboards/${d.id}`} className="block truncate text-sm font-semibold text-fg after:absolute after:inset-0 focus-ring">
                      {d.title}
                    </Link>
                    <p className="mt-0.5 line-clamp-2 text-xs text-fg-muted">{d.description || "No description"}</p>
                  </div>
                  {manage && mine(d) && (
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="icon-xs" aria-label={`Actions for ${d.title}`} className="relative z-10">
                          <MoreHorizontal />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onSelect={() => setForm({ open: true, dashboard: d })}>
                          <Pencil /> Rename
                        </DropdownMenuItem>
                        {!d.is_default && (
                          <DropdownMenuItem onSelect={() => void setDefault(d)}>
                            <Star /> Show on home
                          </DropdownMenuItem>
                        )}
                        <DropdownMenuItem onSelect={() => void toggleShare(d)}>
                          <Share2 /> {d.is_shared ? "Stop sharing" : "Share with organization"}
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem onSelect={() => setDeleting(d)} className="text-danger">
                          <Trash2 /> Delete
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  )}
                </div>
                <div className="mt-auto flex flex-wrap items-center gap-2 text-xs text-fg-muted">
                  {d.is_default && (
                    <Badge tone="accent">
                      <Star /> Home
                    </Badge>
                  )}
                  {d.is_shared && <Badge tone="info">Shared</Badge>}
                  {!mine(d) && <Badge tone="outline">Shared with you</Badge>}
                  <span>
                    {d.widget_count} widget{d.widget_count === 1 ? "" : "s"}
                  </span>
                  <span>· updated {formatRelative(d.updated_at)}</span>
                </div>
              </Card>
            </li>
          ))}
        </ul>
      )}
      <DashboardFormDialog
        org={slug}
        open={form.open}
        dashboard={form.dashboard}
        onOpenChange={(open) => setForm((f) => ({ ...f, open }))}
        onSaved={(d) => {
          if (!form.dashboard && d.id) router.push(`/o/${slug}/dashboards/${d.id}`);
        }}
      />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete “${deleting?.title ?? ""}”?`}
        description="The dashboard and its widgets are removed. Saved queries stay available."
        confirmLabel="Delete dashboard"
        loading={del.isPending}
        onConfirm={async () => {
          if (!deleting) return;
          try {
            await del.mutateAsync(deleting.id);
            toast.success("Dashboard deleted");
            setDeleting(null);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </div>
  );
}
