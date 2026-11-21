"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "./button";
import { cn } from "@/lib/utils";

/** Previous / next controls for cursor-paged lists (`useCursorList`). */
export function Pager({
  page,
  hasPrev,
  hasNext,
  onPrev,
  onNext,
  loading,
  className,
}: {
  page: number;
  hasPrev: boolean;
  hasNext: boolean;
  onPrev: () => void;
  onNext: () => void;
  loading?: boolean;
  className?: string;
}) {
  if (!hasPrev && !hasNext) return null;
  return (
    <nav aria-label="Pagination" className={cn("flex items-center justify-end gap-2 pt-3 text-xs text-fg-muted", className)}>
      <Button variant="secondary" size="xs" onClick={onPrev} disabled={!hasPrev || loading}>
        <ChevronLeft /> Previous
      </Button>
      <span className="tabular">Page {page}</span>
      <Button variant="secondary" size="xs" onClick={onNext} disabled={!hasNext || loading}>
        Next <ChevronRight />
      </Button>
    </nav>
  );
}
