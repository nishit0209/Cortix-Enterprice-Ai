import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { OrgProvider, type OrgContextValue } from "@/components/shell/org-context";
import { TooltipProvider } from "@/components/ui/overlays";
import { ApprovalsInbox } from "./approvals";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const APPROVAL = {
  id: "ap1",
  kind: "model_promotion",
  title: "Promote churn model v4 to production",
  impact: "high",
  status: "pending",
  approver_permission: "ml.promote",
  payload: { model_id: "m1", version: 4 },
  created_at: "2026-11-21T10:00:00Z",
};

type Call = { url: string; method: string; body: unknown };

function stubApi(onDecision: (body: Record<string, unknown>) => Response) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ url, method, body });
      if (url.includes("/approvals/ap1/decision")) return onDecision(body);
      if (url.includes("/approvals")) return json({ items: [APPROVAL], next_cursor: null, previous_cursor: null });
      if (url.includes("/csrf")) return json({});
      return json({}, 404);
    }),
  );
  document.cookie = "csrftoken=test";
  return calls;
}

function renderInbox() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const value = { slug: "acme", can: () => true } as unknown as OrgContextValue;
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <OrgProvider value={value}>
          <ApprovalsInbox />
        </OrgProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("ApprovalsInbox", () => {
  it("lists pending approvals from the API with impact and a link to the subject", async () => {
    stubApi(() => json({}));
    renderInbox();
    expect(await screen.findByText("Promote churn model v4 to production")).toBeInTheDocument();
    expect(screen.getByText("High impact")).toBeInTheDocument();
    expect(screen.getByText("Model promotion")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the subject" })).toHaveAttribute("href", "/o/acme/ml/models/m1");
  });

  it("requires the password for a high-impact decision and sends it with the decision", async () => {
    const user = userEvent.setup();
    const calls = stubApi(() => json({ ...APPROVAL, status: "approved" }));
    renderInbox();
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /Approve/ }));
    expect(await within(dialog).findByText(/Enter your password/)).toBeInTheDocument();
    expect(calls.some((c) => c.url.includes("/decision"))).toBe(false);

    await user.type(within(dialog).getByLabelText("Password"), "s3cret-pass");
    await user.type(within(dialog).getByLabelText("Comment"), "Metrics look good");
    await user.click(within(dialog).getByRole("button", { name: /Approve/ }));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/decision"))).toBe(true));
    const decision = calls.find((c) => c.url.includes("/decision"))!;
    expect(decision.method).toBe("POST");
    expect(decision.body).toEqual({ decision: "approve", comment: "Metrics look good", password: "s3cret-pass" });
  });

  it("shows a wrong password next to the field", async () => {
    const user = userEvent.setup();
    stubApi(() => json({ type: "about:blank", title: "Bad", status: 400, code: "reauth_failed", errors: [{ field: "password", code: "reauth_failed", message: "x" }] }, 400));
    renderInbox();
    await user.click(await screen.findByRole("button", { name: "Reject" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Password"), "wrong");
    await user.click(within(dialog).getByRole("button", { name: /Reject/ }));
    expect(await within(dialog).findByText("That password is incorrect.")).toBeInTheDocument();
  });
});
