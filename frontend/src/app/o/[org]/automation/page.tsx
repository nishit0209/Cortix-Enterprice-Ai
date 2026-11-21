import { Suspense } from "react";
import { AutomationView } from "@/components/automation/automation-view";

export default function AutomationPage() {
  return (
    <Suspense>
      <AutomationView />
    </Suspense>
  );
}
