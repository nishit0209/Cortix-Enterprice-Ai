import * as React from "react";
import { cn } from "@/lib/utils";

/** NEXORA mark: an "N" drawn as a directed graph edge between two nodes (data flowing through a governed path). */
export function LogoMark({ size = 28, className }: { size?: number; className?: string }) {
  const id = React.useId().replace(/:/g, "");
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" className={cn("shrink-0", className)} aria-hidden>
      <defs>
        <linearGradient id={`${id}-g`} x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#5b52f0" />
          <stop offset="0.55" stopColor="#7b4dff" />
          <stop offset="1" stopColor="#11a394" />
        </linearGradient>
        <linearGradient id={`${id}-s`} x1="0" y1="0" x2="0" y2="32" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#fff" stopOpacity="0.28" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8.5" fill={`url(#${id}-g)`} />
      <rect x="0.5" y="0.5" width="31" height="31" rx="8" fill="none" stroke={`url(#${id}-s)`} />
      <path
        d="M10.5 21.5V10.5L21.5 21.5V10.5"
        fill="none"
        stroke="#fff"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="10.5" cy="10.5" r="2.3" fill="#fff" />
      <circle cx="21.5" cy="21.5" r="2.3" fill="#fff" />
    </svg>
  );
}

export function Logo({ className, collapsed }: { className?: string; collapsed?: boolean }) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <LogoMark />
      {!collapsed && <span className="text-[15px] font-semibold tracking-[0.14em] text-fg">NEXORA</span>}
    </span>
  );
}
