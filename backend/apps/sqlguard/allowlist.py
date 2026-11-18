"""Static allow/deny lists of the SQL guard (SECURITY_ARCHITECTURE.md §5.1 steps 4, 5, 8).

Everything here is an ALLOWLIST: a function, cast type or node that is not listed is rejected.
sqlglot parses well-known functions into typed classes (``SUM`` -> ``exp.Sum``) and everything else into
``exp.Anonymous``; both are checked. Class names are resolved defensively so a sqlglot upgrade that renames a
class can only make the guard stricter, never looser.
"""

from __future__ import annotations

from sqlglot import exp


def _classes(*names: str) -> frozenset[type]:
    found = []
    for name in names:
        cls = getattr(exp, name, None)
        if isinstance(cls, type) and issubclass(cls, exp.Expression):
            found.append(cls)
    return frozenset(found)


# --- functions -----------------------------------------------------------------------------------------------
ALLOWED_FUNCTION_CLASSES: frozenset[type] = _classes(
    # aggregates
    "Sum", "Avg", "Min", "Max", "Count", "Stddev", "StddevPop", "StddevSamp", "Variance", "VariancePop",
    "PercentileCont", "PercentileDisc", "Median", "Mode", "GroupConcat", "ArrayAgg", "LogicalAnd", "LogicalOr",
    "Corr", "CovarPop", "CovarSamp",
    # window functions
    "RowNumber", "Rank", "DenseRank", "Lag", "Lead", "Ntile", "FirstValue", "LastValue", "NthValue",
    "PercentRank", "CumeDist",
    # dates and times
    "TimestampTrunc", "DateTrunc", "Extract", "CurrentDate", "CurrentTimestamp", "CurrentTime", "TimeToStr",
    "StrToDate", "StrToTime", "Date", "DateFromParts", "JustifyDays", "JustifyHours", "JustifyInterval",
    # conditionals
    "Coalesce", "Nullif", "Greatest", "Least", "If", "Case",
    # strings
    "Lower", "Upper", "Trim", "Length", "Substring", "StrPosition", "Replace", "Concat", "ConcatWs", "Left",
    "Right", "SplitPart", "Initcap", "Reverse",
    # math
    "Round", "Floor", "Ceil", "Abs", "Pow", "Sqrt", "Ln", "Log", "Mod", "Exp", "Sign", "Trunc", "Cbrt",
    # casts (target types are checked separately)
    "Cast",
    # operators that sqlglot models as Func subclasses (boolean connectives, EXISTS, COLLATE, regex, json and
    # array operators on allowed columns: x -> 'a', x ->> 'a', x #>> '{a,b}', a @> b, a ~ 'x')
    "And", "Or", "Exists", "Collate", "RegexpLike", "RegexpILike", "RegexpFullMatch",
    "JSONExtract", "JSONExtractScalar", "JSONBExtract", "JSONBExtractScalar", "JSONArrayContains",
    "JSONBContains", "JSONBContainsAllTopKeys", "JSONBContainsAnyTopKeys", "JSONBContainsTopKey",
    "JSONBDeleteAtPath", "ArrayContainedBy", "ArrayContains", "ArrayContainsAll", "ArrayOverlaps",
)

# Names accepted when sqlglot leaves a call as exp.Anonymous (lower-case, unqualified).
ALLOWED_FUNCTION_NAMES: frozenset[str] = frozenset({
    "sum", "avg", "min", "max", "count", "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop", "var_samp",
    "percentile_cont", "percentile_disc", "mode", "string_agg", "array_agg", "bool_and", "bool_or", "every",
    "corr", "covar_pop", "covar_samp",
    "row_number", "rank", "dense_rank", "lag", "lead", "ntile", "first_value", "last_value", "nth_value",
    "percent_rank", "cume_dist",
    "date_trunc", "date_part", "extract", "now", "current_date", "age", "make_date", "make_timestamp",
    "to_char", "to_date", "to_timestamp", "date", "justify_days", "justify_hours", "justify_interval",
    "coalesce", "nullif", "greatest", "least",
    "lower", "upper", "trim", "btrim", "ltrim", "rtrim", "length", "char_length", "character_length",
    "substring", "substr", "position", "strpos", "replace", "concat", "concat_ws", "left", "right", "split_part",
    "initcap", "reverse",
    "round", "floor", "ceil", "ceiling", "abs", "power", "pow", "sqrt", "ln", "log", "log10", "mod", "exp",
    "sign", "trunc", "cbrt", "div",
})

