"""Deterministic NL->SQL builder (0 Gemini calls) — AI_ARCHITECTURE.md §4.

Handles single-table questions of the forms:
  * "top|bottom N <dim> by <measure> [time filter]"
  * "<measure> by <dim> [time filter]"
  * "total <measure> [time filter]" / "how many <things>" / "average <measure>"
  * "<measure> over time | trend | by month | monthly <measure> [time filter]"
  * "compare <measure> this month vs last month" (also week / quarter / year), "<measure> growth this year vs
    last year" (one row with the % change)
  * "which N <dim> sold the most <measure>" / "<dim> with the highest <measure>" (ranking)
  * "share of <measure> by <dim>" / "<measure> share by <dim>" (value + % of total)
  * "<measure> growth by month" / "month over month <measure>" / "yoy <measure>" (series + % change)
  * "average <measure> per <entity>" where the entity is an identifier ("revenue per order": SUM / COUNT DISTINCT)
  * "<measure> by <dim> and <dim>" (two grouping columns)
  * a bare entity noun counts it ("weekly orders" = COUNT(DISTINCT order_id) per week)
  * dimension-value filters ("in the North region", known values from column metadata; apps.nl2sql.filters)

Columns come from ctx.columns_meta (roles: measure | dimension | time | identifier; inferred from the type
when absent) and metrics from ctx.metrics. Phrases are matched exactly or with rapidfuzz (typos), never by
partial/token-set matching, so unrecognised extra words (e.g. a filter) make the builder decline instead
of silently dropping a condition. Output is Postgres SQL over LOGICAL table names from ctx.policy.tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import sqlglot
from rapidfuzz import process
from rapidfuzz.distance import OSA
from sqlglot import exp

from nexora.contracts import ChartHint, QuestionContext

from . import filters as value_filters
from .slots import normalize_question
from .sqlutil import MAX_LIMIT, parse_select, quote_ident, render, validate_against_context
from .timephrases import Period, find_periods

NUMERIC_TYPES = re.compile(r"int|numeric|decimal|float|double|real|money|number|bigint|smallint", re.I)
TIME_TYPES = re.compile(r"date|time", re.I)
ROLE_ALIASES = {
    "measure": "measure", "metric": "measure", "fact": "measure", "numeric": "measure",
    "dimension": "dimension", "category": "dimension", "attribute": "dimension", "categorical": "dimension",
    "time": "time", "date": "time", "datetime": "time", "timestamp": "time", "temporal": "time",
    "identifier": "identifier", "id": "identifier", "key": "identifier",
}
STOPWORDS = {"the", "a", "an", "our", "my", "all", "of", "each", "every", "per", "overall", "total", "in", "for"}
FILLER_RE = re.compile(
    r"^(?:(?:please|can you|could you|kindly)\s+)?(?:show(?:\s+me)?|list|give(?:\s+me)?|get|display|plot|chart|"
    r"graph|tell(?:\s+me)?|find|compute|calculate|report|what(?:\s+is|\s+are|\s+was|\s+were|'s)?|which(?:\s+are)?|"
    r"how\s+(?:are|is|did|do|does))\s+(?:me\s+)?(?:the\s+)?"
)
GRAINS = {"day": "day", "daily": "day", "week": "week", "weekly": "week", "month": "month", "monthly": "month",
          "quarter": "quarter", "quarterly": "quarter", "year": "year", "yearly": "year", "annual": "year",
          "annually": "year"}
AGG_WORDS = [
    (re.compile(r"^(?:average|avg|mean)\s+(?:of\s+)?"), "AVG"),
    (re.compile(r"^(?:median)\s+(?:of\s+)?"), "MEDIAN"),
    (re.compile(r"^(?:total|sum\s+of|sum)\s+(?:of\s+)?"), "SUM"),
    (re.compile(r"^(?:number\s+of\s+(?:distinct\s+|unique\s+)?|count\s+of\s+(?:distinct\s+|unique\s+)?|"
                r"count\s+(?:distinct\s+|unique\s+)?|how\s+many\s+(?:distinct\s+|unique\s+)?|#\s*of\s+)"), "COUNT"),
    (re.compile(r"^(?:max(?:imum)?|highest|largest|biggest|peak)\s+(?:of\s+)?"), "MAX"),
    (re.compile(r"^(?:min(?:imum)?|lowest|smallest)\s+(?:of\s+)?"), "MIN"),
]
AGG_LABEL = {"SUM": "total", "AVG": "avg", "COUNT": "count", "MAX": "max", "MIN": "min", "MEDIAN": "median"}
COUNT_NOUNS = {"row", "record", "entry", "line", "transaction", "item"}
PII_SEMANTIC_TYPES = {"email", "phone", "person_name", "name", "address", "ssn", "pan", "aadhaar", "ip_address"}
ADDITIVE_AGGS = ("SUM(", "COUNT(")


@dataclass(frozen=True)
class Column:
    table: str
    name: str
    type: str
    role: str
    synonyms: tuple[str, ...] = ()
    description: str = ""
    values: tuple[str, ...] = ()  # known dimension values (semantic layer / dataset profile): filters
    is_pii: bool = False
    masked: bool = False  # a column mask applies to this user: never filtered on


@dataclass(frozen=True)
class Measure:
    expr: str          # SQL aggregate expression (quoted identifiers)
    alias: str
    label: str
    table: str | None  # None = usable on any table (COUNT(*))
    assumption: str


@dataclass
class BuilderResult:
    sql: str
    explanation: str
    assumptions: tuple[str, ...]
    chart_hint: ChartHint
    pattern: str
    tables: tuple[str, ...] = field(default_factory=tuple)


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def norm_phrase(text: str) -> str:
    words = [w for w in re.split(r"[\s_]+", (text or "").lower().strip()) if w]
    return " ".join(_stem(w) for w in words)


def _strip_stop(text: str) -> str:
    words = [w for w in text.split() if w not in STOPWORDS]
    return " ".join(words)


def _snake(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return (s or "value")[:50]


def infer_role(col: dict) -> str:
    raw = str(col.get("role") or "").lower()
    if raw in ROLE_ALIASES:
        return ROLE_ALIASES[raw]
    typ = str(col.get("type") or "")
    sem = str(col.get("semantic_type") or "").lower()
    name = str(col.get("name") or "").lower()
    if TIME_TYPES.search(typ) or sem in ("date", "datetime", "timestamp"):
        return "time"
    if sem in ("id", "identifier", "key") or name == "id" or name.endswith("_id"):
        return "identifier"
    if NUMERIC_TYPES.search(typ):
        return "measure"
    return "dimension"


class SchemaIndex:
    """Allowed columns + metrics of a QuestionContext, with phrase matching."""

    def __init__(self, ctx: QuestionContext):
        self.ctx = ctx
        self.tables: list[str] = list(ctx.policy.tables.keys())
        self.allowed: dict[str, frozenset[str]] = {
            t: frozenset(c.lower() for c in ctx.policy.allowed_columns.get(t, frozenset())) for t in self.tables
        }
        self.columns: list[Column] = []
        row_filtered = set((getattr(ctx.policy, "row_predicates", None) or {}).keys())
        for table, cols in (ctx.columns_meta or {}).items():
            if table not in ctx.policy.tables:
                continue
            allowed = self.allowed.get(table)
            for c in cols:
                name = str(c.get("name") or "")
                if not name or name.lower() not in (allowed or frozenset()):  # fail closed
                    continue
                masked = bool(c.get("masked")) or (table, name) in (ctx.policy.column_masks or {})
                self.columns.append(Column(
                    table=table, name=name, type=str(c.get("type") or ""), role=infer_role(c),
                    synonyms=tuple(str(s) for s in (c.get("synonyms") or ()) if s),
                    description=str(c.get("description") or ""),
                    # Known values only where they cannot reveal rows the user may not see: never for a masked
                    # column, and not on a table with a row-level policy (the named-value rule still works there).
                    values=() if masked or table in row_filtered else value_filters.column_values(c),
                    is_pii=bool(c.get("is_pii")) or str(c.get("semantic_type") or "").lower() in PII_SEMANTIC_TYPES,
                    masked=masked,
                ))
        self.metrics = [m for m in (ctx.metrics or ()) if m.get("expression")]

    # ---------------------------------------------------------------- matching primitives
    @staticmethod
    def _choices_for(col: Column) -> list[str]:
        names = {norm_phrase(col.name), norm_phrase(col.name.replace("_", " "))}
        names |= {norm_phrase(s) for s in col.synonyms}
        return [n for n in names if n]

    @staticmethod
    def _best(phrase: str, choices: dict[str, object]) -> object | None:
        if not phrase or not choices:
            return None
        if phrase in choices:
            return choices[phrase]
        # Typo tolerance: optimal-string-alignment edit distance (transpositions count once).
        budget = 0 if len(phrase) < 5 else (1 if len(phrase) < 10 else 2)
        if budget == 0:
            return None
        hits = process.extract(phrase, list(choices.keys()), scorer=OSA.distance, score_cutoff=budget,
                               limit=None)
        if not hits:
            return None
        best = min(h[1] for h in hits)
        targets = {id(choices[h[0]]): choices[h[0]] for h in hits if h[1] == best}
        if len(targets) != 1:
            return None  # ambiguous typo: decline rather than guess
        return next(iter(targets.values()))

    def _match_columns(self, phrase: str, roles: set[str], table: str | None = None) -> Column | None:
        target = norm_phrase(_strip_stop(phrase.lower()))
        choices: dict[str, Column] = {}
        for col in self.columns:
            if col.role not in roles or (table and col.table != table):
                continue
            for name in self._choices_for(col):
                choices.setdefault(name, col)
        return self._best(target, choices)  # type: ignore[return-value]

    def match_dimension(self, phrase: str, table: str | None = None) -> Column | None:
        return self._match_columns(phrase, {"dimension", "identifier"}, table) or self._match_columns(
            phrase, {"time", "measure"}, table)

    def time_column(self, table: str) -> Column | None:
        cands = [c for c in self.columns if c.table == table and c.role == "time"]
        if not cands:
            return None
        preferred = ("order_date", "date", "transaction_date", "sale_date", "created_at", "invoice_date")
        for p in preferred:
            for c in cands:
                if c.name.lower() == p:
                    return c
        return cands[0]

    def table_names(self) -> dict[str, str]:
        return {norm_phrase(t.replace("_", " ")): t for t in self.tables}

    # ---------------------------------------------------------------- metrics
    def _metric_table(self, metric: dict) -> str | None:
        if metric.get("table") in self.ctx.policy.tables:
            return metric["table"]
        try:
            node = sqlglot.parse_one(str(metric["expression"]), read="postgres")
        except Exception:  # noqa: BLE001
            return None
        cols = {c.name.lower() for c in node.find_all(exp.Column)}
        for t in self.tables:
            if cols and cols <= self.allowed.get(t, frozenset()):
                return t
        return None

    def _match_metric(self, phrase: str) -> Measure | None:
        target = norm_phrase(_strip_stop(phrase.lower()))
        choices: dict[str, dict] = {}
        for m in self.metrics:
            for n in (m.get("key"), m.get("name"), *(m.get("synonyms") or ())):
                if n:
                    choices.setdefault(norm_phrase(str(n).replace("_", " ")), m)
        metric = self._best(target, choices)
        if metric is None:
            return None
        table = self._metric_table(metric)  # type: ignore[arg-type]
        if table is None:
            return None
        key = str(metric.get("key") or metric.get("name"))  # type: ignore[union-attr]
        return Measure(expr=str(metric["expression"]), alias=_snake(key), label=str(metric.get("name") or key),
                       table=table, assumption=f"'{phrase.strip()}' uses the metric {key} = {metric['expression']}")

    # ---------------------------------------------------------------- measures
    def match_measure(self, phrase: str, default_agg: str | None = None) -> Measure | None:
        raw = re.sub(r"\s+", " ", (phrase or "").lower()).strip()
        raw = re.sub(r"^(?:the|our|my)\s+", "", raw)
        if not raw:
            return None
        metric = self._match_metric(raw)
        if metric:
            return metric
        agg = None
        rest = raw
        for rx, name in AGG_WORDS:
            m = rx.match(rest)
            if m:
                agg, rest = name, rest[m.end():].strip()
                break
        distinct = bool(re.search(r"\b(?:distinct|unique)\b", raw))
        if agg and rest:
            metric = self._match_metric(rest)
            if metric:
                return metric
        agg = agg or default_agg
        if agg == "COUNT":
            return self._count_measure(rest, distinct)
        col = self._match_columns(rest, {"measure"})
        if col is None:
            return None
        agg = agg or "SUM"
        q = quote_ident(col.name)
        expr = (f"PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {q})" if agg == "MEDIAN" else f"{agg}({q})")
        alias = _snake(f"{AGG_LABEL[agg]}_{col.name}")
        verb = {"SUM": "the sum of", "AVG": "the average of", "MAX": "the maximum of", "MIN": "the minimum of",
                "MEDIAN": "the median of"}[agg]
        return Measure(expr=expr, alias=alias, label=f"{AGG_LABEL[agg]} {col.name.replace('_', ' ')}",
                       table=col.table, assumption=f"'{phrase.strip()}' is computed as {verb} {col.name}")

    def reserved_names(self) -> set[str]:
        """Column / synonym / metric / table names: never treated as filter values."""
        out: set[str] = set(self.table_names())
        for col in self.columns:
            out |= set(self._choices_for(col))
            out.add(col.name.lower().replace("_", " "))
        for m in self.metrics:
            for n in (m.get("key"), m.get("name"), *(m.get("synonyms") or ())):
                if n:
                    out.add(str(n).lower().replace("_", " "))
        return out

    def entity(self, phrase: str) -> tuple[str, str | None, str] | None:
        """A countable entity: (COUNT expression, table, label) for an identifier column or a row noun."""
        target = norm_phrase(_strip_stop((phrase or "").lower()))
        if not target:
            return None
        if target in COUNT_NOUNS or target in self.table_names():
            return "COUNT(*)", self.table_names().get(target), "row"
        col = self._match_columns(phrase, {"identifier"}) or self._match_columns(phrase + " id", {"identifier"})
        if col is None:
            return None
        return f"COUNT(DISTINCT {quote_ident(col.name)})", col.table, col.name

    def implicit_count(self, phrase: str) -> Measure | None:
        """A bare entity noun as a measure ("weekly orders", "top regions by customers")."""
        found = self.entity(phrase)
        if found is None:
            return None
        expr, table, name = found
        if expr == "COUNT(*)":
            return Measure(expr=expr, alias="row_count", label="number of rows", table=table,
                           assumption="Counted rows")
        return Measure(expr=expr, alias=_snake(f"distinct_{name}"), label=f"number of {name.replace('_', ' ')}",
                       table=table, assumption=f"'{phrase.strip()}' counted as distinct values of {name}")

    def ratio_measure(self, measure_phrase: str, entity_phrase: str) -> Measure | None:
        """'average revenue per order' -> SUM(revenue) / COUNT(DISTINCT order_id) (entity must be countable)."""
        found = self.entity(entity_phrase)
        if found is None:
            return None
        count_expr, entity_table, entity_name = found
        phrase = re.sub(r"^(?:the\s+)?(?:average|avg|mean|total)\s+(?:of\s+)?", "", measure_phrase.strip().lower())
        col = self._match_columns(phrase, {"measure"})
        if col is None or (entity_table and entity_table != col.table):
            return None
        entity_label = "row" if count_expr == "COUNT(*)" else entity_name.removesuffix("_id")
        expr = f"SUM({quote_ident(col.name)}) / NULLIF({count_expr}, 0)"
        plain_count = count_expr.replace('"', "")
        return Measure(expr=expr, alias=_snake(f"{col.name}_per_{entity_label}"),
                       label=f"{col.name.replace('_', ' ')} per {entity_label.replace('_', ' ')}", table=col.table,
                       assumption=(f"'{measure_phrase.strip()} per {entity_phrase.strip()}' is computed as "
                                   f"SUM({col.name}) / {plain_count}"))

    def _count_measure(self, rest: str, distinct: bool) -> Measure | None:
        target = norm_phrase(_strip_stop(rest))
        if not target or target in COUNT_NOUNS or target in self.table_names():
            table = self.table_names().get(target) if target else None
            return Measure(expr="COUNT(*)", alias="row_count", label="number of rows", table=table,
                           assumption="Counted rows")
        col = self._match_columns(rest, {"identifier", "dimension"})
        if col is None:
            # "number of customers" -> customer_id / customer
            col = self._match_columns(rest + " id", {"identifier", "dimension"})
        if col is None:
            return None
        q = quote_ident(col.name)
        return Measure(expr=f"COUNT(DISTINCT {q})", alias=_snake(f"distinct_{col.name}"),
                       label=f"number of distinct {col.name.replace('_', ' ')}", table=col.table,
                       assumption=f"'{rest.strip()}' counted as distinct values of {col.name}"
                       + ("" if distinct else " (distinct)"))


# ------------------------------------------------------------------------------------------- SQL pieces
def _conditions(time_col: Column | None, periods: list[Period], flts: list, table: str) -> str:
    conds = [c for c in (_time_filter(time_col, periods),) if c]
    for f in flts or ():
        if f.table != table:
            raise _Decline("filter on another table")
        conds.append(f.sql())
    return " AND ".join(conds)


def _filter_notes(flts: list) -> list[str]:
    return [f.describe() for f in flts or ()]


def _filter_text(flts: list) -> str:
    return "".join(f" where {f.column.replace('_', ' ')} is {f.value}" for f in flts or ())


def _time_filter(time_col: Column | None, periods: list[Period]) -> str:
    if not periods:
        return ""
    if time_col is None:
        raise _Decline("no time column for the time filter")
    q = quote_ident(time_col.name)
    conds: list[str] = []
    for p in periods:
        if p.start:
            conds.append(f"{q} >= DATE '{p.start.isoformat()}'")
        if p.end:
            conds.append(f"{q} < DATE '{p.end.isoformat()}'")
    return " AND ".join(conds)


class _Decline(Exception):
    pass


def _finalize(sql: str, ctx: QuestionContext) -> str:
    ast, issue = parse_select(sql)
    if issue:
        raise _Decline(f"builder produced invalid SQL: {issue.message}")
    allowed = {t: ctx.policy.allowed_columns.get(t, frozenset()) for t in ctx.policy.tables}
    problem = validate_against_context(ast, ctx.policy.tables, allowed)
    if problem:
        raise _Decline(problem.message)
    return render(ast)


def _pick_table(index: SchemaIndex, *needs: str | None) -> str:
    named = {n for n in needs if n}
    if len(named) > 1:
        raise _Decline("columns span several tables")
    if named:
        return named.pop()
    if len(index.tables) == 1:
        return index.tables[0]
    raise _Decline("cannot choose a table")


def _clean(text: str) -> str:
    t = text.strip()
    for _ in range(2):
        t = FILLER_RE.sub("", t).strip()
    t = re.sub(r"\s+(?:please|pls)$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-")
    return t


# ------------------------------------------------------------------------------------------- patterns
_DIRECTION_DESC = ("most", "highest", "largest", "biggest", "greatest", "top", "best", "maximum", "max")
TOP_RE = re.compile(r"^(top|bottom|best|worst|highest|lowest|largest|smallest)\s+(\d{1,4})\s+(.+?)\s+"
                    r"(?:by|in terms of|based on|ranked by|with the (?:highest|most|lowest|least)|for)\s+(.+)$")
TOP_NO_N_RE = re.compile(r"^(?:the\s+)?(top|bottom|best|worst)\s+(.+?)\s+(?:by|in terms of|based on)\s+(.+)$")
# "(which) 3 products sold the most units", "region has the highest revenue", "channel with the lowest discount"
SUPERLATIVE_RE = re.compile(
    r"^(?:(\d{1,4})\s+)?(.+?)\s+(?:sold|sell|had|have|has|made|make|generated|generate|brought\s+in|got|get|earned|"
    r"recorded|drove|drive|did|with)\s+(?:the\s+)?(most|highest|largest|biggest|greatest|maximum|max|least|lowest|"
    r"smallest|fewest|minimum|min)\s+(.+)$")
COMPARE_RE = re.compile(
    r"^(?:compare\s+)?(.*?)\s*(?:for\s+|in\s+)?(?:this|current)\s+(week|month|quarter|year)\s+"
    r"(?:vs\.?|versus|compared\s+(?:to|with)|against|and|to)\s+(?:the\s+)?(?:last|previous|prior)\s+\2$")
COMPARE_REV_RE = re.compile(
    r"^(?:compare\s+)?(.*?)\s*(?:for\s+|in\s+)?(?:last|previous|prior)\s+(week|month|quarter|year)\s+"
    r"(?:vs\.?|versus|compared\s+(?:to|with)|against|and|to)\s+(?:the\s+)?(?:this|current)\s+\2$")
GROWTH_WORDS_RE = re.compile(
    r"^(?:(?:the\s+)?(?:%|percent(?:age)?)?\s*(?:growth|change|increase|decrease|difference)\s+(?:rate\s+)?"
    r"(?:in|of)\s+(.+)|(.+?)\s+(?:%\s*|percent(?:age)?\s+)?(?:growth|change|increase|decrease|difference)"
    r"(?:\s+rate)?)$")
GROWTH_SERIES_RES = (
    # "month over month revenue growth", "mom revenue", "year-over-year sales"
    (re.compile(r"^(day|week|month|quarter|year)\s+(?:over|on)\s+\1\s+(.+?)(?:\s+(?:growth|change)(?:\s+rate)?)?$"),
     "unit_first"),
    (re.compile(r"^(dod|wow|mom|qoq|yoy)\s+(.+?)(?:\s+(?:growth|change)(?:\s+rate)?)?$"), "abbr_first"),
    (re.compile(r"^(.+?)\s+(?:growth|change)(?:\s+rate)?\s+(dod|wow|mom|qoq|yoy)$"), "abbr_last"),
    (re.compile(r"^(.+?)\s+(?:growth|change)(?:\s+rate)?\s+(day|week|month|quarter|year)\s+(?:over|on)\s+\2$"),
     "unit_last"),
    # "monthly revenue growth", "revenue growth by quarter", "revenue growth rate"
    (re.compile(r"^(daily|weekly|monthly|quarterly|yearly|annual)\s+(.+?)\s+(?:growth|change)(?:\s+rate)?$"),
     "grain_first"),
    (re.compile(r"^(.+?)\s+(?:growth|change)(?:\s+rate)?(?:\s+(?:by|per|each|every)\s+(day|week|month|quarter|year))?$"),
     "grain_last"),
)
ABBR_GRAIN = {"dod": "day", "wow": "week", "mom": "month", "qoq": "quarter", "yoy": "year"}
SHARE_RES = (
    # "share of revenue by region", "percentage of orders per channel", "contribution of each region to revenue",
    # "(what) percentage of revenue comes from each channel"
    (re.compile(r"^(?:the\s+)?(?:%|percent(?:age)?|share|proportion|contribution|portion)\s+(?:of\s+)?(?:the\s+)?"
                r"(?:total\s+)?(.+?)(?:\s+(?:comes?|came|is|are|was|were))?\s+"
                r"(?:by|per|for each|across|from each|by each|from every|in each)\s+(.+)$"), "measure_dim"),
    (re.compile(r"^(?:the\s+)?(.+?)\s+(?:share|contribution|mix|distribution|breakdown\s+in\s+%)\s+"
                r"(?:by|per|across|of each|for each)\s+(.+)$"), "measure_dim"),
    (re.compile(r"^(?:the\s+)?(.+?)\s+(?:share|contribution)\s+(?:of|to)\s+(?:the\s+)?(?:total\s+)?(.+)$"),
     "dim_measure"),
    (re.compile(r"^(.+?)\s+(?:by|per|for each|across)\s+(.+?)\s+(?:as\s+(?:a\s+)?|in\s+)?(?:%|percent(?:age)?|share)"
                r"(?:\s+of\s+(?:the\s+)?total)?$"), "measure_dim"),
)
PER_ENTITY_RE = re.compile(r"^(.+?)\s+per\s+(.+?)(?:\s+(?:by|for each|across|split by|broken down by|grouped by|"
                           r"each)\s+(.+))?$")
SERIES_SUFFIX_RE = re.compile(r"^(.+?)\s+(?:over\s+time|trend|trends|over\s+the\s+period|time\s+series|"
                              r"(?:by|per|each|every)\s+(day|week|month|quarter|year))$")
SERIES_PREFIX_RE = re.compile(r"^(daily|weekly|monthly|quarterly|yearly|annual)\s+(.+?)(?:\s+trend)?$")
BY_RE = re.compile(r"^(.+?)\s+(?:by|per|for each|across|split by|broken down by|grouped by|for every)\s+(.+)$")
TOTAL_RE = re.compile(r"^(?:how\s+much\s+|what\s+)?(.+?)(?:\s+(?:did\s+we\s+(?:make|have|sell|get)|do\s+we\s+have|"
                      r"is\s+there|are\s+there|were\s+there|was\s+there))?$")


def _series_grain(grain: str | None, periods: list[Period]) -> str:
    if grain:
        return grain
    if periods and periods[0].days is not None:
        days = periods[0].days
        if days <= 31:
            return "day"
        if days <= 120:
            return "week"
    return "month"


def build_sql(question: str, ctx: QuestionContext, *, today: date) -> BuilderResult | None:
    """Return a BuilderResult, or None when the question is outside the builder's safe repertoire."""
    try:
        return _build(question, ctx, today)
    except _Decline:
        return None


