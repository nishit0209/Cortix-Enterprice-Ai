import { Suspense } from "react";
import { LineageView } from "@/components/lineage/lineage-view";

export default function LineagePage() {
  return (
    <Suspense fallback={null}>
      <LineageView />
    </Suspense>
  );
}
