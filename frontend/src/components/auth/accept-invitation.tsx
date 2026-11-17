"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, MailCheck, User } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { api, isApiError } from "@/lib/api";
import { describeError } from "@/lib/errors";
import { authKeys, useMe } from "@/lib/queries/auth";
import { rememberOrg } from "@/lib/routing";
import type { Org } from "@/lib/types";
import { FormAlert, PasswordInput } from "./login-form";

/** POST /invitations/accept { token, password?, full_name? } — new users set a name and password here. */
export function AcceptInvitation() {
  const params = useSearchParams();
  const token = params.get("token") ?? "";
  const router = useRouter();
  const qc = useQueryClient();
  const me = useMe();
  const signedIn = !!me.data;
  const [fullName, setFullName] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<{ title: string; description?: string } | null>(null);
  const [fieldErrors, setFieldErrors] = React.useState<Record<string, string>>({});

  if (!token) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-semibold tracking-[-0.02em] text-fg">Invitation link incomplete</h1>
        <p className="text-sm text-fg-muted">Open the link from your invitation email again, or ask the sender for a new one.</p>
        <Button variant="secondary" asChild>
          <Link href="/login">Go to sign in</Link>
        </Button>
      </div>
    );
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setFieldErrors({});
    setBusy(true);
    try {
      const body: Record<string, string> = { token };
      if (!signedIn) {
        if (fullName.trim()) body.full_name = fullName.trim();
        if (password) body.password = password;
      }
      const res = await api.post<{ org: Org }>("/invitations/accept", body, { silent401: true });
      rememberOrg(res.org.slug);
      await qc.invalidateQueries({ queryKey: authKeys.me });
      router.replace(`/o/${res.org.slug}/dashboard`);
    } catch (err) {
      if (isApiError(err) && err.errors.length) setFieldErrors(err.fieldErrors());
      const d = describeError(err);
      setError({
        title: isApiError(err) && (err.status === 404 || err.status === 410) ? "This invitation is no longer valid" : d.title,
        description: isApiError(err) && (err.status === 404 || err.status === 410) ? "It may have expired or been revoked." : d.description,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-8">
      <div className="space-y-2">
        <span className="flex size-11 items-center justify-center rounded-card bg-accent-soft text-accent-soft-fg">
          <MailCheck className="size-5" aria-hidden />
        </span>
        <h1 className="pt-2 text-2xl font-semibold tracking-[-0.02em] text-fg">Join your team on NEXORA</h1>
        <p className="text-sm text-fg-muted">
          {signedIn
            ? `Accept the invitation as ${me.data!.user.email}.`
            : "Set your name and a password to accept. Already have an account? Sign in first, then reopen the link."}
        </p>
      </div>
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error && <FormAlert {...error} />}
        {!signedIn && (
          <>
            <Field id="full_name" label="Full name" error={fieldErrors.full_name}>
              <Input value={fullName} onChange={(e) => setFullName(e.target.value)} leading={<User />} autoComplete="name" />
            </Field>
            <Field id="password" label="Password" hint="At least 12 characters." error={fieldErrors.password}>
              <PasswordInput value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
            </Field>
          </>
        )}
        <Button type="submit" size="lg" className="w-full" loading={busy}>
          Accept invitation <ArrowRight />
        </Button>
      </form>
      {!signedIn && (
        <p className="text-center text-sm text-fg-muted">
          Have an account?{" "}
          <Link
            href={`/login?next=${encodeURIComponent(`/invitations/accept?token=${token}`)}`}
            className="font-medium text-accent hover:underline"
          >
            Sign in
          </Link>
        </p>
      )}
    </div>
  );
}