def _build(question: str, ctx: QuestionContext, today: date) -> BuilderResult | None:
    index = SchemaIndex(ctx)
    if not index.tables or not index.columns:
        return None
    norm = normalize_question(question)
    norm, flts, problem = value_filters.extract(norm, index.columns, reserved_names=index.reserved_names())
    if problem:
        return None

    # compare this X vs last X (both periods recognised together)
    for rx, reverse in ((COMPARE_RE, False), (COMPARE_REV_RE, True)):
        m = rx.match(_clean(norm))
        if m:
            return _compare(index, ctx, m.group(1), m.group(2), today, reverse, flts)

    matches = find_periods(norm, today)
    if len(matches) > 1:
        return None
    periods = [mm.period for mm in matches]
    rest = norm
    for mm in reversed(matches):
        rest = rest[:mm.start_idx] + " " + rest[mm.end_idx:]
    rest = _clean(re.sub(r"\s+", " ", rest))
    rest = re.sub(r"\s+(?:in|for|during|over|from|of)$", "", rest).strip()

    for rx, order in SHARE_RES:
        m = rx.match(rest)
        if m:
            measure_phrase, dim_phrase = (m.group(1), m.group(2)) if order == "measure_dim" else (m.group(2),
                                                                                                    m.group(1))
            return _share(index, ctx, measure_phrase, dim_phrase, periods, flts)

    if re.search(r"\b(?:growth|change|over|dod|wow|mom|qoq|yoy)\b", rest):
        for rx, kind in GROWTH_SERIES_RES:
            m = rx.match(rest)
            if not m:
                continue
            if kind == "unit_first":
                grain, measure_phrase = m.group(1), m.group(2)
            elif kind == "abbr_first":
                grain, measure_phrase = ABBR_GRAIN[m.group(1)], m.group(2)
            elif kind == "abbr_last":
                grain, measure_phrase = ABBR_GRAIN[m.group(2)], m.group(1)
            elif kind == "unit_last":
                grain, measure_phrase = m.group(2), m.group(1)
            elif kind == "grain_first":
                grain, measure_phrase = GRAINS[m.group(1)], m.group(2)
            else:
                grain, measure_phrase = m.group(2), m.group(1)
            if re.search(r"\b(?:by|per|for each|across)\b", measure_phrase):
                return None  # "growth by region" is not a time series; leave it to the LLM path
            return _growth_series(index, ctx, measure_phrase, grain, periods, flts)

    m = TOP_RE.match(rest)
    if m:
        direction = "ASC" if m.group(1) in ("bottom", "worst", "lowest", "smallest") else "DESC"
        return _ranked(index, ctx, int(m.group(2)), m.group(3), m.group(4), direction, periods, flts)
    m = TOP_NO_N_RE.match(rest)
    if m:
        direction = "ASC" if m.group(1) in ("bottom", "worst") else "DESC"
        return _ranked(index, ctx, 10, m.group(2), m.group(3), direction, periods, flts, implicit_n=True)
    m = SUPERLATIVE_RE.match(rest)
    if m:
        direction = "DESC" if m.group(3) in _DIRECTION_DESC else "ASC"
        n = int(m.group(1)) if m.group(1) else 1
        return _ranked(index, ctx, n, m.group(2), m.group(4), direction, periods, flts)

    m = PER_ENTITY_RE.match(rest)
    if m:
        ratio = index.ratio_measure(m.group(1), m.group(2))
        if ratio is not None:
            group = (m.group(3) or "").strip()
            grain = GRAINS.get(group)
            if grain:
                return _series(index, ctx, "", grain, periods, flts, measure=ratio)
            if group:
                return _grouped(index, ctx, "", group, periods, flts, measure=ratio)
            return _total(index, ctx, "", periods, flts, question=norm, measure=ratio)

    m = SERIES_PREFIX_RE.match(rest)
    if m:
        return _series(index, ctx, m.group(2), GRAINS[m.group(1)], periods, flts)
    m = SERIES_SUFFIX_RE.match(rest)
    if m:
        return _series(index, ctx, m.group(1), GRAINS.get(m.group(2) or "", None), periods, flts)

    m = BY_RE.match(rest)
    if m:
        grain = GRAINS.get(m.group(2).strip())
        if grain:
            return _series(index, ctx, m.group(1), grain, periods, flts)
        return _grouped(index, ctx, m.group(1), m.group(2), periods, flts)

    m = TOTAL_RE.match(rest)
    if m:
        return _total(index, ctx, m.group(1), periods, flts, question=norm)
    return None


