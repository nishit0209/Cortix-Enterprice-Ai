"use client";

import * as React from "react";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import { FileText, RotateCw, Upload } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { Dropzone } from "@/components/ui/dropzone";
import { Progress } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { JobProgress } from "@/components/jobs/job-progress";
import { CLASSIFICATIONS, attestationText, needsAttestation } from "@/components/datasets/classification";
import { describeError } from "@/lib/errors";
import { docKeys, uploadDocument } from "@/lib/queries/documents";
import type { Classification } from "@/lib/types";
import { formatBytes } from "@/lib/utils";

const ACCEPT = [".pdf", ".docx", ".txt", ".md", ".html", ".png", ".jpg", ".jpeg", ".tiff"];
const MAX_MB = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB ?? 200);

type Phase = { kind: "select" } | { kind: "uploading"; percent: number } | { kind: "processing"; jobId: string | null; docId: string | null } | { kind: "failed"; message: string };

/** Drag-drop document upload → extraction job progress (extract → OCR → chunk → embed). */
export function DocumentUploadDialog({ org, open, onOpenChange }: { org: string; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const [file, setFile] = React.useState<File | null>(null);
  const [title, setTitle] = React.useState("");
  const [classification, setClassification] = React.useState<Classification>("internal");
  const [attested, setAttested] = React.useState(false);
  const [phase, setPhase] = React.useState<Phase>({ kind: "select" });
  const [fileError, setFileError] = React.useState<string | null>(null);
  const abort = React.useRef<AbortController | null>(null);

  React.useEffect(() => {
    if (!open) {
      abort.current?.abort();
      setFile(null);
      setTitle("");
      setClassification("internal");
      setAttested(false);
      setPhase({ kind: "select" });
      setFileError(null);
    }
  }, [open]);

  const needs = needsAttestation(classification);
  const start = async () => {
    if (!file) return;
    const ctrl = new AbortController();
    abort.current = ctrl;
    setPhase({ kind: "uploading", percent: 0 });
    try {
      const res = await uploadDocument(org, file, { title: title.trim() || undefined, classification, attest: needs ? attested : undefined }, (p) => setPhase({ kind: "uploading", percent: p.percent }), ctrl.signal);
      void qc.invalidateQueries({ queryKey: docKeys.list(org) });
      setPhase({ kind: "processing", jobId: res.job_id ?? null, docId: res.document?.id ?? res.id ?? null });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      const d = describeError(e);
      setPhase({ kind: "failed", message: `${d.title}: ${d.description}` });
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader icon={<FileText />} title="Upload document" description="PDF, Word, text or scanned images. Text is extracted (OCR for scans), split into passages and embedded locally." />
        <DialogBody className="space-y-4">
          {phase.kind === "select" || phase.kind === "failed" ? (
            <>
              {file ? (
                <div className="flex items-center gap-3 rounded-card border border-border bg-surface-2/60 px-4 py-3">
                  <FileText className="size-5 text-accent" aria-hidden />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-fg">{file.name}</div>
                    <div className="text-xs text-fg-muted">{formatBytes(file.size)}</div>
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => setFile(null)}>
                    Change
                  </Button>
                </div>
              ) : (
                <Dropzone
                  accept={ACCEPT}
                  maxBytes={MAX_MB * 1024 * 1024}
                  onFile={(f) => {
                    setFile(f);
                    setFileError(null);
                    setTitle(f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " "));
                  }}
                  onReject={setFileError}
                  hint={`${ACCEPT.join(" ")} · up to ${MAX_MB} MB`}
                />
              )}
              {fileError && <p className="text-sm text-danger">{fileError}</p>}
              <Field id="doc-title" label="Title">
                <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
              </Field>
              <div className="flex flex-col gap-1.5">
                <span className="text-sm font-medium text-fg">Classification</span>
                <Select value={classification} onValueChange={(v) => { setClassification(v as Classification); setAttested(false); }}>
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
                <p className="text-xs text-fg-muted">Under synthetic_only, only synthetic or public documents get generative answers; others are answered extractively.</p>
              </div>
              {needs && (
                <label className="flex items-start gap-2.5 rounded-control border border-border bg-surface-2/60 p-3 text-sm text-fg-muted">
                  <Checkbox checked={attested} onCheckedChange={(v) => setAttested(!!v)} aria-label="Attest classification" className="mt-0.5" />
                  <span>{attestationText(classification).replace("dataset", "document")}</span>
                </label>
              )}
              {phase.kind === "failed" && <p role="alert" className="rounded-control bg-danger-soft px-3 py-2 text-sm text-danger">{phase.message}</p>}
            </>
          ) : phase.kind === "uploading" ? (
            <div className="space-y-2 py-4">
              <div className="flex justify-between text-sm">
                <span className="text-fg">Uploading {file?.name}</span>
                <span className="tabular text-fg-muted">{phase.percent}%</span>
              </div>
              <Progress value={phase.percent} label="Upload progress" />
            </div>
          ) : phase.jobId ? (
            <JobProgress org={org} jobId={phase.jobId} title="Processing document" onDone={() => void qc.invalidateQueries({ queryKey: docKeys.list(org) })} />
          ) : (
            <p className="text-sm text-fg-muted">Uploaded. Processing continues in the background; the library updates automatically.</p>
          )}
        </DialogBody>
        <DialogFooter>
          {phase.kind === "processing" ? (
            <>
              {phase.docId && (
                <Button variant="secondary" asChild>
                  <Link href={`/o/${org}/documents/${phase.docId}`}>Open document</Link>
                </Button>
              )}
              <Button onClick={() => onOpenChange(false)}>Done</Button>
            </>
          ) : phase.kind === "uploading" ? (
            <Button variant="secondary" onClick={() => { abort.current?.abort(); setPhase({ kind: "select" }); toast.message("Upload cancelled"); }}>
              Cancel upload
            </Button>
          ) : (
            <>
              <Button variant="secondary" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button onClick={() => void start()} disabled={!file || (needs && !attested)}>
                {phase.kind === "failed" ? <RotateCw /> : <Upload />} {phase.kind === "failed" ? "Retry" : "Upload"}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
