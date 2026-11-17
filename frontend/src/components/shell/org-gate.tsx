"use client";

import * as React from "react";
import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import { Building2, Loader2 } from "lucide-react";
import { useMe } from "@/lib/queries/auth";
import { useOrg } from "@/lib/queries/org";
import { permissionsAllow } from "@/lib/permissions";
import { rememberOrg } from "@/lib/routing";
import { Logo } from "@/components/brand/logo";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState } from "@/components/ui/feedback";
import { AppShell } from "./app-shell";
import { BreadcrumbProvider, OrgProvider, type OrgContextValue } from "./org-context";

function FullScreen({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-6 bg-bg px-4">
      <Logo />
      {children}
    </div>
  );
}

/** Auth + tenant guard for every /o/[org]/... route. */
export function OrgGate({ children }: { children: React.ReactNode }) {
  const params = useParams<{ org: string }>();
  const slug = params.org;
  const router = useRouter();
  const pathname = usePathname();
  const me = useMe();
  const member = me.data?.orgs.find((o) => o.slug === slug);
  const org = useOrg(member ? slug : undefined);

  React.useEffect(() => {
    if (me.data === null) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [me.data, router, pathname]);

  React.useEffect(() => {
    if (member) rememberOrg(member.slug);
  }, [member]);

  const can = React.useCallback(
    (perm: string | string[]) => permissionsAllow(org.data?.permissions, member?.role, perm),
    [org.data?.permissions, member?.role],
  );

  const value = React.useMemo<OrgContextValue | null>(
    () => (me.data && member ? { slug, me: me.data, org: org.data, summary: member, can } : null),
    [me.data, member, slug, org.data, can],
  );

  if (me.isLoading || me.data === null) {
    return (
      <FullScreen>
        <Loader2 className="size-5 animate-spin text-fg-subtle" aria-label="Loading workspace" />
      </FullScreen>
    );
  }

  if (me.isError) {
    return (
      <FullScreen>
        <div className="w-full max-w-md rounded-card border border-border bg-surface shadow-soft">
          <ErrorState error={me.error} onRetry={() => void me.refetch()} title="Can't load your workspace" />
        </div>
      </FullScreen>
    );
  }

  if (!value) {
    const first = me.data?.orgs[0];
    return (
      <FullScreen>
        <div className="w-full max-w-md rounded-card border border-border bg-surface shadow-soft">
          <EmptyState
            icon={<Building2 />}
            title="Organization not found"
            description="It doesn't exist or you're not a member. Ask an owner for an invitation, or open one of your organizations."
            action={
              first ? (
                <Button asChild>
                  <Link href={`/o/${first.slug}/dashboard`}>Open {first.name}</Link>
                </Button>
              ) : (
                <Button asChild>
                  <Link href="/onboarding">Create an organization</Link>
                </Button>
              )
            }
          />
        </div>
      </FullScreen>
    );
  }

  return (
    <OrgProvider value={value}>
      <BreadcrumbProvider>
        <AppShell>{children}</AppShell>
      </BreadcrumbProvider>
    </OrgProvider>
  );
}
