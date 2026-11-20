import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import {
  cellsFromPixels,
  clampItem,
  collides,
  GRID_COLS,
  gridItemsFromWidgets,
  keyboardChange,
  layoutPatch,
  MAX_H,
  moveItem,
  placeNew,
  readingOrder,
  resizeItem,
  sameLayout,
  type GridItem,
} from "./layout";
import { pickHomeDashboard } from "./home";
import { chartSpecFor, kpiFromData, widgetErrorState } from "./widgets";
import type { DashboardSummary, WidgetData } from "@/lib/types";

/** Mirror of the backend's `apps.bi.dashboards.clean_layout` bounds (12 columns, 1 <= h <= 24, y <= 1000). */
function backendAccepts(i: GridItem): boolean {
  const ints = [i.x, i.y, i.w, i.h].every((v) => Number.isInteger(v));
  return ints && i.x >= 0 && i.x < 12 && i.w >= 1 && i.w <= 12 && i.x + i.w <= 12 && i.y >= 0 && i.y <= 1000 && i.h >= 1 && i.h <= 24;
}

function noOverlaps(items: GridItem[]): boolean {
  return items.every((a, i) => items.every((b, j) => i === j || !collides(a, b)));
}

const W = (id: string, x: number, y: number, w: number, h: number): GridItem => ({ id, x, y, w, h });

