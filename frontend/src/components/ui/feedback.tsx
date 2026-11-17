"use client";

import * as React from "react";
import { AlertOctagon, Copy, RotateCw } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { describeError } from "@/lib/errors";
import { Button } from "./button";

/* ---------------- Skeleton ---------------- */

export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div aria-hidden className={cn("skeleton rounded-control", className)} {...props} />;
}

export function SkeletonText({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={cn("space-y-2", className)} aria-hidden>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton key={i} className="h-3" style={{ width: `${92 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}

/* ---------------- Empty state ---------------- */

export function EmptyState({
  icon,
  title,
  description,
  action,
  secondaryAction,
  className,
  compact,
  children,
}: {
  icon?: React.ReactNode;
  title: React.ReactNode;
  description?: React.ReactNode;
  action?: React.ReactNode;
  secondaryAction?: React.ReactNode;
  className?: string;
  compact?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center text-center",
        compact ? "gap-2 px-4 py-8" : "gap-3 px-6 py-14",
        className,
      )}
    >
      {icon && (
        <div className="relative mb-1">
          <div className="absolute inset-0 -z-0 scale-150 rounded-full bg-accent/10 blur-xl" aria-hidden />
          <span
            className={cn(
              "relative flex items-center justify-center rounded-card border border-border bg-surface text-fg-muted shadow-soft",
              compact ? "size-9 [&_svg]:size-4" : "size-11 [&_svg]:size-5",
            )}
          >
            {icon}
          </span>
        </div>
      )}
      <div className="max-w-md space-y-1">
        <h3 className={cn("font-semibold text-fg", compact ? "text-sm" : "text-md")}>{title}</h3>
        {description && <p className="text-sm text-fg-muted">{description}</p>}
      </div>
      {children}
      {(action || secondaryAction) && (
        <div className="mt-2 flex flex-wrap items-center justify-center gap-2">
          {action}
          {secondaryAction}
        </div>
      )}
    </div>
  );
}

/* ---------------- Error state ---------------- */

export function ErrorState({
  error,
  onRetry,
  className,
  compact,
  title,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
  compact?: boolean;
  title?: string;
}) {
  const d = describeError(error);
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-center justify-center gap-3 text-center",
        compact ? "px-4 py-8" : "px-6 py-14",
        className,
      )}
    >
      <span className="flex size-10 items-center justify-center rounded-card bg-danger-soft text-danger">
        <AlertOctagon className="size-5" aria-hidden />
      </span>
      <div className="max-w-md space-y-1">
        <h3 className="text-sm font-semibold text-fg">{title ?? d.title}</h3>
        <p className="text-sm text-fg-muted">{d.description}</p>
        {d.action && <p className="text-xs text-fg-subtle">{d.action}</p>}
      </div>
      <div className="flex flex-wrap items-center justify-center gap-2">
        {onRetry && (
          <Button variant="secondary" size="sm" onClick={onRetry}>
            <RotateCw /> Retry
          </Button>
        )}
        {d.requestId && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              void navigator.clipboard?.writeText(d.requestId!);
              toast.success("Request ID copied");
            }}
          >
            <Copy /> <span className="font-mono text-xs">{d.code}</span>
            <span className="font-mono text-xs text-fg-subtle">· {d.requestId.slice(0, 8)}</span>
          </Button>
        )}
      </div>
    </div>
  );
}

/* ---------------- Progress ---------------- */

export function Progress({
  value,
  max = 100,
  tone = "accent",
  className,
  label,
  indeterminate,
}: {
  value: number;
  max?: number;
  tone?: "accent" | "teal" | "success" | "warning" | "danger";
  className?: string;
  label?: string;
  indeterminate?: boolean;
}) {
  const pct = Math.max(0, Math.min(100, max > 0 ? (value / max) * 100 : 0));
  const toneClass = {
    accent: "bg-accent",
    teal: "bg-teal",
    success: "bg-success",
    warning: "bg-warning",
    danger: "bg-danger",
  }[tone];
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={indeterminate ? undefined : Math.round(value)}
      className={cn("h-1.5 w-full overflow-hidden rounded-full bg-surface-3", className)}
    >
      <div
        className={cn("h-full rounded-full transition-[width] duration-300 ease-out", toneClass, indeterminate && "skeleton w-1/3")}
        style={indeterminate ? undefined : { width: `${pct}%` }}
      />
    </div>
  );
}

/* ---------------- Kbd ---------------- */

export function Kbd({ className, ...props }: React.HTMLAttributes<HTMLElement>) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-[4px] border border-border bg-surface-2 px-1 font-sans text-[11px] font-medium text-fg-muted shadow-[inset_0_-1px_0_var(--border)]",
        className,
      )}
      {...props}
    />
  );
}

/* ---------------- Separator ---------------- */

export function Separator({ className, vertical }: { className?: string; vertical?: boolean }) {
  return (
    <div
      role="separator"
      aria-orientation={vertical ? "vertical" : "horizontal"}
      className={cn("shrink-0 bg-border", vertical ? "h-full w-px" : "h-px w-full", className)}
    />
  );
}
