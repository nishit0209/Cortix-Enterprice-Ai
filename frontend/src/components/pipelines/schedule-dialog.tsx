"use client";

import * as React from "react";
import { CalendarClock, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/checkbox";
import { CRON_PRESETS, describeCron, validateCron } from "@/lib/pipelines/cron";
import { errorMessage } from "@/lib/errors";
import { useSaveSchedule } from "@/lib/queries/pipelines";
import type { PipelineSchedule } from "@/lib/types";
import { formatDateTime } from "@/lib/utils";

const CUSTOM = "__custom__";
const TIMEZONES = ["Asia/Kolkata", "UTC", "Europe/London", "Europe/Berlin", "America/New_York", "America/Los_Angeles", "Asia/Singapore", "Australia/Sydney"];

/** Schedule editor: cron presets + custom expression with live validation and a plain-language preview. */
export function ScheduleDialog({
  org,
  pipelineId,
  schedule,
  open,
  onOpenChange,
}: {
  org: string;
  pipelineId: string;
  schedule: PipelineSchedule | null;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const save = useSaveSchedule(org, pipelineId);
  const [cron, setCron] = React.useState(schedule?.cron ?? CRON_PRESETS[1].value);
  const [tz, setTz] = React.useState(schedule?.timezone ?? "Asia/Kolkata");
  const [active, setActive] = React.useState(schedule?.is_active ?? true);
  const [mode, setMode] = React.useState<string>(CRON_PRESETS.some((p) => p.value === schedule?.cron) || !schedule ? (schedule?.cron ?? CRON_PRESETS[1].value) : CUSTOM);
  React.useEffect(() => {
    if (!open) return;
    setCron(schedule?.cron ?? CRON_PRESETS[1].value);
    setTz(schedule?.timezone ?? "Asia/Kolkata");
    setActive(schedule?.is_active ?? true);
    setMode(!schedule || CRON_PRESETS.some((p) => p.value === schedule.cron) ? (schedule?.cron ?? CRON_PRESETS[1].value) : CUSTOM);
  }, [open, schedule]);
  const error = validateCron(cron);
  const timezones = TIMEZONES.includes(tz) ? TIMEZONES : [tz, ...TIMEZONES];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader icon={<CalendarClock />} title="Schedule" description="Runs are queued by the scheduler; a tick that arrives while a run is active is skipped." />
        <DialogBody className="space-y-4">
          <div className="flex flex-col gap-1.5">
            <span className="text-sm font-medium text-fg">Frequency</span>
            <Select
              value={mode}
              onValueChange={(v) => {
                setMode(v);
                if (v !== CUSTOM) setCron(v);
              }}
            >
              <SelectTrigger aria-label="Frequency preset">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CRON_PRESETS.map((p) => (
                  <SelectItem key={p.value} value={p.value} description={<span className="font-mono">{p.value}</span>}>
                    {p.label}
                  </SelectItem>
                ))}
                <SelectItem value={CUSTOM}>Custom cron expression…</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {mode === CUSTOM && (
            <Field id="cron" label="Cron expression" error={error ?? undefined} hint="minute hour day-of-month month day-of-week">
              <Input value={cron} onChange={(e) => setCron(e.target.value)} className="font-mono" spellCheck={false} />
            </Field>
          )}
          <div className="flex flex-col gap-1.5">
            <span className="text-sm font-medium text-fg">Time zone</span>
            <Select value={tz} onValueChange={setTz}>
              <SelectTrigger aria-label="Time zone">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {timezones.map((t) => (
                  <SelectItem key={t} value={t}>
                    {t}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <label className="flex items-center justify-between gap-3 rounded-control border border-border px-3 py-2.5 text-sm">
            <span>
              <span className="font-medium text-fg">Active</span>
              <span className="block text-xs text-fg-muted">Pause without losing the schedule.</span>
            </span>
            <Switch checked={active} onCheckedChange={setActive} aria-label="Schedule active" />
          </label>
          <div className="rounded-control bg-accent-soft/60 px-3 py-2 text-sm text-accent-soft-fg" aria-live="polite">
            {error ? "Enter a valid cron expression." : describeCron(cron, tz)}
            {schedule?.next_run_at && <span className="block text-xs opacity-80">Next run (current schedule): {formatDateTime(schedule.next_run_at)}</span>}
          </div>
        </DialogBody>
        <DialogFooter className="sm:justify-between">
          {schedule ? (
            <Button
              variant="danger-ghost"
              loading={save.isPending && save.variables === null}
              onClick={async () => {
                try {
                  await save.mutateAsync(null);
                  toast.success("Schedule removed");
                  onOpenChange(false);
                } catch (e) {
                  toast.error(errorMessage(e));
                }
              }}
            >
              <Trash2 /> Remove schedule
            </Button>
          ) : (
            <span />
          )}
          <div className="flex gap-2">
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              disabled={!!error}
              loading={save.isPending && save.variables !== null}
              onClick={async () => {
                try {
                  await save.mutateAsync({ cron: cron.trim(), timezone: tz, is_active: active });
                  toast.success(`Scheduled: ${describeCron(cron, tz)}`);
                  onOpenChange(false);
                } catch (e) {
                  toast.error(errorMessage(e));
                }
              }}
            >
              Save schedule
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
