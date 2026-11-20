/**
 * Cache behaviour of the dashboard hooks. Each widget-data fetch re-runs a saved query through the SQL guard and the
 * warehouse (and writes an audit row), so dashboard-level mutations must neither cancel nor re-run them.
 */
import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/overlays";
import { WidgetCard } from "@/components/dashboards/widget-card";
import { dashKeys, useCreateWidget, useDeleteWidget, useSaveLayout, useUpdateDashboard } from "./dashboards";
import type { Dashboard, DashboardWidget, WidgetData } from "@/lib/types";

const BASE = "/api/v1/orgs/acme/dashboards";
const DATA_URL = `${BASE}/d1/widgets/w1/data`;

const widget: DashboardWidget = { id: "w1", title: "Regions", kind: "table", layout: { x: 0, y: 0, w: 6, h: 4 }, chart: null };
const other: DashboardWidget = { id: "w2", title: "Notes", kind: "text", layout: { x: 6, y: 0, w: 6, h: 4 }, chart: null, text: "hi" };
const dashboard = { id: "d1", title: "Sales", widgets: [widget, other] } as Dashboard;
const data: WidgetData = {
  columns: [{ name: "region", type: "text" }],
  rows: [["North"], ["South"]],
  row_count: 2,
  truncated: false,
  chart: null,
  refreshed_at: new Date().toISOString(),
  query_audit_id: "qa1",
};

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Call = { method: string; url: string };

/** Answer the API by method + path; widget data is answered by `dataReply` (which may be a pending promise). */
function stubApi(dataReply: () => Promise<Response>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      calls.push({ method, url });
      if (url === DATA_URL) return dataReply();
      if (url === `${BASE}/d1` && method === "PATCH") return json(dashboard);
      if (url === `${BASE}/d1` && method === "GET") return json(dashboard);
      if (url.startsWith(BASE) && method === "GET") return json([{ id: "d1", title: "Sales", widget_count: 2 }]);
      if (url === `${BASE}/d1/widgets` && method === "POST") return json({ ...widget, id: "w3" }, 201);
      if (url === `${BASE}/d1/widgets/w2` && method === "DELETE") return new Response(null, { status: 204 });
      return json({ status: 404, code: "not_found", detail: "Not found." }, 404);
    }),
  );
  return calls;
}

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  qc.setQueryData(dashKeys.detail("acme", "d1"), dashboard);
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>
      <TooltipProvider>{children}</TooltipProvider>
    </QueryClientProvider>
  );
  render(<WidgetCard org="acme" dashboardId="d1" widget={widget} bodyHeight={240} />, { wrapper });
  return { qc, wrapper };
}

const dataCalls = (calls: Call[]) => calls.filter((c) => c.url === DATA_URL).length;

describe("dashboard query keys", () => {
  it("keeps widget data outside the dashboards list/detail prefix", () => {
    const list = dashKeys.list("acme");
    const data = dashKeys.widgetData("acme", "d1", "w1");
    expect(data.slice(0, list.length)).not.toEqual(list);
    expect(data.slice(0, dashKeys.widgetDataAll("acme", "d1").length)).toEqual([...dashKeys.widgetDataAll("acme", "d1")]);
  });
});

describe("dashboard mutations leave widget data alone", () => {
  beforeEach(() => {
    document.cookie = "csrftoken=test-token; path=/";
  });

  it("saving the layout does not cancel a widget whose data is still loading", async () => {
    let release: (r: Response) => void = () => {};
    const calls = stubApi(() => new Promise<Response>((res) => (release = res)));
    const { wrapper } = setup();
    expect(screen.getByRole("status", { name: "Loading Regions" })).toBeInTheDocument();
    await waitFor(() => expect(dataCalls(calls)).toBe(1));

    const save = renderHook(() => useSaveLayout("acme", "d1"), { wrapper });
    await act(async () => {
      await save.result.current.mutateAsync([{ id: "w1", x: 0, y: 2, w: 6, h: 4 }]);
    });
    expect(calls.some((c) => c.method === "PATCH" && c.url === `${BASE}/d1`)).toBe(true);
    // Still loading (not reverted to an empty tile), and it completes with the rows.
    expect(screen.getByRole("status", { name: "Loading Regions" })).toBeInTheDocument();
    act(() => release(json(data)));
    expect(within(await screen.findByRole("table")).getByText("North")).toBeInTheDocument();
    expect(dataCalls(calls)).toBe(1);
  });

  it("renaming a dashboard, adding a widget or removing one does not re-run the other widgets' queries", async () => {
    const calls = stubApi(async () => json(data));
    const { qc, wrapper } = setup();
    await screen.findByRole("table");
    expect(dataCalls(calls)).toBe(1);

    const update = renderHook(() => useUpdateDashboard("acme"), { wrapper });
    const create = renderHook(() => useCreateWidget("acme"), { wrapper });
    const remove = renderHook(() => useDeleteWidget("acme", "d1"), { wrapper });
    await act(async () => {
      await update.result.current.mutateAsync({ id: "d1", title: "Sales 2026", is_default: true });
      await create.result.current.mutateAsync({ dashboardId: "d1", title: "More", kind: "table", saved_query_id: "q1", layout: { x: 0, y: 4, w: 6, h: 4 } });
      await remove.result.current.mutateAsync("w2");
    });
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    expect(dataCalls(calls)).toBe(1);

    // "Refresh all" is the explicit way to re-run every widget.
    await act(async () => {
      await qc.invalidateQueries({ queryKey: dashKeys.widgetDataAll("acme", "d1") });
    });
    expect(dataCalls(calls)).toBe(2);
  });

  it("offers to load a widget whose request was cancelled instead of rendering an empty tile", async () => {
    let release: (r: Response) => void = () => {};
    const calls = stubApi(() => new Promise<Response>((res) => (release = res)));
    const { qc } = setup();
    await waitFor(() => expect(dataCalls(calls)).toBe(1));
    await act(async () => {
      await qc.cancelQueries({ queryKey: dashKeys.widgetData("acme", "d1", "w1") });
    });
    expect(await screen.findByRole("note", { name: "Regions not loaded" })).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Loading Regions" })).toBeNull();
    release(json(data));

    stubApi(async () => json(data));
    act(() => screen.getByRole("button", { name: "Load data" }).click());
    expect(within(await screen.findByRole("table")).getByText("North")).toBeInTheDocument();
  });
});
