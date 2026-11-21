import { describe, expect, it } from "vitest";
import type { BuilderDag } from "./dag";
import { fromWireDag, toWireDag } from "./wire";

const dest = (config: Record<string, unknown>) => ({ nodes: [{ id: "out", type: "destination", config }], edges: [] }) as unknown as BuilderDag;

describe("destination mode", () => {
  it("sends mode=append only for an existing dataset", () => {
    expect(toWireDag(dest({ target: "existing", dataset_id: "d1", mode: "append" })).nodes[0].config).toEqual({ dataset_id: "d1", mode: "append" });
    expect(toWireDag(dest({ target: "existing", dataset_id: "d1", mode: "new_version" })).nodes[0].config).toEqual({ dataset_id: "d1" });
    expect(toWireDag(dest({ target: "new", dataset_name: " Sales ", mode: "append" })).nodes[0].config).toEqual({ name: "Sales" });
  });
  it("round-trips append from the wire", () => {
    const b = fromWireDag({ nodes: [{ id: "out", type: "destination.dataset", config: { dataset_id: "d1", mode: "append" } }], edges: [] });
    expect(b.nodes[0].config).toMatchObject({ target: "existing", dataset_id: "d1", mode: "append" });
    const n = fromWireDag({ nodes: [{ id: "out", type: "destination.dataset", config: { dataset_id: "d1" } }], edges: [] });
    expect(n.nodes[0].config).toMatchObject({ mode: "new_version" });
  });
});
