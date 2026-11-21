import { describe, expect, it } from "vitest";
import {
  MAX_CONDITIONS,
  coerceLiteral,
  emptyCondition,
  parsePredicate,
  serializeConditions,
  splitList,
  summarizePredicate,
  validateConditions,
  type ConditionRow,
} from "./conditions";

const row = (over: Partial<ConditionRow>): ConditionRow => ({ ...emptyCondition("region"), ...over });

describe("row-policy condition builder serialization", () => {
  it("serializes literals, lists, user attributes and null ops in the backend's {all:[{col,op,value}]} shape", () => {
    const rows: ConditionRow[] = [
      row({ column: "region", op: "in", source: "user_attribute", attribute: "regions" }),
      row({ column: "amount", op: ">", value: " 100.5 " }),
      row({ column: "status", op: "not_in", list: ["closed", " lost ", "closed", ""] }),
      row({ column: "deleted_at", op: "is_null", value: "ignored" }),
      row({ column: "vip", op: "=", value: "TRUE" }),
      row({ column: "zip", op: "=", value: "00501" }),
    ];
    const wire = serializeConditions(rows, { amount: "Float64", vip: "Boolean", zip: "Utf8" });
    expect(wire).toEqual({
      all: [
        { col: "region", op: "in", value: "$user.regions" },
        { col: "amount", op: ">", value: 100.5 },
        { col: "status", op: "not_in", value: ["closed", "lost"] },
        { col: "deleted_at", op: "is_null" },
        { col: "vip", op: "=", value: true },
        { col: "zip", op: "=", value: "00501" },
      ],
    });
    // Null ops never carry a value key (the backend rejects one).
    expect("value" in wire.all[3]).toBe(false);
    // Round-trips through JSON unchanged.
    expect(JSON.parse(JSON.stringify(wire))).toEqual(wire);
  });

  it("parses a stored predicate back into editable rows", () => {
    const rows = parsePredicate({
      all: [
        { col: "region", op: "=", value: "$user.region" },
        { col: "amount", op: ">=", value: 10 },
        { col: "tier", op: "in", value: ["gold", 2] },
        { col: "x", op: "not_null", value: null },
        { col: "y", op: "LIKE", value: "%a" },
      ],
    });
    expect(rows).toHaveLength(4);
    expect(rows[0]).toMatchObject({ column: "region", source: "user_attribute", attribute: "region" });
    expect(rows[1]).toMatchObject({ op: ">=", source: "literal", value: "10" });
    expect(rows[2]).toMatchObject({ op: "in", list: ["gold", "2"] });
    expect(rows[3]).toMatchObject({ op: "not_null", value: "" });
    expect(parsePredicate("region = 'x'")).toEqual([]);
    expect(parsePredicate(null)).toEqual([]);
  });

  it("serialize(parse(p)) is the identity for valid predicates", () => {
    const p = {
      all: [
        { col: "region", op: "in", value: "$user.regions" },
        { col: "amount", op: "<", value: 5 },
        { col: "tier", op: "in", value: ["gold", "silver"] },
        { col: "closed", op: "is_null" },
      ],
    };
    expect(serializeConditions(parsePredicate(p), { amount: "Int64" })).toEqual(p);
  });

  it("validates with the backend's rules", () => {
    expect(validateConditions([])).toEqual({ [-1]: "Add at least one condition." });
    expect(validateConditions(Array.from({ length: MAX_CONDITIONS + 1 }, () => row({ value: "x" })))[-1]).toMatch(/At most 20/);
    const errors = validateConditions(
      [
        row({ column: "", value: "x" }),
        row({ column: "nope", value: "x" }),
        row({ value: "" }),
        row({ op: "in", list: [] }),
        row({ op: "in", list: ["$user.regions"] }),
        row({ source: "user_attribute", attribute: "Regions" }),
        row({ value: "$user.region" }),
        row({ op: "is_null" }),
        row({ source: "user_attribute", attribute: "regions" }),
      ],
      ["region", "amount"],
    );
    expect(errors).toEqual({
      0: "Choose a column.",
      1: 'Unknown column "nope".',
      2: "Enter a value.",
      3: "Add at least one value.",
      4: "Use the user-attribute source instead of $user inside a list.",
      5: "Attribute keys are lowercase snake_case.",
      6: "Use the user-attribute source for $user values.",
    });
  });

  it("coerces literals by column type and splits typed lists", () => {
    expect(coerceLiteral("42", "Int64")).toBe(42);
    expect(coerceLiteral("4x", "Int64")).toBe("4x");
    expect(coerceLiteral("false", "Boolean")).toBe(false);
    expect(coerceLiteral("42", "Utf8")).toBe("42");
    expect(splitList("a, b\nc,,  ")).toEqual(["a", "b", "c"]);
  });

  it("summarizes predicates for humans", () => {
    expect(
      summarizePredicate({
        all: [
          { col: "region", op: "in", value: "$user.regions" },
          { col: "amount", op: ">", value: 100 },
          { col: "status", op: "not_in", value: ["a", "b", "c", "d", "e", "f", "g"] },
          { col: "deleted_at", op: "is_null" },
        ],
      }),
    ).toBe("region is one of $user.regions AND amount is greater than 100 AND status is not one of ('a', 'b', 'c', 'd', 'e', +2 more) AND deleted_at is empty");
    expect(summarizePredicate(null)).toBe("All rows");
    expect(summarizePredicate([{ col: "x", op: "=", value: "y" }])).toBe("x equals 'y'");
  });
});