def _measure_or_decline(index: SchemaIndex, phrase: str, default_agg: str | None = None) -> Measure:
    measure = index.match_measure(phrase, default_agg)
    if measure is None and default_agg is None:
        measure = index.implicit_count(phrase)
    if measure is None:
        raise _Decline(f"unknown measure {phrase!r}")
    return measure


def _where(filter_sql: str) -> str:
    return f" WHERE {filter_sql}" if filter_sql else ""


def _period_note(periods: list[Period]) -> str:
    return f" for {periods[0].describe()}" if periods else ""


def _notes(measure: Measure, time_col: Column | None, flts: list) -> list[str]:
    notes = [measure.assumption]
    if time_col:
        notes.append(f"Time filter applied on {time_col.name}")
    return notes + _filter_notes(flts)


def _filter_tables(flts: list) -> list[str]:
    return [f.table for f in flts or ()]


def _ranked(index, ctx, n, dim_phrase, measure_phrase, direction, periods, flts, implicit_n=False) -> BuilderResult:
    measure = _measure_or_decline(index, measure_phrase)
    dim = index.match_dimension(dim_phrase, measure.table)
    if dim is None:
        raise _Decline(f"unknown dimension {dim_phrase!r}")
    table = _pick_table(index, measure.table, dim.table, *_filter_tables(flts))
    n = max(1, min(int(n), MAX_LIMIT))
    time_col = index.time_column(table) if periods else None
    d = quote_ident(dim.name)
    sql = (f"SELECT {d}, {measure.expr} AS {quote_ident(measure.alias)} FROM {quote_ident(table)}"  # noqa: S608 - quoted identifiers + validated literals, re-parsed by sqlglot
           f"{_where(_conditions(time_col, periods, flts, table))} GROUP BY {d} "
           f"ORDER BY {quote_ident(measure.alias)} {direction} NULLS LAST LIMIT {n}")
    assumptions = _notes(measure, time_col, flts)
    if implicit_n:
        assumptions.append("No count given; showing the top 10")
    word = "Top" if direction == "DESC" else "Bottom"
    return BuilderResult(
        sql=_finalize(sql, ctx),
        explanation=(f"{word} {n} {dim.name.replace('_', ' ')} by {measure.label}{_filter_text(flts)}"
                     f"{_period_note(periods)}."),
        assumptions=tuple(assumptions),
        chart_hint=ChartHint(type="bar", x=dim.name, y=(measure.alias,)),
        pattern="top_n",
        tables=(table,),
    )


