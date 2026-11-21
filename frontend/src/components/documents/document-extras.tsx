"use client";

import * as React from "react";
import Link from "next/link";
import { DatabaseZap, FileDiff, FolderPlus, Folders, ScanSearch } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status";
import { isUnavailableEndpoint, errorMessage } from "@/lib/errors";
import { displayValue } from "@/lib/mdm";
import {
  EXTRACTION_SCHEMAS,
  useCollections,
  useCompareDocuments,
  useCreateCollection,
  useExtract,
  useExtractions,
  usePromoteTable,
  useSetDocumentCollection,
  type CompareResult,
  type Extraction,
} from "@/lib/queries/document-extras";
import { useDocuments } from "@/lib/queries/documents";
import type { DocumentItem, DocumentTable } from "@/lib/types";
import { cn, formatPercent, formatRelative, titleCase } from "@/lib/utils";
import { ModuleError, NativeSelect } from "@/components/mdm/common";

const CLASSIFICATIONS = ["public", "internal", "confidential", "restricted"];

/* ---------------- collections ---------------- */

export function CollectionsPanel({ selected, onSelect }: { selected: string | null; onSelect: (id: string | null) => void }) {
  const { slug, can } = useOrgContext();
  const list = useCollections(slug);
  const create = useCreateCollection(slug);
  const [open, setOpen] = React.useState(false);
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [classification, setClassification] = React.useState("internal");
  const [error, setError] = React.useState<string | null>(null);
  if (list.isError) return <Card><ModuleError compact error={list.error} what="Collections" onRetry={() => void list.refetch()} /></Card>;
  return (
    <Card>
      <CardHeader
        icon={<Folders />}
        title="Collections"
        description="Group related documents (e.g. supplier invoices) to search and ask across them."
        actions={
          can("documents.manage") && (
            <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
              <FolderPlus /> New
            </Button>
          )
        }
      />
      <CardContent>
        {list.isLoading ? (
          <Skeleton className="h-16" />
        ) : !(list.data ?? []).length ? (
          <p className="text-sm text-fg-muted">No collections yet.</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            <button type="button" onClick={() => onSelect(null)} className={cn("rounded-full border px-2.5 py-1 text-xs focus-ring", !selected ? "border-accent bg-accent-soft text-accent-soft-fg" : "border-border text-fg-muted hover:bg-surface-2")}>
              All documents
            </button>
            {list.data!.map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => onSelect(c.id)}
                title={c.description}
                className={cn("rounded-full border px-2.5 py-1 text-xs focus-ring", selected === c.id ? "border-accent bg-accent-soft text-accent-soft-fg" : "border-border text-fg-muted hover:bg-surface-2")}
              >
                {c.name}
                {c.document_count !== null && <span className="ml-1 tabular text-fg-subtle">{c.document_count}</span>}
              </button>
            ))}
          </div>
        )}
      </CardContent>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent size="sm">
          <DialogHeader title="New collection" />
          <DialogBody className="space-y-3">
            <Field id="col-name" label="Name">
              <Input id="col-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
            </Field>
            <Field id="col-desc" label="Description">
              <Input id="col-desc" value={description} onChange={(e) => setDescription(e.target.value)} />
            </Field>
            <Field id="col-class" label="Classification">
              <NativeSelect id="col-class" value={classification} onChange={(e) => setClassification(e.target.value)}>
                {CLASSIFICATIONS.map((c) => (
                  <option key={c} value={c}>
                    {titleCase(c)}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            {error && <p className="text-sm text-danger">{error}</p>}
          </DialogBody>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              loading={create.isPending}
              onClick={async () => {
                if (!name.trim()) return setError("Name the collection.");
                try {
                  await create.mutateAsync({ name: name.trim(), description: description.trim() || undefined, classification });
                  toast.success("Collection created");
                  setOpen(false);
                  setName("");
                  setDescription("");
                  setError(null);
                } catch (e) {
                  setError(errorMessage(e));
                }
              }}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

export function CollectionPicker({ document }: { document: DocumentItem }) {
  const { slug, can } = useOrgContext();
  const list = useCollections(slug);
  const set = useSetDocumentCollection(slug, document.id);
  if (list.isError || !(list.data ?? []).length) return null;
  return (
    <label className="flex items-center gap-2 text-xs text-fg-muted">
      Collection
      <NativeSelect
        className="h-8 w-44"
        value={document.collection_id ?? ""}
        disabled={!can("documents.manage") || set.isPending}
        onChange={async (e) => {
          try {
            await set.mutateAsync(e.target.value || null);
            toast.success("Collection updated");
          } catch (err) {
            toast.error(errorMessage(err));
          }
        }}
      >
        <option value="">None</option>
        {list.data!.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </NativeSelect>
    </label>
  );
}

/* ---------------- extraction ---------------- */

export function ExtractPanel({ documentId, onPage }: { documentId: string; onPage: (p: number) => void }) {
  const { slug, can } = useOrgContext();
  const history = useExtractions(slug, documentId);
  const extract = useExtract(slug, documentId);
  const [schema, setSchema] = React.useState(EXTRACTION_SCHEMAS[0].value);
  const [latest, setLatest] = React.useState<Extraction | null>(null);
  const items = React.useMemo(() => {
    const list = history.data ?? [];
    return latest && !list.some((x) => x.id === latest.id) ? [latest, ...list] : list;
  }, [history.data, latest]);
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader icon={<ScanSearch />} title="Extract fields" description="Local rules find the fields and cite the page each value came from. Results need a reviewer's approval before they are used." />
        <CardContent className="flex flex-wrap items-end gap-2">
          <Field id="ex-schema" label="Schema">
            <NativeSelect id="ex-schema" value={schema} onChange={(e) => setSchema(e.target.value)} className="w-40">
              {EXTRACTION_SCHEMAS.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Button
            disabled={!can("documents.manage")}
            loading={extract.isPending}
            onClick={async () => {
              try {
                setLatest(await extract.mutateAsync(schema));
                toast.success("Fields extracted");
              } catch (e) {
                toast.error(errorMessage(e));
              }
            }}
          >
            <ScanSearch /> Extract
          </Button>
        </CardContent>
      </Card>
      {history.isError && !isUnavailableEndpoint(history.error) && <ModuleError compact error={history.error} what="Extractions" />}
      {history.isLoading && !latest ? (
        <Skeleton className="h-32 rounded-card" />
      ) : !items.length ? (
        <Card>
          <EmptyState compact icon={<ScanSearch />} title="No extractions yet" />
        </Card>
      ) : (
        items.map((x) => <ExtractionCard key={x.id || x.created_at} extraction={x} onPage={onPage} />)
      )}
    </div>
  );
}

function ExtractionCard({ extraction, onPage }: { extraction: Extraction; onPage: (p: number) => void }) {
  return (
    <Card>
      <CardHeader
        title={titleCase(extraction.schema_key)}
        description={`${extraction.method ? `${titleCase(extraction.method)} extraction` : "Extraction"}${extraction.reason ? ` (${extraction.reason.replace(/_/g, " ")})` : ""} · ${formatRelative(extraction.created_at)}`}
        actions={<StatusBadge status={extraction.status} />}
      />
      <CardContent className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-xs text-fg-muted">
            <tr>
              <th className="py-1.5 pr-3 text-left font-medium">Field</th>
              <th className="py-1.5 pr-3 text-left font-medium">Value</th>
              <th className="py-1.5 pr-3 text-left font-medium">Confidence</th>
              <th className="py-1.5 text-left font-medium">Source</th>
            </tr>
          </thead>
          <tbody>
            {extraction.fields.map((f) => (
              <tr key={f.name} className="border-t border-border align-top">
                <td className="py-1.5 pr-3 text-fg-muted">{f.name.replace(/_/g, " ")}</td>
                <td className="py-1.5 pr-3 font-medium">{displayValue(f.value) || <span className="text-fg-subtle">not found</span>}</td>
                <td className="py-1.5 pr-3 tabular text-xs">{f.confidence === null ? "—" : formatPercent(f.confidence, 0)}</td>
                <td className="py-1.5 text-xs">
                  {f.citations.map((c, i) => (
                    <button key={i} type="button" className="mr-1.5 text-accent hover:underline" title={c.quote ?? undefined} onClick={() => onPage(c.page)}>
                      p.{c.page}
                    </button>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

/* ---------------- table promotion ---------------- */

export function PromoteTableButton({ documentId, table, title }: { documentId: string; table: DocumentTable; title: string }) {
  const { slug, can } = useOrgContext();
  const promote = usePromoteTable(slug, documentId);
  const [open, setOpen] = React.useState(false);
  const [name, setName] = React.useState(title);
  const [classification, setClassification] = React.useState("internal");
  const [attest, setAttest] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [done, setDone] = React.useState<string | null>(table.promoted_dataset_id ?? null);
  if (done) {
    return (
      <Button variant="ghost" size="xs" asChild>
        <Link href={`/o/${slug}/data/datasets/${done}`}>
          <DatabaseZap /> Dataset
        </Link>
      </Button>
    );
  }
  if (!can("datasets.create")) return null;
  const needsAttest = classification === "confidential" || classification === "restricted";
  return (
    <>
      <Button variant="ghost" size="xs" onClick={() => setOpen(true)}>
        <DatabaseZap /> Save as dataset
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent size="sm">
          <DialogHeader title="Save table as a dataset" description="It is ingested like an upload: profiled, quality-checked and queryable under your data policies." />
          <DialogBody className="space-y-3">
            <Field id="pt-name" label="Dataset name">
              <Input id="pt-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
            </Field>
            <Field id="pt-class" label="Classification">
              <NativeSelect id="pt-class" value={classification} onChange={(e) => setClassification(e.target.value)}>
                {CLASSIFICATIONS.map((c) => (
                  <option key={c} value={c}>
                    {titleCase(c)}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            {needsAttest && (
              <label className="flex items-start gap-2 text-xs">
                <Checkbox checked={attest} onCheckedChange={(c) => setAttest(c === true)} />I confirm this classification is correct for the data in this table.
              </label>
            )}
            {error && <p className="text-sm text-danger">{error}</p>}
          </DialogBody>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              loading={promote.isPending}
              disabled={needsAttest && !attest}
              onClick={async () => {
                if (!name.trim()) return setError("Name the dataset.");
                try {
                  const res = await promote.mutateAsync({ tableId: table.id, name: name.trim(), classification, attest: needsAttest ? attest : undefined });
                  toast.success("Dataset created; ingestion runs in the background");
                  setOpen(false);
                  if (res.dataset_id) setDone(res.dataset_id);
                } catch (e) {
                  setError(errorMessage(e));
                }
              }}
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/* ---------------- compare ---------------- */

const CHANGE_TONE = { added: "success", removed: "danger", changed: "warning", same: "neutral" } as const;

export function ComparePanel() {
  const { slug } = useOrgContext();
  const docs = useDocuments(slug);
  const compare = useCompareDocuments(slug);
  const [a, setA] = React.useState("");
  const [b, setB] = React.useState("");
  const [showSame, setShowSame] = React.useState(false);
  const [result, setResult] = React.useState<CompareResult | null>(null);
  const ready = (docs.data ?? []).filter((d) => d.status === "ready");
  const name = (id: string) => ready.find((d) => d.id === id)?.title ?? "Document";
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader icon={<FileDiff />} title="Compare two documents" description="A local section-by-section diff, e.g. two versions of a contract or policy." />
        <CardContent className="flex flex-wrap items-end gap-2">
          <Field id="cmp-a" label="Original">
            <NativeSelect id="cmp-a" value={a} onChange={(e) => setA(e.target.value)} className="w-56">
              <option value="">Choose…</option>
              {ready.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.title}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Field id="cmp-b" label="Revised">
            <NativeSelect id="cmp-b" value={b} onChange={(e) => setB(e.target.value)} className="w-56">
              <option value="">Choose…</option>
              {ready
                .filter((d) => d.id !== a)
                .map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.title}
                  </option>
                ))}
            </NativeSelect>
          </Field>
          <Button
            disabled={!a || !b || a === b}
            loading={compare.isPending}
            onClick={async () => {
              try {
                setResult(await compare.mutateAsync({ a, b }));
              } catch (e) {
                toast.error(errorMessage(e));
              }
            }}
          >
            <FileDiff /> Compare
          </Button>
        </CardContent>
      </Card>
      {result && (
        <Card>
          <CardHeader
            title={`${name(a)} → ${name(b)}`}
            description={result.similarity !== null ? `${formatPercent(result.similarity, 0)} similar` : undefined}
            actions={
              <label className="flex items-center gap-1.5 text-xs text-fg-muted">
                <Checkbox checked={showSame} onCheckedChange={(c) => setShowSame(c === true)} /> Show unchanged
              </label>
            }
          />
          <CardContent className="space-y-3">
            {result.summary && (
              <p className="rounded-control bg-surface-2 p-3 text-sm">
                {result.summary_mode && <Badge tone="outline" className="mr-2">{result.summary_mode}</Badge>}
                {result.summary}
              </p>
            )}
            {!result.sections.filter((s) => showSame || s.change !== "same").length ? (
              <p className="text-sm text-fg-muted">No differences{showSame ? "" : " (unchanged sections hidden)"}.</p>
            ) : (
              result.sections
                .filter((s) => showSame || s.change !== "same")
                .map((s, i) => (
                  <div key={`${s.title}-${i}`} className="rounded-control border border-border">
                    <div className="flex items-center gap-2 border-b border-border px-3 py-1.5 text-sm">
                      <Badge tone={CHANGE_TONE[s.change]}>{titleCase(s.change)}</Badge>
                      <span className="font-medium">{s.title}</span>
                      {s.similarity !== null && <span className="ml-auto text-xs text-fg-subtle">{formatPercent(s.similarity, 0)} similar</span>}
                    </div>
                    <div className="grid gap-px bg-border text-xs sm:grid-cols-2">
                      <p className={cn("bg-surface p-3 whitespace-pre-wrap", s.change === "removed" && "bg-danger-soft/50")}>{s.a ?? <span className="text-fg-subtle">—</span>}</p>
                      <p className={cn("bg-surface p-3 whitespace-pre-wrap", s.change === "added" && "bg-success-soft/50")}>{s.b ?? <span className="text-fg-subtle">—</span>}</p>
                    </div>
                  </div>
                ))
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
