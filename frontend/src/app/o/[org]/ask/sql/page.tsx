import { Suspense } from "react";
import { SqlEditorView } from "@/components/ask/sql-editor";

export default function AskSqlPage() {
  return (
    <Suspense fallback={null}>
      <SqlEditorView />
    </Suspense>
  );
}
