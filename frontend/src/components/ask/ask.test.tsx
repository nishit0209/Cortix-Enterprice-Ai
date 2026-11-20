import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/overlays";
import { AnswerCard } from "./answer-card";
import { StageStepper } from "./stage-stepper";
import { Composer } from "./composer";
import { buildTurns } from "./ask-workspace";
import { mergeLive, type LiveTurn } from "@/hooks/use-chat";
import { initialChatState } from "@/lib/chat/reducer";
import type { AnswerView } from "@/lib/chat/view";
import type { ChatMessage } from "@/lib/types";
import { DocumentAskPanel } from "@/components/documents/ask-panel";
import { docAnswerReducer, initialDocAnswer } from "@/lib/documents/answer";
import { highlightTerms, snippet } from "@/lib/queries/documents";
import { datasetGrowth, dailyRequests, pctChange } from "@/lib/dashboard";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/o/acme/ask",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({}),
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>{ui}</TooltipProvider>
    </QueryClientProvider>,
  );
}

const ctx = { slug: "acme", policy: "synthetic_only", datasetId: "ds1", classification: "internal", canManagePolicy: true };

function view(over: Partial<AnswerView>): AnswerView {
  return {
    key: "k",
    messageId: "m1",
    phase: "done",
    stage: null,
    seenStages: [],
    source: "builder",
    sql: null,
    result: null,
    chart: null,
    insights: [],
    assumptions: [],
    narration: "",
    error: null,
    durationMs: 140,
    suggestions: [],
    feedback: null,
    createdAt: null,
    ...over,
  };
}

const card = (v: AnswerView) => <AnswerCard view={v} question="Revenue by region" ctx={ctx} datasetColumns={[]} onFollowUp={() => {}} canExport />;

describe("AnswerCard", () => {
  it("renders a result as a chart with a table toggle, insights, assumptions and the SQL panel", async () => {
    const user = userEvent.setup();
    wrap(
      card(
        view({
          sql: { raw: "select region, sum(amount) from s group by 1", executed: "SELECT region, SUM(amount) FROM t GROUP BY 1 LIMIT 1001", source: "builder" },
          result: { columns: [{ name: "region", type: "text" }, { name: "amount", type: "numeric" }], rows: [["West", 10], ["East", 5]], row_count: 2, truncated: true },
          chart: { type: "bar", x: "region", y: ["amount"] },
          insights: [{ kind: "top_n", text: "West leads with 10." }],
          assumptions: ["amount is in INR"],
        }),
      ),
    );
    expect(screen.getByText("truncated")).toBeInTheDocument();
    expect(screen.getByText("West leads with 10.")).toBeInTheDocument();
    expect(screen.getByText("amount is in INR")).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Chart type" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Table/ }));
    expect(screen.getAllByText("West").length).toBeGreaterThan(0);
    // SQL transparency panel: collapsed by default, shows the executed SQL and an editor link when opened.
    await user.click(screen.getByRole("button", { name: /SQL/ }));
    expect(screen.getByText(/LIMIT/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open in SQL editor/ })).toHaveAttribute("href", expect.stringContaining("/o/acme/ask/sql?dataset=ds1&sql="));
    expect(screen.getByRole("button", { name: "Mark answer as correct" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /CSV/ })).toBeInTheDocument();
  });

  it("explains a policy block and links to the AI policy settings", () => {
    wrap(card(view({ phase: "error", error: { code: "ai_policy_blocked", detail: null } })));
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("Blocked by your AI data policy")).toBeInTheDocument();
    expect(within(alert).getByRole("link", { name: /Review AI policy/ })).toHaveAttribute("href", "/o/acme/settings/ai-policy");
    expect(within(alert).getByText("Internal")).toBeInTheDocument();
  });

  it("shows a reset countdown for quota exhaustion", () => {
    const resetsAt = new Date(Date.now() + 2 * 3600_000 + 5 * 60_000).toISOString();
    wrap(card(view({ phase: "error", error: { code: "ai_quota_exhausted", detail: null, resetsAt } })));
    expect(screen.getByText("Resets in")).toBeInTheDocument();
    expect(screen.getByText(/^2h 0[45]m$/)).toBeInTheDocument();
  });

  it("shows friendly guard copy and the rejected SQL for guard reject codes", () => {
    wrap(
      card(
        view({
          phase: "error",
          error: { code: "sql_rejected", detail: "multiple_statements", rejectCode: "multiple_statements" },
          sql: { raw: "select 1; drop table x", executed: null, source: "llm" },
        }),
      ),
    );
    expect(screen.getByText("SQL rejected by the guard")).toBeInTheDocument();
    expect(screen.getByText(/Only one statement can run at a time/)).toBeInTheDocument();
    expect(screen.getByText(/drop/)).toBeInTheDocument();
  });

  it("explains a missing Gemini configuration", () => {
    wrap(card(view({ phase: "error", error: { code: "ai_not_configured", detail: null } })));
    expect(screen.getByText("Gemini isn't configured")).toBeInTheDocument();
  });
});

