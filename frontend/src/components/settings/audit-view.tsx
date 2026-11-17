"use client";

import * as React from "react";
import { Braces, Filter, ScrollText, X } from "lucide-react";
import { useOrgContext } from "@/components/shell/org-context";
import { PageHeader } from "@/components/ui/page-header";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogHeader } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { Tooltip } from "@/components/ui/overlays";
import { FeatureStatusBadge } from "@/components/ui/status";
import { useAuditEvents } from "@/lib/queries/org";
import { isUnavailableEndpoint } from "@/lib/errors";
import { formatDateTime, formatRelative } from "@/lib/utils";
import type { AuditEvent } from "@/lib/types";

function short(id: string | null | undefined) {
  if (!id) return "—";
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

function useDebounced<T>(value: T, ms = 350) {
  const [v, setV] = React.useState(value);
  React.useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function AuditView() {
  const { slug, can } = useOrgContext();
  const [action, setAction] = React.useState("");
  const [targetType, setTargetType] = React.useState("");
  const filters = { action: useDebounced(action.trim()), target_type: useDebounced(targetType.trim()) };
  const q = useAuditEvents(slug, filters);
  const [selected, setSelected] = React.useState<AuditEvent | null>(null);

  const events = React.useMemo(() => (q.data?.pages ?? []).flatMap((p) => p.items), [q.data]);

  const columns = React.useMemo<DataTableColumn<AuditEvent>[]>(
    () => [
      {
        accessorKey: "occurred_at",
        header: "Time",
        cell: ({ getValue }) => (
          <Tooltip content={formatDateTime(getValue() as string)}>
            <span className="whitespace-nowrap text-fg-muted">{formatRelative(getValue() as string)}</span>
          </Tooltip>
        ),
      },
      {
        accessorKey: "action",
        header: "Action",
        cell: ({ getValue }) => <span className="font-mono text-xs font-medium text-fg">{String(getValue())}</span>,
      },
      {
        id: "actor",
        accessorFn: (e) => `${e.actor_type} ${e.actor_id ?? ""}`,
        header: "Actor",
        meta: { label: "Actor" },
        cell: ({ row }) => (
          <span className="inline-flex items-center gap-1.5">
            <Badge tone={row.original.actor_type === "user" ? "neutral" : "info"}>{row.original.actor_type}</Badge>
            <span className="font-mono text-xs text-fg-muted" title={row.original.actor_id ?? undefined}>
              {short(row.original.actor_id)}
            </span>
          </span>
        ),
      },
      {
        id: "target",
        accessorFn: (e) => `${e.target_type ?? ""} ${e.target_id ?? ""}`,
        header: "Target",
        meta: { label: "Target" },
        cell: ({ row }) =>
          row.original.target_type ? (
            <span className="inline-flex items-center gap-1.5">
              <span className="text-fg">{row.original.target_type}</span>
              <span className="font-mono text-xs text-fg-subtle" title={row.original.target_id ?? undefined}>
                {short(row.original.target_id)}
              </span>
            </span>
          ) : (
            <span className="text-fg-subtle">—</span>
          ),
      },
      {
        id: "details",
        header: () => <span className="sr-only">Details</span>,
        enableSorting: false,
        enableHiding: false,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="xs"
            onClick={(e) => {
              e.stopPropagation();
              setSelected(row.original);
            }}
          >
            <Braces /> Details
          </Button>
        ),
      },
    ],
    [],
  );

  if (!can("audit.view")) {
    return (
      <div className="space-y-6">
        <PageHeader title="Audit log" />
        <EmptyState icon={<ScrollText />} title="You don't have access" description="Viewing the audit log requires the audit.view permission." />
      </div>
    );
  }

  const unavailable = q.isError && isUnavailableEndpoint(q.error);
  const filtered = !!(filters.action || filters.target_type);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Audit log"
        badge={<FeatureStatusBadge status="working" />}
        description="An append-only record of every change in this organization."
      />
      <DataTable
        caption="Audit events"
        columns={columns}
        data={events}
        loading={q.isLoading}
        error={q.isError && !unavailable ? q.error : undefined}
        onRetry={() => void q.refetch()}
        searchPlaceholder="Search loaded events…"
        pageSize={50}
        getRowId={(e) => e.id}
        onRowClick={setSelected}
        toolbar={
          <div className="flex flex-wrap items-center gap-2">
            <Input
              aria-label="Filter by exact action"
              placeholder="action, e.g. dataset.created"
              className="w-56"
              leading={<Filter />}
              value={action}
              onChange={(e) => setAction(e.target.value)}
            />
            <Input
              aria-label="Filter by target type"
              placeholder="target type"
              className="w-36"
              value={targetType}
              onChange={(e) => setTargetType(e.target.value)}
            />
            {(action || targetType) && (
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Clear server filters"
                onClick={() => {
                  setAction("");
                  setTargetType("");
                }}
              >
                <X />
              </Button>
            )}
          </div>
        }
        emptyState={
          <EmptyState
            icon={<ScrollText />}
            title={unavailable ? "Audit service unavailable" : filtered ? "No events match these filters" : "No audit events yet"}
            description={
              unavailable
                ? "Events appear here once the audit service responds."
                : filtered
                  ? "Filters match the action and target type exactly."
                  : "Every create, update, delete and policy change is recorded here."
            }
          />
        }
        footer={
          q.hasNextPage ? (
            <div className="flex justify-center">
              <Button variant="secondary" size="sm" loading={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
                Load older events
              </Button>
            </div>
          ) : events.length > 0 ? (
            <p className="text-center text-xs text-fg-subtle">Beginning of the audit trail.</p>
          ) : undefined
        }
      />
      <Dialog open={!!selected} onOpenChange={(o) => !o && setSelected(null)}>
        <DialogContent size="lg">
          {selected && (
            <>
              <DialogHeader
                icon={<ScrollText />}
                title={<span className="font-mono">{selected.action}</span>}
                description={formatDateTime(selected.occurred_at)}
              />
              <DialogBody className="space-y-4">
                <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
                  <KV label="Event ID" value={selected.id} mono />
                  <KV label="Request ID" value={selected.request_id} mono />
                  <KV label="Actor" value={`${selected.actor_type}${selected.actor_id ? ` · ${selected.actor_id}` : ""}`} mono />
                  <KV label="Target" value={selected.target_type ? `${selected.target_type} · ${selected.target_id ?? "—"}` : null} mono />
                  <KV label="IP" value={selected.ip} mono />
                </dl>
                {selected.changes && Object.keys(selected.changes).length > 0 && <JsonBlock title="Changes" value={selected.changes} />}
                <JsonBlock title="Metadata" value={selected.metadata ?? {}} />
              </DialogBody>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function KV({ label, value, mono }: { label: string; value: string | null | undefined; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-fg-subtle">{label}</dt>
      <dd className={mono ? "truncate font-mono text-xs text-fg" : "truncate text-fg"} title={value ?? undefined}>
        {value || "—"}
      </dd>
    </div>
  );
}

function JsonBlock({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="space-y-1.5">
      <div className="text-xs font-medium text-fg-muted">{title}</div>
      <pre className="scrollbar-thin max-h-72 overflow-auto rounded-control border border-border bg-surface-2/70 p-3 font-mono text-xs leading-5 text-fg">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}
