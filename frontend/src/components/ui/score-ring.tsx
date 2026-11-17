"use client";

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";

export function scoreTone(score: number | null | undefined): "success" | "warning" | "danger" | "neutral" {
  if (score === null || score === undefined) return "neutral";
  if (score >= 85) return "success";
  if (score >= 65) return "warning";
  return "danger";
}

const TONE_VAR = {
  success: "var(--success)",
  warning: "var(--warning)",
  danger: "var(--danger)",
  neutral: "var(--fg-subtle)",
} as const;

/** Circular 0–100 score (data quality). `null` renders an em dash, never a fake value. */
export function ScoreRing({
  score,
  size = 56,
  stroke = 5,
  label,
  className,
}: {
  score: number | null | undefined;
  size?: number;
  stroke?: number;
  label?: string;
  className?: string;
}) {
  const reduce = useReducedMotion();
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const value = score === null || score === undefined ? null : Math.max(0, Math.min(100, score));
  const tone = scoreTone(value);
  const offset = value === null ? c : c * (1 - value / 100);
  return (
    <div
      className={cn("relative inline-flex items-center justify-center", className)}
      style={{ width: size, height: size }}
      role="img"
      aria-label={`${label ?? "Score"}: ${value === null ? "not available" : `${Math.round(value)} out of 100`}`}
    >
      <svg width={size} height={size} className="-rotate-90" aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-3)" strokeWidth={stroke} />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={TONE_VAR[tone]}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          initial={{ strokeDashoffset: reduce ? offset : c }}
          animate={{ strokeDashoffset: offset }}
          transition={{ duration: reduce ? 0 : 0.7, ease: [0.2, 0.8, 0.2, 1] }}
        />
      </svg>
      <span
        className="tabular absolute font-semibold text-fg"
        style={{ fontSize: Math.max(11, Math.round(size * 0.26)) }}
      >
        {value === null ? "—" : Math.round(value)}
      </span>
    </div>
  );
}
