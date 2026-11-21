import { Badge } from "@/components/ui/badge";
import type { DocumentItem } from "@/lib/types";

/** "Embedding pending" while a ready document's chunks still wait for local embeddings (keyword search still works). */
export function EmbeddingPendingBadge({ doc }: { doc: Pick<DocumentItem, "status" | "embedding_status"> }) {
  if (doc.status !== "ready" || doc.embedding_status !== "pending") return null;
  return (
    <Badge tone="info" title="Semantic search for this document starts once its embeddings are computed; keyword search already works.">
      Embedding pending
    </Badge>
  );
}