describe("StageStepper", () => {
  it("marks the current stage and the stages already passed", () => {
    render(<StageStepper stage="validating" seen={["authorizing", "resolving", "validating"]} active />);
    const current = screen.getByText("Validating").closest("li")!;
    expect(current).toHaveAttribute("aria-current", "step");
    expect(screen.getByTitle("Generating SQL (skipped)")).toBeInTheDocument();
  });
});

describe("Composer", () => {
  it("sends on Enter, keeps Shift+Enter for new lines and recalls the last question with ArrowUp", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer onSend={onSend} streaming={false} lastQuestion="Top 5 regions" />);
    const box = screen.getByLabelText("Ask a question about your data");
    await user.type(box, "{ArrowUp}");
    expect(box).toHaveValue("Top 5 regions");
    await user.clear(box);
    await user.type(box, "line one{Shift>}{Enter}{/Shift}line two");
    expect(box).toHaveValue("line one\nline two");
    await user.type(box, "{Enter}");
    expect(onSend).toHaveBeenCalledWith("line one\nline two");
    expect(box).toHaveValue("");
  });

  it("stops a streaming answer with Escape", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    render(<Composer onSend={() => {}} onStop={onStop} streaming />);
    await user.keyboard("{Escape}");
    expect(onStop).toHaveBeenCalled();
  });
});

const msg = (id: string, role: "user" | "assistant", at: string, content = id): ChatMessage => ({
  id,
  role,
  content,
  status: "complete",
  error_code: null,
  created_at: at,
  sql: null,
  result: null,
  chart: null,
  insights: [],
  assumptions: [],
});

describe("thread assembly", () => {
  it("pairs questions with answers in time order", () => {
    const turns = buildTurns([msg("a1", "assistant", "2026-01-01T00:00:02Z"), msg("u1", "user", "2026-01-01T00:00:01Z", "q1"), msg("u2", "user", "2026-01-01T00:00:03Z", "q2")]);
    expect(turns.map((t) => [t.question, t.answer?.id ?? null])).toEqual([
      ["q1", "a1"],
      ["q2", null],
    ]);
  });

  it("hides messages newer than the live turn until its persisted answer exists", () => {
    const base = [msg("u1", "user", "2026-01-01T00:00:01Z"), msg("a1", "assistant", "2026-01-01T00:00:02Z")];
    const live: LiveTurn = { turnId: "t", conversationId: "c1", question: "q2", state: { ...initialChatState(0), phase: "streaming" }, baseline: new Set(["u1", "a1"]) };
    const withUser = [...base, msg("u2", "user", "2026-01-01T00:00:03Z")];
    expect(mergeLive(withUser, live, "c1")).toEqual({ messages: base, showLive: true });
    const done: LiveTurn = { ...live, state: { ...live.state, phase: "done", messageId: "a2" } };
    const all = [...withUser, msg("a2", "assistant", "2026-01-01T00:00:04Z")];
    expect(mergeLive(all, done, "c1")).toEqual({ messages: all, showLive: false });
    expect(mergeLive(all, done, "other")).toEqual({ messages: all, showLive: false });
  });
});