describe("dashboard layout serialization (PATCH /dashboards/{id} layout[])", () => {
  it("serializes exactly [{id, x, y, w, h}] for every widget, dropping any other field", () => {
    const items = [
      { ...W("a", 0, 0, 6, 4), title: "Revenue", kind: "chart" },
      { ...W("b", 6, 0, 6, 4), extra: { nested: true } },
    ] as unknown as GridItem[];
    const body = layoutPatch(items);
    expect(body).toEqual({ layout: [W("a", 0, 0, 6, 4), W("b", 6, 0, 6, 4)] });
    // What goes over the wire is the same after a JSON round-trip (no Dates, no undefined, no functions).
    expect(JSON.parse(JSON.stringify(body))).toEqual(body);
    expect(Object.keys(body.layout[0]).sort()).toEqual(["h", "id", "w", "x", "y"]);
  });

  it("turns stored widgets into clamped, non-overlapping integer positions the backend accepts", () => {
    const items = gridItemsFromWidgets([
      { id: "a", layout: { x: 0, y: 0, w: 6, h: 4 } },
      { id: "b", layout: { x: 3, y: 1, w: 6, h: 4 } }, // overlaps a
      { id: "c", layout: { x: 11, y: 2.6, w: 6, h: 40 } }, // too wide, too tall, fractional
      { id: "d", layout: null }, // missing -> backend default 6x4
      { id: "e", layout: { x: -3, y: -1, w: 0, h: 0 } },
    ]);
    expect(items.map((i) => i.id).sort()).toEqual(["a", "b", "c", "d", "e"]);
    expect(items.every(backendAccepts)).toBe(true);
    expect(noOverlaps(items)).toBe(true);
    const c = items.find((i) => i.id === "c")!;
    expect(c.w).toBe(6);
    expect(c.x).toBe(GRID_COLS - 6);
    expect(c.h).toBe(MAX_H);
    const d = items.find((i) => i.id === "d")!;
    expect([d.w, d.h]).toEqual([6, 4]);
  });

  it("keeps every layout valid and overlap-free through a long sequence of moves and resizes", () => {
    let items = gridItemsFromWidgets(Array.from({ length: 9 }, (_, i) => ({ id: `w${i}`, layout: { x: (i % 3) * 4, y: Math.floor(i / 3) * 3, w: 4, h: 3 } })));
    // Deterministic pseudo-random gestures (LCG), including out-of-bounds targets.
    let seed = 7;
    const rnd = (n: number) => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed % n;
    };
    for (let step = 0; step < 200; step++) {
      const id = `w${rnd(9)}`;
      items = step % 3 === 0 ? resizeItem(items, id, { w: rnd(16) - 2, h: rnd(30) - 2 }) : moveItem(items, id, { x: rnd(16) - 3, y: rnd(20) - 3 });
      expect(items.every(backendAccepts)).toBe(true);
      expect(noOverlaps(items)).toBe(true);
    }
    const body = layoutPatch(items);
    expect(body.layout).toHaveLength(9);
    expect(new Set(body.layout.map((l) => l.id)).size).toBe(9);
  });

  it("moving a widget onto another pushes the other one down and keeps input order", () => {
    const items = [W("a", 0, 0, 6, 4), W("b", 6, 0, 6, 4), W("c", 0, 4, 12, 2)];
    const next = moveItem(items, "c", { y: 0 });
    expect(next.map((i) => i.id)).toEqual(["a", "b", "c"]);
    const c = next.find((i) => i.id === "c")!;
    expect(c).toMatchObject({ x: 0, y: 0, w: 12, h: 2 });
    expect(next.find((i) => i.id === "a")!.y).toBe(2);
    expect(next.find((i) => i.id === "b")!.y).toBe(2);
    expect(noOverlaps(next)).toBe(true);
  });

  it("compacts upwards after a widget moves away", () => {
    const items = [W("a", 0, 0, 12, 2), W("b", 0, 2, 12, 3)];
    const next = moveItem(items, "a", { y: 10 });
    expect(next.find((i) => i.id === "b")!.y).toBe(0);
    expect(next.find((i) => i.id === "a")!.y).toBe(3);
  });

  it("clamps resizes to the 12-column grid and the 24-row tile limit", () => {
    const next = resizeItem([W("a", 8, 0, 4, 2)], "a", { w: 9, h: 99 });
    expect(next[0]).toMatchObject({ w: 9, h: 24, x: 3 });
    expect(clampItem(W("z", 4.4, 1.6, 3.5, 2.2))).toEqual(W("z", 4, 2, 4, 2));
  });

  it("only sends a PATCH when a position actually changed", () => {
    const a = [W("a", 0, 0, 6, 4), W("b", 6, 0, 6, 4)];
    expect(sameLayout(a, [W("b", 6, 0, 6, 4), W("a", 0, 0, 6, 4)])).toBe(true);
    expect(sameLayout(a, [W("a", 0, 0, 6, 4), W("b", 6, 0, 6, 5)])).toBe(false);
    expect(sameLayout(a, [W("a", 0, 0, 6, 4)])).toBe(false);
  });

  it("places a new widget in the first free gap of its size", () => {
    expect(placeNew([W("a", 0, 0, 6, 4)], { w: 6, h: 4 })).toEqual({ x: 6, y: 0, w: 6, h: 4 });
    expect(placeNew([W("a", 0, 0, 12, 4)], { w: 3, h: 2 })).toEqual({ x: 0, y: 4, w: 3, h: 2 });
    expect(placeNew([], { w: 20, h: 40 })).toEqual({ x: 0, y: 0, w: 12, h: 24 });
  });

  it("maps pointer pixels and arrow keys to whole grid cells", () => {
    // 1200px wide, 16px gaps -> (1200 - 11 * 16) / 12 = 85.33px columns; rows are 76px + 16px gap.
    expect(cellsFromPixels(101, 92, 1200)).toEqual({ dx: 1, dy: 1 });
    expect(cellsFromPixels(-20, -20, 1200)).toEqual({ dx: 0, dy: 0 });
    expect(cellsFromPixels(-310, 0, 1200)).toEqual({ dx: -3, dy: 0 });
    expect(keyboardChange(W("a", 2, 2, 4, 3), "ArrowRight", false)).toEqual({ x: 3, y: 2 });
    expect(keyboardChange(W("a", 2, 2, 4, 3), "ArrowDown", true)).toEqual({ w: 4, h: 4 });
    expect(keyboardChange(W("a", 2, 2, 4, 3), "Enter", false)).toBeNull();
    expect(readingOrder([W("b", 6, 0, 6, 4), W("c", 0, 4, 6, 4), W("a", 0, 0, 6, 4)]).map((i) => i.id)).toEqual(["a", "b", "c"]);
  });
});