def _dimensions(index: SchemaIndex, phrase: str, table: str | None) -> list[Column]:
    """One dimension, or two joined by 'and' ("region and channel")."""
    dim = index.match_dimension(phrase, table)
    if dim is not None:
        return [dim]
    parts = re.split(r"\s+(?:and|&)\s+|\s*,\s*", phrase.strip())
    if len(parts) != 2:
        raise _Decline(f"unknown dimension {phrase!r}")
    dims = [index.match_dimension(p, table) for p in parts]
    if any(d is None for d in dims) or dims[0] == dims[1]:
        raise _Decline(f"unknown dimension {phrase!r}")
    return dims  # type: ignore[return-value]


def _grouped(index, ctx, measure_phrase, dim_phrase, periods, flts, measure: Measure | None = None) -> BuilderResult:
    measure = measure or _measure_or_decline(index, measure_phrase)
    dims = _dimensions(index, dim_phrase, measure.table)
    table = _pick_table(index, measure.table, *{d.table for d in dims}, *_filter_tables(flts))
    time_col = index.time_column(table) if periods else None
    cols = ", ".join(quote_ident(d.name) for d in dims)
    sql = (f"SELECT {cols}, {measure.expr} AS {quote_ident(measure.alias)} FROM {quote_ident(table)}"  # noqa: S608 - quoted identifiers + validated literals, re-parsed by sqlglot
           f"{_where(_conditions(time_col, periods, flts, table))} GROUP BY {cols} "
           f"ORDER BY {quote_ident(measure.alias)} DESC NULLS LAST LIMIT {MAX_LIMIT}")
    names = " and ".join(d.name.replace("_", " ") for d in dims)
    return BuilderResult(
        sql=_finalize(sql, ctx),
        explanation=f"{measure.label.capitalize()} by {names}{_filter_text(flts)}{_period_note(periods)}.",
        assumptions=tuple(_notes(measure, time_col, flts)),
        chart_hint=ChartHint(type="bar", x=dims[0].name, y=(measure.alias,),
                             series=dims[1].name if len(dims) > 1 else None),
        pattern="by_dimension",
        tables=(table,),
    )


