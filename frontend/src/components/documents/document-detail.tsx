"use client";

import * as React from "react";
import Link from "next/link";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, BookOpen, ChevronLeft, ChevronRight, Download, FileText, Layers, Loader2, ScanSearch, ScanText, Search, Table2, Tags, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { ClassificationBadge, StatusBadge } from "@/components/ui/status";
import { EmbeddingPendingBadge } from "./embedding-badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ResultTable } from "@/components/ask/result-table";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { downloadSignedUrl, isExpiredDownload } from "@/lib/download";
import {
  isDocProcessing,
  useDeleteDocument,
  useDocument,
  useDocumentChunks,
  useDocumentEntities,
  useDocumentPages,
  useDocumentTables,
} from "@/lib/queries/documents";
import type { DocumentEntity, DocumentTable } from "@/lib/types";
import { cn, formatBytes, formatDateTime, formatNumber, titleCase } from "@/lib/utils";
import { DocumentAskPanel } from "./ask-panel";
import { SearchPanel } from "./documents-view";
import { CollectionPicker, ExtractPanel, PromoteTableButton } from "./document-extras";

function Unavailable({ what, error, onRetry }: { what: string; error: unknown; onRetry: () => void }) {
  return isUnavailableEndpoint(error) ? (
    <EmptyState compact icon={<FileText />} title={`${what} aren't available`} description="Nothing was extracted, or the documents service doesn't expose this yet." />
  ) : (
    <ErrorState compact error={error} onRetry={onRetry} />
  );
}

