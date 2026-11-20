import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/overlays";
import { WidgetCard } from "./widget-card";
import type { DashboardWidget, WidgetData } from "@/lib/types";

const DATA_URL = "/api/v1/orgs/acme/dashboards/d1/widgets/w1/data";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** Stub the API: every call is answered by `reply` and recorded. */
function stubApi(reply: () => Response) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return reply();
    }),
  );
  return calls;
}

function renderWidget(widget: Partial<DashboardWidget>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const w: DashboardWidget = { id: "w1", title: "Revenue by region", kind: "chart", layout: { x: 0, y: 0, w: 6, h: 4 }, chart: null, ...widget };
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <WidgetCard org="acme" dashboardId="d1" widget={w} bodyHeight={240} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

const data = (over: Partial<WidgetData> = {}): WidgetData => ({
  columns: [
    { name: "region", type: "text" },
    { name: "revenue", type: "numeric" },
  ],
  rows: [
    ["North", 120],
    ["South", 75],
  ],
  row_count: 2,
  truncated: false,
  chart: { type: "bar", x: "region", y: ["revenue"], series: null },
  refreshed_at: new Date().toISOString(),
  query_audit_id: "qa1",
  ...over,
});

describe("WidgetCard renders live /widgets/{id}/data", () => {
  it("shows a loading state, then the KPI value from the first row", async () => {
    const calls = stubApi(() => json(data({ columns: [{ name: "orders", type: "bigint" }, { name: "region", type: "text" }], rows: [[42, "West"]], row_count: 1 })));
    renderWidget({ kind: "kpi", title: "Orders" });
    expect(screen.getByRole("status", { name: "Loading Orders" })).toBeInTheDocument();
    const kpi = await screen.findByTestId("kpi-widget");
    expect(kpi).toHaveTextContent("42");
    expect(kpi).toHaveTextContent("region: West");
    expect(calls).toEqual([DATA_URL]);
  });

  it("renders a table widget's rows and notes truncation", async () => {
    stubApi(() => json(data({ truncated: true })));
    renderWidget({ kind: "table", title: "Regions" });
    const table = await screen.findByRole("table");
    expect(within(table).getByText("North")).toBeInTheDocument();
    expect(within(table).getByText("South")).toBeInTheDocument();
    expect(screen.getByText("Showing the first 2 rows")).toBeInTheDocument();
  });

  it("renders a chart widget from the data (no error, no empty state)", async () => {
    stubApi(() => json(data()));
    const { container } = renderWidget({ kind: "chart" });
    await waitFor(() => expect(container.querySelector(".recharts-responsive-container")).not.toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("The query returned no rows.")).toBeNull();
  });

  it("says when the query returned no rows", async () => {
    stubApi(() => json(data({ rows: [], row_count: 0 })));
    renderWidget({ kind: "chart" });
    expect(await screen.findByText("The query returned no rows.")).toBeInTheDocument();
  });

  it("re-runs the query on refresh", async () => {
    const user = userEvent.setup();
    const calls = stubApi(() => json(data()));
    renderWidget({ kind: "table", title: "Regions" });
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Refresh Regions" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls.every((c) => c === DATA_URL)).toBe(true);
  });

  it("shows a permission state (no retry) when the viewer may not query the dataset", async () => {
    stubApi(() => json({ status: 404, code: "not_found", detail: "Not found." }, 404));
    renderWidget({ kind: "table" });
    expect(await screen.findByText("No access to this data")).toBeInTheDocument();
    expect(screen.getByRole("note")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("explains a deleted saved query and a guard rejection under the viewer's policy", async () => {
    stubApi(() => json({ status: 409, code: "widget_query_missing", detail: "The saved query of this widget was deleted." }, 409));
    const first = renderWidget({ kind: "chart" });
    expect(await screen.findByText("Query deleted")).toBeInTheDocument();
    first.unmount();

    stubApi(() => json({ status: 422, code: "column_not_allowed", detail: "denied" }, 422));
    renderWidget({ kind: "chart" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Query rejected under your policy");
    expect(alert).toHaveTextContent("The query references a column your access policy denies.");
    expect(within(alert).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("renders a text widget without any request", () => {
    const calls = stubApi(() => json({}));
    renderWidget({ kind: "text", title: "Notes", text: "Numbers refresh on every view." });
    expect(screen.getByText("Numbers refresh on every view.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Refresh/ })).toBeNull();
    expect(calls).toHaveLength(0);
  });
});
