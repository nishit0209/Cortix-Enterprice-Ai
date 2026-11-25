"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { ArrowRight, Building2, Database, Loader2, LogOut, ShieldCheck, Sparkles, UserPlus } from "lucide-react";
import { Logo } from "@/components/brand/logo";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { FormAlert } from "@/components/auth/login-form";
import { useCreateOrg, useLogout, useMe } from "@/lib/queries/auth";
import { describeError } from "@/lib/errors";
import { isApiError } from "@/lib/api";
import { rememberOrg } from "@/lib/routing";

const schema = z.object({ name: z.string().trim().min(2, "Use at least 2 characters").max(120) });

const NEXT_STEPS = [
  { icon: UserPlus, label: "Invite a teammate" },
  { icon: Database, label: "Upload a dataset" },
  { icon: ShieldCheck, label: "Review data quality" },
  { icon: Sparkles, label: "Ask your first question" },
];

/** Shown to signed-in users who belong to no organization yet. */
export default function OnboardingPage() {
  const router = useRouter();
  const me = useMe();
  const create = useCreateOrg();
  const logout = useLogout();
  const [error, setError] = React.useState<{ title: string; description?: string } | null>(null);
  const form = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema), defaultValues: { name: "" } });

  React.useEffect(() => {
    if (me.data === null) router.replace("/login?next=/onboarding");
  }, [me.data, router]);

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      const org = await create.mutateAsync(values);
      rememberOrg(org.slug);
      router.replace(`/o/${org.slug}/dashboard`);
    } catch (e) {
      if (isApiError(e) && e.errors.length) form.setError("name", { message: e.errors[0].message });
      else {
        const d = describeError(e);
        setError({ title: d.title, description: d.description });
      }
    }
  });

  if (!me.data) {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-bg">
        <Loader2 className="size-5 animate-spin text-fg-subtle" aria-label="Loading" />
      </div>
    );
  }

  const firstName = (me.data.user.full_name || "").split(" ")[0];

  return (
    <div className="relative min-h-dvh overflow-hidden bg-bg">
      <div
        className="pointer-events-none absolute inset-x-0 top-0 h-[420px] opacity-60 dark:opacity-40"
        style={{ background: "radial-gradient(60% 60% at 50% 0%, var(--accent-soft) 0%, transparent 70%)" }}
        aria-hidden
      />
      <header className="relative flex h-16 items-center justify-between px-6">
        <Logo />
        <Button
          variant="ghost"
          size="sm"
          onClick={async () => {
            await logout.mutateAsync().catch(() => undefined);
            router.replace("/login");
          }}
        >
          <LogOut /> Sign out
        </Button>
      </header>
      <main className="relative mx-auto flex max-w-lg flex-col gap-8 px-4 pb-16 pt-10 sm:pt-16">
        <div className="space-y-2 text-center">
          <span className="mx-auto flex size-12 items-center justify-center rounded-card border border-border bg-surface shadow-soft">
            <Building2 className="size-5 text-accent" aria-hidden />
          </span>
          <h1 className="pt-3 text-2xl font-semibold tracking-[-0.02em] text-fg">
            {firstName ? `Welcome, ${firstName}` : "Welcome to Cortix"}
          </h1>
          <p className="text-sm text-fg-muted">
            Create an organization to get started, or ask a teammate to invite you to theirs.
          </p>
        </div>
        <Card className="p-6 shadow-soft">
          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            {error && <FormAlert {...error} />}
            <Field id="org-name" label="Organization name" error={form.formState.errors.name?.message}>
              <Input placeholder="Acme Analytics" autoFocus {...form.register("name")} />
            </Field>
            <Button type="submit" className="w-full" size="lg" loading={create.isPending}>
              Create organization <ArrowRight />
            </Button>
          </form>
        </Card>
        <div>
          <p className="mb-3 text-center text-xs font-medium uppercase tracking-[0.08em] text-fg-subtle">What&apos;s next</p>
          <ol className="grid grid-cols-2 gap-2">
            {NEXT_STEPS.map(({ icon: Icon, label }, i) => (
              <li key={label} className="flex items-center gap-2.5 rounded-control border border-border bg-surface px-3 py-2.5 text-sm">
                <span className="tabular flex size-5 shrink-0 items-center justify-center rounded-full bg-surface-2 text-[11px] font-semibold text-fg-muted">
                  {i + 1}
                </span>
                <Icon className="size-4 shrink-0 text-fg-subtle" aria-hidden />
                <span className="truncate text-fg">{label}</span>
              </li>
            ))}
          </ol>
        </div>
        {me.data.orgs.length > 0 && (
          <p className="text-center text-sm text-fg-muted">
            Or go back to{" "}
            <Link href={`/o/${me.data.orgs[0].slug}/dashboard`} className="font-medium text-accent hover:underline">
              {me.data.orgs[0].name}
            </Link>
          </p>
        )}
      </main>
    </div>
  );
}
