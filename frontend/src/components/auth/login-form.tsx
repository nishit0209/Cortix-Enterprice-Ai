"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useQueryClient } from "@tanstack/react-query";
import { AlertCircle, ArrowRight, Eye, EyeOff, Mail } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { api, isApiError } from "@/lib/api";
import { describeError } from "@/lib/errors";
import { authKeys, useLogin, useMe } from "@/lib/queries/auth";
import { homeFor, safeNext } from "@/lib/routing";
import type { MeResponse } from "@/lib/types";

const schema = z.object({
  email: z.string().trim().email("Enter a valid email address"),
  password: z.string().min(1, "Enter your password"),
});
type Values = z.infer<typeof schema>;

export function PasswordInput(props: React.ComponentProps<typeof Input>) {
  const [show, setShow] = React.useState(false);
  return (
    <Input
      {...props}
      type={show ? "text" : "password"}
      trailing={
        <button
          type="button"
          onClick={() => setShow((s) => !s)}
          className="flex size-7 items-center justify-center rounded-[4px] text-fg-subtle hover:text-fg focus-ring"
          aria-label={show ? "Hide password" : "Show password"}
          aria-pressed={show}
        >
          {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
        </button>
      }
    />
  );
}

export function FormAlert({ title, description }: { title: string; description?: string }) {
  return (
    <div role="alert" className="flex gap-2.5 rounded-control border border-danger/25 bg-danger-soft px-3 py-2.5 text-sm">
      <AlertCircle className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
      <div>
        <div className="font-medium text-danger">{title}</div>
        {description && <div className="text-fg-muted">{description}</div>}
      </div>
    </div>
  );
}

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const qc = useQueryClient();
  const login = useLogin();
  const me = useMe();
  const [formError, setFormError] = React.useState<{ title: string; description?: string } | null>(null);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { email: "", password: "" } });

  // Already signed in (valid session): skip the form.
  React.useEffect(() => {
    if (me.data) router.replace(next ?? homeFor(me.data));
  }, [me.data, next, router]);

  const onSubmit = form.handleSubmit(async (values) => {
    setFormError(null);
    try {
      await login.mutateAsync(values);
      const fresh = await api.get<MeResponse>("/me", { silent401: true });
      qc.setQueryData(authKeys.me, fresh);
      router.replace(next ?? homeFor(fresh));
    } catch (e) {
      if (isApiError(e) && e.errors.length) {
        for (const fe of e.errors) {
          if (fe.field === "email" || fe.field === "password") form.setError(fe.field, { message: fe.message });
        }
      }
      if (isApiError(e) && (e.status === 400 || e.status === 401 || e.status === 403)) {
        setFormError({ title: "Incorrect email or password", description: "Check your details and try again." });
      } else {
        const d = describeError(e);
        setFormError({ title: d.title, description: d.description });
      }
    }
  });

  return (
    <div className="space-y-8">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-[-0.02em] text-fg">Welcome back</h1>
        <p className="text-sm text-fg-muted">Sign in to your NEXORA workspace.</p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        {formError && <FormAlert {...formError} />}
        <Field id="email" label="Work email" error={form.formState.errors.email?.message}>
          <Input
            type="email"
            autoComplete="email"
            placeholder="you@company.com"
            leading={<Mail />}
            autoFocus
            {...form.register("email")}
          />
        </Field>
        <Field id="password" label="Password" error={form.formState.errors.password?.message}>
          <PasswordInput autoComplete="current-password" placeholder="••••••••••" {...form.register("password")} />
        </Field>
        <Button type="submit" size="lg" className="w-full" loading={login.isPending || form.formState.isSubmitting}>
          Sign in <ArrowRight />
        </Button>
      </form>
      <p className="text-center text-sm text-fg-muted">
        New to NEXORA?{" "}
        <Link href={next ? `/signup?next=${encodeURIComponent(next)}` : "/signup"} className="font-medium text-accent hover:underline">
          Create a workspace
        </Link>
      </p>
    </div>
  );
}
