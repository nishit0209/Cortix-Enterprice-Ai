import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/overlays";
import { PinDialog } from "./dialogs";

const BASE = "/api/v1/orgs/acme/dashboards";
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/**
 * A backend with no dashboards yet. `POST /dashboards` creates one; the first pin answers `firstPin`, later pins
 * succeed. Every call is recorded as "METHOD url".
 */
function stubBackend(firstPin: () => Response) {
  const calls: string[] = [];
  const dashboards: Array<{ id: string; title: string; is_default: boolean; widget_count: number }> = [];
  let pins = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input).split("?")[0];
      const method = init?.method ?? "GET";
      calls.push(`${method} ${url}`);
      if (url === BASE && method === "GET") return json({ items: dashboards, next_cursor: null, previous_cursor: null });
      if (url === BASE && method === "POST") {
        const body = JSON.parse(String(init?.body)) as { title: string };
        const d = { id: `d${dashboards.length + 1}`, title: body.title, is_default: dashboards.length === 0, widget_count: 0 };
        dashboards.push(d);
        return json({ ...d, widgets: [] }, 201);
      }
      const detail = /^\/api\/v1\/orgs\/acme\/dashboards\/([^/]+)$/.exec(url);
      if (detail && method === "GET") {
        const d = dashboards.find((x) => x.id === detail[1]);
        return d ? json({ ...d, widgets: [] }) : json({ status: 404, code: "not_found", detail: "Not found." }, 404);
      }
      if (/\/widgets$/.test(url) && method === "POST") {
        pins += 1;
        return pins === 1 ? firstPin() : json({ id: "w1", title: "Revenue", kind: "chart", layout: { x: 0, y: 0, w: 6, h: 4 } }, 201);
      }
      return json({ status: 404, code: "not_found", detail: "Not found." }, 404);
    }),
  );
  return calls;
}

function renderPin() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <PinDialog org="acme" messageId="m1" defaultTitle="Revenue by region" chart={null} open onOpenChange={() => {}} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("PinDialog with a new dashboard", () => {
  beforeEach(() => {
    document.cookie = "csrftoken=test-token; path=/";
  });

  it("reuses the dashboard it created when the pin is retried after a failure", async () => {
    const user = userEvent.setup();
    const calls = stubBackend(() => json({ status: 409, code: "pin_not_available", detail: "This answer has no audited query to pin." }, 409));
    renderPin();

    // No dashboards yet, so the dialog offers a new one.
    await user.type(await screen.findByLabelText("New dashboard name"), "Sales");
    await user.click(screen.getByRole("button", { name: /Pin/ }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(calls.filter((c) => c === `POST ${BASE}`)).toHaveLength(1);

    // Retry: the widget goes to the dashboard already created; no second dashboard.
    await waitFor(() => expect(screen.getByRole("button", { name: /Pin/ })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: /Pin/ }));
    expect(await screen.findByText("The answer is on your dashboard.")).toBeInTheDocument();
    expect(calls.filter((c) => c === `POST ${BASE}`)).toHaveLength(1);
    expect(calls.filter((c) => c.startsWith("POST ") && c.endsWith("/widgets"))).toEqual([`POST ${BASE}/d1/widgets`, `POST ${BASE}/d1/widgets`]);
    expect(screen.getByRole("link", { name: "Open dashboard" })).toHaveAttribute("href", "/o/acme/dashboards/d1");
  });
});
