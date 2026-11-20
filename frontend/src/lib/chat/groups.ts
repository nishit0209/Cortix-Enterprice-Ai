import type { Conversation } from "@/lib/types";

export interface ConversationGroup {
  label: string;
  items: Conversation[];
}

const DAY = 86_400_000;

function startOfDay(t: number): number {
  const d = new Date(t);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/** Group conversations (most recent first) into Today / Yesterday / Previous 7 days / Previous 30 days / Older. */
export function groupConversations(convs: Conversation[], now: number = Date.now()): ConversationGroup[] {
  const today = startOfDay(now);
  const buckets: Array<[string, (t: number) => boolean]> = [
    ["Today", (t) => t >= today],
    ["Yesterday", (t) => t >= today - DAY],
    ["Previous 7 days", (t) => t >= today - 7 * DAY],
    ["Previous 30 days", (t) => t >= today - 30 * DAY],
    ["Older", () => true],
  ];
  const sorted = [...convs].sort((a, b) => activity(b) - activity(a));
  const groups = new Map<string, Conversation[]>();
  for (const c of sorted) {
    const t = activity(c);
    const label = buckets.find(([, test]) => test(t))![0];
    groups.set(label, [...(groups.get(label) ?? []), c]);
  }
  return buckets.map(([label]) => ({ label, items: groups.get(label) ?? [] })).filter((g) => g.items.length > 0);
}

export function activity(c: Conversation): number {
  const t = new Date(c.last_message_at ?? c.created_at).getTime();
  return Number.isNaN(t) ? 0 : t;
}

/** A title from the first question: single line, ≤ 60 characters, cut at a word boundary. */
export function titleFromQuestion(q: string): string {
  const one = q.replace(/\s+/g, " ").trim();
  if (one.length <= 60) return one;
  const cut = one.slice(0, 60);
  const sp = cut.lastIndexOf(" ");
  return `${(sp > 30 ? cut.slice(0, sp) : cut).trim()}…`;
}
