"use client";

import { Suspense } from "react";
import { DatasetsView } from "@/components/datasets/datasets-view";

export default function DatasetsPage() {
  return (
    <Suspense>
      <DatasetsView />
    </Suspense>
  );
}
