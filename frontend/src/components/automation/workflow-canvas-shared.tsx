import * as React from "react";
import { Bell, Bot, BrainCircuit, Clock, Database, FileText, GitBranch, Hand, Layers, ShoppingBasket, UserRound } from "lucide-react";
import type { StepKind } from "@/lib/automation/catalog";

/** Shared by the editor and the (dynamically loaded) canvas; keeps @xyflow/react out of the editor chunk. */
export const DND_TYPE = "application/x-nexora-wf-step";

const ICONS: Record<string, React.ReactNode> = {
  condition: <GitBranch />,
  notify: <Bell />,
  approval: <Hand />,
  wait: <Clock />,
  data: <Database />,
  bi: <Layers />,
  customers: <UserRound />,
  products: <ShoppingBasket />,
  ml: <BrainCircuit />,
  documents: <FileText />,
};

export function stepIcon(k: Pick<StepKind, "kind" | "category">): React.ReactNode {
  return ICONS[k.kind] ?? ICONS[k.category] ?? <Bot />;
}
