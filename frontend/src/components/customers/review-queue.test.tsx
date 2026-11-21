import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { OrgProvider, type OrgContextValue } from "@/components/shell/org-context";
import { TooltipProvider } from "@/components/ui/overlays";
import { ReviewQueue } from "./customers-view";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const match = (id: string, name: string) => ({
  id,
  score: 0.81,
  status: "pending_review",
  features: { email: 0, phone: 1, name: 0.92 },
  record_a: { id: `${id}a`, source_system: "crm", source_key: "C-1", attributes: { name, phone: "+919800000001", email: "a@crm.test" }, profile_id: "p1" },
  record_b: { id: `${id}b`, source_system: "web", source_key: "W-9", attributes: { name, phone: "+919800000001", email: "b@web.test" }, profile_id: "p2" },
  reviewed_by: null,
  decided_at: null,
  note: "",
  created_at: "2026-11-21T08:00:00Z",
});

type Call = { url: string; method: string; body: unknown };

function stubApi() {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url.includes("/decision")) return json({ ...match("m1", "Asha Rao"), status: "confirmed" });
      if (url.includes("/customers/matches")) {
        return url.includes("cursor=")
          ? json({ items: [match("m2", "Ravi Kumar")], next_cursor: null, previous_cursor: null })
          : json({ items: [match("m1", "Asha Rao")], next_cursor: "http://x/api?cursor=abc", previous_cursor: null });
      }
      return json({}, 404);
    }),
  );
  document.cookie = "csrftoken=test";
  return calls;
}

function renderQueue() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const value = { slug: "acme", can: () => true } as unknown as OrgContextValue;
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <OrgProvider value={value}>
          <ReviewQueue />
        </OrgProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Customer 360 review queue", () => {
  it("shows both records side by side with per-feature scores and confirms a merge", async () => {
    const user = userEvent.setup();
    const calls = stubApi();
    renderQueue();
    expect(await screen.findByText("a@crm.test")).toBeInTheDocument();
    expect(screen.getByText("b@web.test")).toBeInTheDocument();
    expect(screen.getByText("Phone")).toBeInTheDocument();
    expect(calls[0].url).toContain("status=pending_review");

    await user.type(screen.getByLabelText("Decision note"), "same phone");
    await user.click(screen.getByRole("button", { name: /Same customer/ }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/customers/matches/m1/decision"))).toBe(true));
    expect(calls.find((c) => c.url.includes("/decision"))!.body).toEqual({ decision: "confirm", note: "same phone" });
  });

  it("pages through the queue with the server cursor", async () => {
    const user = userEvent.setup();
    const calls = stubApi();
    renderQueue();
    await screen.findByText("a@crm.test");
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findAllByText("Ravi Kumar")).not.toHaveLength(0);
    expect(calls.some((c) => c.url.includes("cursor=abc"))).toBe(true);
    expect(screen.getByText("Page 2")).toBeInTheDocument();
  });
});