describe("home dashboard selection", () => {
  const d = (over: Partial<DashboardSummary>): DashboardSummary => ({
    id: "x",
    title: "T",
    description: "",
    is_default: false,
    widget_count: 0,
    updated_at: "2026-11-01T00:00:00Z",
    is_shared: false,
    owner_id: "me",
    ...over,
  });

  it("prefers the caller's own default over a teammate's shared default", () => {
    const list = [
      d({ id: "theirs", owner_id: "u2", is_default: true, is_shared: true, updated_at: "2026-11-19T00:00:00Z" }),
      d({ id: "mine", owner_id: "me", is_default: true }),
    ];
    expect(pickHomeDashboard(list, "me")?.id).toBe("mine");
  });

  it("falls back to the most recently updated shared default, else nothing", () => {
    const list = [
      d({ id: "old", owner_id: "u2", is_default: true, is_shared: true, updated_at: "2026-10-01T00:00:00Z" }),
      d({ id: "new", owner_id: "u3", is_default: true, is_shared: true, updated_at: "2026-11-10T00:00:00Z" }),
      d({ id: "plain", owner_id: "me" }),
    ];
    expect(pickHomeDashboard(list, "me")?.id).toBe("new");
    expect(pickHomeDashboard([d({ id: "plain" })], "me")).toBeNull();
    expect(pickHomeDashboard(undefined, "me")).toBeNull();
  });
});

describe("widget data helpers", () => {
  const data = (over: Partial<WidgetData>): WidgetData => ({
    columns: [],
    rows: [],
    row_count: 0,
    truncated: false,
    chart: null,
    refreshed_at: "2026-11-20T10:00:00Z",
    ...over,
  });

  it("reads a KPI from the first numeric, unmasked column (the chart hint wins when numeric)", () => {
    const d1 = data({ columns: [{ name: "region", type: "text" }, { name: "revenue", type: "numeric" }, { name: "orders", type: "bigint" }], rows: [["West", "1250.5", 42]], row_count: 1 });
    expect(kpiFromData(d1)).toEqual({ label: "revenue", value: 1250.5, caption: "region: West · orders: 42" });
    expect(kpiFromData(d1, { type: "kpi", x: null, y: ["orders"], series: null }).value).toBe(42);
    const masked = data({ columns: [{ name: "salary", type: "numeric", masked: true }, { name: "n", type: "int" }], rows: [[null, 7]], row_count: 1 });
    expect(kpiFromData(masked)).toMatchObject({ label: "n", value: 7 });
    expect(kpiFromData(data({ columns: [{ name: "total", type: "numeric" }] }))).toEqual({ label: "total", value: null, caption: "No rows" });
  });

  it("uses the widget's own chart config over the data's hint", () => {
    const d1 = data({ columns: [{ name: "month", type: "date" }, { name: "sales", type: "numeric" }], rows: [["2026-01-01", 10], ["2026-02-01", 12]], row_count: 2, chart: { type: "bar", x: "month", y: ["sales"], series: null } });
    expect(chartSpecFor({ chart: { type: "line", x: "month", y: ["sales"], series: null } }, d1).type).toBe("line");
    expect(chartSpecFor({ chart: null }, d1).type).toBe("bar");
  });

  it("explains failed /data requests: permission, deleted query, guard rejection, outage", () => {
    expect(widgetErrorState(new ApiError({ status: 403, code: "permission_denied", detail: null })).kind).toBe("permission");
    expect(widgetErrorState(new ApiError({ status: 404, code: "not_found", detail: null })).kind).toBe("permission");
    expect(widgetErrorState(new ApiError({ status: 409, code: "widget_query_missing", detail: "gone" })).kind).toBe("missing");
    const rejected = widgetErrorState(new ApiError({ status: 422, code: "column_not_allowed", detail: null }));
    expect(rejected.kind).toBe("rejected");
    expect(rejected.description).toMatch(/column/i);
    expect(widgetErrorState(new ApiError({ status: 503, code: "service_unavailable", detail: null })).kind).toBe("unavailable");
    expect(widgetErrorState(new Error("boom")).kind).toBe("error");
  });
});
