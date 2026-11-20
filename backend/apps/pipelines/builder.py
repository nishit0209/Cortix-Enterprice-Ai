"""Pipeline-builder dialect -> the engine's canonical node types and configs (additive, W3).

The frontend builder (`frontend/src/lib/pipelines/catalog.ts`) names nodes `source` / `destination` (and accepts the
DATA_ENGINEERING_DESIGN §5.2 dotted names such as `transform.filter`), and its inspector forms use their own config
keys (`casts`, `keys`, `mode`, `fn`/`as`, `desc`, `dataset_name`, text expressions, ...). `normalize_node` maps them
onto the canonical form validated by `dag.py`. Canonical DAGs pass through unchanged, and a builder key is only
translated when the canonical key is absent, so an explicit canonical value always wins. The stored DAG keeps the
builder's form (it round-trips to the canvas); normalization runs on every validation.
"""

from __future__ import annotations

from typing import Any

from .expressions import ExpressionError, parse_text

TYPE_ALIASES = {
    "source": "source.dataset", "source_dataset": "source.dataset", "dataset_source": "source.dataset",
    "source_connector": "source.connector", "connector_source": "source.connector",
    "destination": "destination.dataset", "destination_dataset": "destination.dataset",
    "dataset_destination": "destination.dataset",
    "transform.select": "select_columns", "select": "select_columns", "transform.rename": "rename",
    "transform.cast": "cast", "transform.filter": "filter", "transform.derive": "derive", "transform.sort": "sort",
    "transform.aggregate": "aggregate", "transform.join": "join", "clean.trim_whitespace": "trim_whitespace",
    "clean.case": "change_case", "clean.fill_null": "fill_null", "clean.drop_null_rows": "drop_nulls",
    "drop_null_rows": "drop_nulls", "dedup.exact": "deduplicate", "dedup": "deduplicate",
}
FILTER_OPS = {"=": "eq", "==": "eq", "!=": "ne", "<>": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}
AGG_FUNCS = {"avg": "mean", "average": "mean", "distinct_count": "count_distinct"}
FILL_STRATEGIES = {"constant": "value", "forward_fill": "forward", "backward_fill": "backward"}
NO_VALUE_OPS = ("is_null", "not_null")
_BLANK = ("", None)

Problem = tuple[str, str, str]  # (field, code, message)


def canonical_type(node_type: str) -> str:
    return TYPE_ALIASES.get(node_type, node_type)


def _rename(cfg: dict[str, Any], old: str, new: str) -> None:
    if old in cfg and new not in cfg:
        cfg[new] = cfg.pop(old)


def _cast(cfg: dict[str, Any], problems: list[Problem]) -> None:
    casts = cfg.pop("casts", None) if "columns" not in cfg else None
    if casts is None:
        return
    if not isinstance(casts, list) or not all(isinstance(r, dict) for r in casts):
        problems.append(("config.casts", "invalid", "Casts must be a list of {column, to}."))
        return
    cfg["columns"] = {str(r.get("column") or ""): r.get("to") for r in casts}


def _filter(cfg: dict[str, Any]) -> None:
    _rename(cfg, "mode", "match")
    conditions = cfg.get("conditions")
    if isinstance(conditions, list):
        cfg["conditions"] = [_condition(c) if isinstance(c, dict) else c for c in conditions]


def _condition(c: dict[str, Any]) -> dict[str, Any]:
    c = {**c, "op": FILTER_OPS.get(c.get("op"), c.get("op"))}
    # the inspector resets `value` to "" whenever the operator changes, also to is_null / not_null (no value)
    if c["op"] in NO_VALUE_OPS and "value" in c and c["value"] in _BLANK:
        c.pop("value")
    return c


def _sort(cfg: dict[str, Any]) -> None:
    keys = cfg.get("by")
    if not isinstance(keys, list):
        return
    out = []
    for k in keys:
        if isinstance(k, dict) and "descending" not in k and ("desc" in k or "direction" in k):
            k = dict(k)
            flag = k.pop("desc", None)
            direction = k.pop("direction", None)
            k["descending"] = bool(flag) if flag is not None else str(direction).lower() == "desc"
        out.append(k)
    cfg["by"] = out


def _aggregate(cfg: dict[str, Any]) -> None:
    aggs = cfg.get("aggregations")
    if not isinstance(aggs, list):
        return
    out = []
    for a in aggs:
        if isinstance(a, dict) and "func" not in a and "fn" in a:
            a = dict(a)
            fn = a.pop("fn")
            a["func"] = AGG_FUNCS.get(fn, fn)
            alias = a.pop("as", None)
            if alias and "alias" not in a:
                a["alias"] = alias
        if isinstance(a, dict) and "column" in a and a["column"] in ("*", "", None):
            # the inspector offers "*" (all rows) for count(*); a missing column stays an error for other functions
            a = {**a, "column": None}
        out.append(a)
    cfg["aggregations"] = out


def _fill_null(cfg: dict[str, Any]) -> None:
    if cfg.get("strategy") in FILL_STRATEGIES:
        cfg["strategy"] = FILL_STRATEGIES[cfg["strategy"]]
    # the inspector only hides `value` for statistic / fill-forward strategies; it keeps the default ""
    if cfg.get("strategy", "value") != "value" and "value" in cfg and cfg["value"] in _BLANK:
        cfg.pop("value")


def _derive(cfg: dict[str, Any], problems: list[Problem]) -> None:
    text = cfg.get("expression")
    if not isinstance(text, str):
        return
    try:
        cfg["expression"] = parse_text(text)
    except ExpressionError as exc:
        problems.append(("config.expression", exc.code, str(exc)))


def _destination(cfg: dict[str, Any], problems: list[Problem]) -> None:
    _rename(cfg, "dataset_name", "name")
    mode = cfg.pop("mode", None)
    if mode not in (None, "", "new_version"):
        problems.append(("config.mode", "unsupported_mode",
                         "Append mode is not supported: every run writes a new version of the output dataset."))


def normalize_node(node_type: str, config: Any) -> tuple[str, Any, list[Problem]]:
    """-> (canonical type, canonical config, problems). Unknown types and non-dict configs are left as they are."""
    canonical = canonical_type(node_type)
    if not isinstance(config, dict):
        return canonical, config, []
    cfg = dict(config)
    problems: list[Problem] = []
    if canonical == "cast":
        _cast(cfg, problems)
    elif canonical == "filter":
        _filter(cfg)
    elif canonical == "fill_null":
        _fill_null(cfg)
    elif canonical == "deduplicate":
        _rename(cfg, "keys", "subset")
        if cfg.get("subset") == []:
            cfg["subset"] = None
    elif canonical == "sort":
        _sort(cfg)
    elif canonical == "aggregate":
        _aggregate(cfg)
    elif canonical == "derive":
        _derive(cfg, problems)
    elif canonical == "destination.dataset":
        _destination(cfg, problems)
    return canonical, cfg, problems
