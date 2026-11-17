"use client";

import * as React from "react";
import { Card } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/feedback";

/** Route-level error boundary inside the app shell (keeps navigation usable). */
export default function OrgError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  React.useEffect(() => {
    console.error(error);
  }, [error]);
  return (
    <Card>
      <ErrorState error={error} onRetry={reset} title="This page hit an unexpected error" />
    </Card>
  );
}
