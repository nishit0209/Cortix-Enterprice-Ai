"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Check, Database, GitMerge, Layers, RefreshCw, Search, Settings2, Split, UserRound, Users, Workflow } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { JobProgress } from "@/components/jobs/job-progress";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
import { Pager } from "@/components/ui/pager";
import { StatCard } from "@/components/ui/stat-card";
import { FeatureStatusBadge, StatusBadge } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorMessage } from "@/lib/errors";
import { useCustomerMatches, useCustomers, useCustomerSegments, useCustomerSources, useDecideCustomerMatch, useResolutionRuns, useStartResolution, type CustomerProfile, type IdentityMatch } from "@/lib/queries/customers";
import { formatNumber, formatRelative } from "@/lib/utils";
import { CompareRecords, FeatureBars, ModuleError, NativeSelect, RunList, ScoreBar } from "@/components/mdm/common";
import { CustomerSettingsPanel, ScoringPanel, SegmentsPanel, SourcesPanel } from "./customer-panels";

const PRIORITY = ["name", "first_name", "last_name", "email", "phone", "address", "dob", "external_id", "created_at"];

export function CustomersView() {
  const { slug, can } = useOrgContext();
  const canManage = can("customers.manage");
  const canReview = can("customers.review");
  const sources = useCustomerSources(slug);
  const segments = useCustomerSegments(slug, canManage);
  const pending = useCustomerMatches(slug, "pending_review", canReview);
  const runs = useResolutionRuns(slug);
  const start = useStartResolution(slug);
  const [jobId, setJobId] = React.useState<string | null>(null);
  const [tab, setTab] = React.useState("customers");
  const lastRun = runs.items?.[0];

  const resolve = async () => {
    try {
      const res = await start.mutateAsync();
      setJobId(res.job_id);
      toast.success("Identity resolution started");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const pendingCount = pending.items ? `${formatNumber(pending.items.length)}${pending.hasNext ? "+" : ""}` : "—";

  return (
    <div className="space-y-6">
      <PageHeader
        title="Customer 360"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Map customer datasets, resolve identities into golden profiles with human review, build segments and score churn and lifetime value with your ML models."
        actions={
          canManage && (
            <Button onClick={resolve} loading={start.isPending} disabled={!!jobId}>
              <GitMerge /> Run identity resolution
            </Button>
          )
        }
      />

      {jobId && (
        <Card className="p-4">
          <JobProgress
            org={slug}
            jobId={jobId}
            title="Identity resolution"
            onDone={(s) => {
              if (s.status === "succeeded") toast.success("Profiles are up to date");
              void runs.refetch();
              setTimeout(() => setJobId(null), 4000);
            }}
          />
        </Card>
      )}

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Customer 360 summary">
        <StatCard label="Sources" icon={<Database />} loading={sources.isLoading} value={sources.data ? formatNumber(sources.data.length) : "—"} hint={sources.data ? `${formatNumber(sources.data.reduce((a, s) => a + (s.record_count ?? 0), 0))} records` : undefined} />
        <StatCard label="Pending reviews" icon={<GitMerge />} accent="warning" loading={canReview && pending.isLoading} value={canReview ? pendingCount : "—"} hint={canReview ? undefined : "Needs customers.review"} />
        <StatCard label="Segments" icon={<Layers />} accent="teal" loading={canManage && segments.isLoading} value={canManage && segments.data ? formatNumber(segments.data.length) : "—"} />
        <StatCard
          label="Last resolution"
          icon={<Workflow />}
          accent="success"
          loading={runs.isLoading}
          value={lastRun ? <StatusBadge status={lastRun.status} /> : "Never"}
          hint={lastRun ? formatRelative(lastRun.finished_at ?? lastRun.created_at) : undefined}
        />
      </section>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList aria-label="Customer 360 sections" className="flex-wrap">
          <TabsTrigger value="customers">
            <Users /> Customers
          </TabsTrigger>
          {canReview && (
            <TabsTrigger value="review">
              <GitMerge /> Review queue
            </TabsTrigger>
          )}
          {canManage && (
            <TabsTrigger value="segments">
              <Layers /> Segments
            </TabsTrigger>
          )}
          <TabsTrigger value="sources">
            <Database /> Sources
          </TabsTrigger>
          <TabsTrigger value="runs">
            <RefreshCw /> Runs & scoring
          </TabsTrigger>
          <TabsTrigger value="settings">
            <Settings2 /> Matching settings
          </TabsTrigger>
        </TabsList>
        <TabsContent value="customers">
          <CustomerList />
        </TabsContent>
        {canReview && (
          <TabsContent value="review">
            <ReviewQueue />
          </TabsContent>
        )}
        {canManage && (
          <TabsContent value="segments">
            <SegmentsPanel />
          </TabsContent>
        )}
        <TabsContent value="sources">
          <SourcesPanel />
        </TabsContent>
        <TabsContent value="runs">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader title="Resolution runs" description="Each run syncs the sources, matches identities and rebuilds golden profiles. Your earlier review decisions are kept." />
              <CardContent>
                {runs.isError ? (
                  <ModuleError compact error={runs.error} what="Resolution runs" onRetry={() => void runs.refetch()} />
                ) : (
                  <>
                    <RunList
                      runs={runs.items}
                      loading={runs.isLoading}
                      empty={<EmptyState compact icon={<GitMerge />} title="No runs yet" description="Map a profile source, then run identity resolution." />}
                    />
                    <Pager page={runs.page} hasPrev={runs.hasPrev} hasNext={runs.hasNext} onPrev={runs.goPrev} onNext={runs.goNext} loading={runs.isFetching} />
                  </>
                )}
              </CardContent>
            </Card>
            <ScoringPanel />
          </div>
        </TabsContent>
        <TabsContent value="settings">
          <CustomerSettingsPanel />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function CustomerList() {
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const [search, setSearch] = React.useState("");
  const [term, setTerm] = React.useState("");
  const [segment, setSegment] = React.useState("");
  const segments = useCustomerSegments(slug, can("customers.manage"));
  React.useEffect(() => {
    const t = setTimeout(() => setTerm(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);
  const list = useCustomers(slug, { search: term || undefined, segment_id: segment || undefined });

  const columns = React.useMemo<DataTableColumn<CustomerProfile>[]>(
    () => [
      {
        id: "name",
        accessorFn: (p) => p.display_name,
        header: "Customer",
        cell: ({ row }) => (
          <Link href={`/o/${slug}/customers/${row.original.id}`} className="font-medium hover:underline" onClick={(e) => e.stopPropagation()}>
            {row.original.display_name || "Unnamed customer"}
          </Link>
        ),
      },
      { id: "email", accessorFn: (p) => p.email ?? "", header: "Email", cell: ({ row }) => row.original.email ?? "—" },
      { id: "phone", accessorFn: (p) => p.phone ?? "", header: "Phone", cell: ({ row }) => row.original.phone ?? "—" },
      {
        id: "sources",
        accessorFn: (p) => p.sources.join(","),
        header: "Sources",
        cell: ({ row }) => (
          <span className="flex flex-wrap gap-1">
            {row.original.sources.map((s) => (
              <Badge key={s} tone="outline">
                {s}
              </Badge>
            ))}
          </span>
        ),
      },
      { id: "orders", accessorFn: (p) => p.order_count, header: "Orders", meta: { align: "right" }, cell: ({ row }) => <span className="tabular">{formatNumber(row.original.order_count)}</span> },
      { id: "monetary", accessorFn: (p) => p.monetary, header: "Spend", meta: { align: "right" }, cell: ({ row }) => <span className="tabular">{formatNumber(row.original.monetary)}</span> },
      { id: "last", accessorFn: (p) => p.last_seen ?? "", header: "Last seen", cell: ({ row }) => formatRelative(row.original.last_seen) },
    ],
    [slug],
  );

  if (list.isError) return <Card><ModuleError error={list.error} what="Customer profiles" onRetry={() => void list.refetch()} /></Card>;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full max-w-xs">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, email or phone" className="pl-8" aria-label="Search customers" />
        </div>
        {(segments.data ?? []).length > 0 && (
          <NativeSelect value={segment} onChange={(e) => setSegment(e.target.value)} aria-label="Filter by segment" className="w-auto">
            <option value="">All segments</option>
            {segments.data!.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({formatNumber(s.size)})
              </option>
            ))}
          </NativeSelect>
        )}
      </div>
      <DataTable
        caption="Customer profiles"
        columns={columns}
        data={list.items}
        loading={list.isLoading}
        getRowId={(p) => p.id}
        pageSize={50}
        columnToggle={false}
        onRowClick={(p) => router.push(`/o/${slug}/customers/${p.id}`)}
        emptyState={
          <EmptyState
            compact
            icon={<UserRound />}
            title={term || segment ? "No matching customers" : "No customer profiles yet"}
            description={term || segment ? "Try another search or segment." : "Map a dataset under Sources, then run identity resolution to build golden profiles."}
          />
        }
      />
      <Pager page={list.page} hasPrev={list.hasPrev} hasNext={list.hasNext} onPrev={list.goPrev} onNext={list.goNext} loading={list.isFetching} />
    </div>
  );
}

export function ReviewQueue() {
  const { slug } = useOrgContext();
  const [status, setStatus] = React.useState("pending_review");
  const list = useCustomerMatches(slug, status);
  if (list.isError) return <Card><ModuleError error={list.error} what="The review queue" onRetry={() => void list.refetch()} /></Card>;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-fg-muted">Pairs scored between the review and auto-merge thresholds. Confirm merges the two profiles; reject keeps them apart for good.</p>
        <NativeSelect value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Match status" className="w-auto">
          <option value="pending_review">Pending review</option>
          <option value="confirmed">Confirmed</option>
          <option value="rejected">Rejected</option>
          <option value="auto_merged">Auto-merged</option>
        </NativeSelect>
      </div>
      {list.isLoading ? (
        <Skeleton className="h-64 rounded-card" />
      ) : !list.items?.length ? (
        <Card>
          <EmptyState icon={<Check />} title={status === "pending_review" ? "Nothing to review" : "No matches with this status"} description="New pairs appear here after an identity resolution run." />
        </Card>
      ) : (
        list.items.map((m) => <MatchCard key={m.id} match={m} />)
      )}
      <Pager page={list.page} hasPrev={list.hasPrev} hasNext={list.hasNext} onPrev={list.goPrev} onNext={list.goNext} loading={list.isFetching} />
    </div>
  );
}

function MatchCard({ match }: { match: IdentityMatch }) {
  const { slug } = useOrgContext();
  const decide = useDecideCustomerMatch(slug);
  const [note, setNote] = React.useState("");
  const [confirmReject, setConfirmReject] = React.useState(false);
  const pending = match.status === "pending_review";
  const run = async (decision: "confirm" | "reject") => {
    try {
      await decide.mutateAsync({ id: match.id, decision, note });
      toast.success(decision === "confirm" ? "Merged into one customer" : "Kept as separate customers");
      setConfirmReject(false);
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };
  const label = (r: IdentityMatch["record_a"]) => (
    <span className="flex flex-wrap items-center gap-1.5">
      <Badge tone="outline">{r.source_system ?? "source"}</Badge>
      <span className="font-mono text-[11px]">{r.source_key}</span>
      {r.profile_id && (
        <Link className="text-[11px] text-accent hover:underline" href={`/o/${slug}/customers/${r.profile_id}`}>
          profile
        </Link>
      )}
    </span>
  );
  return (
    <Card className="space-y-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <ScoreBar value={match.score} label="Match score" />
          <StatusBadge status={match.status} />
          <span className="text-xs text-fg-subtle">{formatRelative(match.created_at)}</span>
        </div>
        {pending && (
          <div className="flex flex-wrap items-center gap-2">
            <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note (optional)" className="h-8 w-48" aria-label="Decision note" maxLength={500} />
            <Button size="sm" variant="secondary" onClick={() => setConfirmReject(true)} disabled={decide.isPending}>
              <Split /> Not the same
            </Button>
            <Button size="sm" onClick={() => void run("confirm")} loading={decide.isPending}>
              <GitMerge /> Same customer
            </Button>
          </div>
        )}
        {!pending && match.note && <p className="text-xs text-fg-muted">Note: {match.note}</p>}
      </div>
      <CompareRecords a={match.record_a.attributes} b={match.record_b.attributes} labelA={label(match.record_a)} labelB={label(match.record_b)} priority={PRIORITY} />
      <FeatureBars features={match.features} />
      <ConfirmDialog
        open={confirmReject}
        onOpenChange={setConfirmReject}
        title="Keep these records apart?"
        description="They will never be merged automatically again. If they share a profile today, it is split."
        confirmLabel="Keep separate"
        loading={decide.isPending}
        onConfirm={() => run("reject")}
      />
    </Card>
  );
}

export function SourceKindBadge({ kind }: { kind: string }) {
  return <Badge tone={kind === "profile" ? "accent" : "teal"}>{kind === "profile" ? "Profiles" : "Events"}</Badge>;
}
