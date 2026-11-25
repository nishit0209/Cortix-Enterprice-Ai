"use client";

import * as React from "react";
import { Check, FileLock2, Info, KeyRound, Lock, ShieldCheck, ShieldOff, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { PageHeader } from "@/components/ui/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field } from "@/components/ui/field";
import { FeatureStatusBadge } from "@/components/ui/status";
import { FormAlert, PasswordInput } from "@/components/auth/login-form";
import { useAiPolicy, useConsentText, useGrantConsent, useRevokeConsent } from "@/lib/queries/org";
import { describeError, errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { isApiError } from "@/lib/api";
import { cn, formatDateTime } from "@/lib/utils";
import type { AIDataPolicy, AiPolicyState } from "@/lib/types";

export const POLICY_INFO: Record<
  AIDataPolicy,
  { title: string; sends: string; enables: string[]; requires: string; icon: React.ComponentType<{ className?: string }> }
> = {
  synthetic_only: {
    title: "Synthetic only",
    sends: "Only datasets and documents you attest as synthetic or public. Nothing else ever leaves Cortix.",
    enables: ["AI answers on synthetic/public data", "Local insights and builder answers on all data"],
    requires: "Default for every organization.",
    icon: ShieldCheck,
  },
  schema_only: {
    title: "Schema only",
    sends: "Table and column names, types and descriptions for any dataset. Never row values.",
    enables: ["Natural-language questions over internal data (SQL is generated from the schema)", "Results computed locally"],
    requires: "Owner/admin consent with password re-authentication.",
    icon: FileLock2,
  },
  aggregates_only: {
    title: "Aggregates only",
    sends: "Schema plus aggregated results (counts, sums, averages). No individual rows.",
    enables: ["AI narration and explanations of aggregated results"],
    requires: "A paid Gemini tier (free-tier prompts may be used for training).",
    icon: Lock,
  },
  full: {
    title: "Full",
    sends: "Schema, aggregates and row-level values for permitted datasets.",
    enables: ["Row-level AI analysis and summaries"],
    requires: "A paid Gemini tier and explicit consent.",
    icon: KeyRound,
  },
};

const ORDER: AIDataPolicy[] = ["synthetic_only", "schema_only", "aggregates_only", "full"];

export function AiPolicyView() {
  const { slug, can } = useOrgContext();
  const q = useAiPolicy(slug);
  const revoke = useRevokeConsent(slug);
  const [consentOpen, setConsentOpen] = React.useState(false);
  const [revokeOpen, setRevokeOpen] = React.useState(false);
  const canConsent = can("org.ai_policy.consent");

  const header = (
    <PageHeader
      title="AI data policy"
      badge={<FeatureStatusBadge status="working" />}
      description="Decide what may be sent to Gemini, Cortix's only AI provider. Every change is audited."
    />
  );

  if (q.isLoading) {
    return (
      <div className="space-y-6">
        {header}
        <Skeleton className="h-44 w-full rounded-card" />
        <div className="grid gap-4 md:grid-cols-2">
          {ORDER.map((p) => (
            <Skeleton key={p} className="h-40 rounded-card" />
          ))}
        </div>
      </div>
    );
  }
  if (q.isError || !q.data) {
    return (
      <div className="space-y-6">
        {header}
        <Card>
          {isUnavailableEndpoint(q.error) ? (
            <EmptyState
              icon={<ShieldCheck />}
              title="Policy service unavailable"
              description="Your organization stays on the default synthetic_only policy until the policy service responds."
            />
          ) : (
            <ErrorState error={q.error} onRetry={() => void q.refetch()} />
          )}
        </Card>
      </div>
    );
  }

  const s = q.data;
  const current = POLICY_INFO[s.policy] ?? POLICY_INFO.synthetic_only;
  const CurrentIcon = current.icon;
  const freeTier = s.gemini_tier === "free";
  const schemaAllowed = s.allowed_values.includes("schema_only");
  const canGrant = canConsent && s.policy === "synthetic_only" && schemaAllowed;
  const canRevoke = canConsent && (s.policy !== "synthetic_only" || !!s.consent?.valid);

  return (
    <div className="space-y-6">
      {header}

      <Card className="relative overflow-hidden">
        <div
          className="pointer-events-none absolute -right-24 -top-24 size-72 rounded-full opacity-60 blur-3xl"
          style={{ background: "radial-gradient(circle, var(--teal-soft) 0%, transparent 70%)" }}
          aria-hidden
        />
        <div className="relative flex flex-col gap-6 p-6 md:flex-row md:items-start md:justify-between">
          <div className="flex gap-4">
            <span className="flex size-12 shrink-0 items-center justify-center rounded-card bg-teal-soft text-teal">
              <CurrentIcon className="size-5" />
            </span>
            <div className="space-y-2">
              <div className="text-xs font-medium uppercase tracking-[0.08em] text-fg-subtle">Current policy</div>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-xl font-semibold tracking-[-0.015em] text-fg">{current.title}</h2>
                <Badge tone="outline" className="font-mono">
                  {s.policy}
                </Badge>
              </div>
              <p className="max-w-xl text-sm text-fg-muted">{current.sends}</p>
              <div className="flex flex-wrap items-center gap-2 pt-1 text-xs">
                <Badge tone={freeTier ? "warning" : "success"}>
                  <Sparkles /> Gemini {freeTier ? "free" : s.gemini_tier} tier
                </Badge>
                {s.consent ? (
                  <Badge tone={s.consent.valid ? "success" : "neutral"}>
                    {s.consent.valid ? <Check /> : <ShieldOff />} Consent {s.consent.valid ? "valid" : "not valid"} · v{String(s.consent.version)}
                  </Badge>
                ) : (
                  <Badge tone="neutral">No consent on file</Badge>
                )}
              </div>
              {s.consent?.granted_at && (
                <p className="text-xs text-fg-subtle">
                  Granted {s.consent.granted_by ? `by ${s.consent.granted_by} ` : ""}on {formatDateTime(s.consent.granted_at)}
                </p>
              )}
            </div>
          </div>
          <div className="flex shrink-0 flex-col gap-2 sm:flex-row md:flex-col">
            {canGrant && (
              <Button onClick={() => setConsentOpen(true)}>
                <FileLock2 /> Grant schema_only
              </Button>
            )}
            {canRevoke && (
              <Button variant="secondary" onClick={() => setRevokeOpen(true)}>
                <ShieldOff /> Revoke consent
              </Button>
            )}
            {!canConsent && (
              <p className="max-w-[220px] text-xs text-fg-muted">Only owners and admins can change the AI data policy.</p>
            )}
          </div>
        </div>
      </Card>

      <section aria-labelledby="policy-values" className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 id="policy-values" className="text-md font-semibold text-fg">
            The four policies
          </h2>
          <p className="hidden text-xs text-fg-muted sm:block">Ordered from most to least restrictive</p>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          {ORDER.map((p) => (
            <PolicyCard key={p} policy={p} state={s} />
          ))}
        </div>
      </section>

      <Card>
        <CardHeader icon={<Info />} title="How classification interacts with policy" />
        <CardContent className="grid gap-3 text-sm text-fg-muted md:grid-cols-3">
          <p>
            <span className="font-medium text-fg">Synthetic and public</span> datasets can be used with AI under every
            policy, after an attestation recorded in the audit log.
          </p>
          <p>
            <span className="font-medium text-fg">Internal</span> datasets need at least <span className="font-mono">schema_only</span>{" "}
            for AI-generated SQL. Their values stay in Cortix unless a paid-tier policy allows more.
          </p>
          <p>
            <span className="font-medium text-fg">Confidential</span> datasets never send values to Gemini. Local insights and the
            visual query builder always work without AI.
          </p>
        </CardContent>
      </Card>

      <ConsentDialog org={slug} state={s} open={consentOpen} onOpenChange={setConsentOpen} />
      <ConfirmDialog
        open={revokeOpen}
        onOpenChange={setRevokeOpen}
        title="Revoke AI consent?"
        description="The organization returns to synthetic_only immediately. AI features stop using internal datasets."
        confirmLabel="Revoke consent"
        loading={revoke.isPending}
        onConfirm={async () => {
          try {
            await revoke.mutateAsync();
            toast.success("Consent revoked", { description: "Policy is now synthetic_only." });
            setRevokeOpen(false);
          } catch (e) {
            toast.error(errorMessage(e));
          }
        }}
      />
    </div>
  );
}

function PolicyCard({ policy, state }: { policy: AIDataPolicy; state: AiPolicyState }) {
  const info = POLICY_INFO[policy];
  const Icon = info.icon;
  const active = state.policy === policy;
  const tierLocked = state.gemini_tier === "free" && (policy === "aggregates_only" || policy === "full");
  const notAllowed = !state.allowed_values.includes(policy) && !active;
  return (
    <Card className={cn("relative flex flex-col p-5 transition-colors", active && "border-accent/60 bg-accent-soft/30 ring-1 ring-accent/20")}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span
            className={cn(
              "flex size-8 items-center justify-center rounded-control",
              active ? "bg-accent text-accent-fg" : "bg-surface-2 text-fg-muted",
            )}
          >
            <Icon className="size-4" />
          </span>
          <div>
            <div className="text-sm font-semibold text-fg">{info.title}</div>
            <div className="font-mono text-[11px] text-fg-subtle">{policy}</div>
          </div>
        </div>
        {active ? (
          <Badge tone="accent">
            <Check /> Active
          </Badge>
        ) : tierLocked ? (
          <Badge tone="warning">
            <Lock /> Paid tier
          </Badge>
        ) : notAllowed ? (
          <Badge tone="neutral">Unavailable</Badge>
        ) : null}
      </div>
      <p className="mt-3 text-sm text-fg-muted">{info.sends}</p>
      <ul className="mt-3 space-y-1.5 text-sm">
        {info.enables.map((e) => (
          <li key={e} className="flex gap-2 text-fg">
            <Check className="mt-0.5 size-4 shrink-0 text-success" aria-hidden />
            {e}
          </li>
        ))}
      </ul>
      <div className="mt-auto pt-4">
        <p className="border-t border-border pt-3 text-xs text-fg-subtle">{info.requires}</p>
      </div>
    </Card>
  );
}

function ConsentDialog({
  org,
  state,
  open,
  onOpenChange,
}: {
  org: string;
  state: AiPolicyState;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const text = useConsentText(org, open);
  const grant = useGrantConsent(org);
  const [acknowledged, setAcknowledged] = React.useState(false);
  const [password, setPassword] = React.useState("");
  const [passwordError, setPasswordError] = React.useState<string | null>(null);
  const [formError, setFormError] = React.useState<{ title: string; description?: string } | null>(null);

  React.useEffect(() => {
    if (!open) {
      setAcknowledged(false);
      setPassword("");
      setPasswordError(null);
      setFormError(null);
    }
  }, [open]);

  const submit = async () => {
    setFormError(null);
    setPasswordError(null);
    if (!password) {
      setPasswordError("Re-enter your password to confirm");
      return;
    }
    try {
      await grant.mutateAsync({
        policy: "schema_only",
        consent_text_version: text.data?.version ?? state.consent_text_version,
        acknowledged: true,
        password,
      });
      toast.success("schema_only enabled", { description: "Consent recorded in the audit log." });
      onOpenChange(false);
    } catch (e) {
      if (isApiError(e)) {
        const pw = e.errors.find((x) => x.field === "password");
        if (pw || e.code === "invalid_password" || e.code === "authentication_failed") {
          setPasswordError(pw?.message ?? "Incorrect password");
          return;
        }
      }
      const d = describeError(e);
      setFormError({ title: d.title, description: d.description });
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader
          icon={<FileLock2 />}
          title="Consent to schema_only"
          description="Gemini will receive table and column metadata for internal datasets. Row values are never sent."
        />
        <DialogBody className="space-y-4">
          {formError && <FormAlert {...formError} />}
          <div className="rounded-card border border-border bg-surface-2/60">
            <div className="flex items-center justify-between border-b border-border px-4 py-2 text-xs text-fg-muted">
              <span className="font-medium">Consent terms</span>
              {text.data && <span className="font-mono">v{String(text.data.version)}</span>}
            </div>
            <div className="scrollbar-thin max-h-56 overflow-y-auto px-4 py-3 text-sm leading-6 text-fg">
              {text.isLoading ? (
                <div className="space-y-2">
                  <Skeleton className="h-3.5 w-full" />
                  <Skeleton className="h-3.5 w-11/12" />
                  <Skeleton className="h-3.5 w-4/5" />
                </div>
              ) : text.isError ? (
                <ErrorState compact error={text.error} onRetry={() => void text.refetch()} />
              ) : (
                <div className="whitespace-pre-wrap">{text.data?.text}</div>
              )}
            </div>
          </div>
          <label className="flex cursor-pointer gap-3 text-sm">
            <Checkbox checked={acknowledged} onCheckedChange={(v) => setAcknowledged(v === true)} className="mt-0.5" />
            <span className="text-fg-muted">
              I have read these terms and consent, on behalf of my organization, to sending schema metadata to Gemini.
            </span>
          </label>
          <Field id="consent-password" label="Confirm your password" error={passwordError ?? undefined} hint="Required to change the AI data policy.">
            <PasswordInput
              autoComplete="current-password"
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                setPasswordError(null);
              }}
            />
          </Field>
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={!acknowledged || !text.data} loading={grant.isPending}>
            Grant consent
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
