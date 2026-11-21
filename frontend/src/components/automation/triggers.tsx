"use client";

import * as React from "react";
import { CalendarClock, Copy, KeyRound, Plus, Radio, Trash2, Webhook } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { TRIGGER_EVENTS } from "@/lib/automation/catalog";
import type { Trigger } from "@/lib/automation/graph";
import { errorMessage } from "@/lib/errors";
import { CRON_PRESETS, describeCron, validateCron } from "@/lib/pipelines/cron";
import { useCreateTrigger, useDeleteTrigger, useTriggers, type TriggerCreated } from "@/lib/queries/automation";
import { formatRelative } from "@/lib/utils";
import { ModuleError, NativeSelect } from "@/components/mdm/common";

const ICON: Record<string, React.ReactNode> = { schedule: <CalendarClock />, webhook: <Webhook />, event: <Radio /> };

export function hookUrl(t: { id: string; url: string | null }): string {
  if (t.url) return t.url.startsWith("http") ? t.url : `${typeof window !== "undefined" ? window.location.origin : ""}${t.url}`;
  return `${typeof window !== "undefined" ? window.location.origin : ""}/api/v1/hooks/${t.id}`;
}

export function describeTrigger(t: Pick<Trigger, "type" | "config">): string {
  const c = t.config;
  if (t.type === "schedule" && typeof c.cron === "string") return describeCron(c.cron, typeof c.timezone === "string" ? c.timezone : undefined);
  if (t.type === "event") return TRIGGER_EVENTS.find((e) => e.value === c.event)?.label ?? String(c.event ?? "Event");
  if (t.type === "webhook") return "HTTP POST signed with HMAC-SHA256";
  return "Manual";
}