export function DocumentDetail() {
  const { id } = useParams<{ id: string }>();
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const q = useDocument(slug, id);
  const [downloading, setDownloading] = React.useState(false);
  const ready = q.data?.status === "ready";
  const [tab, setTab] = React.useState("pages");
  const page = Math.max(1, Number(params.get("page") ?? 1) || 1);
  const pages = useDocumentPages(slug, id, ready);
  const chunks = useDocumentChunks(slug, id, ready && tab === "chunks");
  const entities = useDocumentEntities(slug, id, ready && tab === "entities");
  const tables = useDocumentTables(slug, id, ready && tab === "tables");
  const [deleting, setDeleting] = React.useState(false);
  const del = useDeleteDocument(slug);
  useSetBreadcrumb(q.data?.title);

  const goPage = React.useCallback(
    (n: number) => {
      const sp = new URLSearchParams(params.toString());
      sp.set("page", String(n));
      router.replace(`${pathname}?${sp}`, { scroll: false });
      setTab("pages");
    },
    [params, pathname, router],
  );

  if (q.isLoading) return <Skeleton className="h-96 rounded-card" />;
  if (q.isError || !q.data) {
    return (
      <Card>
        {isUnavailableEndpoint(q.error) ? (
          <EmptyState
            icon={<FileText />}
            title="Document not found"
            action={
              <Button variant="secondary" asChild>
                <Link href={`/o/${slug}/documents`}>
                  <ArrowLeft /> Documents
                </Link>
              </Button>
            }
          />
        ) : (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        )}
      </Card>
    );
  }
  const d = q.data;
  const pageList = [...(pages.data ?? [])].sort((a, b) => a.number - b.number);
  const total = d.page_count ?? pageList.length;
  const current = pageList.find((p) => p.number === page);
  const languages = Array.isArray(d.languages)
    ? d.languages
    : d.languages && typeof d.languages === "object"
      ? Object.entries(d.languages)
          .filter(([, share]) => Number(share) >= 0.05)
          .sort((a, b) => Number(b[1]) - Number(a[1]))
          .map(([l]) => l)
      : d.language
        ? [d.language]
        : [];

  const entityCols: DataTableColumn<DocumentEntity>[] = [
    { id: "kind", accessorKey: "kind", header: "Type", cell: ({ row }) => <Badge tone="accent">{titleCase(row.original.kind)}</Badge> },
    {
      id: "value",
      accessorKey: "value",
      header: "Value",
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-1.5 font-medium">
          {row.original.value}
          {row.original.is_pii && <Badge tone="warning">PII</Badge>}
        </span>
      ),
    },
    { id: "normalized", accessorFn: (r) => r.normalized ?? "", header: "Normalized", meta: { hideOnMobile: true } },
    {
      id: "page",
      accessorFn: (r) => r.page ?? 0,
      header: "Page",
      meta: { align: "right" },
      cell: ({ row }) =>
        row.original.page ? (
          <button type="button" className="text-accent hover:underline focus-ring" onClick={(e) => { e.stopPropagation(); goPage(row.original.page!); }}>
            p.{row.original.page}
          </button>
        ) : (
          "—"
        ),
    },
    { id: "confidence", accessorFn: (r) => r.confidence ?? -1, header: "Confidence", meta: { align: "right" }, cell: ({ row }) => (row.original.confidence !== null && row.original.confidence !== undefined ? `${Math.round(row.original.confidence * 100)}%` : "—") },
  ];

  return (
    <div className="space-y-6">
      <Link href={`/o/${slug}/documents`} className="inline-flex items-center gap-1.5 rounded-[4px] text-sm text-fg-muted hover:text-fg focus-ring">
        <ArrowLeft className="size-4" /> Documents
      </Link>
      <Card>
        <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-start gap-4">
            <span className="flex size-11 shrink-0 items-center justify-center rounded-card bg-accent-soft text-accent-soft-fg">
              <FileText className="size-5" aria-hidden />
            </span>
            <div className="min-w-0 space-y-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="truncate text-xl font-semibold tracking-[-0.015em] text-fg">{d.title}</h1>
                <StatusBadge status={d.status} />
                <EmbeddingPendingBadge doc={d} />
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
                <ClassificationBadge value={d.classification} />
                {d.doc_type && <Badge tone="outline">{d.doc_type}</Badge>}
                {languages.map((l) => (
                  <Badge key={l} tone="neutral">
                    {l}
                  </Badge>
                ))}
                <span>{d.filename}</span>
                <span>· {formatBytes(d.size_bytes)}</span>
                <span>· {formatNumber(total)} pages</span>
                <span>· {formatDateTime(d.created_at)}</span>
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <CollectionPicker document={d} />
            {d.download_url && (
              <Button
                variant="secondary"
                size="sm"
                loading={downloading}
                onClick={async () => {
                  setDownloading(true);
                  try {
                    try {
                      await downloadSignedUrl(d.download_url!, d.filename);
                    } catch (e) {
                      // Signed links expire: fetch the document again for a fresh link and retry once.
                      if (!isExpiredDownload(e)) throw e;
                      const fresh = (await q.refetch()).data?.download_url;
                      if (!fresh) throw e;
                      await downloadSignedUrl(fresh, d.filename);
                    }
                  } catch (e) {
                    toast.error(errorMessage(e));
                  } finally {
                    setDownloading(false);
                  }
                }}
              >
                <Download /> Original
              </Button>
            )}
            {can("documents.manage") && (
              <Button variant="danger-ghost" size="sm" onClick={() => setDeleting(true)}>
                <Trash2 /> Delete
              </Button>
            )}
          </div>
        </div>
        {isDocProcessing(d) && (
          <div className="flex items-center gap-2 border-t border-border bg-info-soft/60 px-5 py-2.5 text-sm text-info" role="status">
            <Loader2 className="size-4 animate-spin" aria-hidden /> Processing ({d.status}). Pages, passages and entities appear when it finishes.
          </div>
        )}
        {d.status === "failed" && (
          <div className="border-t border-border bg-danger-soft px-5 py-2.5 text-sm text-danger" role="alert">
            Processing failed{d.error ? `: ${d.error}` : "."}
          </div>
        )}
        {(d.warnings ?? []).length > 0 && (
          <ul className="space-y-0.5 border-t border-border bg-warning-soft/50 px-5 py-2.5 text-xs text-warning" aria-label="Processing warnings">
            {(d.warnings ?? []).map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        )}
        {d.summary && (
          <div className="border-t border-border px-5 py-3 text-sm text-fg-muted">
            <span className="mr-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-fg-subtle">Summary (extractive)</span>
            {d.summary}
          </div>
        )}
      </Card>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="pages">
              <BookOpen /> Pages
            </TabsTrigger>
            <TabsTrigger value="chunks">
              <Layers /> Passages
            </TabsTrigger>
            <TabsTrigger value="entities">
              <Tags /> Entities
            </TabsTrigger>
            <TabsTrigger value="tables">
              <Table2 /> Tables
            </TabsTrigger>
            <TabsTrigger value="search">
              <Search /> Search
            </TabsTrigger>
            <TabsTrigger value="extract">
              <ScanSearch /> Extract
            </TabsTrigger>
          </TabsList>
          <TabsContent value="pages">
            {!ready ? (
              <Card>
                <EmptyState compact icon={<Loader2 className="animate-spin" />} title="Not ready yet" />
              </Card>
            ) : pages.isError ? (
              <Unavailable what="Pages" error={pages.error} onRetry={() => void pages.refetch()} />
            ) : (
              <Card>
                <div className="flex items-center gap-2 border-b border-border px-4 py-2">
                  <Button variant="ghost" size="icon-xs" onClick={() => goPage(page - 1)} disabled={page <= 1} aria-label="Previous page">
                    <ChevronLeft />
                  </Button>
                  <span className="tabular text-sm text-fg">
                    Page {page} <span className="text-fg-muted">of {formatNumber(total)}</span>
                  </span>
                  <Button variant="ghost" size="icon-xs" onClick={() => goPage(page + 1)} disabled={page >= total} aria-label="Next page">
                    <ChevronRight />
                  </Button>
                  {current?.method === "ocr" && (
                    <Badge tone="info" className="ml-auto">
                      <ScanText /> OCR{current.ocr_confidence ? ` ${Math.round(Number(current.ocr_confidence))}%` : ""}
                    </Badge>
                  )}
                  {(current?.method === "ocr_unavailable" || current?.method === "ocr_failed") && (
                    <Badge tone="warning" className="ml-auto" title="Scanned page: install Tesseract OCR on the server to extract its text">
                      <ScanText /> {current.method === "ocr_failed" ? "OCR failed" : "OCR unavailable"}
                    </Badge>
                  )}
                  {current?.label && <span className="text-xs text-fg-muted">{current.label}</span>}
                </div>
                <div className="scrollbar-thin max-h-[640px] overflow-y-auto">
                  {pages.isLoading ? (
                    <div className="space-y-2 p-6">
                      {Array.from({ length: 8 }).map((_, i) => (
                        <Skeleton key={i} className="h-3" style={{ width: `${70 + ((i * 13) % 30)}%` }} />
                      ))}
                    </div>
                  ) : current ? (
                    <article className="mx-auto max-w-[72ch] whitespace-pre-wrap px-6 py-6 font-serif text-[15px] leading-7 text-fg" lang={current.language && current.language !== "und" && current.language !== "mixed" ? current.language : undefined}>
                      {current.text || <span className="text-fg-subtle">(no text on this page)</span>}
                    </article>
                  ) : (
                    <EmptyState compact icon={<BookOpen />} title="Page not available" />
                  )}
                </div>
                {pageList.length > 1 && (
                  <div className="scrollbar-thin flex gap-1 overflow-x-auto border-t border-border px-3 py-2" aria-label="Pages">
                    {pageList.map((p) => (
                      <button
                        key={p.number}
                        type="button"
                        onClick={() => goPage(p.number)}
                        aria-current={p.number === page ? "page" : undefined}
                        className={cn("tabular min-w-8 rounded-[4px] px-1.5 py-0.5 text-xs focus-ring", p.number === page ? "bg-accent text-accent-fg" : "text-fg-muted hover:bg-surface-2")}
                      >
                        {p.number}
                      </button>
                    ))}
                  </div>
                )}
              </Card>
            )}
          </TabsContent>
          <TabsContent value="chunks">
            {chunks.isError ? (
              <Unavailable what="Passages" error={chunks.error} onRetry={() => void chunks.refetch()} />
            ) : chunks.isLoading || !ready ? (
              <Skeleton className="h-64 rounded-card" />
            ) : (
              <ol className="space-y-2">
                {(chunks.data ?? []).length === 0 && <EmptyState compact icon={<Layers />} title="No passages" />}
                {[...(chunks.data ?? [])].sort((a, b) => a.ordinal - b.ordinal).map((c) => (
                  <li key={c.id} className="rounded-card border border-border bg-surface p-3">
                    <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-fg-muted">
                      <span className="tabular font-mono">#{c.ordinal}</span>
                      {c.page !== null && (
                        <button type="button" onClick={() => goPage(c.page ?? 1)} className="text-accent hover:underline focus-ring">
                          p.{c.page}
                          {c.page_to && c.page_to !== c.page ? `–${c.page_to}` : ""}
                        </button>
                      )}
                      {c.section && <span className="truncate">{c.section}</span>}
                      {c.token_count !== undefined && <span className="tabular ml-auto">{c.token_count} tokens</span>}
                    </div>
                    <p className="line-clamp-4 text-sm text-fg">{c.text}</p>
                  </li>
                ))}
              </ol>
            )}
          </TabsContent>
          <TabsContent value="entities">
            {entities.isError ? (
              <Unavailable what="Entities" error={entities.error} onRetry={() => void entities.refetch()} />
            ) : (
              <DataTable
                caption="Extracted entities"
                columns={entityCols}
                data={entities.data}
                loading={entities.isLoading || !ready}
                getRowId={(r) => r.id}
                filters={[{ columnId: "kind", label: "Type", options: Array.from(new Set((entities.data ?? []).map((e) => e.kind))).map((t) => ({ value: t, label: titleCase(t) })) }]}
                emptyState={<EmptyState compact icon={<Tags />} title="No entities found" />}
              />
            )}
          </TabsContent>
          <TabsContent value="tables">
            {tables.isError ? (
              <Unavailable what="Tables" error={tables.error} onRetry={() => void tables.refetch()} />
            ) : tables.isLoading || !ready ? (
              <Skeleton className="h-64 rounded-card" />
            ) : (tables.data ?? []).length === 0 ? (
              <Card>
                <EmptyState compact icon={<Table2 />} title="No tables detected" />
              </Card>
            ) : (
              <div className="space-y-4">
                {(tables.data ?? []).map((t) => (
                  <DocTableCard key={t.id} table={t} onPage={goPage} documentId={d.id} />
                ))}
              </div>
            )}
          </TabsContent>
          <TabsContent value="search">
            <SearchPanel org={slug} titles={new Map([[d.id, d.title]])} documentIds={[d.id]} onOpen={(_, p) => goPage(p)} />
          </TabsContent>
          <TabsContent value="extract">
            {ready ? (
              <ExtractPanel documentId={d.id} onPage={goPage} />
            ) : (
              <Card>
                <EmptyState compact icon={<Loader2 className="animate-spin" />} title="Not ready yet" />
              </Card>
            )}
          </TabsContent>
        </Tabs>
        {can("documents.ask") && ready && (
          <div className="xl:sticky xl:top-20 xl:self-start">
            <DocumentAskPanel org={slug} documentIds={[d.id]} title="Ask this document" onCitation={(c) => (c.document_id === d.id ? goPage(c.page) : router.push(`/o/${slug}/documents/${c.document_id}?page=${c.page}`))} />
          </div>
        )}
      </div>
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={`Delete “${d.title}”?`}
        description="The file, its pages, passages, embeddings and extracted data are removed."
        confirmLabel="Delete document"
        loading={del.isPending}
        onConfirm={async () => {
          try {
            await del.mutateAsync(d.id);
            toast.success("Document deleted");
            router.replace(`/o/${slug}/documents`);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </div>
  );
}

function DocTableCard({ table, onPage, documentId }: { table: DocumentTable; onPage: (p: number) => void; documentId: string }) {
  const width = Math.max(table.header?.length ?? 0, ...(table.rows ?? []).map((r) => (Array.isArray(r) ? r.length : 0)));
  const columns = Array.from({ length: width }, (_, i) => String(table.header?.[i] || `col_${i + 1}`));
  const title = table.title || `Table ${table.index + 1}`;
  return (
    <Card>
      <CardHeader
        icon={<Table2 />}
        title={title}
        description={table.truncated ? `Showing the first ${table.rows.length} of ${table.n_rows ?? "?"} rows` : undefined}
        actions={
          <>
            <PromoteTableButton documentId={documentId} table={table} title={title} />
            {table.page ? (
              <Button variant="ghost" size="xs" onClick={() => onPage(table.page ?? 1)}>
                Page {table.page}
              </Button>
            ) : null}
          </>
        }
      />
      <CardContent>{columns.length ? <ResultTable columns={columns} rows={table.rows ?? []} caption={title} /> : <p className="text-sm text-fg-muted">Empty table.</p>}</CardContent>
    </Card>
  );
}
