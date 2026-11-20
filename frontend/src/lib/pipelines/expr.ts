/**
 * Text <-> JSON-tree conversion for the pipeline `derive` step.
 *
 * The backend (`apps/pipelines/expressions.py`) only accepts a whitelisted JSON tree, never a string:
 *   {"col": "price"} · {"lit": 18} · {"op": "mul", "args": [{"col": "price"}, {"lit": 1.18}]}
 * The builder lets people type a readable formula instead and converts it here:
 *   price * 1.18 · round(amount - discount, 2) · if(qty > 10, 'bulk', 'single') · "unit price" * qty
 *
 * Bare identifiers and "double-quoted" names are columns, 'single-quoted' text is a string literal.
 * Functions use the backend operator names; the server still validates arity, types and column existence.
 */

export type ExprNode = { col: string } | { lit: string | number | boolean | null } | { op: string; args: ExprNode[] };

export const EXPR_FUNCTIONS = [
  "abs",
  "round",
  "coalesce",
  "if",
  "concat",
  "lower",
  "upper",
  "trim",
  "length",
  "substring",
  "replace",
  "year",
  "month",
  "day",
  "date_diff_days",
  "cast",
  "is_null",
  "not_null",
  "add",
  "sub",
  "mul",
  "div",
  "mod",
  "neg",
  "eq",
  "ne",
  "gt",
  "gte",
  "lt",
  "lte",
  "and",
  "or",
  "not",
] as const;

const FUNCTIONS = new Set<string>(EXPR_FUNCTIONS);
const KEYWORDS = new Set(["and", "or", "not", "true", "false", "null"]);
const VARIADIC = new Set(["add", "mul", "and", "or", "concat", "coalesce"]);
const MAX_VARIADIC = 10;

export class ExprError extends Error {
  readonly position: number;
  constructor(message: string, position: number) {
    super(message);
    this.name = "ExprError";
    this.position = position;
  }
}

type Tok =
  | { t: "num"; v: number; p: number }
  | { t: "str"; v: string; p: number }
  | { t: "qid"; v: string; p: number }
  | { t: "id"; v: string; p: number }
  | { t: "op"; v: string; p: number }
  | { t: "eof"; p: number };

function tokenize(src: string): Tok[] {
  const out: Tok[] = [];
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    if (/\s/.test(ch)) {
      i++;
      continue;
    }
    if (/[0-9]/.test(ch) || (ch === "." && /[0-9]/.test(src[i + 1] ?? ""))) {
      const m = /^(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?/.exec(src.slice(i))!;
      out.push({ t: "num", v: Number(m[0]), p: i });
      i += m[0].length;
      continue;
    }
    if (ch === "'" || ch === '"') {
      let j = i + 1;
      let v = "";
      for (;;) {
        if (j >= src.length) throw new ExprError(ch === "'" ? "Unclosed text literal." : "Unclosed quoted column name.", i);
        if (src[j] === ch) {
          if (src[j + 1] === ch) {
            v += ch;
            j += 2;
            continue;
          }
          break;
        }
        v += src[j++];
      }
      out.push(ch === "'" ? { t: "str", v, p: i } : { t: "qid", v, p: i });
      i = j + 1;
      continue;
    }
    if (/[A-Za-z_]/.test(ch)) {
      const m = /^[A-Za-z_][A-Za-z0-9_]*/.exec(src.slice(i))!;
      out.push({ t: "id", v: m[0], p: i });
      i += m[0].length;
      continue;
    }
    const two = src.slice(i, i + 2);
    if (["<=", ">=", "!=", "<>", "==", "&&", "||"].includes(two)) {
      out.push({ t: "op", v: two, p: i });
      i += 2;
      continue;
    }
    if ("+-*/%()<>=,!".includes(ch)) {
      out.push({ t: "op", v: ch, p: i });
      i++;
      continue;
    }
    throw new ExprError(`Unexpected character "${ch}".`, i);
  }
  out.push({ t: "eof", p: src.length });
  return out;
}

