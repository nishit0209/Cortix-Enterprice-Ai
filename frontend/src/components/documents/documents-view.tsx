"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { FileSearch, FileText, Library, Search, Upload } from "lucide-react";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
import { ClassificationBadge, FeatureStatusBadge, StatusBadge } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip } from "@/components/ui/overlays";
import { isUnavailableEndpoint } from "@/lib/errors";
import { highlightTerms, snippet, useDocuments, useDocumentSearch } from "@/lib/queries/documents";
import type { DocumentItem } from "@/lib/types";
import { formatBytes, formatDateTime, formatNumber, formatRelative } from "@/lib/utils";
import { DocumentAskPanel, Highlighted } from "./ask-panel";
import { DocumentUploadDialog } from "./document-upload-dialog";

export function DocumentsView() {
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const docs = useDocuments(slug);
  const [uploading, setUploading] = React.useState(false);
  const [tab, setTab] = React.useState("library");
  const titles = React.useMemo(() => new Map((docs.data ?? []).map((d) => [d.id, d.title])), [docs.data]);
  const unavailable = docs.isError && isUnavailableEndpoint(docs.error);

  const columns = React.useMemo<DataTableColumn<DocumentItem>[]>(
    () => [
      {
        id: "title",
        accessorKey: "title",
        header: "Document",
        cell: ({ row }) => (
          <span className="flex items-center gap-2.5">
            <span className="flex size-7 items-center justify-center rounded-control bg-accent-soft text-accent-soft-fg">
              <FileText className="size-3.5" aria-hidden />
            </span>
            <span className="min-w-0">
              <span className="block max-w-[320px] truncate font-medium">{row.original.title}</span>
              <span className="block truncate text-xs text-fg-subtle">{row.original.filename}</span>
            </span>
          </span>
        ),
      },
      { id: "status", accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
      { id: "classification", accessorKey: "classification", header: "Classification", cell: ({ row }) => <ClassificationBadge value={row.original.classification} /> },
      { id: "doc_type", accessorFn: (r) => r.doc_type ?? "", header: "Type", meta: { hideOnMobile: true }, cell: ({ row }) => (row.original.doc_type ? <Badge tone="outline">{row.original.doc_type}</Badge> : <span className="text-fg-subtle">—</span>) },
      { id: "pages", accessorFn: (r) => r.page_count ?? -1, header: "Pages", meta: { align: "right" }, cell: ({ row }) => formatNumber(row.original.page_count) },
      { id: "size", accessorKey: "size_bytes", header: "Size", meta: { align: "right", hideOnMobile: true }, cell: ({ row }) => formatBytes(row.original.size_bytes) },
      {
        id: "created_at",
        accessorKey: "created_at",
        header: "Uploaded",
        cell: ({ row }) => (
          <Tooltip content={formatDateTime(row.original.created_at)}>
            <span className="whitespace-nowrap text-fg-muted">{formatRelative(row.original.created_at)}</span>
          </Tooltip>
        ),
      },
    ],
    [],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Documents"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Upload documents, search them with hybrid semantic + keyword search, and ask questions answered with page citations."
        actions={
          can("documents.manage") && !unavailable ? (
            <Button onClick={() => setUploading(true)}>
              <Upload /> Upload document
            </Button>
          ) : undefined
        }
      />
      {unavailable ? (
        <Card>
          <EmptyState icon={<FileText />} title="Document intelligence isn't available yet" description="It appears here once the documents service is deployed." />
        </Card>
      ) : (
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="library">
              <Library /> Library
            </TabsTrigger>
            <TabsTrigger value="search">
              <Search /> Search
            </TabsTrigger>
          </TabsList>
          <TabsContent value="library">
            <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_400px]">
              <DataTable
                caption="Documents"
                columns={columns}
                data={docs.data}
                loading={docs.isLoading}
                error={docs.isError ? docs.error : undefined}
                onRetry={() => void docs.refetch()}
                onRowClick={(d) => router.push(`/o/${slug}/documents/${d.id}`)}
                getRowId={(d) => d.id}
                searchPlaceholder="Filter by title…"
                initialSorting={[{ id: "created_at", desc: true }]}
                filters={[{ columnId: "status", label: "Status", options: Array.from(new Set((docs.data ?? []).map((d) => d.status))).map((s) => ({ value: s, label: s })) }]}
                emptyState={
                  <EmptyState
                    icon={<FileText />}
                    title="No documents yet"
                    description="Upload contracts, reports or invoices. Everything is processed and embedded locally."
                    action={
                      can("documents.manage") ? (
                        <Button onClick={() => setUploading(true)}>
                          <Upload /> Upload document
                        </Button>
                      ) : undefined
                    }
                  />
                }
              />
              {can("documents.ask") && (
                <div className="xl:sticky xl:top-20 xl:self-start">
                  <DocumentAskPanel org={slug} onCitation={(c) => router.push(`/o/${slug}/documents/${c.document_id}?page=${c.page}`)} />
                </div>
              )}
            </div>
          </TabsContent>
          <TabsContent value="search">
            <SearchPanel org={slug} titles={titles} onOpen={(id, page) => router.push(`/o/${slug}/documents/${id}?page=${page}`)} />
          </TabsContent>
        </Tabs>
      )}
      <DocumentUploadDialog org={slug} open={uploading} onOpenChange={setUploading} />
    </div>
  );
}

