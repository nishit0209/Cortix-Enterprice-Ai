"use client";

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Check, Loader2 } from "lucide-react";
import { CHAT_STAGES, STAGE_LABELS, stageIndex } from "@/lib/chat/reducer";
import type { ChatStage } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Animated stage stepper: authorizing → resolving → generating → validating → executing → explaining.
 * Stages the backend skipped (e.g. a cache hit never "generates") are shown as passed but dimmed.
 */
export function StageStepper({
  stage,
  seen,
  active,
  failed,
}: {
  stage: ChatStage | null;
  seen: ChatStage[];
  active: boolean;
  failed?: boolean;
}) {
  const reduce = useReducedMotion();
  const current = stageIndex(stage);
  return (
    <ol className="flex flex-wrap items-center gap-x-1 gap-y-2" aria-label="Answer progress">
      {CHAT_STAGES.map((s, i) => {
        const done = i < current || (!active && i <= current && !failed);
        const isCurrent = i === current && active;
        const isFailed = failed && i === current;
        const skipped = done && !seen.includes(s);
        return (
          <li key={s} className="flex items-center gap-1" aria-current={isCurrent ? "step" : undefined}>
            <motion.span
              layout={!reduce}
              className={cn(
                "inline-flex h-6 items-center gap-1.5 rounded-full border px-2 text-[11px] font-medium transition-colors duration-200",
                isFailed
                  ? "border-danger/40 bg-danger-soft text-danger"
                  : isCurrent
                    ? "border-accent/40 bg-accent-soft text-accent-soft-fg"
                    : done
                      ? cn("border-transparent bg-surface-2 text-fg-muted", skipped && "opacity-60")
                      : "border-dashed border-border text-fg-subtle",
              )}
              title={skipped ? `${STAGE_LABELS[s]} (skipped)` : STAGE_LABELS[s]}
            >
              {isCurrent ? (
                <Loader2 className="size-3 animate-spin" aria-hidden />
              ) : done ? (
                <Check className="size-3" aria-hidden />
              ) : (
                <span className="size-1.5 rounded-full bg-current opacity-50" aria-hidden />
              )}
              {STAGE_LABELS[s]}
            </motion.span>
            {i < CHAT_STAGES.length - 1 && (
              <span className={cn("h-px w-3 transition-colors duration-300", i < current ? "bg-accent/50" : "bg-border")} aria-hidden />
            )}
          </li>
        );
      })}
      <span className="sr-only" aria-live="polite">
        {active && stage ? `${STAGE_LABELS[stage]}…` : ""}
      </span>
    </ol>
  );
}