# Never allowed, even if a future sqlglot maps them to an allowed class (belt and braces; the allowlist is
# the real control). Prefix matches cover families such as pg_*, lo_*, dblink*.
FORBIDDEN_FUNCTION_PREFIXES: tuple[str, ...] = (
    "pg_", "lo_", "dblink", "query_to_xml", "table_to_xml", "cursor_to_xml", "database_to_xml", "schema_to_xml",
    "txid_", "set_config", "current_setting", "version", "inet_", "generate_series", "repeat", "lpad", "rpad",
    "encode", "decode", "convert", "sha", "md5", "crypt", "gen_random", "format", "has_", "row_to_json",
    "to_json", "to_jsonb", "json_", "jsonb_", "xml", "unnest", "string_to_table", "regexp_", "file_", "copy",
)

# Functions only the rewriter may insert (mask expressions). Users can never call them.
REWRITER_ONLY_FUNCTIONS = frozenset({"md5", "repeat"})

# Set-returning / table functions are forbidden in FROM / LATERAL regardless of the allowlist.
SET_RETURNING_CLASSES: frozenset[type] = _classes(
    "Explode", "ExplodingGenerateSeries", "GenerateSeries", "Unnest", "Posexplode", "ExplodeOuter",
)

# --- casts ---------------------------------------------------------------------------------------------------
_DT = exp.DataType.Type
ALLOWED_CAST_TYPES: frozenset = frozenset(
    t for t in (
        getattr(_DT, name, None)
        for name in (
            "TEXT", "VARCHAR", "CHAR", "BPCHAR", "NCHAR", "NVARCHAR", "BIGINT", "INT", "SMALLINT", "TINYINT",
            "DECIMAL", "DOUBLE", "FLOAT", "BOOLEAN", "DATE", "TIMESTAMP", "TIMESTAMPTZ", "TIME", "TIMETZ",
            "INTERVAL", "NUMERIC", "BIGDECIMAL",
        )
    )
    if t is not None
)

# --- columns / objects ---------------------------------------------------------------------------------------
SYSTEM_COLUMNS: frozenset[str] = frozenset({"ctid", "xmin", "xmax", "cmin", "cmax", "tableoid", "oid"})
SYSTEM_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema", "pg_toast", "pg_temp"})


def is_system_name(name: str) -> bool:
    n = (name or "").lower()
    return n.startswith("pg_") or n in SYSTEM_SCHEMAS or n.startswith("information_schema")


# --- statements / clauses ------------------------------------------------------------------------------------
# Any of these anywhere in the tree means the statement is not a plain read (SELECT INTO, data-modifying CTEs,
# DDL, utility commands): reject as not_select.
NOT_SELECT_NODES: frozenset[type] = _classes(
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterColumn", "Command", "Copy",
    "TruncateTable", "Transaction", "Commit", "Rollback", "Set", "SetItem", "Use", "Into", "Grant", "Revoke",
    "Describe", "Show", "LoadData", "Cache", "Uncache", "Pragma", "Analyze", "Kill", "Refresh", "Comment",
    "Summarize", "Declare", "DeclareItem", "Execute", "Prepare", "Returning",
)
# Clauses that are valid in a SELECT but forbidden by policy.
FORBIDDEN_CLAUSE_NODES: frozenset[type] = _classes(
    "Lock", "Placeholder", "Parameter", "SessionParameter", "RawString", "TableSample", "Pivot", "Unpivot",
    "MatchRecognize", "Connect", "Prior", "Hint", "Lambda", "Changes", "HistoricalData", "Version",
)
ROOT_TYPES: tuple[type, ...] = (exp.Select, exp.Union, exp.Intersect, exp.Except)
