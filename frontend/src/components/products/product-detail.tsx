"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Boxes, GitMerge, Pencil, ShieldCheck, Tag, Wand2 } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
import { ScoreRing } from "@/components/ui/score-ring";
import { StatCard } from "@/components/ui/stat-card";
import { StatusBadge } from "@/components/ui/status";
import { errorMessage } from "@/lib/errors";
import { displayValue } from "@/lib/mdm";
import { CORRECTABLE_FIELDS, useProduct, useProposeCorrection, type ProductDetail } from "@/lib/queries/products";
import { formatNumber, formatRelative, titleCase } from "@/lib/utils";
import { ModuleError, NativeSelect, ScoreBar } from "@/components/mdm/common";

const ISSUE_LABELS: Record<string, string> = {
  invalid_size: "Size has an unknown unit",
  invalid_sku: "SKU format is invalid",
  name_format: "Name is too short or all caps",
  inconsistent_brand: "Sources disagree on the brand",
  inconsistent_category: "Sources disagree on the category",
  inconsistent_size: "Sources disagree on the size",
  price_spread: "Source prices differ by more than 20%",
};

export function ProductDetailView() {
  const { id } = useParams<{ id: string }>();
  const { slug, can } = useOrgContext();
  const q = useProduct(slug, id);
  const p = q.data;
  const [propose, setPropose] = React.useState(false);
  useSetBreadcrumb(p?.name);
  if (q.isError) {
    return (
      <Card>
        <ModuleError error={q.error} what="This product" onRetry={() => void q.refetch()} />
        <div className="flex justify-center pb-6">
          <Button variant="secondary" asChild>
            <Link href={`/o/${slug}/products`}>
              <ArrowLeft /> Products
            </Link>
          </Button>
        </div>
      </Card>
    );
  }
  if (!p) return <Skeleton className="h-96 rounded-card" />;
  const qd = p.quality_detail;
  const issues = Array.isArray(qd?.issues) ? (qd!.issues as unknown[]).map(String) : [];
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href={`/o/${slug}/products`} className="inline-flex items-center gap-1 hover:underline">
            <ArrowLeft className="size-3" /> Product 360
          </Link>
        }
        title={p.name}
        description={[p.brand, p.category, p.size].filter(Boolean).join(" · ") || undefined}
        badge={p.sku ? <Badge tone="outline" className="font-mono">{p.sku}</Badge> : undefined}
        actions={
          can("products.manage") && (
            <Button variant="secondary" onClick={() => setPropose(true)}>
              <Pencil /> Propose a correction
            </Button>
          )
        }
      />
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Product summary">
        <StatCard label="Source records" icon={<Boxes />} value={formatNumber(p.record_count)} hint={p.sources.join(", ")} />
        <StatCard label="Price" icon={<Tag />} accent="teal" value={p.price === null ? "—" : formatNumber(p.price)} />
        <StatCard label="Attributes" icon={<ShieldCheck />} accent="accent" value={formatNumber(p.attributes.length)} />
        <StatCard label="Updated" icon={<GitMerge />} accent="success" value={formatRelative(p.updated_at)} />
      </section>
      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader title="Quality" description="Completeness, validity and consistency across this product's source records." />
          <CardContent className="space-y-4">
            {!qd ? (
              <p className="text-sm text-fg-muted">Not scored yet.</p>
            ) : (
              <>
                <div className="flex items-center gap-4">
                  <ScoreRing score={qd.overall} />
                  <dl className="flex-1 space-y-1.5 text-xs">
                    {(["completeness", "validity", "consistency"] as const).map((k) => (
                      <div key={k} className="flex items-center justify-between gap-2">
                        <dt className="text-fg-muted">{titleCase(k)}</dt>
                        <dd>
                          <ScoreBar value={qd[k] / 100} />
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
                {issues.length > 0 && (
                  <ul className="space-y-1 text-sm">
                    {issues.map((i) => (
                      <li key={i} className="flex items-center gap-2 text-warning">
                        <span className="size-1.5 rounded-full bg-warning" aria-hidden /> {ISSUE_LABELS[i] ?? titleCase(i.replace(/_/g, " "))}
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </CardContent>
        </Card>
        <Card className="lg:col-span-2">
          <CardHeader title="Attributes" description="Normalized master values; approved corrections override source values." />
          <CardContent>
            {p.attributes.length === 0 && Object.keys(p.values ?? {}).length === 0 ? (
              <p className="text-sm text-fg-muted">No attributes.</p>
            ) : (
              <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
                {Object.entries(p.values ?? {})
                  .filter(([k, v]) => k !== "attributes" && v !== null && v !== "" && typeof v !== "object")
                  .map(([k, v]) => (
                    <div key={k} className="flex justify-between gap-2 border-b border-border/60 py-1">
                      <dt className="text-fg-muted">
                        {titleCase(k.replace(/_/g, " "))}
                        {k in (p.overrides ?? {}) && (
                          <Badge tone="success" className="ml-1.5">
                            corrected
                          </Badge>
                        )}
                      </dt>
                      <dd className="font-medium">{displayValue(v)}</dd>
                    </div>
                  ))}
                {p.attributes.map((a) => (
                  <div key={a.key} className="flex justify-between gap-2 border-b border-border/60 py-1">
                    <dt className="text-fg-muted">{a.key}</dt>
                    <dd className="text-right">
                      <span className="font-medium">{displayValue(a.value)}</span>
                      <span className="ml-1.5 text-[11px] text-fg-subtle">{a.origin}</span>
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </CardContent>
        </Card>
        <Card className="lg:col-span-3">
          <CardHeader title="Source records" />
          <CardContent className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-fg-muted">
                <tr>
                  <th className="py-2 pr-3 text-left font-medium">Source</th>
                  <th className="py-2 pr-3 text-left font-medium">Key</th>
                  <th className="py-2 pr-3 text-left font-medium">Raw</th>
                  <th className="py-2 text-left font-medium">Normalized</th>
                </tr>
              </thead>
              <tbody>
                {p.records.map((r) => (
                  <tr key={r.id} className="border-t border-border align-top">
                    <td className="py-2 pr-3">
                      <Badge tone="outline">{r.source_system}</Badge>
                    </td>
                    <td className="py-2 pr-3 font-mono text-xs">{r.source_key}</td>
                    <td className="py-2 pr-3 text-xs text-fg-muted">{kv(r.attributes)}</td>
                    <td className="py-2 text-xs">{kv(r.normalized)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
        <Card className="lg:col-span-3">
          <CardHeader title="Corrections" />
          <CardContent>
            {p.corrections.length === 0 ? (
              <EmptyState compact icon={<Wand2 />} title="No corrections" />
            ) : (
              <ul className="divide-y divide-border text-sm">
                {p.corrections.map((c) => (
                  <li key={c.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <span>
                      <span className="font-mono text-xs">{c.field}</span>: <span className="text-fg-muted line-through">{displayValue(c.old_value) || "—"}</span> →{" "}
                      <span className="font-medium">{displayValue(c.new_value)}</span>
                      {c.reason && <span className="ml-2 text-xs text-fg-subtle">{c.reason}</span>}
                    </span>
                    <span className="flex items-center gap-2 text-xs text-fg-subtle">
                      <StatusBadge status={c.status} /> {formatRelative(c.created_at)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
      <ProposeDialog open={propose} onOpenChange={setPropose} product={p} />
    </div>
  );
}

function kv(o: Record<string, unknown> | undefined) {
  return Object.entries(o ?? {})
    .filter(([, v]) => v !== null && v !== "")
    .map(([k, v]) => `${k}: ${displayValue(v)}`)
    .join(" · ");
}

function ProposeDialog({ open, onOpenChange, product }: { open: boolean; onOpenChange: (o: boolean) => void; product: ProductDetail }) {
  const { slug } = useOrgContext();
  const propose = useProposeCorrection(slug);
  const [field, setField] = React.useState<string>("name");
  const [attrKey, setAttrKey] = React.useState("");
  const [value, setValue] = React.useState("");
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (open) {
      setValue("");
      setReason("");
      setError(null);
    }
  }, [open]);
  const submit = async () => {
    setError(null);
    const f = field === "attribute" ? `attributes.${attrKey.trim()}` : field;
    if (field === "attribute" && !attrKey.trim()) return setError("Name the attribute.");
    if (!value.trim()) return setError("Enter the corrected value.");
    try {
      await propose.mutateAsync({ master_id: product.id, field: f, value: field === "price" ? Number(value) : value.trim(), reason: reason.trim() || undefined });
      toast.success("Correction proposed. A reviewer applies it on approval.");
      onOpenChange(false);
    } catch (e) {
      setError(errorMessage(e));
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader title="Propose a correction" description="It is applied only when someone with products.review approves it." />
        <DialogBody className="space-y-3">
          <Field id="pc-field" label="Field">
            <NativeSelect id="pc-field" value={field} onChange={(e) => setField(e.target.value)}>
              {CORRECTABLE_FIELDS.map((f) => (
                <option key={f} value={f}>
                  {titleCase(f)} (now: {displayValue(product.values?.[f]) || "—"})
                </option>
              ))}
              <option value="attribute">Another attribute…</option>
            </NativeSelect>
          </Field>
          {field === "attribute" && (
            <Field id="pc-attr" label="Attribute name">
              <Input id="pc-attr" value={attrKey} onChange={(e) => setAttrKey(e.target.value)} maxLength={70} />
            </Field>
          )}
          <Field id="pc-value" label="Corrected value">
            <Input id="pc-value" value={value} onChange={(e) => setValue(e.target.value)} type={field === "price" ? "number" : "text"} />
          </Field>
          <Field id="pc-reason" label="Reason">
            <Input id="pc-reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
          </Field>
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
          <Button onClick={submit} loading={propose.isPending}>
            Propose
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