def _share(index, ctx, measure_phrase, dim_phrase, periods, flts) -> BuilderResult:
    measure = _measure_or_decline(index, measure_phrase)
    if not measure.expr.startswith(ADDITIVE_AGGS) or "/" in measure.expr:
        raise _Decline("a share of total needs an additive measure (sum or count)")
    dim = index.match_dimension(dim_phrase, measure.table)
    if dim is None:
        raise _Decline(f"unknown dimension {dim_phrase!r}")
    table = _pick_table(index, measure.table, dim.table, *_filter_tables(flts))
    time_col = index.time_column(table) if periods else None
    d = quote_ident(dim.name)
    alias = quote_ident(measure.alias)
    share = f"ROUND(100.0 * {measure.expr} / NULLIF(SUM({measure.expr}) OVER (), 0), 2)"
    sql = (f"SELECT {d}, {measure.expr} AS {alias}, {share} AS \"share_pct\" FROM {quote_ident(table)}"  # noqa: S608 - quoted identifiers + validated literals, re-parsed by sqlglot
           f"{_where(_conditions(time_col, periods, flts, table))} GROUP BY {d} "
           f"ORDER BY {alias} DESC NULLS LAST LIMIT {MAX_LIMIT}")
    notes = _notes(measure, time_col, flts) + ["share_pct = each group's percentage of the total"]
    return BuilderResult(
        sql=_finalize(sql, ctx),
        explanation=(f"Share of {measure.label} by {dim.name.replace('_', ' ')}{_filter_text(flts)}"
                     f"{_period_note(periods)}."),
        assumptions=tuple(notes),
        chart_hint=ChartHint(type="pie", x=dim.name, y=("share_pct",)),
        pattern="share_of_total",
        tables=(table,),
    )


