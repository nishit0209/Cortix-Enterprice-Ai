"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Boxes, Check, Database, GitMerge, Info, Plus, RefreshCw, Search, Sparkles, Split, Trash2, Wand2, X } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { DatasetPicker } from "@/components/ask/dataset-picker";
import { JobProgress } from "@/components/jobs/job-progress";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
import { Pager } from "@/components/ui/pager";
import { StatCard } from "@/components/ui/stat-card";
import { FeatureStatusBadge, StatusBadge } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorMessage } from "@/lib/errors";
import { displayValue } from "@/lib/mdm";
import { useDatasets } from "@/lib/queries/datasets";
import {
  PRODUCT_FIELDS,
  searchModeMessage,
  useCreateProductSource,
  useDecideCorrection,
  useDecideProductMatch,
  useDeleteProductSource,
  useProductCorrections,
  useProductMatches,
  useProductRuns,
  useProducts,
  useProductSearch,
  useProductSources,
  useStartProductRun,
  type ProductCorrection,
  type ProductMaster,
  type ProductMatch,
  type ProductSource,
} from "@/lib/queries/products";
import { formatNumber, formatPercent, formatRelative, titleCase } from "@/lib/utils";
import { ColumnMapping, CompareRecords, excludedByPolicy, FeatureBars, ModuleError, NativeSelect, RunList, ScoreBar } from "@/components/mdm/common";

const PRIORITY = ["sku", "name", "brand", "category", "pack_size", "unit", "price", "external_id"];