function join(op: string, a: ExprNode, b: ExprNode): ExprNode {
  if (VARIADIC.has(op) && "op" in a && a.op === op && a.args.length < MAX_VARIADIC) return { op, args: [...a.args, b] };
  return { op, args: [a, b] };
}

const CMP: Record<string, string> = { "=": "eq", "==": "eq", "!=": "ne", "<>": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte" };

/** Parse a formula into the backend expression tree. Throws `ExprError` with a character position. */
export function parseExpression(src: string): ExprNode {
  if (!src.trim()) throw new ExprError("Enter an expression.", 0);
  const toks = tokenize(src);
  let k = 0;
  const peek = () => toks[k];
  const isOp = (v: string) => peek().t === "op" && (peek() as { v: string }).v === v;
  const isWord = (w: string) => peek().t === "id" && (peek() as { v: string }).v.toLowerCase() === w;
  const expect = (v: string) => {
    if (!isOp(v)) throw new ExprError(`Expected "${v}".`, peek().p);
    k++;
  };

  const orExpr = (): ExprNode => {
    let left = andExpr();
    while (isWord("or") || isOp("||")) {
      k++;
      left = join("or", left, andExpr());
    }
    return left;
  };
  const andExpr = (): ExprNode => {
    let left = notExpr();
    while (isWord("and") || isOp("&&")) {
      k++;
      left = join("and", left, notExpr());
    }
    return left;
  };
  const notExpr = (): ExprNode => {
    if (isWord("not") || isOp("!")) {
      k++;
      return { op: "not", args: [notExpr()] };
    }
    return cmpExpr();
  };
  const cmpExpr = (): ExprNode => {
    const left = addExpr();
    const t = peek();
    if (t.t === "op" && CMP[t.v]) {
      k++;
      return { op: CMP[t.v], args: [left, addExpr()] };
    }
    return left;
  };
  const addExpr = (): ExprNode => {
    let left = mulExpr();
    while (isOp("+") || isOp("-")) {
      const op = (toks[k++] as { v: string }).v === "+" ? "add" : "sub";
      left = join(op, left, mulExpr());
    }
    return left;
  };
  const mulExpr = (): ExprNode => {
    let left = unary();
    while (isOp("*") || isOp("/") || isOp("%")) {
      const v = (toks[k++] as { v: string }).v;
      left = join(v === "*" ? "mul" : v === "/" ? "div" : "mod", left, unary());
    }
    return left;
  };
  const unary = (): ExprNode => {
    if (isOp("-")) {
      k++;
      const inner = unary();
      if ("lit" in inner && typeof inner.lit === "number") return { lit: -inner.lit };
      return { op: "neg", args: [inner] };
    }
    if (isOp("+")) {
      k++;
      return unary();
    }
    return primary();
  };
  const primary = (): ExprNode => {
    const t = peek();
    if (t.t === "num") {
      k++;
      return { lit: t.v };
    }
    if (t.t === "str") {
      k++;
      return { lit: t.v };
    }
    if (t.t === "qid") {
      k++;
      if (!t.v) throw new ExprError("Empty column name.", t.p);
      return { col: t.v };
    }
    if (t.t === "op" && t.v === "(") {
      k++;
      const e = orExpr();
      expect(")");
      return e;
    }
    if (t.t === "id") {
      k++;
      const lower = t.v.toLowerCase();
      if (lower === "true" || lower === "false") return { lit: lower === "true" };
      if (lower === "null") return { lit: null };
      if (isOp("(")) {
        if (!FUNCTIONS.has(lower)) throw new ExprError(`Unknown function "${t.v}".`, t.p);
        k++;
        const args: ExprNode[] = [];
        if (!isOp(")")) {
          args.push(orExpr());
          while (isOp(",")) {
            k++;
            args.push(orExpr());
          }
        }
        expect(")");
        return { op: lower, args };
      }
      if (KEYWORDS.has(lower)) throw new ExprError(`Unexpected "${t.v}".`, t.p);
      return { col: t.v };
    }
    if (t.t === "eof") throw new ExprError("The expression ends too early.", t.p);
    throw new ExprError(`Unexpected "${(t as { v: string }).v}".`, t.p);
  };

  const tree = orExpr();
  if (peek().t !== "eof") throw new ExprError(`Unexpected "${(peek() as { v?: string }).v ?? ""}".`, peek().p);
  return tree;
}

/** `{ok, tree}` or `{ok:false, error}`, for validation without try/catch at call sites. */
export function tryParseExpression(src: string): { ok: true; tree: ExprNode } | { ok: false; error: string } {
  try {
    return { ok: true, tree: parseExpression(src) };
  } catch (e) {
    return { ok: false, error: e instanceof ExprError ? `${e.message} (at character ${e.position + 1})` : String(e) };
  }
}

/** Column names referenced anywhere in a tree. */
export function expressionColumns(tree: ExprNode): string[] {
  const out = new Set<string>();
  const walk = (n: ExprNode) => {
    if ("col" in n) out.add(n.col);
    else if ("op" in n) n.args.forEach(walk);
  };
  walk(tree);
  return [...out];
}

/* ---------------- printing ---------------- */

const INFIX: Record<string, { sym: string; prec: number }> = {
  or: { sym: "or", prec: 1 },
  and: { sym: "and", prec: 2 },
  eq: { sym: "=", prec: 4 },
  ne: { sym: "!=", prec: 4 },
  gt: { sym: ">", prec: 4 },
  gte: { sym: ">=", prec: 4 },
  lt: { sym: "<", prec: 4 },
  lte: { sym: "<=", prec: 4 },
  add: { sym: "+", prec: 5 },
  sub: { sym: "-", prec: 5 },
  mul: { sym: "*", prec: 6 },
  div: { sym: "/", prec: 6 },
  mod: { sym: "%", prec: 6 },
};

function printLit(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return String(v);
  return `'${String(v).replace(/'/g, "''")}'`;
}

function printCol(name: string): string {
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(name) && !KEYWORDS.has(name.toLowerCase()) ? name : `"${name.replace(/"/g, '""')}"`;
}

/** Print a backend expression tree as a formula that `parseExpression` reads back to the same tree. */
export function printExpression(node: unknown, parentPrec = 0): string {
  if (!node || typeof node !== "object") return printLit(node);
  const n = node as Record<string, unknown>;
  if ("col" in n) return printCol(String(n.col));
  if ("lit" in n) {
    const s = printLit(n.lit);
    return typeof n.lit === "number" && n.lit < 0 && parentPrec > 0 ? `(${s})` : s;
  }
  const op = String(n.op ?? "");
  const args = Array.isArray(n.args) ? (n.args as unknown[]) : [];
  const inf = INFIX[op];
  if (inf && args.length >= 2 && (args.length === 2 || VARIADIC.has(op))) {
    // Left-associative: the right operand of a non-commutative op needs parentheses at equal precedence.
    // Comparisons do not chain, so both of their operands bind tighter.
    const parts = args.map((a, i) => printExpression(a, i === 0 && inf.prec !== 4 ? inf.prec : inf.prec + 0.5));
    const s = parts.join(` ${inf.sym} `);
    return inf.prec < parentPrec ? `(${s})` : s;
  }
  if (op === "neg" && args.length === 1) return `-${printExpression(args[0], 7)}`;
  if (op === "not" && args.length === 1) {
    const s = `not ${printExpression(args[0], 3)}`;
    return parentPrec > 3 ? `(${s})` : s;
  }
  return `${op}(${args.map((a) => printExpression(a, 0)).join(", ")})`;
}
