"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import { Logo } from "@/components/brand/logo";
import { ErrorState } from "@/components/ui/feedback";
import { useMe } from "@/lib/queries/auth";
import { homeFor } from "@/lib/routing";

/** Entry point: routes visitors to /login, /onboarding or their organization's dashboard. */
export default function Home() {
  const router = useRouter();
  const me = useMe();

  React.useEffect(() => {
    if (me.data === null) router.replace("/login");
    else if (me.data) router.replace(homeFor(me.data));
  }, [me.data, router]);

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-6 bg-bg px-4">
      <Logo />
      {me.isError ? (
        <div className="w-full max-w-md rounded-card border border-border bg-surface shadow-soft">
          <ErrorState error={me.error} onRetry={() => void me.refetch()} />
        </div>
      ) : (
        <Loader2 className="size-5 animate-spin text-fg-subtle" aria-label="Loading" />
      )}
    </div>
  );
}
