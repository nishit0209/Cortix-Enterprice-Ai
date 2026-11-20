"use client";

import * as React from "react";
import { CircleDashed } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { EmptyState, ErrorState } from "@/components/ui/feedback";
import { isUnavailableEndpoint } from "@/lib/errors";
import type { ModelStage, TaskType } from "@/lib/ml/types";
import { titleCase } from "@/lib/utils";

export const TASK_LABELS: Record<TaskType, string> = {
  classification: "Classification",
  regression: "Regression",
  forecast: "Forecast",
  clustering: "Clustering",
};

export const TASK_HINTS: Record<TaskType, string> = {
  classification: "Predict a category, e.g. churn yes/no.",
  regression: "Predict a number, e.g. next month's revenue.",
  forecast: "Project a numeric series forward in time.",
  clustering: "Group similar rows; no target column.",
};

export function TaskBadge({ task }: { task: TaskType | null }) {
  if (!task) return null;
  const tone = { classification: "accent", regression: "teal", forecast: "info", clustering: "neutral" } as const;
  return <Badge tone={tone[task]}>{TASK_LABELS[task]}</Badge>;
}

const STAGE_TONE: Record<string, "success" | "info" | "neutral" | "outline"> = {
  production: "success",
  staging: "info",
  archived: "neutral",
  none: "outline",
};

export function StageBadge({ stage }: { stage: ModelStage | string }) {
  return (
    <Badge tone={STAGE_TONE[stage] ?? "neutral"} dot={stage === "production"}>
      {stage === "none" ? "Registered" : titleCase(stage)}
    </Badge>
  );
}

/** Error state that explains a 404/405/501 as "not deployed yet" instead of a failure. */
export function MlError({ error, onRetry, what, compact }: { error: unknown; onRetry?: () => void; what: string; compact?: boolean }) {
  if (isUnavailableEndpoint(error)) {
    return (
      <EmptyState
        compact={compact}
        icon={<CircleDashed />}
        title={`${what} isn't available yet`}
        description="The ML service on this server doesn't provide this yet. Nothing is simulated; this view fills in once the backend is deployed."
      />
    );
  }
  return <ErrorState compact={compact} error={error} onRetry={onRetry} />;
}
