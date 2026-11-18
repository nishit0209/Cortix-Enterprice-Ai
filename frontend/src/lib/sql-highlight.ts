/**
 * Tiny, dependency-free SQL tokenizer for syntax highlighting (read-only display + the editor overlay).
 * It never alters text: concatenating every token's `text` returns the input exactly.
 */

export type SqlTokenType = "keyword" | "function" | "string" | "number" | "comment" | "operator" | "identifier" | "quoted" | "punct" | "space" | "param";

export interface SqlToken {
  type: SqlTokenType;
  text: string;
}

const KEYWORDS = new Set(
  (
    "select from where and or not in is null as on join inner left right full outer cross group by order having limit offset " +
    "distinct case when then else end union all intersect except with asc desc nulls first last between like ilike exists " +
    "true false interval date timestamp over partition window rows range preceding following current row filter within " +
    "insert update delete create drop alter truncate grant revoke into values set returning lateral using fetch only"
  ).split(" "),
);

const FUNCTIONS = new Set(
  (
    "count sum avg min max round floor ceil abs coalesce nullif cast extract date_trunc date_part to_char to_date lower upper " +
    "trim length substring concat greatest least now current_date row_number rank dense_rank lag lead percentile_cont median " +
    "stddev variance string_agg array_agg"
  ).split(" "),
);

export function tokenizeSql(sql: string): SqlToken[] {
  const out: SqlToken[] = [];
  let i = 0;
  const n = sql.length;
  const push = (type: SqlTokenType, text: string) => {
    const last = out[out.length - 1];
    if (last && last.type === type && (type === "space" || type === "punct")) last.text += text;
    else out.push({ type, text });
  };
  while (i < n) {
    const ch = sql[i];
    const rest = sql.slice(i);
    if (/\s/.test(ch)) {
      const m = /^\s+/.exec(rest)!;
      push("space", m[0]);
      i += m[0].length;
    } else if (rest.startsWith("--")) {
      const end = sql.indexOf("\n", i);
      const text = end === -1 ? rest : sql.slice(i, end);
      push("comment", text);
      i += text.length;
    } else if (rest.startsWith("/*")) {
      const end = sql.indexOf("*/", i + 2);
      const text = end === -1 ? rest : sql.slice(i, end + 2);
      push("comment", text);
      i += text.length;
    } else if (ch === "'") {
      let j = i + 1;
      while (j < n) {
        if (sql[j] === "'" && sql[j + 1] === "'") j += 2;
        else if (sql[j] === "'") break;
        else j++;
      }
      const text = sql.slice(i, Math.min(n, j + 1));
      push("string", text);
      i += text.length;
    } else if (ch === '"') {
      const end = sql.indexOf('"', i + 1);
      const text = end === -1 ? rest : sql.slice(i, end + 1);
      push("quoted", text);
      i += text.length;
    } else if (/[0-9]/.test(ch) || (ch === "." && /[0-9]/.test(sql[i + 1] ?? ""))) {
      const m = /^(\d+\.?\d*|\.\d+)(e[+-]?\d+)?/i.exec(rest)!;
      push("number", m[0]);
      i += m[0].length;
    } else if ((ch === "%" && sql[i + 1] === "(") || ch === "$" || (ch === ":" && /[a-z_]/i.test(sql[i + 1] ?? ""))) {
      const m = /^(%\([^)]*\)s|\$\d+|:[a-z_]\w*)/i.exec(rest);
      if (m) {
        push("param", m[0]);
        i += m[0].length;
      } else {
        push("operator", ch);
        i++;
      }
    } else if (/[a-z_]/i.test(ch)) {
      const m = /^[a-z_][a-z0-9_$]*/i.exec(rest)!;
      const word = m[0];
      const lower = word.toLowerCase();
      const nextNonSpace = /^\s*\(/.test(sql.slice(i + word.length));
      push(KEYWORDS.has(lower) ? "keyword" : FUNCTIONS.has(lower) && nextNonSpace ? "function" : nextNonSpace ? "function" : "identifier", word);
      i += word.length;
    } else if (/[<>=!|:+\-*/%^~]/.test(ch)) {
      const m = /^(<>|<=|>=|!=|\|\||::|[<>=!+\-*/%^~|:])/.exec(rest)!;
      push("operator", m[0]);
      i += m[0].length;
    } else {
      push("punct", ch);
      i++;
    }
  }
  return out;
}

export const SQL_TOKEN_CLASS: Record<SqlTokenType, string> = {
  keyword: "text-accent font-medium",
  function: "text-teal",
  string: "text-[var(--chart-3)]",
  number: "text-[var(--chart-4)]",
  comment: "text-fg-subtle italic",
  operator: "text-fg-muted",
  identifier: "text-fg",
  quoted: "text-fg",
  punct: "text-fg-muted",
  space: "",
  param: "text-info",
};
