import { describe, expect, it } from "vitest";
import { inferSchemas, layeredLayout, parseDag, serializeDag, serverErrorsByNode, topoOrder, validateDag, type BuilderDag } from "./dag";
import { describeCron, validateCron } from "./cron";

const SRC_COLS = {
  ds1: [
    { name: "order_id", type: "bigint" },
    { name: "region", type: "text" },
    { name: "amount", type: "numeric" },
  ],
  ds2: [
    { name: "region", type: "text" },
    { name: "manager", type: "text" },
  ],
};

function linear(): BuilderDag {
  return {
    nodes: [
      { id: "src", type: "source", config: { dataset_id: "ds1" }, position: { x: 0, y: 0 } },
      { id: "s1", type: "rename", config: { mapping: { amount: "net_amount" } }, position: { x: 1, y: 0 } },
      { id: "s2", type: "deduplicate", config: { keys: ["order_id"], keep: "first" }, position: { x: 2, y: 0 } },
      { id: "dst", type: "destination", config: { dataset_name: "sales_clean", mode: "new_version" }, position: { x: 3, y: 0 } },
    ],
    edges: [
      { from: "src", to: "s1" },
      { from: "s1", to: "s2" },
      { from: "s2", to: "dst" },
    ],
  };
}

describe("validateDag", () => {
  it("accepts a valid linear pipeline", () => {
    const dag = linear();
    const v = validateDag(dag, inferSchemas(dag, SRC_COLS));
    expect(v).toEqual({ ok: true, issues: [], byNode: {} });
  });

  it("requires a source and exactly one destination", () => {
    const v = validateDag({ nodes: [{ id: "a", type: "sort", config: { by: [{ column: "x" }] } }], edges: [] });
    expect(v.issues.map((i) => i.message)).toEqual(
      expect.arrayContaining(["The pipeline needs a source.", "The pipeline needs a dataset output."]),
    );
    const two = linear();
    two.nodes.push({ id: "dst2", type: "destination", config: { dataset_name: "x" } });
    two.edges.push({ from: "s2", to: "dst2" });
    expect(validateDag(two).byNode.dst2).toContain("Only one output is allowed per pipeline.");
  });

  it("detects cycles", () => {
    const dag = linear();
    dag.edges.push({ from: "s2", to: "s1" });
    const v = validateDag(dag);
    expect(v.byNode.s1).toContain("This step is part of a cycle.");
    expect(v.byNode.s2).toContain("This step is part of a cycle.");
  });

  it("checks arity per step and missing connections", () => {
    const dag = linear();
    dag.edges = dag.edges.filter((e) => e.to !== "s2");
    const v = validateDag(dag);
    expect(v.byNode.s2).toContain("Connect an input to this step.");
    expect(v.byNode.s1).toContain("Connect this step to a following step.");
    expect(v.byNode.s2).toContain("Not reachable from a source.");
  });

  it("flags required config and bad names", () => {
    const dag = linear();
    dag.nodes[1].config = { mapping: { amount: "Net Amount" } };
    dag.nodes[3].config = { dataset_name: "" };
    const v = validateDag(dag);
    expect(v.byNode.s1).toContain("New names use lowercase letters, digits and underscores.");
    expect(v.byNode.dst).toContain("Output dataset name is required.");
  });

  it("checks column references against the inferred input schema", () => {
    const dag = linear();
    dag.nodes[2].config = { keys: ["amount"] }; // renamed away by s1
    const v = validateDag(dag, inferSchemas(dag, SRC_COLS));
    expect(v.byNode.s2).toEqual(['Column "amount" is not in the input.']);
  });

  it("validates joins: two inputs, a left port and keys on both sides", () => {
    const dag: BuilderDag = {
      nodes: [
        { id: "a", type: "source", config: { dataset_id: "ds1" } },
        { id: "b", type: "source", config: { dataset_id: "ds2" } },
        { id: "j", type: "join", config: { how: "left", on: [{ left: "region", right: "region" }] } },
        { id: "d", type: "destination", config: { dataset_name: "joined" } },
      ],
      edges: [
        { from: "a", to: "j", port: "left" },
        { from: "b", to: "j", port: "right" },
        { from: "j", to: "d" },
      ],
    };
    const schemas = inferSchemas(dag, SRC_COLS);
    expect(validateDag(dag, schemas).ok).toBe(true);
    expect(schemas.j?.map((c) => c.name)).toEqual(["order_id", "region", "amount", "manager"]);
    const bad = { ...dag, edges: dag.edges.filter((e) => e.from !== "b") };
    expect(validateDag(bad).byNode.j).toContain("A join needs exactly two inputs (left and right).");
    const badKey = { ...dag, nodes: dag.nodes.map((n) => (n.id === "j" ? { ...n, config: { how: "inner", on: [{ left: "region", right: "nope" }] } } : n)) };
    expect(validateDag(badKey, inferSchemas(badKey, SRC_COLS)).byNode.j).toContain('Column "nope" is not in the input.');
  });
});