def _series(index, ctx, measure_phrase, grain, periods, flts, measure: Measure | None = None) -> BuilderResult:
    measure = measure or _measure_or_decline(index, measure_phrase)
    table = _pick_table(index, measure.table, *_filter_tables(flts))
    time_col = index.time_column(table)
    if time_col is None:
        raise _Decline("no time column")
    grain = _series_grain(grain, periods)
    bucket = f"DATE_TRUNC('{grain}', {quote_ident(time_col.name)})"
    sql = (f"SELECT {bucket} AS {quote_ident(grain)}, {measure.expr} AS {quote_ident(measure.alias)} "  # noqa: S608 - quoted identifiers + validated literals, re-parsed by sqlglot
           f"FROM {quote_ident(table)}{_where(_conditions(time_col, periods, flts, table))} "
           f"GROUP BY {bucket} ORDER BY {quote_ident(grain)} LIMIT {MAX_LIMIT}")
    return BuilderResult(
        sql=_finalize(sql, ctx),
        explanation=f"{measure.label.capitalize()} per {grain}{_filter_text(flts)}{_period_note(periods)}.",
        assumptions=tuple([measure.assumption, f"Bucketed by {grain} on {time_col.name}", *_filter_notes(flts)]),
        chart_hint=ChartHint(type="line", x=grain, y=(measure.alias,)),
        pattern="time_series",
        tables=(table,),
    )


