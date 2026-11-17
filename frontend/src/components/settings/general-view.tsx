"use client";

import * as React from "react";
import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { ArrowRight, Building2, ScrollText, ShieldCheck, Users } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { PageHeader } from "@/components/ui/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/feedback";
import { POLICY_LABELS } from "@/components/ui/status";
import { useUpdateOrg } from "@/lib/queries/org";
import { ROLE_LABELS } from "@/lib/permissions";
import { errorMessage } from "@/lib/errors";
import { isApiError } from "@/lib/api";
import { formatDate, titleCase } from "@/lib/utils";

const schema = z.object({ name: z.string().trim().min(2, "Use at least 2 characters").max(120) });

export function GeneralSettingsView() {
  const { slug, org, summary, can } = useOrgContext();
  const update = useUpdateOrg(slug);
  const editable = can("org.settings.edit");
  const form = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema),
    values: { name: org?.name ?? summary?.name ?? "" },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      await update.mutateAsync(values);
      toast.success("Organization updated");
      form.reset(values);
    } catch (e) {
      if (isApiError(e) && e.errors.some((x) => x.field === "name")) {
        form.setError("name", { message: e.errors.find((x) => x.field === "name")!.message });
      } else toast.error(errorMessage(e));
    }
  });

  return (
    <div className="space-y-6">
      <PageHeader title="Settings" description="Organization profile and administration." />
      <div className="grid gap-6 lg:grid-cols-12">
        <Card className="lg:col-span-7">
          <form onSubmit={onSubmit} noValidate>
            <CardHeader icon={<Building2 />} title="Organization profile" description="Visible to all members." />
            <CardContent className="space-y-4">
              <Field id="org-name" label="Name" error={form.formState.errors.name?.message}>
                <Input disabled={!editable} {...form.register("name")} />
              </Field>
              <Field id="org-slug" label="URL" hint="The slug is permanent and appears in every shareable link.">
                <Input value={`/o/${slug}`} readOnly className="font-mono text-fg-muted" />
              </Field>
            </CardContent>
            <CardFooter>
              <span>{editable ? "Changes are recorded in the audit log." : "Only owners and admins can edit."}</span>
              {editable && (
                <Button type="submit" size="sm" loading={update.isPending} disabled={!form.formState.isDirty}>
                  Save changes
                </Button>
              )}
            </CardFooter>
          </form>
        </Card>
        <Card className="lg:col-span-5">
          <CardHeader title="Details" />
          <CardContent>
            {org ? (
              <dl className="divide-y divide-border text-sm">
                <Row label="Your role">{ROLE_LABELS[org.role] ?? org.role}</Row>
                <Row label="Plan">
                  <Badge tone="accent">{titleCase(org.plan)}</Badge>
                </Row>
                <Row label="Status">{titleCase(org.status)}</Row>
                <Row label="Data region">{org.region.toUpperCase()}</Row>
                <Row label="AI data policy">{POLICY_LABELS[org.ai_data_policy] ?? org.ai_data_policy}</Row>
                <Row label="Created">{formatDate(org.created_at)}</Row>
              </dl>
            ) : (
              <div className="space-y-3">
                {[0, 1, 2, 3].map((i) => (
                  <Skeleton key={i} className="h-5 w-full" />
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        <LinkCard href={`/o/${slug}/settings/members`} icon={<Users />} title="Members" text="Invite teammates and manage roles." />
        <LinkCard href={`/o/${slug}/settings/ai-policy`} icon={<ShieldCheck />} title="AI data policy" text="Control what may be sent to Gemini." />
        {can("audit.view") && (
          <LinkCard href={`/o/${slug}/audit`} icon={<ScrollText />} title="Audit log" text="Review every change in this organization." />
        )}
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2.5">
      <dt className="text-fg-muted">{label}</dt>
      <dd className="font-medium text-fg">{children}</dd>
    </div>
  );
}

function LinkCard({ href, icon, title, text }: { href: string; icon: React.ReactNode; title: string; text: string }) {
  return (
    <Link href={href} className="group block rounded-card focus-ring">
      <Card interactive className="flex h-full items-start gap-3 p-4">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-control bg-surface-2 text-fg-muted [&_svg]:size-4">{icon}</span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1 text-sm font-semibold text-fg">
            {title}
            <ArrowRight className="size-3.5 text-fg-subtle transition-transform group-hover:translate-x-0.5" aria-hidden />
          </div>
          <p className="text-sm text-fg-muted">{text}</p>
        </div>
      </Card>
    </Link>
  );
}
