"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { GitBranch, Upload } from "lucide-react";
import { useOrgContext } from "@/components/shell/org-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { FeatureStatusBadge } from "@/components/ui/status";
import { useDatasets } from "@/lib/queries/datasets";
import { LineageGraphView } from "./lineage-graph";

export function LineageView() {
  const { slug, can } = useOrgContext();
  const datasets = useDatasets(slug);
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const datasetId = params.get("dataset") ?? datasets.data?.[0]?.id ?? null;
  const depth = Number(params.get("depth") ?? 3) || 3;
  const setParam = (k: string, v: string) => {
    const sp = new URLSearchParams(params.toString());
    sp.set(k, v);
    router.replace(`${pathname}?${sp}`, { scroll: false });
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Lineage"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Where data comes from and where it goes: uploads and sources, dataset versions, pipelines and what consumes them. Assets you can't see are collapsed."
        actions={
          <div className="flex flex-wrap gap-2">
            <Select value={datasetId ?? undefined} onValueChange={(v) => setParam("dataset", v)} disabled={!datasets.data?.length}>
              <SelectTrigger className="w-60" aria-label="Root dataset">
                <SelectValue placeholder="Choose a dataset" />
              </SelectTrigger>
              <SelectContent>
                {(datasets.data ?? []).map((d) => (
                  <SelectItem key={d.id} value={d.id}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={String(depth)} onValueChange={(v) => setParam("depth", v)}>
              <SelectTrigger className="w-32" aria-label="Depth">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {[1, 2, 3, 4, 5, 6].map((d) => (
                  <SelectItem key={d} value={String(d)}>
                    Depth {d}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        }
      />
      <Card>
        <CardContent className="pt-5">
          {datasets.isSuccess && (datasets.data ?? []).length === 0 ? (
            <EmptyState
              icon={<GitBranch />}
              title="Nothing to trace yet"
              description="Upload a dataset to start recording lineage."
              action={
                can("datasets.create") ? (
                  <Button size="sm" asChild>
                    <Link href={`/o/${slug}/data/datasets?upload=1`}>
                      <Upload /> Upload a dataset
                    </Link>
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <LineageGraphView org={slug} nodeType="dataset" nodeId={datasetId} depth={depth} height={560} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
