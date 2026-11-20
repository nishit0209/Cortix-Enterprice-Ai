import { describe, expect, it } from "vitest";
import { availableChartTypes, normalizeColumns, pivotSeries, resolveChart } from "./chart";
import { columnRoles, exampleQuestions, followUps } from "./suggestions";
import { groupConversations, titleFromQuestion } from "./groups";
import { chatErrorCopy, isGuardCode } from "./error-copy";
import { tokenizeSql } from "@/lib/sql-highlight";
import type { Conversation, DatasetColumn } from "@/lib/types";

const COLS = [
  { name: "region", type: "text" },
  { name: "revenue", type: "numeric" },
];
const ROWS = [
  ["West", 120],
  ["East", 80],
  ["North", 40],
];

describe("chart hint → chart mapping", () => {
  it("honours a valid bar hint", () => {
    const s = resolveChart({ type: "bar", x: "region", y: ["revenue"] }, COLS, ROWS);
    expect(s).toMatchObject({ type: "bar", x: "region", y: ["revenue"], keys: ["revenue"], note: null });
    expect(s.data[0]).toEqual({ region: "West", revenue: 120 });
  });

  it("accepts y as a single string and numeric strings in rows", () => {
    const s = resolveChart({ type: "line", x: "region", y: "revenue" }, COLS, [["a", "1.5"]]);
    expect(s.type).toBe("line");
    expect(s.data[0].revenue).toBe(1.5);
  });

  it("maps a single-row single-measure result to a KPI", () => {
    const s = resolveChart(null, [{ name: "total", type: "bigint" }], [[5020]]);
    expect(s.type).toBe("kpi");
    expect(s.keys).toEqual(["total"]);
  });

  it("infers a line chart for temporal x without a hint and sorts by time", () => {
    const s = resolveChart(null, [{ name: "month", type: "date" }, { name: "orders", type: "bigint" }], [["2026-02-01", 5], ["2026-01-01", 3]]);
    expect(s.type).toBe("line");
    expect(s.data.map((r) => r.month)).toEqual(["2026-01-01", "2026-02-01"]);
  });

  it("falls back from an impossible hint and explains why", () => {
    const s = resolveChart({ type: "scatter", x: "region", y: ["revenue"] }, COLS, ROWS);
    expect(s.type).toBe("bar");
    expect(s.note).toMatch(/scatter/);
  });

  it("drops masked columns from the measures and notes it", () => {
    const s = resolveChart({ type: "bar", x: "region", y: ["salary", "revenue"] }, [...COLS, { name: "salary", type: "numeric", masked: true }], ROWS.map((r) => [...r, null]));
    expect(s.y).toEqual(["revenue"]);
    expect(s.note).toMatch(/Masked/);
  });

  it("turns a pie with negative values into a bar chart", () => {
    const s = resolveChart({ type: "pie", x: "region", y: ["revenue"] }, COLS, [["a", 5], ["b", -2]]);
    expect(s.type).toBe("bar");
    expect(s.note).toMatch(/negative/);
  });

  it("falls back to a table when nothing is numeric", () => {
    expect(resolveChart({ type: "bar" }, [{ name: "a", type: "text" }], [["x"]]).type).toBe("table");
  });

  it("an explicit override wins when it is available", () => {
    expect(resolveChart({ type: "bar", x: "region", y: ["revenue"] }, COLS, ROWS, "pie").type).toBe("pie");
    expect(resolveChart({ type: "bar", x: "region", y: ["revenue"] }, COLS, ROWS, "scatter").type).toBe("bar");
  });

  it("pivots a series column into wide format", () => {
    const s = resolveChart(
      { type: "line", x: "month", y: ["sales"], series: "region" },
      [{ name: "month", type: "date" }, { name: "region", type: "text" }, { name: "sales", type: "numeric" }],
      [["2026-01-01", "West", 3], ["2026-01-01", "East", 1], ["2026-02-01", "West", 4]],
    );
    expect(s.keys).toEqual(["West", "East"]);
    expect(s.data).toEqual([
      { month: "2026-01-01", West: 3, East: 1 },
      { month: "2026-02-01", West: 4 },
    ]);
  });

  it("folds series beyond the cap into Other", () => {
    const rows = Array.from({ length: 10 }, (_, i) => ["x", `s${i}`, 10 - i]);
    const p = pivotSeries(rows.map(([x, s, v]) => ({ x, s, v })), "x", "s", "v", 3);
    expect(p.keys).toEqual(["s0", "s1", "s2", "Other"]);
    expect(p.data[0].Other).toBe(7 + 6 + 5 + 4 + 3 + 2 + 1);
  });

  it("lists available chart types for the switcher", () => {
    const cols = normalizeColumns(COLS, ROWS);
    expect(availableChartTypes(cols, 3)).toEqual(["bar", "line", "area", "pie", "table"]);
    expect(availableChartTypes(normalizeColumns([{ name: "n", type: "int" }], [[1]]), 1)).toContain("kpi");
  });

  it("sniffs kinds when column types are missing", () => {
    const cols = normalizeColumns(["d", "v"], [["2026-01-05", "12"]]);
    expect(cols.map((c) => c.kind)).toEqual(["temporal", "number"]);
  });
});