def _growth_series(index, ctx, measure_phrase, grain, periods, flts) -> BuilderResult:
    measure = _measure_or_decline(index, measure_phrase)
    table = _pick_table(index, measure.table, *_filter_tables(flts))
    time_col = index.time_column(table)
    if time_col is None:
        raise _Decline("no time column")
    grain = _series_grain(grain, periods)
    bucket = f"DATE_TRUNC('{grain}', {quote_ident(time_col.name)})"
    g = quote_ident(grain)
    prev = f"LAG({measure.expr}) OVER (ORDER BY {bucket})"
    growth = f"ROUND(100.0 * ({measure.expr} - {prev}) / NULLIF({prev}, 0), 2)"
    sql = (f"SELECT {bucket} AS {g}, {measure.expr} AS {quote_ident(measure.alias)}, {growth} AS \"growth_pct\" "  # noqa: S608 - quoted identifiers + validated literals, re-parsed by sqlglot
           f"FROM {quote_ident(table)}{_where(_conditions(time_col, periods, flts, table))} "
           f"GROUP BY {bucket} ORDER BY {g} LIMIT {MAX_LIMIT}")
    return BuilderResult(
        sql=_finalize(sql, ctx),
        explanation=(f"{measure.label.capitalize()} per {grain} with the % change from the previous {grain}"
                     f"{_filter_text(flts)}{_period_note(periods)}."),
        assumptions=(measure.assumption, f"Bucketed by {grain} on {time_col.name}",
                     f"growth_pct = change vs the previous {grain} (empty for the first {grain})",
                     *_filter_notes(flts)),
        chart_hint=ChartHint(type="line", x=grain, y=(measure.alias,)),
        pattern="growth_series",
        tables=(table,),
    )


