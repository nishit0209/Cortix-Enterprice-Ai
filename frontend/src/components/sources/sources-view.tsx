"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Cable,
  CheckCircle2,
  ChevronRight,
  Columns3,
  Database,
  FolderTree,
  Import,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  ShieldCheck,
  Table2,
  Trash2,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { FeatureStatusBadge, StatusBadge } from "@/components/ui/status";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip } from "@/components/ui/overlays";
import { JobProgress } from "@/components/jobs/job-progress";
import { CLASSIFICATIONS, attestationText, needsAttestation } from "@/components/datasets/classification";
import { isApiError } from "@/lib/api";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import {
  deKeys,
  importTable,
  parseIntrospection,
  startSourceJob,
  testChecks,
  useSourceCatalog,
  useCreateDataSource,
  useDataSources,
  useDeleteDataSource,
} from "@/lib/queries/pipelines";
import { datasetKeys } from "@/lib/queries/datasets";
import type { Classification, DataSource, IntrospectSchema, IntrospectTable, SourceCheck } from "@/lib/types";
import { cn, formatDateTime, formatNumber, formatRelative } from "@/lib/utils";

const SSL_MODES = ["verify-full", "verify-ca", "require", "prefer"];

export function SourcesView() {
  const { slug, can } = useOrgContext();
  const q = useDataSources(slug);
  const [adding, setAdding] = React.useState(false);
  const [open, setOpen] = React.useState<DataSource | null>(null);
  const manage = can("sources.manage");

  const columns = React.useMemo<DataTableColumn<DataSource>[]>(
    () => [
      {
        id: "name",
        accessorKey: "name",
        header: "Name",
        cell: ({ row }) => (
          <span className="flex items-center gap-2.5">
            <span className="flex size-7 items-center justify-center rounded-control bg-info-soft text-info">
              <Database className="size-3.5" aria-hidden />
            </span>
            <span className="font-medium">{row.original.name}</span>
          </span>
        ),
      },
      {
        id: "endpoint",
        accessorFn: (r) => `${r.config?.host}:${r.config?.port}/${r.config?.dbname}`,
        header: "Endpoint",
        cell: ({ row }) => (
          <span className="font-mono text-xs text-fg-muted">
            {row.original.config?.host}:{row.original.config?.port}/{row.original.config?.dbname}
          </span>
        ),
        meta: { hideOnMobile: true },
      },
      { id: "status", accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
      {
        id: "readonly",
        accessorFn: (r) => (r.verified_readonly ? "yes" : "no"),
        header: "Read-only",
        cell: ({ row }) =>
          row.original.verified_readonly ? (
            <Badge tone="success">
              <ShieldCheck /> Verified
            </Badge>
          ) : (
            <Badge tone="outline">Not verified</Badge>
          ),
      },
      {
        id: "tested",
        accessorFn: (r) => r.last_tested_at ?? "",
        header: "Last test",
        cell: ({ row }) => <span className="text-fg-muted">{row.original.last_tested_at ? formatRelative(row.original.last_tested_at) : "Never"}</span>,
      },
    ],
    [],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Sources"
        badge={<FeatureStatusBadge status="prototype" />}
        description="Read-only PostgreSQL connections. Credentials are write-only and encrypted; every test checks the host and that the role cannot write."
        actions={
          manage ? (
            <Button onClick={() => setAdding(true)}>
              <Plus /> Add PostgreSQL source
            </Button>
          ) : undefined
        }
      />
      {q.isError && isUnavailableEndpoint(q.error) ? (
        <Card>
          <EmptyState icon={<Cable />} title="Sources aren't available yet" description="The connector service hasn't been deployed." />
        </Card>
      ) : (
        <DataTable
          caption="Data sources"
          columns={columns}
          data={q.data}
          loading={q.isLoading}
          error={q.isError ? q.error : undefined}
          onRetry={() => void q.refetch()}
          onRowClick={setOpen}
          getRowId={(r) => r.id}
          searchPlaceholder="Search sources…"
          emptyState={
            <EmptyState
              icon={<Cable />}
              title="No sources yet"
              description="Connect a PostgreSQL database with a read-only role, browse its tables and import them as datasets."
              action={
                manage ? (
                  <Button onClick={() => setAdding(true)}>
                    <Plus /> Add PostgreSQL source
                  </Button>
                ) : undefined
              }
            />
          }
        />
      )}
      <AddSourceDialog
        org={slug}
        open={adding}
        onOpenChange={setAdding}
        onCreated={(s) => {
          setAdding(false);
          setOpen(s);
        }}
      />
      <SourceDialog org={slug} source={open} onClose={() => setOpen(null)} canManage={manage} />
    </div>
  );
}

/* ---------------- add source ---------------- */

interface FormState {
  name: string;
  host: string;
  port: string;
  dbname: string;
  schema: string;
  sslmode: string;
  username: string;
  password: string;
}

const EMPTY: FormState = { name: "", host: "", port: "5432", dbname: "", schema: "public", sslmode: "verify-full", username: "", password: "" };

function validate(f: FormState): Partial<Record<keyof FormState, string>> {
  const e: Partial<Record<keyof FormState, string>> = {};
  if (!f.name.trim()) e.name = "Give the source a name.";
  if (!f.host.trim()) e.host = "Host is required.";
  else if (/[\s/]/.test(f.host)) e.host = "Enter a hostname or IP address only.";
  const port = Number(f.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) e.port = "Port must be 1–65535.";
  if (!f.dbname.trim()) e.dbname = "Database name is required.";
  if (!f.username.trim()) e.username = "Username is required.";
  if (!f.password) e.password = "Password is required.";
  return e;
}

function AddSourceDialog({
  org,
  open,
  onOpenChange,
  onCreated,
}: {
  org: string;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onCreated: (s: DataSource) => void;
}) {
  const [form, setForm] = React.useState<FormState>(EMPTY);
  const [errors, setErrors] = React.useState<Partial<Record<keyof FormState, string>>>({});
  const create = useCreateDataSource(org);
  React.useEffect(() => {
    if (!open) {
      setForm(EMPTY);
      setErrors({});
      create.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const set = (k: keyof FormState) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const v = validate(form);
    setErrors(v);
    if (Object.keys(v).length) return;
    try {
      const src = await create.mutateAsync({
        name: form.name.trim(),
        type: "postgresql",
        config: { host: form.host.trim(), port: Number(form.port), dbname: form.dbname.trim(), sslmode: form.sslmode, schema: form.schema.trim() || undefined },
        username: form.username.trim(),
        password: form.password,
      });
      toast.success("Source saved. Testing the connection…");
      onCreated(src);
    } catch (err) {
      if (isApiError(err) && err.errors.length) {
        const fe = err.fieldErrors();
        setErrors({
          name: fe.name,
          host: fe.host ?? fe["config.host"],
          port: fe.port ?? fe["config.port"],
          dbname: fe.dbname ?? fe["config.dbname"],
          username: fe.username,
          password: fe.password,
        });
      }
      toast.error(errorMessage(err));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader
          icon={<Database />}
          title="Add PostgreSQL source"
          description="Use a dedicated read-only role (for example nexora_ro). NEXORA verifies it cannot write before importing anything."
        />
        <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col" noValidate>
          <DialogBody className="grid gap-4 sm:grid-cols-6">
            <Field id="src-name" label="Name" error={errors.name} className="sm:col-span-6">
              <Input value={form.name} onChange={set("name")} placeholder="Orders replica" autoFocus />
            </Field>
            <Field id="src-host" label="Host" error={errors.host} className="sm:col-span-4">
              <Input value={form.host} onChange={set("host")} placeholder="db.example.internal" autoComplete="off" />
            </Field>
            <Field id="src-port" label="Port" error={errors.port} className="sm:col-span-2">
              <Input value={form.port} onChange={set("port")} inputMode="numeric" />
            </Field>
            <Field id="src-db" label="Database" error={errors.dbname} className="sm:col-span-3">
              <Input value={form.dbname} onChange={set("dbname")} placeholder="analytics" />
            </Field>
            <Field id="src-schema" label="Schema allow-list" hint="Only this schema is browsable." className="sm:col-span-3">
              <Input value={form.schema} onChange={set("schema")} placeholder="public" />
            </Field>
            <div className="flex flex-col gap-1.5 sm:col-span-2">
              <span className="text-sm font-medium text-fg">TLS</span>
              <Select value={form.sslmode} onValueChange={(v) => setForm((f) => ({ ...f, sslmode: v }))}>
                <SelectTrigger aria-label="SSL mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SSL_MODES.map((m) => (
                    <SelectItem key={m} value={m}>
                      {m}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {form.sslmode !== "verify-full" && <span className="text-xs text-warning">verify-full is recommended.</span>}
            </div>
            <Field id="src-user" label="Username" error={errors.username} className="sm:col-span-2">
              <Input value={form.username} onChange={set("username")} autoComplete="off" />
            </Field>
            <Field
              id="src-pass"
              label="Password"
              error={errors.password}
              className="sm:col-span-2"
              labelAside={
                <Tooltip content="Write-only: stored encrypted and never shown again.">
                  <span className="text-fg-subtle">
                    <Lock className="size-3.5" aria-label="write-only" />
                  </span>
                </Tooltip>
              }
            >
              <Input type="password" value={form.password} onChange={set("password")} autoComplete="new-password" />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending}>
              Save &amp; test connection
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/* ---------------- source detail: test + browse + import ---------------- */

const CHECK_LABELS: Record<string, string> = {
  dns: "DNS resolution & host allow-list",
  ssrf: "Host is not private/loopback",
  tls: "TLS handshake",
  login: "Login",
  privileges: "Read-only privileges",
  readonly: "Read-only privileges",
};

function SourceDialog({ org, source, onClose, canManage }: { org: string; source: DataSource | null; onClose: () => void; canManage: boolean }) {
  const qc = useQueryClient();
  const { can } = useOrgContext();
  const [tab, setTab] = React.useState("connection");
  const [testJob, setTestJob] = React.useState<string | null>(null);
  const [checks, setChecks] = React.useState<SourceCheck[] | null>(null);
  const [testError, setTestError] = React.useState<{ code: string; message?: string } | null>(null);
  const [introJob, setIntroJob] = React.useState<string | null>(null);
  const [schemas, setSchemas] = React.useState<IntrospectSchema[] | null>(null);
  const [importing, setImporting] = React.useState<IntrospectTable | null>(null);
  const [deleting, setDeleting] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const del = useDeleteDataSource(org);
  const autoTested = React.useRef<string | null>(null);
  const catalog = useSourceCatalog(org, source?.id ?? "", !!source);
  // Show the last stored catalog right away; "Refresh" re-runs introspection as a job.
  React.useEffect(() => {
    if (schemas === null && catalog.data && catalog.data.schemas.length > 0) setSchemas(catalog.data.schemas);
  }, [catalog.data, schemas]);

  const test = React.useCallback(async () => {
    if (!source) return;
    setBusy(true);
    setChecks(null);
    setTestError(null);
    try {
      const job = await startSourceJob(org, source.id, "test");
      setTestJob(job.job_id);
    } catch (e) {
      setTestError({ code: isApiError(e) ? e.code : "error", message: errorMessage(e) });
    } finally {
      setBusy(false);
    }
  }, [org, source]);

  React.useEffect(() => {
    setTab("connection");
    setTestJob(null);
    setChecks(testChecks(source?.last_test_result));
    setTestError(null);
    setIntroJob(null);
    setSchemas(null);
    // A freshly created source (never tested) is tested right away.
    if (source && canManage && !source.last_tested_at && autoTested.current !== source.id) {
      autoTested.current = source.id;
      void test();
    }
  }, [source, canManage, test]);

  const introspect = async () => {
    if (!source) return;
    setBusy(true);
    setSchemas(null);
    try {
      const job = await startSourceJob(org, source.id, "introspect");
      setIntroJob(job.job_id);
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={!!source} onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="xl" className="h-[min(720px,calc(100dvh-32px))]">
        {source && (
          <>
            <DialogHeader
              icon={<Database />}
              title={source.name}
              description={
                <span className="font-mono text-xs">
                  {source.username ? `${source.username}@` : ""}
                  {source.config.host}:{source.config.port}/{source.config.dbname} · {source.config.sslmode}
                </span>
              }
            />
            <DialogBody className="pt-0">
              <Tabs value={tab} onValueChange={setTab}>
                <TabsList>
                  <TabsTrigger value="connection">
                    <ShieldCheck /> Connection
                  </TabsTrigger>
                  <TabsTrigger value="browse">
                    <FolderTree /> Browse &amp; import
                  </TabsTrigger>
                </TabsList>
                <TabsContent value="connection" className="space-y-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge status={source.status} />
                    {source.verified_readonly ? (
                      <Badge tone="success">
                        <ShieldCheck /> Read-only verified
                      </Badge>
                    ) : (
                      <Badge tone="warning">Read-only not verified</Badge>
                    )}
                    <span className="text-xs text-fg-muted">Last tested {source.last_tested_at ? formatDateTime(source.last_tested_at) : "never"}</span>
                    {canManage && (
                      <Button variant="secondary" size="sm" className="ml-auto" onClick={() => void test()} loading={busy && !introJob}>
                        <RefreshCw /> Test connection
                      </Button>
                    )}
                  </div>
                  {testJob && (
                    <Card>
                      <CardContent className="pt-4">
                        <JobProgress
                          key={testJob}
                          org={org}
                          jobId={testJob}
                          title="Connection test"
                          onDone={(s) => {
                            const r = (s.result ?? {}) as { ok?: boolean };
                            const parsed = testChecks(s.result);
                            if (parsed) setChecks(parsed);
                            if (s.status !== "succeeded" || r.ok === false) setTestError(s.error ?? { code: "connection_test_failed" });
                            void qc.invalidateQueries({ queryKey: deKeys.sources(org) });
                          }}
                        />
                      </CardContent>
                    </Card>
                  )}
                  {checks && checks.length > 0 && (
                    <ul className="divide-y divide-border rounded-card border border-border" aria-label="Connection checks">
                      {checks.map((c) => (
                        <li key={c.key} className="flex items-start gap-3 px-4 py-2.5 text-sm">
                          {c.ok ? <CheckCircle2 className="mt-0.5 size-4 text-success" aria-label="passed" /> : <XCircle className="mt-0.5 size-4 text-danger" aria-label="failed" />}
                          <div>
                            <div className="font-medium text-fg">{CHECK_LABELS[c.key] ?? c.key}</div>
                            {c.message && <div className="text-xs text-fg-muted">{c.message}</div>}
                          </div>
                        </li>
                      ))}
                    </ul>
                  )}
                  {testError && <SourceErrorNote code={testError.code} message={testError.message} />}
                  {!testJob && !checks && !testError && (
                    <p className="text-sm text-fg-muted">
                      Tests resolve and pin the host (private addresses are refused), verify TLS, log in, and confirm the role has no write,
                      superuser or server-file privileges.
                    </p>
                  )}
                </TabsContent>
                <TabsContent value="browse" className="space-y-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="text-sm text-fg-muted">Introspect the allow-listed schema, then import tables as versioned datasets.</p>
                    {canManage && (
                      <Button variant="secondary" size="sm" className="ml-auto" onClick={() => void introspect()} loading={busy && tab === "browse"}>
                        <RefreshCw /> {schemas ? "Refresh" : "Introspect"}
                      </Button>
                    )}
                  </div>
                  {introJob && !schemas && (
                    <JobProgress
                      key={introJob}
                      org={org}
                      jobId={introJob}
                      title="Introspection"
                      showLogs={false}
                      onDone={(s) => {
                        if (s.status === "succeeded") setSchemas(parseIntrospection(s.result));
                        else toast.error(s.error?.message ?? "Introspection failed");
                      }}
                    />
                  )}
                  {schemas && <SchemaBrowser schemas={schemas} canImport={canManage && can("datasets.create")} onImport={setImporting} />}
                  {!introJob && !schemas && (
                    <EmptyState compact icon={<FolderTree />} title="Nothing browsed yet" description="Run an introspection to list schemas, tables and columns." />
                  )}
                </TabsContent>
              </Tabs>
            </DialogBody>
            <DialogFooter className="sm:justify-between">
              {canManage ? (
                <Button variant="danger-ghost" onClick={() => setDeleting(true)}>
                  <Trash2 /> Delete source
                </Button>
              ) : (
                <span />
              )}
              <Button variant="secondary" onClick={onClose}>
                Close
              </Button>
            </DialogFooter>
            <ImportDialog org={org} source={source} table={importing} onClose={() => setImporting(null)} />
            <ConfirmDialog
              open={deleting}
              onOpenChange={setDeleting}
              title={`Delete “${source.name}”?`}
              description="The connection and its stored credential are removed. Datasets already imported are kept."
              confirmLabel="Delete source"
              confirmText={source.name}
              loading={del.isPending}
              onConfirm={async () => {
                try {
                  await del.mutateAsync(source.id);
                  toast.success("Source deleted");
                  setDeleting(false);
                  onClose();
                } catch (e) {
                  toast.error(errorMessage(e));
                }
              }}
            />
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function SourceErrorNote({ code, message }: { code: string; message?: string }) {
  const copy: Record<string, string> = {
    connector_not_readonly: "The role can write (or is superuser / can read server files). Create a read-only role and update the credentials.",
    connector_host_forbidden: "The host resolves to a private, loopback or link-local address, which is blocked to prevent SSRF.",
  };
  return (
    <div role="alert" className="flex gap-3 rounded-card border border-danger/30 bg-danger-soft/60 px-4 py-3 text-sm">
      <XCircle className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
      <div>
        <div className="font-medium text-fg">Connection test failed</div>
        <p className="text-fg-muted">{copy[code] ?? message ?? "Check the host, credentials and TLS settings."}</p>
        <p className="mt-1 font-mono text-xs text-fg-subtle">{code}</p>
      </div>
    </div>
  );
}

function SchemaBrowser({
  schemas,
  canImport,
  onImport,
}: {
  schemas: IntrospectSchema[];
  canImport: boolean;
  onImport: (t: IntrospectTable) => void;
}) {
  const [schema, setSchema] = React.useState(schemas[0]?.name ?? null);
  const [table, setTable] = React.useState<IntrospectTable | null>(schemas[0]?.tables[0] ?? null);
  const current = schemas.find((s) => s.name === schema);
  if (schemas.length === 0) return <EmptyState compact icon={<FolderTree />} title="No schemas visible" description="The role can't see any tables in the allow-listed schema." />;
  return (
    <div className="grid min-h-[340px] gap-0 overflow-hidden rounded-card border border-border md:grid-cols-[180px_220px_minmax(0,1fr)]">
      <ul className="scrollbar-thin max-h-[360px] overflow-y-auto border-b border-border bg-surface-2/50 p-1.5 md:border-b-0 md:border-r" aria-label="Schemas">
        {schemas.map((s) => (
          <li key={s.name}>
            <button
              type="button"
              onClick={() => {
                setSchema(s.name);
                setTable(s.tables[0] ?? null);
              }}
              className={cn("flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-sm focus-ring", schema === s.name ? "bg-accent-soft text-accent-soft-fg" : "hover:bg-surface-3")}
            >
              <FolderTree className="size-3.5 shrink-0" aria-hidden />
              <span className="truncate">{s.name}</span>
              <span className="tabular ml-auto text-xs opacity-70">{s.tables.length}</span>
            </button>
          </li>
        ))}
      </ul>
      <ul className="scrollbar-thin max-h-[360px] overflow-y-auto border-b border-border p-1.5 md:border-b-0 md:border-r" aria-label="Tables">
        {(current?.tables ?? []).map((t) => (
          <li key={t.name}>
            <button
              type="button"
              onClick={() => setTable(t)}
              className={cn("flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-sm focus-ring", table?.name === t.name ? "bg-surface-2 text-fg" : "text-fg-muted hover:bg-surface-2")}
            >
              <Table2 className="size-3.5 shrink-0" aria-hidden />
              <span className="truncate">{t.name}</span>
              <ChevronRight className="ml-auto size-3.5 opacity-50" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
      <div className="min-w-0 p-3">
        {table ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-sm font-medium text-fg">
                {table.schema}.{table.name}
              </span>
              {table.row_estimate !== null && table.row_estimate !== undefined && (
                <Badge tone="outline">~{formatNumber(table.row_estimate, { compact: true })} rows</Badge>
              )}
              {canImport && (
                <Button size="sm" className="ml-auto" onClick={() => onImport(table)}>
                  <Import /> Import as dataset
                </Button>
              )}
            </div>
            <ul className="scrollbar-thin max-h-[280px] divide-y divide-border overflow-y-auto rounded-control border border-border" aria-label="Columns">
              {table.columns.map((c) => (
                <li key={c.name} className="flex items-center justify-between gap-3 px-3 py-1.5 text-sm">
                  <span className="flex items-center gap-2 font-mono text-xs text-fg">
                    <Columns3 className="size-3 text-fg-subtle" aria-hidden />
                    {c.name}
                  </span>
                  <span className="text-xs text-fg-subtle">
                    {c.type}
                    {c.nullable === false ? " · not null" : ""}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <EmptyState compact icon={<Table2 />} title="Pick a table" />
        )}
      </div>
    </div>
  );
}

function ImportDialog({ org, source, table, onClose }: { org: string; source: DataSource; table: IntrospectTable | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = React.useState("");
  const [classification, setClassification] = React.useState<Classification>("internal");
  const [attested, setAttested] = React.useState(false);
  const [jobId, setJobId] = React.useState<string | null>(null);
  const [datasetId, setDatasetId] = React.useState<string | null>(null);
  const [pending, setPending] = React.useState(false);
  const [done, setDone] = React.useState<"ok" | "failed" | null>(null);
  React.useEffect(() => {
    if (table) {
      setName(table.name.replace(/_/g, " "));
      setClassification("internal");
      setAttested(false);
      setJobId(null);
      setDatasetId(null);
      setDone(null);
    }
  }, [table]);
  const needs = needsAttestation(classification);

  const start = async () => {
    if (!table) return;
    setPending(true);
    try {
      const res = await importTable(org, source.id, {
        table: table.name,
        schema: table.schema,
        name: name.trim(),
        ...(classification !== "internal" ? { classification } : {}),
        ...(needs ? { attest: true } : {}),
      });
      setJobId(res.job_id);
      setDatasetId(res.import?.dataset_id ?? null);
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog open={!!table} onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="md">
        <DialogHeader icon={<Import />} title="Import table" description={table ? `${table.schema}.${table.name} → a new versioned dataset` : undefined} />
        <DialogBody className="space-y-4">
          {jobId ? (
            <JobProgress
              org={org}
              jobId={jobId}
              title="Import"
              onDone={(s) => {
                setDone(s.status === "succeeded" ? "ok" : "failed");
                const id = (s.result as { dataset_id?: string } | null)?.dataset_id;
                if (id) setDatasetId(id);
                void qc.invalidateQueries({ queryKey: datasetKeys.list(org) });
              }}
            />
          ) : (
            <>
              <Field id="imp-name" label="Dataset name">
                <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
              </Field>
              <div className="flex flex-col gap-1.5">
                <span className="text-sm font-medium text-fg">Classification</span>
                <Select value={classification} onValueChange={(v) => setClassification(v as Classification)}>
                  <SelectTrigger aria-label="Classification">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {CLASSIFICATIONS.map((c) => (
                      <SelectItem key={c.value} value={c.value} description={c.description}>
                        {c.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              {needs && (
                <label className="flex items-start gap-2.5 rounded-control border border-border bg-surface-2/60 p-3 text-sm text-fg-muted">
                  <Checkbox checked={attested} onCheckedChange={(v) => setAttested(!!v)} aria-label="Attest classification" className="mt-0.5" />
                  <span>{attestationText(classification)}</span>
                </label>
              )}
            </>
          )}
        </DialogBody>
        <DialogFooter>
          {jobId ? (
            <>
              {done === "ok" && datasetId && (
                <Button variant="secondary" asChild>
                  <Link href={`/o/${org}/data/datasets/${datasetId}`}>Open dataset</Link>
                </Button>
              )}
              <Button onClick={onClose}>{done ? "Close" : "Hide (keeps running)"}</Button>
            </>
          ) : (
            <>
              <Button variant="secondary" onClick={onClose}>
                Cancel
              </Button>
              <Button onClick={() => void start()} loading={pending} disabled={!name.trim() || (needs && !attested)}>
                {pending ? <Loader2 className="animate-spin" /> : <Import />} Import
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
