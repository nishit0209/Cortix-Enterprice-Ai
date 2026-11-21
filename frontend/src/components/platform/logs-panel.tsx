"use client";

import * as React from "react";
import { RefreshCw, ScrollText } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { COMPONENT_RE, LOG_LEVELS, logTone } from "@/lib/platform";
import { usePlatformLogs } from "@/lib/queries/platform";
import { formatDateTime, formatNumber } from "@/lib/utils";
import { useHiddenGuard } from "./platform-console";

export function LogsPanel({ onHidden }: { onHidden: () => void }) {
  const [level, setLevel] = React.useState("all");
  const [componentText, setComponentText] = React.useState("");
  const [component, setComponent] = React.useState("");
  const componentInvalid = componentText.trim() !== "" && !COMPONENT_RE.test(componentText.trim());
  React.useEffect(() => {
    const t = setTimeout(() => {
      const v = componentText.trim();
      setComponent(COMPONENT_RE.test(v) ? v : "");
    }, 300);
    return () => clearTimeout(t);
  }, [componentText]);
  const q = usePlatformLogs({ level: level === "all" ? "" : level, component });
  useHiddenGuard(q.error, onHidden);
  const pages = q.data?.pages ?? [];
  const rows = pages.flatMap((p) => p.items);
  const capture = pages[0]?.capture;
  const [open, setOpen] = React.useState<number | null>(null);

  return (
    <Card>
      <CardHeader
        icon={<ScrollText />}
        title="Application logs"
        description={
          capture
            ? capture.enabled
              ? `Captured records (PLATFORM_LOG_LEVEL and above), newest first · keeps ${formatNumber(capture.max_rows)} rows${capture.dropped ? ` · ${formatNumber(capture.dropped)} dropped` : ""}`
              : "Log capture is disabled on this installation."
            : "Newest first."
        }
        actions={
          <Button size="xs" variant="ghost" onClick={() => void q.refetch()} loading={q.isFetching && !q.isFetchingNextPage} aria-label="Refresh logs">
            <RefreshCw />
          </Button>
        }
      />
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          <Select value={level} onValueChange={setLevel}>
            <SelectTrigger className="w-40" aria-label="Log level">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All levels</SelectItem>
              {LOG_LEVELS.map((l) => (
                <SelectItem key={l} value={l}>
                  {l}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <div>
            <Input className="w-64 font-mono" placeholder="Component (e.g. apps.ml)" aria-label="Log component" aria-invalid={componentInvalid || undefined} value={componentText} maxLength={120} onChange={(e) => setComponentText(e.target.value)} />
            {componentInvalid && <p className="mt-1 text-xs text-danger">Letters, digits, dot, dash and underscore only.</p>}
          </div>
        </div>
        {q.isError ? (
          <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
        ) : q.isLoading ? (
          <Skeleton className="h-64" />
        ) : rows.length === 0 ? (
          <EmptyState compact icon={<ScrollText />} title="No log records" description="Nothing matches these filters." />
        ) : (
          <ul className="divide-y divide-border/60 rounded-card border border-border font-mono text-xs">
            {rows.map((r) => (
              <li key={r.id}>
                <button type="button" className="focus-ring flex w-full items-start gap-3 px-3 py-2 text-left hover:bg-surface-2" aria-expanded={open === r.id} onClick={() => setOpen(open === r.id ? null : r.id)}>
                  <span className="w-40 shrink-0 text-fg-subtle">{formatDateTime(r.created_at)}</span>
                  <Badge tone={logTone(r.level)} className="shrink-0">
                    {r.level}
                  </Badge>
                  <span className="w-40 shrink-0 truncate text-fg-muted" title={r.component}>
                    {r.component}
                  </span>
                  <span className={`min-w-0 flex-1 text-fg ${open === r.id ? "whitespace-pre-wrap break-words" : "truncate"}`}>{r.message}</span>
                </button>
                {open === r.id && (
                  <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 bg-surface-2/60 px-3 py-2">
                    <dt className="text-fg-subtle">Where</dt>
                    <dd>{r.where || "—"}</dd>
                    <dt className="text-fg-subtle">Exception</dt>
                    <dd>{r.exc_type ?? "—"}</dd>
                    <dt className="text-fg-subtle">Request id</dt>
                    <dd>{r.request_id ?? "—"}</dd>
                  </dl>
                )}
              </li>
            ))}
          </ul>
        )}
        {q.hasNextPage && (
          <Button variant="secondary" size="sm" onClick={() => void q.fetchNextPage()} loading={q.isFetchingNextPage}>
            Load older
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
