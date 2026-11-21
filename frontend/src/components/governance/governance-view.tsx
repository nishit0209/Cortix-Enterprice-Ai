"use client";

import * as React from "react";
import { Database, Eye, History, Lock, ScanEye, Timer, UserCog } from "lucide-react";
import { useOrgContext } from "@/components/shell/org-context";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ClassificationBadge, FeatureStatusBadge } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useDatasetColumns, useDatasets } from "@/lib/queries/datasets";
import { useMembers, useRoles } from "@/lib/queries/org";
import { AclSection, ColumnPoliciesSection, RowPoliciesSection, type Directory } from "./dataset-policies";
import { PolicyPreviewPanel, UserAttributesPanel } from "./people-panels";
import { AccessHistoryPanel, RetentionPanel } from "./retention-history";

export function GovernanceView() {
  const { slug, can } = useOrgContext();
  const canPolicy = can("governance.policy.edit");
  const canRetention = can("governance.retention.edit");
  const canAudit = can("audit.view");
  const first = canPolicy ? "datasets" : canRetention ? "retention" : canAudit ? "history" : "none";
  const [tab, setTab] = React.useState(first);
  const datasets = useDatasets(slug);
  const [datasetId, setDatasetId] = React.useState<string | null>(null);
  const members = useMembers(slug);
  const roles = useRoles(slug);
  const columns = useDatasetColumns(slug, datasetId ?? "", !!datasetId && canPolicy);
  const [knownAttributes, setKnownAttributes] = React.useState<string[]>([]);
  const addKeys = React.useCallback(
    (keys: string[]) =>
      setKnownAttributes((k) => {
        const next = Array.from(new Set([...k, ...keys]));
        return next.length === k.length ? k : next;
      }),
    [],
  );
  const live = React.useMemo(() => (datasets.data ?? []).filter((d) => !["deleted", "archived"].includes(String(d.status))), [datasets.data]);
  const dataset = live.find((d) => d.id === datasetId);

  React.useEffect(() => {
    if (!datasetId && live.length) setDatasetId(live[0].id);
  }, [datasetId, live]);

  const dir: Directory = {
    roles: roles.data ?? [],
    members: (members.data ?? []).filter((m) => m.status !== "removed"),
    columns: (columns.data ?? []).map((c) => ({ name: c.name, dtype: c.data_type })),
    attributes: knownAttributes,
  };

  if (first === "none") {
    return (
      <div className="space-y-6">
        <PageHeader title="Access policies" badge={<FeatureStatusBadge status="prototype" />} />
        <Card>
          <EmptyState icon={<Lock />} title="You can't manage access policies" description="This needs governance.policy.edit, governance.retention.edit or audit.view. Ask an owner or admin." />
        </Card>
      </div>
    );
  }

  const datasetPicker = (
    <div className="flex flex-wrap items-center gap-3">
      <Select value={datasetId ?? undefined} onValueChange={setDatasetId}>
        <SelectTrigger className="w-72" aria-label="Dataset">
          <SelectValue placeholder={datasets.isLoading ? "Loading datasets…" : "Choose a dataset"} />
        </SelectTrigger>
        <SelectContent>
          {live.map((d) => (
            <SelectItem key={d.id} value={d.id} description={`${d.column_count ?? "?"} columns · ${d.classification}`}>
              {d.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {dataset && <ClassificationBadge value={dataset.classification} />}
    </div>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Access policies"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Who may use each dataset, which columns they see masked or not at all, and which rows. Every query, preview, export, ML feature and AI request goes through these rules."
      />
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList aria-label="Governance sections">
          {canPolicy && (
            <TabsTrigger value="datasets">
              <Database /> Dataset policies
            </TabsTrigger>
          )}
          {canPolicy && (
            <TabsTrigger value="preview">
              <ScanEye /> Preview as user
            </TabsTrigger>
          )}
          {canPolicy && (
            <TabsTrigger value="attributes">
              <UserCog /> User attributes
            </TabsTrigger>
          )}
          {canRetention && (
            <TabsTrigger value="retention">
              <Timer /> Retention
            </TabsTrigger>
          )}
          {canAudit && (
            <TabsTrigger value="history">
              <History /> Access history
            </TabsTrigger>
          )}
        </TabsList>
        {canPolicy && (
          <TabsContent value="datasets" className="space-y-5">
            {datasetPicker}
            {!datasets.isLoading && live.length === 0 ? (
              <Card>
                <EmptyState compact icon={<Database />} title="No datasets yet" description="Policies attach to datasets. Upload or import one first." />
              </Card>
            ) : datasetId ? (
              <>
                <AclSection key={`acl-${datasetId}`} datasetId={datasetId} dir={dir} />
                <ColumnPoliciesSection key={`col-${datasetId}`} datasetId={datasetId} dir={dir} />
                <RowPoliciesSection key={`row-${datasetId}`} datasetId={datasetId} dir={dir} />
              </>
            ) : null}
          </TabsContent>
        )}
        {canPolicy && (
          <TabsContent value="preview" className="space-y-5">
            <PolicyPreviewPanel datasets={live} dir={dir} initialDataset={datasetId} />
          </TabsContent>
        )}
        {canPolicy && (
          <TabsContent value="attributes">
            <UserAttributesPanel members={dir.members} onKeys={addKeys} />
          </TabsContent>
        )}
        {canRetention && (
          <TabsContent value="retention">
            <RetentionPanel />
          </TabsContent>
        )}
        {canAudit && (
          <TabsContent value="history">
            <AccessHistoryPanel datasets={live} members={dir.members} />
          </TabsContent>
        )}
      </Tabs>
      <p className="flex items-center gap-1.5 text-xs text-fg-subtle">
        <Eye className="size-3.5" /> Owners and admins always keep access. Policy changes are audited.
      </p>
    </div>
  );
}
