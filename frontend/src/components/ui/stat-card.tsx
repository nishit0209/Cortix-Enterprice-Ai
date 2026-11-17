"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import { motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";
import { Skeleton } from "./feedback";

/** Pure-SVG sparkline (no axes). Renders nothing for < 2 points: never fabricates a trend. */
export function Sparkline({
  data,
  width = 96,
  height = 28,
  color = "var(--chart-1)",
  className,
  label,
}: {
  data: number[];
  width?: number;
  height?: number;
  color?: string;
  className?: string;
  label?: string;
}) {
  const gradId = React.useId().replace(/:/g, "");
  if (!data || data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const step = width / (data.length - 1);
  const pts = data.map((v, i) => [i * step, height - 2 - ((v - min) / span) * (height - 4)] as const);
  const line = pts.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${width},${height} L0,${height} Z`;
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
      role="img"
      aria-label={label ?? `Trend over ${data.length} points`}
      data-testid="sparkline"
    >
      <defs>
        <linearGradient id={gradId} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.22} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gradId})`} />
      <path d={line} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export interface StatCardProps {
  label: React.ReactNode;
  value: React.ReactNode;
  icon?: React.ReactNode;
  hint?: React.ReactNode;
  delta?: { value: number; label?: string; positiveIsGood?: boolean } | null;
  sparkline?: number[] | null;
  sparklineColor?: string;
  footer?: React.ReactNode;
  loading?: boolean;
  href?: string;
  className?: string;
  accent?: "accent" | "teal" | "warning" | "success";
}

/** KPI card: label, big tabular number, optional delta + sparkline + footer slot. */
export function StatCard({
  label,
  value,
  icon,
  hint,
  delta,
  sparkline,
  sparklineColor,
  footer,
  loading,
  href,
  className,
  accent = "accent",
}: StatCardProps) {
  const reduce = useReducedMotion();
  const accentBar = {
    accent: "from-accent/70",
    teal: "from-teal/70",
    warning: "from-warning/70",
    success: "from-success/70",
  }[accent];
  const body = (
    <motion.div
      initial={reduce ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
      className={cn(
        "group relative flex h-full flex-col overflow-hidden rounded-card border border-border bg-surface p-5 shadow-xs",
        href && "transition-[border-color,box-shadow] duration-150 hover:border-border-strong hover:shadow-soft",
        className,
      )}
    >
      <div className={cn("pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r to-transparent", accentBar)} aria-hidden />
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-fg-muted">{label}</span>
        {icon && (
          <span className="flex size-7 items-center justify-center rounded-control bg-surface-2 text-fg-subtle transition-colors group-hover:text-fg-muted [&_svg]:size-4">
            {icon}
          </span>
        )}
      </div>
      <div className="mt-3 flex items-end justify-between gap-3">
        {loading ? (
          <Skeleton className="h-8 w-24" />
        ) : (
          <span className="tabular text-2xl font-semibold tracking-[-0.02em] text-fg">{value}</span>
        )}
        {!loading && sparkline && sparkline.length > 1 && (
          <Sparkline data={sparkline} color={sparklineColor} className="mb-1 shrink-0" />
        )}
      </div>
      <div className="mt-2 flex min-h-5 items-center gap-2 text-xs text-fg-muted">
        {loading ? (
          <Skeleton className="h-3 w-32" />
        ) : (
          <>
            {delta && <Delta {...delta} />}
            {hint && <span className="truncate">{hint}</span>}
          </>
        )}
      </div>
      {footer && <div className="mt-3">{footer}</div>}
    </motion.div>
  );
  return href ? (
    <Link href={href} className="block h-full rounded-card focus-ring">
      {body}
    </Link>
  ) : (
    body
  );
}

export const KpiCard = StatCard;

function Delta({ value, label, positiveIsGood = true }: { value: number; label?: string; positiveIsGood?: boolean }) {
  const up = value >= 0;
  const good = up === positiveIsGood;
  return (
    <span className={cn("tabular inline-flex items-center gap-0.5 font-medium", good ? "text-success" : "text-danger")}>
      {up ? <ArrowUpRight className="size-3.5" aria-hidden /> : <ArrowDownRight className="size-3.5" aria-hidden />}
      {Math.abs(value).toFixed(1)}%{label && <span className="ml-1 font-normal text-fg-muted">{label}</span>}
    </span>
  );
}