def _total(index, ctx, measure_phrase, periods, flts, question: str, measure: Measure | None = None) -> BuilderResult:
    if measure is None:
        default_agg = "COUNT" if re.match(r"^(?:how many|number of|count)\b", _clean(question)) else None
        measure = _measure_or_decline(index, measure_phrase, default_agg)
    table = _pick_table(index, measure.table, *_filter_tables(flts))
    time_col = index.time_column(table) if periods else None
    sql = (f"SELECT {measure.expr} AS {quote_ident(measure.alias)} FROM {quote_ident(table)}"  # noqa: S608 - quoted identifiers + validated literals, re-parsed by sqlglot
           f"{_where(_conditions(time_col, periods, flts, table))}")
    return BuilderResult(
        sql=_finalize(sql, ctx),
        explanation=f"{measure.label.capitalize()}{_filter_text(flts)}{_period_note(periods)}.",
        assumptions=tuple(_notes(measure, time_col, flts)),
        chart_hint=ChartHint(type="kpi", y=(measure.alias,)),
        pattern="total",
        tables=(table,),
    )


def _compare(index, ctx, measure_phrase, unit, today, reverse, flts) -> BuilderResult:
    from .timephrases import calendar_period

    measure_phrase = (measure_phrase or "").strip()
    growth = False
    gm = GROWTH_WORDS_RE.match(measure_phrase)
    if gm:
        growth, measure_phrase = True, (gm.group(1) or gm.group(2) or "").strip()
    if not measure_phrase:
        if len(index.metrics) == 1:
            measure_phrase = str(index.metrics[0].get("key") or index.metrics[0].get("name"))
        else:
            measures = [c for c in index.columns if c.role == "measure"]
            if len(measures) != 1:
                raise _Decline("ambiguous measure for comparison")
            measure_phrase = measures[0].name
    measure = _measure_or_decline(index, measure_phrase)
    table = _pick_table(index, measure.table, *_filter_tables(flts))
    time_col = index.time_column(table)
    if time_col is None:
        raise _Decline("no time column")
    cur = calendar_period(today, unit, 0, f"this {unit}", f"this {unit}")
    prev = calendar_period(today, unit, -1, f"last {unit}", f"last {unit}")
    t = quote_ident(time_col.name)
    window = f"{t} >= DATE '{prev.start.isoformat()}' AND {t} < DATE '{cur.end.isoformat()}'"
    extra = "".join(f" AND {f.sql()}" for f in flts if f.table == table)
    if len([f for f in flts if f.table != table]):
        raise _Decline("filter on another table")
    notes = (measure.assumption, f"This {unit} is {unit}-to-date (through {today.isoformat()})",
             f"Periods use {time_col.name}", *_filter_notes(flts))
    if growth:
        if not measure.expr.startswith(ADDITIVE_AGGS) or "/" in measure.expr:
            raise _Decline("growth comparison needs an additive measure")
        cur_cond = f"{t} >= DATE '{cur.start.isoformat()}'"
        cur_v = _conditional(measure.expr, cur_cond)
        prev_v = _conditional(measure.expr, f"{t} < DATE '{cur.start.isoformat()}'")
        sql = (f"SELECT {cur_v} AS \"current_period\", {prev_v} AS \"previous_period\", "  # noqa: S608 - quoted identifiers + validated literals, re-parsed by sqlglot
               f"ROUND(100.0 * ({cur_v} - {prev_v}) / NULLIF({prev_v}, 0), 2) AS \"change_pct\" "
               f"FROM {quote_ident(table)} WHERE {window}{extra}")
        return BuilderResult(
            sql=_finalize(sql, ctx),
            explanation=(f"Change in {measure.label} from {prev.describe()} to {cur.describe()}"
                         f"{_filter_text(flts)}."),
            assumptions=(*notes, "change_pct = (current - previous) / previous x 100"),
            chart_hint=ChartHint(type="kpi", y=("current_period", "previous_period", "change_pct")),
            pattern="compare_growth",
            tables=(table,),
        )
    label = f"CASE WHEN {t} >= DATE '{cur.start.isoformat()}' THEN 'current_period' ELSE 'previous_period' END"
    sql = (f"SELECT {label} AS \"period\", {measure.expr} AS {quote_ident(measure.alias)} "  # noqa: S608 - quoted identifiers + validated literals, re-parsed by sqlglot
           f"FROM {quote_ident(table)} WHERE {window}{extra} GROUP BY {label} ORDER BY \"period\"")
    return BuilderResult(
        sql=_finalize(sql, ctx),
        explanation=(f"{measure.label.capitalize()} for {cur.describe()} compared with {prev.describe()}"
                     f"{_filter_text(flts)}."),
        assumptions=notes,
        chart_hint=ChartHint(type="bar", x="period", y=(measure.alias,)),
        pattern="compare_periods",
        tables=(table,),
    )


def _conditional(expr: str, cond: str) -> str:
    """SUM(x) -> SUM(CASE WHEN cond THEN x END); COUNT(DISTINCT x) / COUNT(*) likewise (FILTER-free, portable)."""
    m = re.fullmatch(r'(SUM|COUNT)\((DISTINCT\s+)?("(?:[^"]|"")+"|\*)\)', expr.strip())
    if not m:
        raise _Decline("cannot split this measure by period")
    fn, distinct, arg = m.group(1), m.group(2) or "", m.group(3)
    if arg.strip() == "*":
        return f"COUNT(CASE WHEN {cond} THEN 1 END)"
    value = f"CASE WHEN {cond} THEN {arg} END"
    return f"{fn}({distinct}{value})" if fn == "COUNT" else f"COALESCE(SUM({value}), 0)"
