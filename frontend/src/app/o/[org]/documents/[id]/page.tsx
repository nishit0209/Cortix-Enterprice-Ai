import { Suspense } from "react";
import { DocumentDetail } from "@/components/documents/document-detail";

export default function DocumentPage() {
  return (
    <Suspense fallback={null}>
      <DocumentDetail />
    </Suspense>
  );
}