const DS_COLS: DatasetColumn[] = [
  { name: "order_id", source_name: "Order ID", data_type: "bigint", semantic_type: "id", nullable: false, is_pii: false, stats: null },
  { name: "order_date", source_name: "Date", data_type: "date", semantic_type: "date", nullable: false, is_pii: false, stats: null },
  { name: "region", source_name: "Region", data_type: "text", semantic_type: "category", nullable: false, is_pii: false, stats: null },
  { name: "channel", source_name: "Channel", data_type: "text", semantic_type: "category", nullable: false, is_pii: false, stats: null },
  { name: "email", source_name: "Email", data_type: "text", semantic_type: "email", nullable: true, is_pii: true, stats: null },
  { name: "amount", source_name: "Amount", data_type: "numeric", semantic_type: "currency", nullable: false, is_pii: false, stats: null },
];

describe("local question suggestions", () => {
  it("derives roles, skipping ids and PII", () => {
    expect(columnRoles(DS_COLS)).toEqual({ measures: ["amount"], dimensions: ["region", "channel"], times: ["order_date"] });
  });
  it("builds example questions from the columns", () => {
    expect(exampleQuestions(DS_COLS)).toEqual([
      "Top 10 region by total amount",
      "Total amount by month",
      "Compare amount this month vs last month",
      "Average amount by channel",
    ]);
  });
  it("builds follow-ups that use dimensions not yet in the result", () => {
    const f = followUps("Top 10 region by total amount", ["region", "amount"], DS_COLS);
    expect(f).toContain("Show amount by month");
    expect(f).toContain("Break amount down by channel");
    expect(f.length).toBeLessThanOrEqual(3);
  });
});

describe("conversation grouping", () => {
  const now = new Date("2026-11-18T12:00:00").getTime();
  const c = (id: string, iso: string): Conversation => ({ id, title: id, dataset_id: "d", created_at: iso, last_message_at: iso });
  it("groups by recency, newest first", () => {
    const g = groupConversations([c("old", "2026-01-01T00:00:00"), c("today", "2026-11-18T09:00:00"), c("yday", "2026-11-17T09:00:00"), c("week", "2026-11-13T09:00:00")], now);
    expect(g.map((x) => [x.label, x.items.map((i) => i.id)])).toEqual([
      ["Today", ["today"]],
      ["Yesterday", ["yday"]],
      ["Previous 7 days", ["week"]],
      ["Older", ["old"]],
    ]);
  });
  it("makes short titles from questions", () => {
    expect(titleFromQuestion("  What is   revenue? ")).toBe("What is revenue?");
    expect(titleFromQuestion("a ".repeat(50)).endsWith("…")).toBe(true);
  });
});

describe("error copy", () => {
  it("maps guard reject codes (direct, via reject_code or sql_rejected detail)", () => {
    expect(isGuardCode("multiple_statements")).toBe(true);
    expect(chatErrorCopy("column_not_allowed").kind).toBe("guard");
    expect(chatErrorCopy("sql_rejected", "forbidden_function").description).toMatch(/allow-list/);
    expect(chatErrorCopy("error", null, "system_object").description).toMatch(/System catalogs/);
    expect(chatErrorCopy("ai_quota_exhausted").kind).toBe("quota");
    expect(chatErrorCopy("ai_policy_blocked").showSql).toBe(false);
    expect(chatErrorCopy("weird_code", "server said").description).toBe("server said");
  });
});

describe("SQL tokenizer", () => {
  it("round-trips the input and classifies tokens", () => {
    const sql = "SELECT region, SUM(amount) AS t -- c\nFROM sales WHERE note = 'it''s' AND x >= 1.5 LIMIT $1";
    const toks = tokenizeSql(sql);
    expect(toks.map((t) => t.text).join("")).toBe(sql);
    const find = (text: string) => toks.find((t) => t.text === text)?.type;
    expect(find("SELECT")).toBe("keyword");
    expect(find("SUM")).toBe("function");
    expect(find("'it''s'")).toBe("string");
    expect(find("1.5")).toBe("number");
    expect(find("-- c")).toBe("comment");
    expect(find("$1")).toBe("param");
    expect(find(">=")).toBe("operator");
  });
});