describe("documents", () => {
  it("highlights whole query terms case-insensitively and ignores stop words", () => {
    expect(highlightTerms("The Notice period is 30 days; notices go by email.", "what is the notice period")).toEqual([
      { text: "The ", match: false },
      { text: "Notice", match: true },
      { text: " ", match: false },
      { text: "period", match: true },
      { text: " is 30 days; notices go by email.", match: false },
    ]);
    expect(highlightTerms("abc", "")).toEqual([{ text: "abc", match: false }]);
    expect(snippet(`${"x ".repeat(300)}warranty ends ${"y ".repeat(300)}`, "warranty", 20)).toMatch(/^….*warranty.*…$/);
  });

  it("folds streamed and JSON answers, de-duplicating citations", () => {
    let s = docAnswerReducer(initialDocAnswer, { kind: "start" });
    s = docAnswerReducer(s, { kind: "event", event: "mode", data: { mode: "extractive", reason: "policy" } });
    s = docAnswerReducer(s, { kind: "event", event: "citations", data: { citations: [{ document_id: "d1", page: 2, title: "MSA" }] } });
    s = docAnswerReducer(s, { kind: "event", event: "token", data: { text: "30 days" } });
    s = docAnswerReducer(s, { kind: "event", event: "answer", data: { citations: [{ document_id: "d1", page: 2 }, { doc_id: "d1", page_no: 5 }] } });
    s = docAnswerReducer(s, { kind: "event", event: "done", data: {} });
    expect(s).toMatchObject({ phase: "done", mode: "extractive", text: "30 days", reason: "policy" });
    expect(s.citations.map((c) => [c.document_id, c.page, c.document_title])).toEqual([
      ["d1", 2, "MSA"],
      ["d1", 5, undefined],
    ]);
    expect(docAnswerReducer(s, { kind: "event", event: "token", data: { text: "x" } }).text).toBe("30 days");
    const j = docAnswerReducer(docAnswerReducer(initialDocAnswer, { kind: "start" }), { kind: "json", body: { answer: "", mode: "extractive", citations: [], not_found: true } });
    expect(j.notFound).toBe(true);
  });

  it("shows the extractive (policy) badge and clickable citations from a JSON answer", async () => {
    document.cookie = "csrftoken=t";
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ answer: "Notice is 30 days.", mode: "extractive", citations: [{ document_id: "d1", document_title: "Supplier MSA", page: 4, text: "30 days notice" }] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onCitation = vi.fn();
    const user = userEvent.setup();
    wrap(<DocumentAskPanel org="acme" onCitation={onCitation} />);
    await user.type(screen.getByLabelText("Question about your documents"), "notice period");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("extractive (policy)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Supplier MSA/ }));
    expect(onCitation).toHaveBeenCalledWith(expect.objectContaining({ document_id: "d1", page: 4 }));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/orgs/acme/documents/ask");
    expect(JSON.parse(String(init.body))).toEqual({ question: "notice period" });
  });
});

describe("dashboard deltas", () => {
  it("computes percentage changes only when there is a baseline", () => {
    expect(pctChange(12, 10)).toBeCloseTo(20);
    expect(pctChange(5, 0)).toBeNull();
    expect(pctChange(null, 3)).toBeNull();
  });
  it("derives dataset growth from creation dates", () => {
    const now = Date.parse("2026-11-18T00:00:00Z");
    const d = (iso: string) => ({ created_at: iso, status: "ready" });
    expect(datasetGrowth([d("2026-11-17T00:00:00Z"), d("2026-10-01T00:00:00Z"), d("2026-09-01T00:00:00Z")], now)).toEqual({ added: 1, pct: 50 });
    expect(datasetGrowth([d("2026-11-17T00:00:00Z")], now)).toEqual({ added: 1, pct: null });
  });
  it("sums AI requests per day across models and fills gaps", () => {
    const out = dailyRequests({
      range: { from: "2026-11-15", to: "2026-11-17", days: 3, timezone: "America/Los_Angeles" },
      daily: [
        { day: "2026-11-15", model: "a", requests: 2, input_tokens: 0, output_tokens: 0, blocked: 0, errors: 0 },
        { day: "2026-11-15", model: "b", requests: 1, input_tokens: 0, output_tokens: 0, blocked: 0, errors: 0 },
        { day: "2026-11-17", model: "a", requests: 4, input_tokens: 0, output_tokens: 0, blocked: 0, errors: 0 },
      ],
    });
    expect(out).toEqual([
      { day: "2026-11-15", requests: 3 },
      { day: "2026-11-16", requests: 0 },
      { day: "2026-11-17", requests: 4 },
    ]);
  });
});
