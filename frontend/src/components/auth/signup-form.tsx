"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Building2, Check, Mail, ShieldCheck, User } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { api, isApiError } from "@/lib/api";
import { describeError } from "@/lib/errors";
import { authKeys, useSignup } from "@/lib/queries/auth";
import { rememberOrg, safeNext } from "@/lib/routing";
import type { MeResponse } from "@/lib/types";
import { cn } from "@/lib/utils";
import { FormAlert, PasswordInput } from "./login-form";

/** Mirrors the server's AUTH_PASSWORD_VALIDATORS (min length 12, not entirely numeric); the server also rejects common passwords. */
export const passwordRules = [
  { id: "len", label: "At least 12 characters", test: (p: string) => p.length >= 12 },
  { id: "num", label: "Not only numbers", test: (p: string) => p.length > 0 && !/^\d+$/.test(p) },
];

const schema = z.object({
  full_name: z.string().trim().min(2, "Enter your full name").max(150),
  email: z.string().trim().email("Enter a valid work email"),
  org_name: z.string().trim().min(2, "Name your organization").max(120),
  password: z
    .string()
    .min(12, "Use at least 12 characters")
    .refine((p) => passwordRules.every((r) => r.test(p)), "Password doesn't meet all requirements"),
});
type Values = z.infer<typeof schema>;

const FIELDS: Array<keyof Values> = ["full_name", "email", "org_name", "password"];

export function SignupForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const qc = useQueryClient();
  const signup = useSignup();
  const [formError, setFormError] = React.useState<{ title: string; description?: string } | null>(null);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { full_name: "", email: "", org_name: "", password: "" },
    mode: "onTouched",
  });
  const password = form.watch("password");

  const onSubmit = form.handleSubmit(async (values) => {
    setFormError(null);
    try {
      const res = await signup.mutateAsync(values);
      rememberOrg(res.org.slug);
      try {
        qc.setQueryData(authKeys.me, await api.get<MeResponse>("/me", { silent401: true }));
      } catch {
        /* the org route will load /me itself */
      }
      router.replace(next ?? `/o/${res.org.slug}/dashboard`);
    } catch (e) {
      let mapped = false;
      if (isApiError(e)) {
        for (const fe of e.errors) {
          if (fe.field && (FIELDS as string[]).includes(fe.field)) {
            form.setError(fe.field as keyof Values, { message: fe.message });
            mapped = true;
          }
        }
      }
      if (!mapped) {
        const d = describeError(e);
        setFormError({ title: d.title, description: d.description });
      }
    }
  });

  return (
    <div className="space-y-8">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-[-0.02em] text-fg">Create your workspace</h1>
        <p className="text-sm text-fg-muted">Start with an isolated organization. You become its owner.</p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        {formError && <FormAlert {...formError} />}
        <Field id="full_name" label="Full name" error={form.formState.errors.full_name?.message}>
          <Input autoComplete="name" placeholder="Asha Rao" leading={<User />} autoFocus {...form.register("full_name")} />
        </Field>
        <Field id="email" label="Work email" error={form.formState.errors.email?.message}>
          <Input type="email" autoComplete="email" placeholder="you@company.com" leading={<Mail />} {...form.register("email")} />
        </Field>
        <Field
          id="org_name"
          label="Organization name"
          hint="You can create more organizations later."
          error={form.formState.errors.org_name?.message}
        >
          <Input autoComplete="organization" placeholder="Acme Analytics" leading={<Building2 />} {...form.register("org_name")} />
        </Field>
        <Field id="password" label="Password" error={form.formState.errors.password?.message}>
          <PasswordInput autoComplete="new-password" placeholder="Create a strong password" {...form.register("password")} />
        </Field>
        <ul className="grid gap-1 text-xs" aria-label="Password requirements">
          {passwordRules.map((r) => {
            const ok = r.test(password ?? "");
            return (
              <li key={r.id} className={cn("flex items-center gap-1.5 transition-colors", ok ? "text-success" : "text-fg-subtle")}>
                <Check className={cn("size-3.5", !ok && "opacity-40")} aria-hidden />
                {r.label}
                <span className="sr-only">{ok ? "met" : "not met"}</span>
              </li>
            );
          })}
        </ul>
        <div className="flex gap-2.5 rounded-control border border-border bg-surface-2/60 px-3 py-2.5 text-xs text-fg-muted">
          <ShieldCheck className="mt-0.5 size-4 shrink-0 text-teal" aria-hidden />
          <span>
            Your organization starts on the <span className="font-medium text-fg">synthetic_only</span> AI data policy:
            only data you attest as synthetic or public is ever sent to Gemini.
          </span>
        </div>
        <Button type="submit" size="lg" className="w-full" loading={signup.isPending || form.formState.isSubmitting}>
          Create workspace <ArrowRight />
        </Button>
      </form>
      <p className="text-center text-sm text-fg-muted">
        Already have an account?{" "}
        <Link href="/login" className="font-medium text-accent hover:underline">
          Sign in
        </Link>
      </p>
    </div>
  );
}
