import { Suspense } from "react";
import { ProjectView } from "@/components/ml/project-view";

export default function MlProjectPage() {
  return (
    <Suspense fallback={null}>
      <ProjectView />
    </Suspense>
  );
}
