"use client";

import * as React from "react";
import Link from "next/link";
import { Check, ChevronDown, Code2, Copy, SquareTerminal } from "lucide-react";
import { toast } from "sonner";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/ui/overlays";
import { SQL_TOKEN_CLASS, tokenizeSql } from "@/lib/sql-highlight";
import { SOURCE_LABELS, sourceTone } from "@/lib/chat/error-copy";
import { cn } from "@/lib/utils";

/** Syntax-highlighted, read-only SQL. */
export function SqlCode({ sql, className, wrap = true }: { sql: string; className?: string; wrap?: boolean }) {
  const tokens = React.useMemo(() => tokenizeSql(sql), [sql]);
  return (
    <pre
      className={cn(
        "scrollbar-thin overflow-auto rounded-control bg-surface-2 px-3.5 py-3 font-mono text-[12.5px] leading-5 text-fg",
        wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre",
        className,
      )}
    >
      <code>
        {tokens.map((t, i) =>
          t.type === "space" ? (
            t.text
          ) : (
            <span key={i} className={SQL_TOKEN_CLASS[t.type]}>
              {t.text}
            </span>
          ),
        )}
      </code>
    </pre>
  );
}

export function SourceBadge({ source, className }: { source: string | null | undefined; className?: string }) {
  if (!source) return null;
  return (
    <Badge tone={sourceTone(source)} className={className}>
      {SOURCE_LABELS[source] ?? source}
    </Badge>
  );
}

export function CopyButton({ text, label = "Copy", size = "xs" }: { text: string; label?: string; size?: "xs" | "sm" }) {
  const [copied, setCopied] = React.useState(false);
  return (
    <Button
      variant="ghost"
      size={size}
      onClick={async () => {
        try {
          await navigator.clipboard?.writeText(text);
          setCopied(true);
          toast.success("Copied to clipboard");
          setTimeout(() => setCopied(false), 1500);
        } catch {
          toast.error("Clipboard is not available");
        }
      }}
    >
      {copied ? <Check /> : <Copy />} {label}
    </Button>
  );
}

/**
 * SQL transparency panel: executed SQL (what actually ran after the guard's policy rewrite), the generated SQL
 * when it differs, the source badge (cache / builder / Gemini / manual), copy, and "open in SQL editor".
 */
export function SqlPanel({
  executed,
  raw,
  source,
  editorHref,
  defaultOpen = false,
  meta,
  tone = "default",
}: {
  executed: string | null;
  raw?: string | null;
  source?: string | null;
  editorHref?: string | null;
  defaultOpen?: boolean;
  meta?: React.ReactNode;
  tone?: "default" | "danger";
}) {
  const [open, setOpen] = React.useState(defaultOpen);
  const [showRaw, setShowRaw] = React.useState(false);
  const reduce = useReducedMotion();
  const shown = showRaw && raw ? raw : (executed ?? raw ?? "");
  if (!shown) return null;
  const rewritten = !!raw && !!executed && raw.trim() !== executed.trim();
  return (
    <div className={cn("rounded-card border bg-surface", tone === "danger" ? "border-danger/40" : "border-border")}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left text-sm focus-ring rounded-card"
      >
        <Code2 className="size-4 text-fg-subtle" aria-hidden />
        <span className="font-medium text-fg">SQL</span>
        <SourceBadge source={source} />
        {rewritten && (
          <Tooltip content="The SQL guard rewrote the query to apply your access policy (columns, masks, row filters, limits).">
            <span>
              <Badge tone="outline">policy applied</Badge>
            </span>
          </Tooltip>
        )}
        <span className="ml-auto flex items-center gap-2 text-xs text-fg-subtle">
          {meta}
          <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} aria-hidden />
        </span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={reduce ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={reduce ? { opacity: 0 } : { height: 0, opacity: 0 }}
            transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
            className="overflow-hidden"
          >
            <div className="space-y-2 px-3.5 pb-3.5">
              {rewritten && (
                <div className="flex gap-1 text-xs" role="group" aria-label="SQL version">
                  <button
                    type="button"
                    onClick={() => setShowRaw(false)}
                    aria-pressed={!showRaw}
                    className={cn("rounded-[4px] px-2 py-0.5 focus-ring", !showRaw ? "bg-surface-2 text-fg" : "text-fg-muted hover:text-fg")}
                  >
                    Executed
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowRaw(true)}
                    aria-pressed={showRaw}
                    className={cn("rounded-[4px] px-2 py-0.5 focus-ring", showRaw ? "bg-surface-2 text-fg" : "text-fg-muted hover:text-fg")}
                  >
                    Generated
                  </button>
                </div>
              )}
              <SqlCode sql={shown} className="max-h-72" />
              <div className="flex flex-wrap items-center gap-1">
                <CopyButton text={shown} label="Copy SQL" />
                {editorHref && (
                  <Button variant="ghost" size="xs" asChild>
                    <Link href={editorHref}>
                      <SquareTerminal /> Open in SQL editor
                    </Link>
                  </Button>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
