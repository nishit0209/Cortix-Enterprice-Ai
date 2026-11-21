"use client";

import * as React from "react";
import Link from "next/link";
import { Check, Inbox, KeyRound, ShieldAlert, X } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Pager } from "@/components/ui/pager";
import { StatusBadge } from "@/components/ui/status";
import { isApiError } from "@/lib/api";
import { approvalHref, type Approval } from "@/lib/automation/graph";
import { errorMessage } from "@/lib/errors";
import { displayValue } from "@/lib/mdm";
import { useApprovals, useDecideApproval } from "@/lib/queries/automation";
import { formatDateTime, formatRelative, titleCase } from "@/lib/utils";
import { ModuleError, NativeSelect } from "@/components/mdm/common";

const KIND_LABELS: Record<string, string> = {
  model_promotion: "Model promotion",
  "ml.promotion": "Model promotion",
  workflow_step: "Workflow step",
  agent_action: "Agent action",
  identity_match: "Identity match",
  product_correction: "Product correction",
  extraction_result: "Extraction review",
  policy_change: "Policy change",
};

export function ApprovalsInbox() {
  const { slug, can } = useOrgContext();
  const [status, setStatus] = React.useState("pending");
  const list = useApprovals(slug, status);
  const [deciding, setDeciding] = React.useState<{ a: Approval; decision: "approve" | "reject" } | null>(null);
  if (list.isError) return <Card><ModuleError error={list.error} what="Approvals" onRetry={() => void list.refetch()} /></Card>;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-fg-muted">Requests you can decide: ML promotions to production, workflow approval steps and agent actions. High-impact decisions ask for your password.</p>
        <NativeSelect value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Approval status" className="w-auto">
          <option value="pending">Pending</option>
          <option value="approved">Approved</option>
          <option value="rejected">Rejected</option>
          <option value="expired">Expired</option>
          <option value="">All</option>
        </NativeSelect>
      </div>
      {list.isLoading ? (
        <Skeleton className="h-40 rounded-card" />
      ) : !list.items?.length ? (
        <Card>
          <EmptyState icon={<Inbox />} title={status === "pending" ? "Nothing waiting for you" : "No approvals"} description="Requests assigned to a permission you hold show up here." />
        </Card>
      ) : (
        <ul className="space-y-2">
          {list.items.map((a) => {
            const href = approvalHref(a, slug);
            const details = Object.entries(a.payload).filter(([k, v]) => k !== "title" && v !== null && typeof v !== "object");
            return (
              <li key={a.id}>
                <Card className="p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">{a.title}</span>
                        <Badge tone="outline">{KIND_LABELS[a.kind] ?? titleCase(a.kind.replace(/[._]/g, " "))}</Badge>
                        {a.impact === "high" && (
                          <Badge tone="danger">
                            <ShieldAlert /> High impact
                          </Badge>
                        )}
                        <StatusBadge status={a.status} />
                      </div>
                      <p className="text-xs text-fg-muted">
                        Requested {formatRelative(a.created_at)}
                        {a.requested_by ? ` by ${a.requested_by}` : ""}
                        {a.approver_permission ? ` · needs ${a.approver_permission}` : ""}
                        {a.expires_at ? ` · expires ${formatDateTime(a.expires_at)}` : ""}
                      </p>
                      {details.length > 0 && (
                        <dl className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
                          {details.slice(0, 8).map(([k, v]) => (
                            <div key={k} className="flex gap-1">
                              <dt className="text-fg-subtle">{k.replace(/_/g, " ")}:</dt>
                              <dd className="font-medium">{displayValue(v)}</dd>
                            </div>
                          ))}
                        </dl>
                      )}
                      {a.status !== "pending" && (
                        <p className="text-xs text-fg-muted">
                          {titleCase(a.status)} {a.decided_at ? formatRelative(a.decided_at) : ""}
                          {a.decided_by ? ` by ${a.decided_by}` : ""}
                          {a.comment ? `: "${a.comment}"` : ""}
                        </p>
                      )}
                      {href && (
                        <Link href={href} className="text-xs text-accent hover:underline">
                          Open the subject
                        </Link>
                      )}
                    </div>
                    {a.status === "pending" && a.approver_permission && !can(a.approver_permission) && (
                      <p className="text-xs text-fg-muted">Waiting for someone with {a.approver_permission}</p>
                    )}
                    {a.status === "pending" && (!a.approver_permission || can(a.approver_permission)) && (
                      <div className="flex gap-2">
                        <Button size="sm" variant="secondary" onClick={() => setDeciding({ a, decision: "reject" })}>
                          <X /> Reject
                        </Button>
                        <Button size="sm" onClick={() => setDeciding({ a, decision: "approve" })}>
                          <Check /> Approve
                        </Button>
                      </div>
                    )}
                  </div>
                </Card>
              </li>
            );
          })}
        </ul>
      )}
      <Pager page={list.page} hasPrev={list.hasPrev} hasNext={list.hasNext} onPrev={list.goPrev} onNext={list.goNext} loading={list.isFetching} />
      <DecisionDialog state={deciding} onClose={() => setDeciding(null)} />
    </div>
  );
}

export function DecisionDialog({ state, onClose }: { state: { a: Approval; decision: "approve" | "reject" } | null; onClose: () => void }) {
  const { slug } = useOrgContext();
  const decide = useDecideApproval(slug);
  const [comment, setComment] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<{ field?: string; message: string } | null>(null);
  React.useEffect(() => {
    setComment("");
    setPassword("");
    setError(null);
  }, [state]);
  const high = state?.a.impact === "high";
  const submit = async () => {
    if (!state) return;
    setError(null);
    if (high && !password) return setError({ field: "password", message: "Enter your password to confirm a high-impact decision." });
    try {
      await decide.mutateAsync({ id: state.a.id, decision: state.decision, comment: comment.trim(), password: high ? password : undefined });
      toast.success(state.decision === "approve" ? "Approved" : "Rejected");
      onClose();
    } catch (e) {
      if (isApiError(e) && e.code === "reauth_failed") setError({ field: "password", message: "That password is incorrect." });
      else setError({ message: errorMessage(e) });
    }
  };
  return (
    <Dialog open={!!state} onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="sm">
        <DialogHeader
          title={state ? `${state.decision === "approve" ? "Approve" : "Reject"}: ${state.a.title}` : ""}
          description={high ? "This is a high-impact action. Your password is checked again before the decision is recorded." : "Your decision and comment are recorded in the audit log."}
        />
        <DialogBody className="space-y-3">
          <Field id="ap-comment" label="Comment">
            <Textarea id="ap-comment" rows={3} value={comment} onChange={(e) => setComment(e.target.value)} maxLength={1000} />
          </Field>
          {high && (
            <Field id="ap-password" label="Password" error={error?.field === "password" ? error.message : undefined}>
              <Input id="ap-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </Field>
          )}
          {error && !error.field && (
            <p role="alert" className="text-sm text-danger">
              {error.message}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant={state?.decision === "reject" ? "danger" : "primary"} onClick={submit} loading={decide.isPending}>
            {high && <KeyRound />} {state?.decision === "reject" ? "Reject" : "Approve"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
