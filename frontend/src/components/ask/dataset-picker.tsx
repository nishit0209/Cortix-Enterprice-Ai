"use client";

import * as React from "react";
import { Database } from "lucide-react";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ClassificationBadge } from "@/components/ui/status";
import type { Dataset } from "@/lib/types";
import { cn, formatNumber } from "@/lib/utils";

export const isReadyDataset = (d: Dataset) => String(d.status) === "ready";

/** Dataset picker limited to ready datasets (only those can be queried). */
export function DatasetPicker({
  datasets,
  value,
  onChange,
  disabled,
  className,
  label = "Dataset",
}: {
  datasets: Dataset[];
  value: string | null;
  onChange: (id: string) => void;
  disabled?: boolean;
  className?: string;
  label?: string;
}) {
  const ready = datasets.filter(isReadyDataset);
  return (
    <Select value={value ?? undefined} onValueChange={onChange} disabled={disabled || ready.length === 0}>
      <SelectTrigger className={cn("h-9", className)} aria-label={label}>
        <span className="flex min-w-0 items-center gap-2">
          <Database className="size-4 shrink-0 text-fg-subtle" aria-hidden />
          <SelectValue placeholder={ready.length === 0 ? "No ready datasets" : "Choose a dataset"} />
        </span>
      </SelectTrigger>
      <SelectContent>
        {ready.map((d) => (
          <SelectItem
            key={d.id}
            value={d.id}
            description={
              <span className="flex items-center gap-2">
                <ClassificationBadge value={d.classification} />
                <span className="tabular">{formatNumber(d.row_count, { compact: true })} rows</span>
              </span>
            }
          >
            {d.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