export function TriggersPanel({ workflowId, canManage }: { workflowId: string; canManage: boolean }) {
  const { slug } = useOrgContext();
  const triggers = useTriggers(slug, workflowId);
  const del = useDeleteTrigger(slug, workflowId);
  const [open, setOpen] = React.useState(false);
  const [pending, setPending] = React.useState<Trigger | null>(null);
  const [created, setCreated] = React.useState<TriggerCreated | null>(null);
  return (
    <Card>
      <CardHeader
        title="Triggers"
        description="Triggers start the published version. Webhook calls need no session: they are verified by signature and timestamp."
        actions={
          canManage && (
            <Button size="sm" onClick={() => setOpen(true)}>
              <Plus /> Add trigger
            </Button>
          )
        }
      />
      <CardContent>
        {triggers.isError ? (
          <ModuleError compact error={triggers.error} what="Triggers" onRetry={() => void triggers.refetch()} />
        ) : triggers.isLoading ? (
          <Skeleton className="h-24" />
        ) : !(triggers.data ?? []).length ? (
          <EmptyState compact icon={<Radio />} title="No triggers" description="Without a trigger the workflow only runs when someone clicks Run." />
        ) : (
          <ul className="divide-y divide-border">
            {triggers.data!.map((t) => (
              <li key={t.id} className="flex items-start justify-between gap-3 py-2.5">
                <div className="flex min-w-0 items-start gap-2.5">
                  <span className="mt-0.5 text-fg-muted [&_svg]:size-4">{ICON[t.type] ?? <Radio />}</span>
                  <div className="min-w-0">
                    <p className="flex items-center gap-2 text-sm font-medium">
                      {t.type[0].toUpperCase() + t.type.slice(1)}
                      {!t.is_active && <Badge tone="neutral">Paused</Badge>}
                    </p>
                    <p className="text-xs text-fg-muted">{describeTrigger(t)}</p>
                    {t.type === "webhook" && <code className="mt-1 block truncate text-[11px] text-fg-subtle">{hookUrl(t)}</code>}
                    {t.last_fired_at && <p className="text-[11px] text-fg-subtle">Last fired {formatRelative(t.last_fired_at)}</p>}
                  </div>
                </div>
                {canManage && (
                  <Button variant="ghost" size="icon-sm" aria-label="Remove trigger" onClick={() => setPending(t)}>
                    <Trash2 />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
      <AddTriggerDialog open={open} onOpenChange={setOpen} workflowId={workflowId} onCreated={(c) => c.secret && setCreated(c)} />
      <SecretDialog created={created} onClose={() => setCreated(null)} />
      <ConfirmDialog
        open={!!pending}
        onOpenChange={(o) => !o && setPending(null)}
        title="Remove this trigger?"
        description={pending?.type === "webhook" ? "Calls signed with its secret will be rejected from now on." : undefined}
        confirmLabel="Remove"
        loading={del.isPending}
        onConfirm={async () => {
          if (!pending) return;
          try {
            await del.mutateAsync(pending.id);
            setPending(null);
            toast.success("Trigger removed");
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </Card>
  );
}

function AddTriggerDialog({ open, onOpenChange, workflowId, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; workflowId: string; onCreated: (c: TriggerCreated) => void }) {
  const { slug } = useOrgContext();
  const create = useCreateTrigger(slug, workflowId);
  const [type, setType] = React.useState<"schedule" | "webhook" | "event">("schedule");
  const [cron, setCron] = React.useState("0 6 * * *");
  const [tz, setTz] = React.useState(() => (typeof Intl !== "undefined" ? Intl.DateTimeFormat().resolvedOptions().timeZone : "UTC"));
  const [event, setEvent] = React.useState(TRIGGER_EVENTS[0].value);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => setError(null), [open, type]);
  const cronError = type === "schedule" ? validateCron(cron) : null;
  const submit = async () => {
    if (cronError) return setError(cronError);
    const config = type === "schedule" ? { cron: cron.trim(), timezone: tz } : type === "event" ? { event } : {};
    try {
      const res = await create.mutateAsync({ type, config });
      toast.success("Trigger added");
      onOpenChange(false);
      onCreated(res);
    } catch (e) {
      setError(errorMessage(e));
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader title="Add a trigger" />
        <DialogBody className="space-y-3">
          <Field id="tr-type" label="Type">
            <NativeSelect id="tr-type" value={type} onChange={(e) => setType(e.target.value as typeof type)}>
              <option value="schedule">Schedule (cron)</option>
              <option value="webhook">Webhook</option>
              <option value="event">Platform event</option>
            </NativeSelect>
          </Field>
          {type === "schedule" && (
            <>
              <Field id="tr-preset" label="Preset">
                <NativeSelect id="tr-preset" value={CRON_PRESETS.some((p) => p.value === cron) ? cron : ""} onChange={(e) => e.target.value && setCron(e.target.value)}>
                  <option value="">Custom</option>
                  {CRON_PRESETS.map((p) => (
                    <option key={p.value} value={p.value}>
                      {p.label}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
              <Field id="tr-cron" label="Cron expression" hint={cronError ?? describeCron(cron, tz)} error={cronError ?? undefined}>
                <Input id="tr-cron" value={cron} onChange={(e) => setCron(e.target.value)} className="font-mono" />
              </Field>
              <Field id="tr-tz" label="Time zone">
                <Input id="tr-tz" value={tz} onChange={(e) => setTz(e.target.value)} />
              </Field>
            </>
          )}
          {type === "event" && (
            <Field id="tr-event" label="Event">
              <NativeSelect id="tr-event" value={event} onChange={(e) => setEvent(e.target.value)}>
                {TRIGGER_EVENTS.map((e) => (
                  <option key={e.value} value={e.value}>
                    {e.label}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          )}
          {type === "webhook" && <p className="text-sm text-fg-muted">A signing secret is generated and shown once. Callers sign the raw body with HMAC-SHA256 and send a timestamp; requests older than 5 minutes are rejected.</p>}
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
            Add trigger
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function SecretDialog({ created, onClose }: { created: TriggerCreated | null; onClose: () => void }) {
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Copied");
    } catch {
      toast.error("Copy failed; select the text instead.");
    }
  };
  if (!created) return null;
  const url = hookUrl({ id: created.id, url: created.url });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader title="Webhook secret" description="Copy it now. It is stored hashed and can't be shown again; to rotate it, remove the trigger and add a new one." />
        <DialogBody className="space-y-3">
          <Field id="wh-url" label="Endpoint">
            <div className="flex gap-2">
              <Input id="wh-url" readOnly value={url} className="font-mono text-xs" />
              <Button variant="secondary" size="icon" aria-label="Copy URL" onClick={() => void copy(url)}>
                <Copy />
              </Button>
            </div>
          </Field>
          <Field id="wh-secret" label="Signing secret">
            <div className="flex gap-2">
              <Input id="wh-secret" readOnly value={created.secret ?? ""} className="font-mono text-xs" />
              <Button variant="secondary" size="icon" aria-label="Copy secret" onClick={() => void copy(created.secret ?? "")}>
                <Copy />
              </Button>
            </div>
          </Field>
          <p className="flex items-start gap-2 text-xs text-fg-muted">
            <KeyRound className="mt-0.5 size-3.5 shrink-0" />
            <span>
              Sign the raw request body with HMAC-SHA256 using this secret
              {created.signature_header ? ` and send it in the ${created.signature_header} header` : ""}
              {created.timestamp_header ? `, with the Unix time in ${created.timestamp_header}` : ", with the request timestamp"}. Requests older than 5 minutes are rejected.
            </span>
          </p>
        </DialogBody>
        <DialogFooter>
          <Button onClick={onClose}>I saved it</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