export function SearchPanel({
  org,
  titles,
  onOpen,
  documentIds,
}: {
  org: string;
  titles: Map<string, string>;
  onOpen: (documentId: string, page: number) => void;
  documentIds?: string[];
}) {
  const search = useDocumentSearch(org);
  const [query, setQuery] = React.useState("");
  const [submitted, setSubmitted] = React.useState("");
  const run = (e?: React.FormEvent) => {
    e?.preventDefault();
    const q = query.trim();
    if (!q) return;
    setSubmitted(q);
    search.mutate({ query: q, ...(documentIds ? { document_ids: documentIds } : {}) });
  };
  return (
    <div className="space-y-4">
      <form onSubmit={run} className="flex gap-2">
        <Input
          aria-label="Search documents"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search passages: payment terms, warranty, termination…"
          leading={<Search />}
          className="flex-1"
        />
        <Button type="submit" loading={search.isPending} disabled={!query.trim()}>
          Search
        </Button>
      </form>
      {search.isError ? (
        isUnavailableEndpoint(search.error) ? (
          <EmptyState compact icon={<FileSearch />} title="Search isn't available yet" />
        ) : (
          <ErrorState compact error={search.error} onRetry={() => run()} />
        )
      ) : search.isPending ? (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-20 rounded-card" />
          ))}
        </div>
      ) : search.data ? (
        search.data.length === 0 ? (
          <EmptyState compact icon={<FileSearch />} title="No matching passages" description="Try other words; search combines meaning and exact keywords." />
        ) : (
          <ol className="space-y-2" aria-label="Search results">
            <li className="text-xs text-fg-muted">
              {search.data.length} passage{search.data.length === 1 ? "" : "s"} for “{submitted}”
            </li>
            {search.data.map((h) => (
              <li key={h.chunk_id}>
                <button
                  type="button"
                  onClick={() => onOpen(h.document_id, h.page ?? 1)}
                  className="block w-full rounded-card border border-border bg-surface p-4 text-left shadow-xs transition-[border-color,box-shadow] hover:border-border-strong hover:shadow-soft focus-ring"
                >
                  <div className="mb-1.5 flex flex-wrap items-center gap-2 text-xs">
                    <span className="font-medium text-fg">{h.document_title ?? titles.get(h.document_id) ?? "Document"}</span>
                    <Badge tone="outline">
                      p.{h.page ?? "?"}
                      {h.page_to && h.page_to !== h.page ? `–${h.page_to}` : ""}
                    </Badge>
                    {typeof h.score === "number" && <span className="tabular ml-auto text-fg-subtle">score {h.score.toFixed(3)}</span>}
                  </div>
                  <p className="text-sm leading-6 text-fg-muted">
                    <Highlighted segments={highlightTerms(snippet(h.text, submitted), submitted)} />
                  </p>
                </button>
              </li>
            ))}
          </ol>
        )
      ) : (
        <Card>
          <CardContent className="pt-5">
            <EmptyState compact icon={<Search />} title="Search your documents" description="Hybrid search ranks passages by meaning (local embeddings) and by keyword." />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