function useDebounced(value: string, ms = 300) {
  const [v, setV] = React.useState(value);
  React.useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function ProductsView() {
  const { slug, can } = useOrgContext();
  const canManage = can("products.manage");
  const canReview = can("products.review");
  const sources = useProductSources(slug);
  const pending = useProductMatches(slug, "pending_review", canReview);
  const proposed = useProductCorrections(slug, "proposed", canReview);
  const runs = useProductRuns(slug);
  const start = useStartProductRun(slug);
  const [job, setJob] = React.useState<{ id: string; kind: string } | null>(null);
  const [tab, setTab] = React.useState("products");
  const count = (items: unknown[] | undefined, more: boolean) => (items ? `${formatNumber(items.length)}${more ? "+" : ""}` : "—");

  const run = async (kind: "normalize" | "enrich") => {
    try {
      const res = await start.mutateAsync(kind);
      setJob({ id: res.job_id, kind });
      toast.success(kind === "normalize" ? "Normalization started" : "Enrichment started");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Product 360"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Normalize product feeds into stable masters: SKU, brand and unit normalization, duplicate and variant matching with review, quality scores, approved corrections and semantic search."
        actions={
          canManage && (
            <div className="flex gap-2">
              <Button variant="secondary" onClick={() => run("enrich")} disabled={!!job || start.isPending}>
                <Wand2 /> Suggest corrections
              </Button>
              <Button onClick={() => run("normalize")} loading={start.isPending} disabled={!!job}>
                <RefreshCw /> Normalize & match
              </Button>
            </div>
          )
        }
      />
      {job && (
        <Card className="p-4">
          <JobProgress
            org={slug}
            jobId={job.id}
            title={job.kind === "normalize" ? "Normalizing products" : "Suggesting corrections"}
            onDone={() => {
              void runs.refetch();
              setTimeout(() => setJob(null), 4000);
            }}
          />
        </Card>
      )}
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Product 360 summary">
        <StatCard label="Sources" icon={<Database />} loading={sources.isLoading} value={sources.data ? formatNumber(sources.data.length) : "—"} hint={sources.data ? `${formatNumber(sources.data.reduce((a, s) => a + s.record_count, 0))} records` : undefined} />
        <StatCard label="Matches to review" icon={<GitMerge />} accent="warning" loading={canReview && pending.isLoading} value={canReview ? count(pending.items, pending.hasNext) : "—"} />
        <StatCard label="Proposed corrections" icon={<Wand2 />} accent="teal" loading={canReview && proposed.isLoading} value={canReview ? count(proposed.items, proposed.hasNext) : "—"} />
        <StatCard
          label="Last run"
          icon={<RefreshCw />}
          accent="success"
          loading={runs.isLoading}
          value={runs.items?.[0] ? <StatusBadge status={runs.items[0].status} /> : "Never"}
          hint={runs.items?.[0] ? `${titleCase(runs.items[0].kind)} · ${formatRelative(runs.items[0].finished_at ?? runs.items[0].created_at)}` : undefined}
        />
      </section>
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList aria-label="Product 360 sections" className="flex-wrap">
          <TabsTrigger value="products">
            <Boxes /> Products
          </TabsTrigger>
          <TabsTrigger value="search">
            <Sparkles /> Search
          </TabsTrigger>
          {canReview && (
            <TabsTrigger value="matches">
              <GitMerge /> Match review
            </TabsTrigger>
          )}
          {canReview && (
            <TabsTrigger value="corrections">
              <Wand2 /> Corrections
            </TabsTrigger>
          )}
          <TabsTrigger value="sources">
            <Database /> Sources
          </TabsTrigger>
          <TabsTrigger value="runs">
            <RefreshCw /> Runs
          </TabsTrigger>
        </TabsList>
        <TabsContent value="products">
          <MasterList />
        </TabsContent>
        <TabsContent value="search">
          <SemanticSearch />
        </TabsContent>
        {canReview && (
          <TabsContent value="matches">
            <MatchReview />
          </TabsContent>
        )}
        {canReview && (
          <TabsContent value="corrections">
            <Corrections />
          </TabsContent>
        )}
        <TabsContent value="sources">
          <Sources />
        </TabsContent>
        <TabsContent value="runs">
          <Card>
            <CardHeader title="Runs" description="Normalization rebuilds masters (ids stay stable, review decisions and approved corrections are kept). Enrichment proposes corrections from local rules." />
            <CardContent>
              {runs.isError ? (
                <ModuleError compact error={runs.error} what="Product runs" onRetry={() => void runs.refetch()} />
              ) : (
                <>
                  <RunList runs={runs.items} loading={runs.isLoading} empty={<EmptyState compact icon={<RefreshCw />} title="No runs yet" description="Map a source, then normalize." />} />
                  <Pager page={runs.page} hasPrev={runs.hasPrev} hasNext={runs.hasNext} onPrev={runs.goPrev} onNext={runs.goNext} loading={runs.isFetching} />
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

export function QualityCell({ value }: { value: number | null }) {
  // apps/products scores quality on 0-100.
  return <ScoreBar value={value === null ? null : value / 100} label="Quality score" />;
}

function MasterList() {
  const { slug } = useOrgContext();
  const router = useRouter();
  const [search, setSearch] = React.useState("");
  const [brand, setBrand] = React.useState("");
  const [category, setCategory] = React.useState("");
  const term = useDebounced(search.trim());
  const b = useDebounced(brand.trim());
  const c = useDebounced(category.trim());
  const list = useProducts(slug, { search: term || undefined, brand: b || undefined, category: c || undefined });
  const columns = React.useMemo<DataTableColumn<ProductMaster>[]>(
    () => [
      {
        id: "name",
        accessorFn: (p) => p.name,
        header: "Product",
        cell: ({ row }) => (
          <Link href={`/o/${slug}/products/${row.original.id}`} className="font-medium hover:underline" onClick={(e) => e.stopPropagation()}>
            {row.original.name}
          </Link>
        ),
      },
      { id: "sku", accessorFn: (p) => p.sku ?? "", header: "SKU", cell: ({ row }) => <span className="font-mono text-xs">{row.original.sku ?? "—"}</span> },
      { id: "brand", accessorFn: (p) => p.brand ?? "", header: "Brand", cell: ({ row }) => row.original.brand ?? "—" },
      { id: "category", accessorFn: (p) => p.category ?? "", header: "Category", cell: ({ row }) => row.original.category ?? "—" },
      { id: "size", accessorFn: (p) => p.size ?? "", header: "Size", cell: ({ row }) => row.original.size ?? "—" },
      { id: "price", accessorFn: (p) => p.price ?? 0, header: "Price", meta: { align: "right" }, cell: ({ row }) => <span className="tabular">{row.original.price === null ? "—" : formatNumber(row.original.price)}</span> },
      { id: "records", accessorFn: (p) => p.record_count, header: "Records", meta: { align: "right" }, cell: ({ row }) => <span className="tabular">{formatNumber(row.original.record_count)}</span> },
      { id: "quality", accessorFn: (p) => p.quality ?? 0, header: "Quality", cell: ({ row }) => <QualityCell value={row.original.quality} /> },
    ],
    [slug],
  );
  if (list.isError) return <Card><ModuleError error={list.error} what="Product masters" onRetry={() => void list.refetch()} /></Card>;
  const filtered = term || b || c;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full max-w-xs">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Name, SKU or brand" className="pl-8" aria-label="Search products" />
        </div>
        <Input value={brand} onChange={(e) => setBrand(e.target.value)} placeholder="Brand" className="w-36" aria-label="Brand filter" />
        <Input value={category} onChange={(e) => setCategory(e.target.value)} placeholder="Category" className="w-36" aria-label="Category filter" />
      </div>
      <DataTable
        caption="Product masters"
        columns={columns}
        data={list.items}
        loading={list.isLoading}
        getRowId={(p) => p.id}
        pageSize={50}
        columnToggle={false}
        onRowClick={(p) => router.push(`/o/${slug}/products/${p.id}`)}
        emptyState={
          <EmptyState
            compact
            icon={<Boxes />}
            title={filtered ? "No matching products" : "No product masters yet"}
            description={filtered ? "Try other filters." : "Map a supplier or catalog dataset under Sources, then run Normalize & match."}
          />
        }
      />
      <Pager page={list.page} hasPrev={list.hasPrev} hasNext={list.hasNext} onPrev={list.goPrev} onNext={list.goNext} loading={list.isFetching} />
    </div>
  );
}

function SemanticSearch() {
  const { slug } = useOrgContext();
  const [input, setInput] = React.useState("");
  const [q, setQ] = React.useState("");
  const res = useProductSearch(slug, q);
  const notice = res.data ? searchModeMessage(res.data) : null;
  return (
    <Card>
      <CardHeader title="Search products" description="Describe what you're looking for; semantic search runs on local embeddings, nothing leaves this server." />
      <CardContent className="space-y-3">
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setQ(input.trim());
          }}
        >
          <Input value={input} onChange={(e) => setInput(e.target.value)} placeholder="e.g. 1 litre toned milk pouch" maxLength={500} aria-label="Search query" />
          <Button type="submit" disabled={!input.trim()} loading={res.isFetching}>
            <Search /> Search
          </Button>
        </form>
        {res.data && (
          <div className="flex items-center gap-2 text-xs">
            <Badge tone={res.data.search_mode === "semantic" ? "success" : "warning"}>{res.data.search_mode === "semantic" ? "Semantic" : "Keyword"}</Badge>
            {notice && (
              <span role="status" className="flex items-center gap-1 text-fg-muted">
                <Info className="size-3.5" /> {notice}
              </span>
            )}
          </div>
        )}
        {res.isError ? (
          <ModuleError compact error={res.error} what="Product search" />
        ) : res.data && !res.data.items.length ? (
          <EmptyState compact icon={<Search />} title="No products found" />
        ) : (
          <ul className="divide-y divide-border">
            {(res.data?.items ?? []).map((p) => (
              <li key={p.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <div className="min-w-0">
                  <Link href={`/o/${slug}/products/${p.id}`} className="font-medium hover:underline">
                    {p.name}
                  </Link>
                  <p className="text-xs text-fg-muted">{[p.brand, p.category, p.size, p.sku].filter(Boolean).join(" · ")}</p>
                </div>
                {typeof p.score === "number" && <span className="tabular text-xs text-fg-muted">relevance {formatPercent(p.score, 0)}</span>}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function MatchReview() {
  const { slug } = useOrgContext();
  const [status, setStatus] = React.useState("pending_review");
  const list = useProductMatches(slug, status);
  if (list.isError) return <Card><ModuleError error={list.error} what="Match review" onRetry={() => void list.refetch()} /></Card>;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-fg-muted">Possible duplicates. Same product merges the two masters; different keeps them apart, even if they're variants of one another.</p>
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
          <EmptyState icon={<Check />} title={status === "pending_review" ? "Nothing to review" : "No matches with this status"} />
        </Card>
      ) : (
        list.items.map((m) => <ProductMatchCard key={m.id} match={m} />)
      )}
      <Pager page={list.page} hasPrev={list.hasPrev} hasNext={list.hasNext} onPrev={list.goPrev} onNext={list.goNext} loading={list.isFetching} />
    </div>
  );
}

function ProductMatchCard({ match }: { match: ProductMatch }) {
  const { slug } = useOrgContext();
  const decide = useDecideProductMatch(slug);
  const [note, setNote] = React.useState("");
  const pending = match.status === "pending_review";
  const run = async (decision: "confirm" | "reject") => {
    try {
      await decide.mutateAsync({ id: match.id, decision, note });
      toast.success(decision === "confirm" ? "Merged into one product" : "Kept as separate products");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };
  const label = (r: ProductMatch["record_a"]) => (
    <span className="flex flex-wrap items-center gap-1.5">
      <Badge tone="outline">{r.source_system ?? "source"}</Badge>
      <span className="font-mono text-[11px]">{r.source_key}</span>
      {r.master_id && (
        <Link className="text-[11px] text-accent hover:underline" href={`/o/${slug}/products/${r.master_id}`}>
          master
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
        </div>
        {pending && (
          <div className="flex flex-wrap items-center gap-2">
            <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note (optional)" className="h-8 w-48" aria-label="Decision note" maxLength={500} />
            <Button size="sm" variant="secondary" onClick={() => void run("reject")} disabled={decide.isPending}>
              <Split /> Different products
            </Button>
            <Button size="sm" onClick={() => void run("confirm")} loading={decide.isPending}>
              <GitMerge /> Same product
            </Button>
          </div>
        )}
      </div>
      <CompareRecords
        a={{ ...(match.record_a.attributes ?? {}), ...prefixed(match.record_a.normalized) }}
        b={{ ...(match.record_b.attributes ?? {}), ...prefixed(match.record_b.normalized) }}
        labelA={label(match.record_a)}
        labelB={label(match.record_b)}
        priority={PRIORITY}
      />
      <FeatureBars features={match.features} />
    </Card>
  );
}

function prefixed(n: Record<string, unknown> | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(n ?? {})) if (v !== null && typeof v !== "object") out[`normalized ${k}`] = v;
  return out;
}

function Corrections() {
  const { slug } = useOrgContext();
  const [status, setStatus] = React.useState("proposed");
  const list = useProductCorrections(slug, status);
  const decide = useDecideCorrection(slug);
  const act = async (c: ProductCorrection, decision: "approve" | "reject") => {
    try {
      await decide.mutateAsync({ id: c.id, decision });
      toast.success(decision === "approve" ? "Applied to the product" : "Correction rejected");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };
  if (list.isError) return <Card><ModuleError error={list.error} what="Corrections" onRetry={() => void list.refetch()} /></Card>;
  return (
    <Card>
      <CardHeader
        title="Corrections"
        description="Proposed by people or by enrichment rules. Nothing changes until a reviewer approves; approved values survive re-runs."
        actions={
          <NativeSelect value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Correction status" className="w-auto">
            <option value="proposed">Proposed</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
          </NativeSelect>
        }
      />
      <CardContent>
        {list.isLoading ? (
          <Skeleton className="h-40" />
        ) : !list.items?.length ? (
          <EmptyState compact icon={<Wand2 />} title="No corrections" description={status === "proposed" ? "Run Suggest corrections or propose one from a product page." : undefined} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-fg-muted">
                <tr>
                  <th className="py-2 pr-3 text-left font-medium">Product</th>
                  <th className="py-2 pr-3 text-left font-medium">Field</th>
                  <th className="py-2 pr-3 text-left font-medium">Change</th>
                  <th className="py-2 pr-3 text-left font-medium">Why</th>
                  <th className="py-2 text-right font-medium" />
                </tr>
              </thead>
              <tbody>
                {list.items.map((c) => (
                  <tr key={c.id} className="border-t border-border align-top">
                    <td className="py-2 pr-3">
                      <Link href={`/o/${slug}/products/${c.master_id}`} className="text-accent hover:underline">
                        Open
                      </Link>
                    </td>
                    <td className="py-2 pr-3 font-mono text-xs">{c.field}</td>
                    <td className="py-2 pr-3">
                      <span className="text-fg-muted line-through">{displayValue(c.old_value) || "—"}</span> → <span className="font-medium">{displayValue(c.new_value)}</span>
                    </td>
                    <td className="py-2 pr-3 text-xs text-fg-muted">
                      {c.reason || "—"}
                      <span className="block text-fg-subtle">
                        {titleCase(c.origin)}
                        {c.confidence !== null ? ` · ${formatPercent(c.confidence, 0)} confidence` : ""} · {formatRelative(c.created_at)}
                      </span>
                    </td>
                    <td className="py-2 text-right whitespace-nowrap">
                      {c.status === "proposed" ? (
                        <span className="inline-flex gap-1">
                          <Button size="xs" variant="secondary" onClick={() => void act(c, "reject")} disabled={decide.isPending}>
                            <X /> Reject
                          </Button>
                          <Button size="xs" onClick={() => void act(c, "approve")} disabled={decide.isPending}>
                            <Check /> Approve
                          </Button>
                        </span>
                      ) : (
                        <StatusBadge status={c.status} />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager page={list.page} hasPrev={list.hasPrev} hasNext={list.hasNext} onPrev={list.goPrev} onNext={list.goNext} loading={list.isFetching} />
      </CardContent>
    </Card>
  );
}

function Sources() {
  const { slug, can } = useOrgContext();
  const canManage = can("products.manage");
  const sources = useProductSources(slug);
  const datasets = useDatasets(slug);
  const del = useDeleteProductSource(slug);
  const [open, setOpen] = React.useState(false);
  const [pending, setPending] = React.useState<ProductSource | null>(null);
  if (sources.isError) return <Card><ModuleError error={sources.error} what="Product sources" onRetry={() => void sources.refetch()} /></Card>;
  return (
    <Card>
      <CardHeader
        title="Sources"
        description="Supplier feeds and catalogs mapped to product fields. Lower priority numbers are trusted more when values disagree."
        actions={
          canManage && (
            <Button size="sm" onClick={() => setOpen(true)}>
              <Plus /> Map a dataset
            </Button>
          )
        }
      />
      <CardContent>
        {sources.isLoading ? (
          <Skeleton className="h-32" />
        ) : !(sources.data ?? []).length ? (
          <EmptyState compact icon={<Database />} title="No sources mapped" />
        ) : (
          <ul className="divide-y divide-border">
            {sources.data!.map((s) => (
              <li key={s.id} className="flex items-start justify-between gap-3 py-3">
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{s.name}</span>
                    <StatusBadge status={s.status} />
                    <span className="text-xs text-fg-subtle">priority {s.priority}</span>
                  </div>
                  <p className="text-xs text-fg-muted">
                    <Link href={`/o/${slug}/data/datasets/${s.dataset_id}`} className="hover:underline">
                      {(datasets.data ?? []).find((d) => d.id === s.dataset_id)?.name ?? "Dataset"}
                    </Link>{" "}
                    · {formatNumber(s.record_count)} records · {s.last_synced_at ? `synced ${formatRelative(s.last_synced_at)}` : "not synced yet"}
                  </p>
                  <p className="text-xs text-fg-subtle">
                    {Object.entries(s.mapping)
                      .map(([k, v]) => `${k} ← ${Array.isArray(v) ? v.join(", ") : v}`)
                      .join(" · ")}
                  </p>
                  {s.last_error && <p className="text-xs text-danger">{s.last_error}</p>}
                </div>
                {canManage && (
                  <Button variant="ghost" size="icon-sm" aria-label={`Remove ${s.name}`} onClick={() => setPending(s)}>
                    <Trash2 />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
      <AddProductSource open={open} onOpenChange={setOpen} />
      <ConfirmDialog
        open={!!pending}
        onOpenChange={(o) => !o && setPending(null)}
        title={`Remove "${pending?.name ?? ""}"?`}
        description="Its records leave the masters on the next normalization run."
        confirmLabel="Remove source"
        loading={del.isPending}
        onConfirm={async () => {
          if (!pending) return;
          try {
            await del.mutateAsync(pending.id);
            setPending(null);
            toast.success("Source removed");
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </Card>
  );
}

function AddProductSource({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { slug } = useOrgContext();
  const datasets = useDatasets(slug);
  const create = useCreateProductSource(slug);
  const [datasetId, setDatasetId] = React.useState<string | null>(null);
  const [mapping, setMapping] = React.useState<Record<string, string | string[]>>({});
  const [name, setName] = React.useState("");
  const [priority, setPriority] = React.useState("100");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (!open) {
      setDatasetId(null);
      setMapping({});
      setName("");
      setError(null);
    }
  }, [open]);
  const submit = async () => {
    setError(null);
    if (!datasetId) return setError("Choose a dataset.");
    if (!mapping.name && !mapping.sku) return setError("Map at least the product name or SKU.");
    try {
      await create.mutateAsync({ dataset_id: datasetId, mapping, name: name.trim() || undefined, priority: Number(priority) || 100 });
      toast.success("Source mapped. Run Normalize & match to load it.");
      onOpenChange(false);
    } catch (e) {
      const blocked = excludedByPolicy(e);
      setError(blocked.length ? `Masked or denied for you by the data policy: ${blocked.join(", ")}` : errorMessage(e));
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader title="Map a dataset to product fields" />
        <DialogBody className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <span className="mb-1 block text-xs font-medium">Dataset</span>
              <DatasetPicker datasets={datasets.data ?? []} value={datasetId} onChange={(id) => (setDatasetId(id), setMapping({}))} />
            </div>
            <Field id="ps-name" label="Name">
              <Input id="ps-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
            </Field>
            <Field id="ps-priority" label="Priority">
              <Input id="ps-priority" type="number" min={0} max={10000} value={priority} onChange={(e) => setPriority(e.target.value)} />
            </Field>
          </div>
          <ColumnMapping
            org={slug}
            datasetId={datasetId}
            fields={[...PRODUCT_FIELDS.map((k) => ({ key: k, label: titleCase(k.replace(/_/g, " ")) })), { key: "attributes", label: "Extra attributes", multi: true }]}
            value={mapping}
            onChange={setMapping}
          />
          {error && (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending}>
            Save mapping
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
