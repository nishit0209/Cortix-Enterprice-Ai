import { Suspense } from "react";
import { PipelinesView } from "@/components/pipelines/pipelines-view";

export default function PipelinesPage() {
  return (
    <Suspense fallback={null}>
      <PipelinesView />
    </Suspense>
  );
}