describe("schema inference", () => {
  it("follows select/rename/derive/aggregate", () => {
    const dag: BuilderDag = {
      nodes: [
        { id: "s", type: "source", config: { dataset_id: "ds1" } },
        { id: "sel", type: "select_columns", config: { columns: ["region", "amount"] } },
        { id: "der", type: "derive", config: { name: "double_amount", expression: "amount * 2" } },
        { id: "agg", type: "aggregate", config: { group_by: ["region"], aggregations: [{ column: "amount", fn: "sum", as: "total" }, { column: "*", fn: "count" }] } },
      ],
      edges: [
        { from: "s", to: "sel" },
        { from: "sel", to: "der" },
        { from: "der", to: "agg" },
      ],
    };
    const s = inferSchemas(dag, SRC_COLS);
    expect(s.sel?.map((c) => c.name)).toEqual(["region", "amount"]);
    expect(s.der?.map((c) => c.name)).toEqual(["region", "amount", "double_amount"]);
    expect(s.agg).toEqual([
      { name: "region", type: "text" },
      { name: "total", type: "double precision" },
      { name: "count_*", type: "bigint" },
    ]);
  });
  it("is null downstream of an unknown source", () => {
    const dag = linear();
    dag.nodes[0].config = { dataset_id: "unknown" };
    expect(inferSchemas(dag, SRC_COLS).dst).toBeNull();
  });
});

describe("serialization", () => {
  it("round-trips through parse(serialize())", () => {
    const dag = linear();
    expect(parseDag(serializeDag(dag))).toEqual(dag);
  });

  it("emits the canonical wire format with ports and rounded positions", () => {
    const dag: BuilderDag = {
      nodes: [{ id: "a", type: "source", config: { dataset_id: "x", unused: undefined }, position: { x: 10.4, y: 20.6 } }],
      edges: [{ from: "a", to: "b", port: "left" }],
    };
    expect(serializeDag(dag)).toEqual({
      nodes: [{ id: "a", type: "source", config: { dataset_id: "x" }, position: { x: 10, y: 21 } }],
      edges: [{ from: "a", to: "b", port: "left" }],
    });
  });

  it("parses design-doc style DAGs (key, dotted types, array edges) and lays them out", () => {
    const d = parseDag({
      nodes: [
        { key: "src", type: "source.dataset", config: { dataset_id: "ds1" } },
        { key: "s1", type: "transform.rename", config: { map: {} } },
        { key: "s2", type: "dedup.exact", config: {} },
        { key: "dst", type: "destination.dataset", config: {} },
      ],
      edges: [["src", "s1"], ["s1", "s2"], { source: "s2", target: "dst" }],
      settings: { max_attempts: 3 },
    });
    expect(d.nodes.map((n) => n.type)).toEqual(["source", "rename", "deduplicate", "destination"]);
    expect(d.edges).toEqual([{ from: "src", to: "s1" }, { from: "s1", to: "s2" }, { from: "s2", to: "dst" }]);
    expect(d.nodes.map((n) => n.position!.x)).toEqual([0, 280, 560, 840]);
    expect(d.settings).toEqual({ max_attempts: 3 });
  });

  it("ignores malformed input", () => {
    expect(parseDag(null)).toEqual({ nodes: [], edges: [] });
    expect(parseDag({ nodes: [{}, 5, { id: "a", type: "sort" }], edges: [[1], {}] }).nodes).toHaveLength(1);
  });

  it("maps server field errors onto nodes", () => {
    const dag = linear();
    expect(serverErrorsByNode([{ field: "nodes.s1.config.mapping", message: "bad" }, { field: "nodes[3]", message: "x" }, { field: "name", message: "n" }], dag)).toEqual({
      s1: ["bad"],
      dst: ["x"],
    });
  });
});

describe("layout + topo order", () => {
  it("orders topologically and reports cycles", () => {
    expect(topoOrder(["a", "b", "c"], [{ from: "a", to: "b" }, { from: "b", to: "c" }])).toEqual({ order: ["a", "b", "c"], cyclic: [] });
    expect(topoOrder(["a", "b"], [{ from: "a", to: "b" }, { from: "b", to: "a" }]).cyclic).toEqual(["a", "b"]);
  });
  it("places fan-in nodes after their deepest predecessor and centers columns", () => {
    const pos = layeredLayout(["a", "b", "c", "d"], [{ from: "a", to: "c" }, { from: "b", to: "c" }, { from: "c", to: "d" }], { dx: 100, dy: 50 });
    expect(pos).toEqual({ a: { x: 0, y: 0 }, b: { x: 0, y: 50 }, c: { x: 100, y: 25 }, d: { x: 200, y: 25 } });
  });
});

describe("cron", () => {
  it("validates 5-field cron expressions", () => {
    expect(validateCron("0 6 * * 1-5")).toBeNull();
    expect(validateCron("*/15 * * * *")).toBeNull();
    expect(validateCron("0 6 * *")).toMatch(/5 fields/);
    expect(validateCron("61 * * * *")).toMatch(/minute/);
    expect(validateCron("0 6 * * 9")).toMatch(/day of week/);
    expect(validateCron("*/0 * * * *")).toMatch(/minute/);
  });
  it("describes common schedules", () => {
    expect(describeCron("0 6 * * 1-5", "Asia/Kolkata")).toBe("Every weekday at 06:00 (Asia/Kolkata)");
    expect(describeCron("30 9 * * *")).toBe("Every day at 09:30");
    expect(describeCron("5 * * * *")).toBe("Every hour at :05");
    expect(describeCron("0 7 * * 3")).toBe("Every Wednesday at 07:00");
    expect(describeCron("0 7 15 * *")).toBe("Day 15 of every month at 07:00");
  });
});
