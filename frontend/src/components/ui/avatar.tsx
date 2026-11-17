"use client";

import * as React from "react";
import * as AvatarPrimitive from "@radix-ui/react-avatar";
import { cn, initials } from "@/lib/utils";

// Deterministic, theme-aware tint per identity (no external images by default).
const TINTS = [
  "bg-accent-soft text-accent-soft-fg",
  "bg-teal-soft text-teal",
  "bg-info-soft text-info",
  "bg-warning-soft text-warning",
  "bg-success-soft text-success",
];

function hash(s: string) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

export function Avatar({
  name,
  src,
  size = 28,
  className,
  square,
}: {
  name: string | null | undefined;
  src?: string | null;
  size?: number;
  className?: string;
  square?: boolean;
}) {
  const label = name || "Unknown";
  const tint = TINTS[hash(label) % TINTS.length];
  return (
    <AvatarPrimitive.Root
      className={cn(
        "relative inline-flex shrink-0 select-none items-center justify-center overflow-hidden font-semibold",
        square ? "rounded-control" : "rounded-full",
        className,
      )}
      style={{ width: size, height: size, fontSize: Math.max(10, Math.round(size * 0.38)) }}
    >
      {src && <AvatarPrimitive.Image src={src} alt={label} className="size-full object-cover" />}
      <AvatarPrimitive.Fallback className={cn("flex size-full items-center justify-center", tint)} aria-label={label}>
        {initials(label)}
      </AvatarPrimitive.Fallback>
    </AvatarPrimitive.Root>
  );
}
