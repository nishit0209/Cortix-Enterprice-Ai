"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Activity, ArrowLeft, Bot, Building2, HeartPulse, ScrollText, Workflow } from "lucide-react";
import NotFound from "@/app/not-found";
import { Logo } from "@/components/brand/logo";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/feedback";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { isApiError } from "@/lib/api";
import { platformGate } from "@/lib/platform";
import { useMe } from "@/lib/queries/auth";
import { homeFor } from "@/lib/routing";
import { AiPanel } from "./ai-panel";
import { HealthPanel, JobsPanel } from "./health-jobs";
import { LogsPanel } from "./logs-panel";
import { OrgsPanel } from "./orgs-panel";

/** Error from a platform route: the server answers 404 to non-admins (e.g. a revoked grant), so show the 404 page. */
export function isPlatformHidden(error: unknown): boolean {
  return isApiError(error) && (error.status === 404 || error.status === 401 || error.status === 403);
}

export function PlatformConsole() {
  const me = useMe();
  const router = useRouter();
  const gate = platformGate(me.data, me.isLoading);
  const [hidden, setHidden] = React.useState(false);
  const [tab, setTab] = React.useState("orgs");

  React.useEffect(() => {
    if (gate === "signed_out") router.replace("/login?next=/platform");
  }, [gate, router]);

  if (gate === "loading" || gate === "signed_out") {
    return (
      <div className="mx-auto max-w-6xl space-y-4 px-4 py-10" aria-busy>
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64" />
      </div>
    );
  }
  if (gate === "not_found" || hidden) return <NotFound />;

  const onHidden = () => setHidden(true);
  const home = me.data ? homeFor(me.data) : "/";

  return (
    <div className="min-h-dvh bg-bg">
      <header className="sticky top-0 z-20 border-b border-border bg-surface/85 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-3 px-4">
          <Logo />
          <Badge tone="warning">Platform console</Badge>
          <span className="ml-auto hidden text-xs text-fg-muted sm:inline">{me.data?.user.email}</span>
          <Button asChild size="sm" variant="ghost">
            <Link href={home}>
              <ArrowLeft /> Back to workspace
            </Link>
          </Button>
        </div>
      </header>
      <main className="mx-auto max-w-7xl space-y-6 px-4 py-6">
        <div>
          <h1 className="text-xl font-semibold tracking-[-0.015em] text-fg">Platform administration</h1>
          <p className="mt-1 max-w-3xl text-sm text-fg-muted">
            Organizations, service health, the job queue, Gemini usage and quotas, and application logs across the whole installation. Counts and status only: tenant data rows are never shown here. Every
            action is audited.
          </p>
        </div>
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList aria-label="Platform sections">
            <TabsTrigger value="orgs">
              <Building2 /> Organizations
            </TabsTrigger>
            <TabsTrigger value="health">
              <HeartPulse /> Health
            </TabsTrigger>
            <TabsTrigger value="jobs">
              <Workflow /> Jobs
            </TabsTrigger>
            <TabsTrigger value="ai">
              <Bot /> AI usage &amp; quotas
            </TabsTrigger>
            <TabsTrigger value="logs">
              <ScrollText /> Logs
            </TabsTrigger>
          </TabsList>
          <TabsContent value="orgs">
            <OrgsPanel onHidden={onHidden} />
          </TabsContent>
          <TabsContent value="health">
            <HealthPanel onHidden={onHidden} />
          </TabsContent>
          <TabsContent value="jobs">
            <JobsPanel onHidden={onHidden} />
          </TabsContent>
          <TabsContent value="ai">
            <AiPanel onHidden={onHidden} />
          </TabsContent>
          <TabsContent value="logs">
            <LogsPanel onHidden={onHidden} />
          </TabsContent>
        </Tabs>
        <p className="flex items-center gap-1.5 text-xs text-fg-subtle">
          <Activity className="size-3.5" /> Platform access is granted with <code className="font-mono">manage.py create_platform_admin --email &lt;email&gt;</code>.
        </p>
      </main>
    </div>
  );
}

/** Calls `onHidden` once when a platform query fails with the "hidden" statuses. */
export function useHiddenGuard(error: unknown, onHidden: () => void) {
  React.useEffect(() => {
    if (error && isPlatformHidden(error)) onHidden();
  }, [error, onHidden]);
}
