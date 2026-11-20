"use client";

import * as React from "react";
import Link from "next/link";
import { LayoutDashboard, Pin, Plus, SquarePen } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/checkbox";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useOptionalOrgContext } from "@/components/shell/org-context";
import { isApiError } from "@/lib/api";
import { defaultSize, placeNew } from "@/lib/dashboards/layout";
import { WIDGET_KINDS } from "@/lib/dashboards/widgets";
import { errorMessage } from "@/lib/errors";
import { useCreateDashboard, useCreateWidget, useDashboard, useDashboards, useSavedQueries, useUpdateDashboard, useUpdateWidget } from "@/lib/queries/dashboards";
import type { ChartHint, DashboardSummary, DashboardWidget, WidgetKind } from "@/lib/types";

const NEW = "__new__";

/* ---------------- create / rename dashboard ---------------- */

export function DashboardFormDialog({
  org,
  open,
  onOpenChange,
  dashboard,
  onSaved,
}: {
  org: string;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  dashboard?: DashboardSummary | null;
  onSaved?: (d: { id: string }) => void;
}) {
  const create = useCreateDashboard(org);
  const update = useUpdateDashboard(org);
  const [title, setTitle] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [shared, setShared] = React.useState(false);
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  React.useEffect(() => {
    if (!open) return;
    setTitle(dashboard?.title ?? "");
    setDescription(dashboard?.description ?? "");
    setShared(!!dashboard?.is_shared);
    setErrors({});
  }, [open, dashboard]);
  const pending = create.isPending || update.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <DialogHeader icon={<LayoutDashboard />} title={dashboard ? "Edit dashboard" : "New dashboard"} description={dashboard ? undefined : "Pin Ask AI answers or saved queries to it."} />
        <form
          className="contents"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!title.trim()) return setErrors({ title: "Give the dashboard a title." });
            try {
              const body = { title: title.trim(), description: description.trim(), is_shared: shared };
              const d = dashboard ? await update.mutateAsync({ id: dashboard.id, ...body }) : await create.mutateAsync(body);
              toast.success(dashboard ? "Dashboard updated" : "Dashboard created");
              onOpenChange(false);
              onSaved?.({ id: d?.id ?? dashboard?.id ?? "" });
            } catch (err) {
              if (isApiError(err)) setErrors(err.fieldErrors());
              toast.error(errorMessage(err));
            }
          }}
        >
          <DialogBody className="space-y-4">
            <Field id="dash-title" label="Title" error={errors.title}>
              <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} placeholder="Sales overview" autoFocus />
            </Field>
            <Field id="dash-desc" label="Description" error={errors.description}>
              <Textarea value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} className="min-h-16" />
            </Field>
            <label className="flex items-start gap-2.5 text-sm text-fg">
              <Switch checked={shared} onCheckedChange={setShared} aria-label="Share with the organization" className="mt-0.5" />
              <span>
                Share with the organization
                <span className="block text-xs text-fg-muted">Members with dashboard access can view it. Every viewer only sees data their own access policy allows.</span>
              </span>
            </label>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending}>
              {dashboard ? "Save" : "Create dashboard"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/* ---------------- add / edit widget ---------------- */

function KindSelect({ value, onChange, allowText }: { value: WidgetKind; onChange: (k: WidgetKind) => void; allowText: boolean }) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as WidgetKind)}>
      <SelectTrigger aria-label="Widget type">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {WIDGET_KINDS.filter((k) => allowText || k.value !== "text").map((k) => (
          <SelectItem key={k.value} value={k.value} description={k.description}>
            {k.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

const CHART_TYPES: Array<{ value: string; label: string }> = [
  { value: "auto", label: "Automatic" },
  { value: "bar", label: "Bar" },
  { value: "line", label: "Line" },
  { value: "area", label: "Area" },
  { value: "pie", label: "Pie" },
];

/** Add a widget from a saved query (or a text note), or edit an existing widget's title, type, chart or text. */
export function WidgetDialog({
  org,
  dashboardId,
  existing,
  widget,
  open,
  onOpenChange,
}: {
  org: string;
  dashboardId: string;
  existing: DashboardWidget[];
  widget?: DashboardWidget | null;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const saved = useSavedQueries(org, open && !widget);
  const create = useCreateWidget(org);
  const update = useUpdateWidget(org, dashboardId);
  const [kind, setKind] = React.useState<WidgetKind>("chart");
  const [title, setTitle] = React.useState("");
  const [text, setText] = React.useState("");
  const [queryId, setQueryId] = React.useState<string>("");
  const [chartType, setChartType] = React.useState("auto");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (!open) return;
    setKind(widget?.kind ?? "chart");
    setTitle(widget?.title ?? "");
    setText(widget?.text ?? "");
    setQueryId(widget?.saved_query_id ?? "");
    setChartType(widget?.chart?.type && widget.chart.type !== "table" ? String(widget.chart.type) : "auto");
    setError(null);
  }, [open, widget]);
  const queries = saved.data ?? [];
  const selectedQuery = queries.find((q) => q.id === queryId);

  const chartFor = (k: WidgetKind): ChartHint | null | undefined => {
    if (k !== "chart") return k === "kpi" ? { type: "kpi" } : undefined;
    if (chartType === "auto") return widget ? null : (selectedQuery?.chart ?? null);
    return { ...(widget?.chart ?? selectedQuery?.chart ?? {}), type: chartType };
  };

  const submit = async () => {
    setError(null);
    const t = title.trim() || selectedQuery?.name || "";
    if (!t) return setError("Give the widget a title.");
    try {
      if (widget) {
        await update.mutateAsync({
          wid: widget.id,
          title: t,
          ...(widget.kind === "text" ? { text } : { chart: chartFor(widget.kind) ?? null }),
        });
        toast.success("Widget updated");
      } else {
        if (kind !== "text" && !queryId) return setError("Choose a saved query.");
        const layout = placeNew(
          existing.map((w) => w.layout),
          defaultSize(kind),
        );
        await create.mutateAsync({
          dashboardId,
          title: t,
          kind,
          layout,
          ...(kind === "text" ? { text } : { saved_query_id: queryId }),
          ...(chartFor(kind) !== undefined ? { chart: chartFor(kind) } : {}),
        });
        toast.success("Widget added");
      }
      onOpenChange(false);
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader icon={widget ? <SquarePen /> : <Plus />} title={widget ? "Edit widget" : "Add a widget"} />
        <form
          className="contents"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <DialogBody className="space-y-4">
            {!widget && (
              <div className="space-y-1.5">
                <span className="text-sm font-medium text-fg">Type</span>
                <KindSelect value={kind} onChange={setKind} allowText />
              </div>
            )}
            {!widget && kind !== "text" && (
              <div className="space-y-1.5">
                <span className="text-sm font-medium text-fg">Saved query</span>
                {saved.isLoading ? (
                  <Skeleton className="h-9" />
                ) : queries.length === 0 ? (
                  <EmptyState
                    compact
                    icon={<Pin />}
                    title="No saved queries yet"
                    description="Ask a question in Ask AI and use “Pin to dashboard” on the answer; it is saved as a query automatically."
                    action={
                      <Button size="sm" variant="secondary" asChild>
                        <Link href={`/o/${org}/ask`}>Open Ask AI</Link>
                      </Button>
                    }
                  />
                ) : (
                  <Select value={queryId} onValueChange={setQueryId}>
                    <SelectTrigger aria-label="Saved query">
                      <SelectValue placeholder="Choose a saved query" />
                    </SelectTrigger>
                    <SelectContent>
                      {queries.map((q) => (
                        <SelectItem key={q.id} value={q.id} description={q.question || undefined}>
                          {q.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </div>
            )}
            <Field id="widget-title" label="Title">
              <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} placeholder={selectedQuery?.name ?? "Revenue by region"} />
            </Field>
            {(widget ? widget.kind : kind) === "chart" && (
              <div className="space-y-1.5">
                <span className="text-sm font-medium text-fg">Chart</span>
                <Select value={chartType} onValueChange={setChartType}>
                  <SelectTrigger aria-label="Chart type">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {CHART_TYPES.map((c) => (
                      <SelectItem key={c.value} value={c.value}>
                        {c.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            {(widget ? widget.kind : kind) === "text" && (
              <Field id="widget-text" label="Text">
                <Textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={20000} className="min-h-28" placeholder="Context for readers of this dashboard" />
              </Field>
            )}
            {error && (
              <p role="alert" className="text-sm text-danger">
                {error}
              </p>
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending || update.isPending}>
              {widget ? "Save" : "Add widget"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/* ---------------- pin an Ask AI answer ---------------- */

/**
 * "Pin to dashboard" on an Ask AI answer: `POST /dashboards/{id}/widgets {message_id, ...}` turns the answer's
 * audited SQL into a saved query, so the widget re-runs it (under each viewer's policy) instead of storing rows.
 */
export function PinDialog({
  org,
  messageId,
  defaultTitle,
  chart,
  open,
  onOpenChange,
}: {
  org: string;
  messageId: string;
  defaultTitle: string;
  chart: ChartHint | null;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const dashboards = useDashboards(org, open);
  const createDash = useCreateDashboard(org);
  const createWidget = useCreateWidget(org);
  const [target, setTarget] = React.useState<string>("");
  const [newTitle, setNewTitle] = React.useState("");
  const [title, setTitle] = React.useState(defaultTitle);
  const [kind, setKind] = React.useState<WidgetKind>(chart?.type === "kpi" ? "kpi" : chart?.type === "table" ? "table" : "chart");
  const [error, setError] = React.useState<string | null>(null);
  const [done, setDone] = React.useState<string | null>(null);
  const userId = useOptionalOrgContext()?.me.user.id;
  // Only the owner may add widgets (shared dashboards of teammates answer 403), so only own dashboards are offered.
  const list = React.useMemo(
    () => (dashboards.data ?? []).filter((d) => !userId || !d.owner_id || d.owner_id === userId),
    [dashboards.data, userId],
  );
  const targetDash = useDashboard(org, target && target !== NEW ? target : null);

  React.useEffect(() => {
    if (!open) return;
    setTitle(defaultTitle.slice(0, 200));
    setError(null);
    setDone(null);
    setNewTitle("");
  }, [open, defaultTitle]);
  React.useEffect(() => {
    if (!open || target) return;
    if (dashboards.isSuccess) setTarget(list.find((d) => d.is_default)?.id ?? list[0]?.id ?? NEW);
  }, [open, target, list, dashboards.isSuccess]);

  const pending = createDash.isPending || createWidget.isPending;
  const submit = async () => {
    setError(null);
    if (!title.trim()) return setError("Give the widget a title.");
    try {
      let dashboardId = target;
      let existing: DashboardWidget[] = targetDash.data?.widgets ?? [];
      if (target === NEW) {
        if (!newTitle.trim()) return setError("Name the new dashboard.");
        const d = await createDash.mutateAsync({ title: newTitle.trim() });
        dashboardId = d.id;
        existing = [];
      }
      await createWidget.mutateAsync({
        dashboardId,
        title: title.trim(),
        kind,
        message_id: messageId,
        layout: placeNew(
          existing.map((w) => w.layout),
          defaultSize(kind),
        ),
        ...(kind === "chart" && chart && chart.type !== "table" && chart.type !== "kpi" ? { chart } : kind === "kpi" ? { chart: { type: "kpi" } } : {}),
      });
      setDone(dashboardId);
      toast.success("Pinned to the dashboard");
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <DialogHeader icon={<Pin />} title="Pin to dashboard" description="The widget re-runs this answer's query on every view, under each viewer's access policy." />
        {done ? (
          <>
            <DialogBody>
              <p className="text-sm text-fg">The answer is on your dashboard.</p>
            </DialogBody>
            <DialogFooter>
              <Button variant="secondary" onClick={() => onOpenChange(false)}>
                Close
              </Button>
              <Button asChild>
                <Link href={`/o/${org}/dashboards/${done}`}>Open dashboard</Link>
              </Button>
            </DialogFooter>
          </>
        ) : (
          <form
            className="contents"
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            <DialogBody className="space-y-4">
              <div className="space-y-1.5">
                <span className="text-sm font-medium text-fg">Dashboard</span>
                {dashboards.isLoading ? (
                  <Skeleton className="h-9" />
                ) : (
                  <Select value={target} onValueChange={setTarget}>
                    <SelectTrigger aria-label="Dashboard">
                      <SelectValue placeholder="Choose a dashboard" />
                    </SelectTrigger>
                    <SelectContent>
                      {list.map((d) => (
                        <SelectItem key={d.id} value={d.id}>
                          {d.title}
                          {d.is_default ? " (default)" : ""}
                        </SelectItem>
                      ))}
                      <SelectItem value={NEW}>+ New dashboard…</SelectItem>
                    </SelectContent>
                  </Select>
                )}
              </div>
              {target === NEW && (
                <Field id="pin-new" label="New dashboard name">
                  <Input value={newTitle} onChange={(e) => setNewTitle(e.target.value)} maxLength={200} placeholder="My dashboard" />
                </Field>
              )}
              <Field id="pin-title" label="Widget title">
                <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
              </Field>
              <div className="space-y-1.5">
                <span className="text-sm font-medium text-fg">Show as</span>
                <KindSelect value={kind} onChange={setKind} allowText={false} />
              </div>
              {error && (
                <p role="alert" className="text-sm text-danger">
                  {error}
                </p>
              )}
            </DialogBody>
            <DialogFooter>
              <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" loading={pending} disabled={!target || (target !== NEW && targetDash.isLoading)}>
                <Pin /> Pin
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
