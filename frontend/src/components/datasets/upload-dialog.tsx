"use client";

import * as React from "react";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { CheckCircle2, FileSpreadsheet, Loader2, RotateCw, ShieldCheck, Upload, X, XCircle } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { Dropzone } from "@/components/ui/dropzone";
import { Field, Label } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import { Progress } from "@/components/ui/feedback";
import { Badge } from "@/components/ui/badge";
import { useJobStream } from "@/hooks/use-job-stream";
import { datasetKeys, uploadFile, useCreateDataset } from "@/lib/queries/datasets";
import { describeError } from "@/lib/errors";
import { isApiError } from "@/lib/api";
import { cn, formatBytes } from "@/lib/utils";
import type { Classification, Dataset } from "@/lib/types";
import { CLASSIFICATIONS, attestationText, needsAttestation } from "./classification";

const ACCEPT = [".csv", ".tsv", ".xlsx", ".json"];
const MAX_MB = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB ?? 200);

type Phase =
  | { kind: "select" }
  | { kind: "uploading"; percent: number }
  | { kind: "processing"; jobId: string; dataset: Dataset }
  | { kind: "failed"; message: string; code?: string };

function defaultName(file: File) {
  return file.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim().slice(0, 120);
}

export function UploadDatasetDialog({
  org,
  open,
  onOpenChange,
}: {
  org: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const qc = useQueryClient();
  const createDataset = useCreateDataset(org);
  const [file, setFile] = React.useState<File | null>(null);
  const [name, setName] = React.useState("");
  const [classification, setClassification] = React.useState<Classification>("internal");
  const [attested, setAttested] = React.useState(false);
  const [phase, setPhase] = React.useState<Phase>({ kind: "select" });
  const [fileError, setFileError] = React.useState<string | null>(null);
  const [nameError, setNameError] = React.useState<string | null>(null);
  const abortRef = React.useRef<AbortController | null>(null);
  const reduce = useReducedMotion();

  const reset = React.useCallback(() => {
    setFile(null);
    setName("");
    setClassification("internal");
    setAttested(false);
    setPhase({ kind: "select" });
    setFileError(null);
    setNameError(null);
  }, []);

  const handleOpenChange = (o: boolean) => {
    if (!o) {
      if (phase.kind === "uploading") abortRef.current?.abort();
      if (phase.kind === "processing" && !job.done) {
        toast.message("Processing continues in the background", {
          description: "The dataset list updates when profiling finishes.",
        });
      }
      reset();
    }
    onOpenChange(o);
  };

  const jobId = phase.kind === "processing" ? phase.jobId : null;
  const job = useJobStream(org, jobId, {
    onDone: (s) => {
      void qc.invalidateQueries({ queryKey: datasetKeys.list(org) });
      void qc.invalidateQueries({ queryKey: ["org", org, "data-stats"] });
      if (phase.kind === "processing") void qc.invalidateQueries({ queryKey: datasetKeys.detail(org, phase.dataset.id) });
      if (s.status === "succeeded") toast.success("Dataset ready", { description: name || undefined });
    },
  });

  const needsAttest = needsAttestation(classification);
  const canSubmit = !!file && name.trim().length > 0 && (!needsAttest || attested) && phase.kind === "select";

  const submit = async () => {
    if (!file) return;
    if (!name.trim()) {
      setNameError("Give the dataset a name");
      return;
    }
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setPhase({ kind: "uploading", percent: 0 });
    try {
      const upload = await uploadFile(org, file, (p) => setPhase({ kind: "uploading", percent: p.percent }), ctrl.signal);
      const body: Record<string, unknown> = { name: name.trim(), upload_id: upload.id, classification };
      // The API records the attestation for synthetic/public (extra field is ignored if unsupported).
      if (needsAttest) body.attest = true;
      const res = await createDataset.mutateAsync(body as { name: string; upload_id: string; classification: Classification });
      setPhase({ kind: "processing", jobId: res.job_id, dataset: res.dataset });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (isApiError(e) && e.errors.some((x) => x.field === "name")) {
        setNameError(e.errors.find((x) => x.field === "name")!.message);
        setPhase({ kind: "select" });
        return;
      }
      const d = describeError(e);
      setPhase({ kind: "failed", message: d.description, code: d.code });
    }
  };

  const processingDone = phase.kind === "processing" && job.done;
  const processingFailed = phase.kind === "processing" && (job.status === "failed" || job.status === "cancelled");

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent size="md">
        <DialogHeader
          icon={<Upload />}
          title="Upload dataset"
          description="Cortix ingests, profiles and scores quality automatically."
        />
        <DialogBody className="space-y-5">
          <AnimatePresence mode="wait" initial={false}>
            {phase.kind === "select" && (
              <motion.div
                key="select"
                initial={reduce ? false : { opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={reduce ? undefined : { opacity: 0, y: -6 }}
                transition={{ duration: 0.16 }}
                className="space-y-5"
              >
                {file ? (
                  <div className="flex items-center gap-3 rounded-card border border-border bg-surface-2/60 px-3.5 py-3">
                    <span className="flex size-9 items-center justify-center rounded-control bg-teal-soft text-teal">
                      <FileSpreadsheet className="size-4.5" aria-hidden />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-fg">{file.name}</div>
                      <div className="text-xs text-fg-muted">{formatBytes(file.size)}</div>
                    </div>
                    <Button variant="ghost" size="icon-sm" onClick={() => setFile(null)} aria-label="Remove file">
                      <X />
                    </Button>
                  </div>
                ) : (
                  <Dropzone
                    accept={ACCEPT}
                    maxBytes={MAX_MB * 1024 * 1024}
                    hint={`CSV, TSV, XLSX or JSON · up to ${MAX_MB} MB`}
                    onFile={(f) => {
                      setFile(f);
                      setFileError(null);
                      if (!name) setName(defaultName(f));
                    }}
                    onReject={setFileError}
                  />
                )}
                {fileError && (
                  <p role="alert" className="text-xs text-danger">
                    {fileError}
                  </p>
                )}
                <Field id="dataset-name" label="Dataset name" error={nameError ?? undefined}>
                  <Input
                    value={name}
                    maxLength={120}
                    placeholder="Sales orders 2026"
                    onChange={(e) => {
                      setName(e.target.value);
                      setNameError(null);
                    }}
                  />
                </Field>
                <div className="space-y-1.5">
                  <Label htmlFor="dataset-classification">Classification</Label>
                  <Select
                    value={classification}
                    onValueChange={(v) => {
                      setClassification(v as Classification);
                      setAttested(false);
                    }}
                  >
                    <SelectTrigger id="dataset-classification">
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
                  <p className="flex items-center gap-1.5 text-xs text-fg-muted">
                    <ShieldCheck className="size-3.5 text-teal" aria-hidden />
                    {needsAttest
                      ? "Eligible for Gemini under every AI data policy."
                      : "Not sent to Gemini under the default synthetic_only policy."}
                  </p>
                </div>
                {needsAttest && (
                  <label className="flex cursor-pointer gap-3 rounded-card border border-border bg-surface-2/60 p-3.5 text-sm">
                    <Checkbox
                      checked={attested}
                      onCheckedChange={(v) => setAttested(v === true)}
                      className="mt-0.5"
                      aria-describedby="attest-text"
                    />
                    <span id="attest-text" className="text-fg-muted">
                      {attestationText(classification)} The attestation is recorded in the audit log.
                    </span>
                  </label>
                )}
              </motion.div>
            )}

            {phase.kind === "uploading" && (
              <motion.div key="uploading" initial={reduce ? false : { opacity: 0 }} animate={{ opacity: 1 }} className="space-y-3 py-4">
                <div className="flex items-center justify-between text-sm">
                  <span className="flex items-center gap-2 font-medium text-fg">
                    <Loader2 className="size-4 animate-spin text-accent" aria-hidden /> Uploading {file?.name}
                  </span>
                  <span className="tabular text-fg-muted">{phase.percent}%</span>
                </div>
                <Progress value={phase.percent} label="Upload progress" />
                <p className="text-xs text-fg-muted">Streaming securely to object storage…</p>
              </motion.div>
            )}

            {phase.kind === "processing" && (
              <motion.div key="processing" initial={reduce ? false : { opacity: 0 }} animate={{ opacity: 1 }} className="space-y-4 py-2">
                <div className="flex items-center gap-3">
                  {processingFailed ? (
                    <XCircle className="size-5 text-danger" aria-hidden />
                  ) : processingDone ? (
                    <CheckCircle2 className="size-5 text-success" aria-hidden />
                  ) : (
                    <Loader2 className="size-5 animate-spin text-accent" aria-hidden />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-fg">{phase.dataset.name}</div>
                    <div className="text-xs text-fg-muted" aria-live="polite">
                      {processingFailed
                        ? job.error?.message ?? "Processing failed."
                        : processingDone
                          ? "Ingested, profiled and quality-scored."
                          : job.message ?? (job.status === "connecting" ? "Connecting to live progress…" : "Processing…")}
                    </div>
                  </div>
                  <Badge tone={processingFailed ? "danger" : processingDone ? "success" : "info"} dot pulse={!job.done}>
                    {processingFailed ? "Failed" : processingDone ? "Ready" : "Processing"}
                  </Badge>
                </div>
                <Progress
                  value={job.progress ?? 0}
                  indeterminate={job.progress === null && !job.done}
                  tone={processingFailed ? "danger" : processingDone ? "success" : "accent"}
                  label="Processing progress"
                />
                <Stages progress={job.progress} done={processingDone && !processingFailed} failed={processingFailed} />
                {job.logs.length > 0 && (
                  <div className="scrollbar-thin max-h-32 overflow-y-auto rounded-control border border-border bg-surface-2/60 p-2.5 font-mono text-[11px] leading-5">
                    {job.logs.map((l, i) => (
                      <div key={i} className={cn(l.level === "error" ? "text-danger" : l.level === "warning" ? "text-warning" : "text-fg-muted")}>
                        {l.step_key && <span className="text-fg-subtle">[{l.step_key}] </span>}
                        {l.message}
                      </div>
                    ))}
                  </div>
                )}
                {job.connection === "error" && !job.done && (
                  <p className="text-xs text-warning">
                    Live updates disconnected. Processing continues; the dataset list refreshes automatically.
                  </p>
                )}
              </motion.div>
            )}

            {phase.kind === "failed" && (
              <motion.div key="failed" initial={reduce ? false : { opacity: 0 }} animate={{ opacity: 1 }} className="flex gap-3 py-4" role="alert">
                <XCircle className="mt-0.5 size-5 shrink-0 text-danger" aria-hidden />
                <div className="space-y-1">
                  <div className="text-sm font-medium text-fg">Upload failed</div>
                  <p className="text-sm text-fg-muted">{phase.message}</p>
                  {phase.code && <p className="font-mono text-xs text-fg-subtle">{phase.code}</p>}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </DialogBody>
        <DialogFooter>
          {phase.kind === "select" && (
            <>
              <Button variant="secondary" onClick={() => handleOpenChange(false)}>
                Cancel
              </Button>
              <Button onClick={() => void submit()} disabled={!canSubmit}>
                <Upload /> Upload and process
              </Button>
            </>
          )}
          {phase.kind === "uploading" && (
            <Button variant="secondary" onClick={() => handleOpenChange(false)}>
              Cancel upload
            </Button>
          )}
          {phase.kind === "processing" && (
            <>
              <Button variant="secondary" onClick={() => handleOpenChange(false)}>
                {job.done ? "Close" : "Continue in background"}
              </Button>
              <Button asChild>
                <Link href={`/o/${org}/data/datasets/${phase.dataset.id}`} onClick={() => handleOpenChange(false)}>
                  Open dataset
                </Link>
              </Button>
            </>
          )}
          {phase.kind === "failed" && (
            <>
              <Button variant="secondary" onClick={() => handleOpenChange(false)}>
                Close
              </Button>
              <Button
                onClick={() => {
                  setPhase({ kind: "select" });
                  toast.dismiss();
                }}
              >
                <RotateCw /> Try again
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const STAGES = [
  { key: "ingest", label: "Ingest", until: 40 },
  { key: "profile", label: "Profile", until: 70 },
  { key: "quality", label: "Quality", until: 90 },
  { key: "warehouse", label: "Warehouse", until: 100 },
];

function Stages({ progress, done, failed }: { progress: number | null; done: boolean; failed: boolean }) {
  const p = done ? 100 : progress ?? 0;
  return (
    <ol className="grid grid-cols-4 gap-2" aria-label="Processing stages">
      {STAGES.map((s, i) => {
        const prev = i === 0 ? 0 : STAGES[i - 1].until;
        const state = p >= s.until ? "done" : p > prev ? "active" : "pending";
        return (
          <li key={s.key} className="space-y-1.5">
            <div
              className={cn(
                "h-1 rounded-full transition-colors duration-300",
                state === "done" ? "bg-success" : state === "active" ? (failed ? "bg-danger" : "bg-accent") : "bg-surface-3",
              )}
            />
            <span className={cn("text-[11px] font-medium", state === "pending" ? "text-fg-subtle" : "text-fg-muted")}>{s.label}</span>
          </li>
        );
      })}
    </ol>
  );
}
