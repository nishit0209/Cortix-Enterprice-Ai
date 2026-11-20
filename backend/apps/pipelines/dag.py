"""Pipeline DAG model and validation (pydantic + graph checks + static schema inference).

    {
      "nodes": [
        {"id": "src", "type": "source.dataset", "config": {"dataset_id": "<uuid>"}},
        {"id": "clean", "type": "trim_whitespace", "config": {}},
        {"id": "dst", "type": "destination.dataset", "config": {"name": "Clean customers"}}
      ],
      "edges": [{"from": "src", "to": "clean"}, {"from": "clean", "to": "dst"}]     # or [["src", "clean"], ...]
    }

MVP shape: a LINEAR chain source -> steps -> destination (exactly one source and one destination, every node
connected, acyclic, at most one incoming and one outgoing edge per node). `join` brings in a second dataset
through its config. Every problem is reported per node and field so the builder can highlight it.

Builder dialect (W3, additive): node types/configs written by the pipeline builder (`source`, `casts`, `keys`, text
expressions, ...) are first mapped onto the canonical form by `builder.normalize_node`.

Builder join (W3, additive): the pipeline builder draws a join's right side as a second `source.dataset` node wired
to the join's right handle (edges may carry `port: "left" | "right"`). Such a source - one that has no inputs and
feeds only that join - is folded into the join's `config.dataset_id` (+ `columns`) before validation, so the chain
stays linear. `DagValidation.folded` maps the folded source to its join. The stored DAG keeps the builder's form.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic import ValidationError as PydanticValidationError
from rest_framework.exceptions import ErrorDetail, ValidationError

from . import steps as S
from . import types as T

MAX_NODES = 50
NodeId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,40}$")]
SOURCE_TYPES = ("source.dataset", "source.connector")
DESTINATION_TYPES = ("destination.dataset",)


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetSourceCfg(_Cfg):
    dataset_id: UUID
    columns: list[S.ColName] | None = Field(default=None, min_length=1, max_length=1000)


class ConnectorSourceCfg(_Cfg):
    data_source_id: UUID
    table: Annotated[str, StringConstraints(min_length=1, max_length=63)]
    schema_name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")] | None = Field(
        default=None, alias="schema")
    columns: list[S.ColName] | None = Field(default=None, min_length=1, max_length=1000)
    max_rows: int | None = Field(default=None, ge=1, le=50_000_000)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DatasetDestinationCfg(_Cfg):
    name: Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)] | None = None
    dataset_id: UUID | None = None
    classification: Literal["internal", "confidential"] = "internal"
    description: Annotated[str, StringConstraints(max_length=2000)] = ""

    @model_validator(mode="after")
    def _one_target(self):
        if (self.name is None) == (self.dataset_id is None):
            raise ValueError("Set exactly one of name (new dataset) or dataset_id (new version of a dataset).")
        return self


NODE_CONFIGS: dict[str, type[BaseModel]] = {
    "source.dataset": DatasetSourceCfg,
    "source.connector": ConnectorSourceCfg,
    "destination.dataset": DatasetDestinationCfg,
    **{t: step.config_model for t, step in S.STEPS.items()},
}
NODE_TYPES = tuple(NODE_CONFIGS)


class NodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NodeId
    type: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    config: dict[str, Any] = Field(default_factory=dict)
    label: Annotated[str, StringConstraints(max_length=120)] | None = None
    position: dict[str, float] | None = None     # builder canvas coordinates (ignored by the engine)


class EdgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: NodeId = Field(alias="from")
    to: NodeId
    port: Literal["left", "right"] | None = None   # builder join handle (see the module docstring)


class DagIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[NodeIn] = Field(min_length=1, max_length=MAX_NODES)
    edges: list[EdgeIn] = Field(default_factory=list, max_length=MAX_NODES * 2)
    settings: dict[str, Any] | None = None         # builder settings, round-tripped and ignored by the engine

    @model_validator(mode="before")
    @classmethod
    def _edge_pairs(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("edges"), list):
            data = dict(data)
            data["edges"] = [
                {"from": e[0], "to": e[1]} if isinstance(e, (list, tuple)) and len(e) == 2 else e
                for e in data["edges"]
            ]
        return data


@dataclass(frozen=True)
class DagIssue:
    node: str | None
    field: str
    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        """`node_id` names the node (None for DAG-level issues); `field` is the full path `nodes.<id>.<field>` so
        clients can pin the message on the node; `node` is kept for W2 clients."""
        path = f"nodes.{self.node}.{self.field}" if self.node and self.field else (
            f"nodes.{self.node}" if self.node else self.field)
        return {"node_id": self.node, "node": self.node, "field": path, "code": self.code, "message": self.message}


@dataclass
class Node:
    id: str
    type: str
    config: Any               # parsed pydantic config
    raw: dict[str, Any]

    @property
    def is_source(self) -> bool:
        return self.type in SOURCE_TYPES

    @property
    def is_destination(self) -> bool:
        return self.type in DESTINATION_TYPES


@dataclass
class Resolvers:
    """Lookups used for semantic checks; each returns None when unknown (checks are then skipped)."""

    dataset_schema: Callable[[UUID], T.Schema | None] | None = None
    connector_schema: Callable[[UUID, str | None, str], T.Schema | None] | None = None
    check_dataset: Callable[[UUID], str | None] | None = None      # -> error code or None
    check_source: Callable[[UUID], str | None] | None = None


@dataclass
class DagValidation:
    issues: list[DagIssue] = field(default_factory=list)
    order: list[Node] = field(default_factory=list)            # source -> ... -> destination
    schemas: dict[str, T.Schema | None] = field(default_factory=dict)
    folded: dict[str, str] = field(default_factory=dict)       # builder right-input source id -> its join id

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def source(self) -> Node | None:
        return self.order[0] if self.order and self.order[0].is_source else None

    @property
    def destination(self) -> Node | None:
        return self.order[-1] if self.order and self.order[-1].is_destination else None

    @property
    def steps(self) -> list[Node]:
        return [n for n in self.order if not n.is_source and not n.is_destination]

    def payload(self) -> dict[str, Any]:
        return {
            "valid": self.ok,
            "errors": [i.as_dict() for i in self.issues],
            "order": [n.id for n in self.order],
            "schemas": {k: (None if v is None else [{"name": c, "type": t} for c, t in v.items()])
                        for k, v in self.schemas.items()},
        }


def _pyd_issues(exc: PydanticValidationError, node: str | None, prefix: str) -> list[DagIssue]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = str(err.get("msg", "Invalid value.")).removeprefix("Value error, ")
        out.append(DagIssue(node, f"{prefix}{'.' if prefix and loc else ''}{loc}", err.get("type", "invalid"), msg))
    return out


def parse_dag(raw: Any) -> tuple[DagIn | None, list[DagIssue]]:
    try:
        return DagIn.model_validate(raw), []
    except PydanticValidationError as exc:
        return None, _pyd_issues(exc, None, "")


def _normalize_builder_nodes(dag: DagIn, result: DagValidation) -> tuple[DagIn, set[tuple[str, str]]]:
    """Map builder node types/configs onto the canonical form (`builder.py`); problems are pinned on the node.

    Returns the DAG and the (node id, field) pairs already reported, so config validation does not repeat them."""
    from .builder import normalize_node

    nodes = []
    reported: set[tuple[str, str]] = set()
    changed = False
    for n in dag.nodes:
        node_type, config, problems = normalize_node(n.type, n.config)
        for fld, code, message in problems:
            result.issues.append(DagIssue(n.id, fld, code, message))
            reported.add((n.id, fld))
        if node_type != n.type or config != n.config:
            changed = True
            n = n.model_copy(update={"type": node_type, "config": config})
        nodes.append(n)
    return (dag.model_copy(update={"nodes": nodes}) if changed else dag), reported


def fold_join_inputs(dag: DagIn) -> tuple[DagIn, dict[str, str], list[DagIssue]]:
    """Fold builder-style join right inputs into `join.config.dataset_id` (see the module docstring).

    Returns (the folded DAG, {folded source id: join id}, issues). A join with two inputs whose right side is not a
    dataset source used only by that join is reported on the join; other shapes are left to the graph checks.
    """
    by_id = {n.id: n for n in dag.nodes}
    incoming: dict[str, list[EdgeIn]] = {}
    outgoing: dict[str, list[EdgeIn]] = {}
    for e in dag.edges:
        incoming.setdefault(e.to, []).append(e)
        outgoing.setdefault(e.from_, []).append(e)
    configs: dict[str, dict[str, Any]] = {}
    folded: dict[str, str] = {}
    dropped_edges: list[EdgeIn] = []
    issues: list[DagIssue] = []
    for node in dag.nodes:
        ins = incoming.get(node.id, [])
        if node.type != "join" or len(ins) != 2:
            continue
        left = [e for e in ins if e.port == "left"]
        right = [e for e in ins if e.port == "right"] or [e for e in ins if e.port != "left"]
        if len(left) != 1 or len(right) != 1 or left[0] is right[0]:
            issues.append(DagIssue(node.id, "edges", "join_inputs",
                                   "Connect the main input to the join's left handle and the right dataset to its "
                                   "right handle."))
            continue
        source = by_id.get(right[0].from_)
        if (source is None or source.type != "source.dataset" or incoming.get(source.id)
                or len(outgoing.get(source.id, [])) != 1 or source.id in folded):
            issues.append(DagIssue(node.id, "edges", "join_right_input",
                                   "The right input of a join must be a dataset source that feeds only this join."))
            continue
        cfg = dict(node.config)
        src_cfg = source.config or {}
        if cfg.get("dataset_id") not in (None, "", src_cfg.get("dataset_id")):
            issues.append(DagIssue(node.id, "config.dataset_id", "join_right_conflict",
                                   "The join has both a right input and a different config.dataset_id."))
            continue
        cfg["dataset_id"] = src_cfg.get("dataset_id")
        if src_cfg.get("columns") and not cfg.get("columns"):
            cfg["columns"] = src_cfg["columns"]
        configs[node.id] = cfg
        folded[source.id] = node.id
        dropped_edges.append(right[0])
    if not folded:
        return dag, folded, issues
    nodes = [n.model_copy(update={"config": configs[n.id]}) if n.id in configs else n
             for n in dag.nodes if n.id not in folded]
    edges = [e for e in dag.edges if not any(e is d for d in dropped_edges)]
    return dag.model_copy(update={"nodes": nodes, "edges": edges}), folded, issues


def validate_dag(raw: Any, resolvers: Resolvers | None = None) -> DagValidation:
    """Full validation: structure, node configs, graph shape and (when resolvers are given) column/type checks."""
    result = DagValidation()
    dag, issues = parse_dag(raw)
    if dag is None:
        result.issues.extend(issues)
        return result
    dag, reported = _normalize_builder_nodes(dag, result)
    dag, result.folded, fold_issues = fold_join_inputs(dag)
    result.issues.extend(fold_issues)
    resolvers = resolvers or Resolvers()
    nodes: dict[str, Node] = {}
    for n in dag.nodes:
        if n.id in nodes:
            result.issues.append(DagIssue(n.id, "id", "duplicate_node", f"Node id {n.id!r} is used twice."))
            continue
        model = NODE_CONFIGS.get(n.type)
        if model is None:
            result.issues.append(DagIssue(n.id, "type", "unknown_node_type",
                                          f"Unknown node type {n.type!r}. Allowed: {', '.join(NODE_TYPES)}."))
            nodes[n.id] = Node(n.id, n.type, None, n.model_dump())
            continue
        try:
            cfg = model.model_validate(n.config)
        except PydanticValidationError as exc:
            result.issues.extend(i for i in _pyd_issues(exc, n.id, "config") if (n.id, i.field) not in reported)
            cfg = None
        nodes[n.id] = Node(n.id, n.type, cfg, n.model_dump(by_alias=True))

    # --- graph shape -----------------------------------------------------------------------------
    succ: dict[str, list[str]] = {k: [] for k in nodes}
    pred: dict[str, list[str]] = {k: [] for k in nodes}
    seen_edges: set[tuple[str, str]] = set()
    for i, e in enumerate(dag.edges):
        if e.from_ not in nodes or e.to not in nodes:
            result.issues.append(DagIssue(None, f"edges.{i}", "unknown_node", "The edge references an unknown node."))
            continue
        if e.from_ == e.to:
            result.issues.append(DagIssue(e.from_, f"edges.{i}", "self_loop", "A node cannot feed itself."))
            continue
        if (e.from_, e.to) in seen_edges:
            result.issues.append(DagIssue(None, f"edges.{i}", "duplicate_edge", "The edge is listed twice."))
            continue
        seen_edges.add((e.from_, e.to))
        succ[e.from_].append(e.to)
        pred[e.to].append(e.from_)

    sources = [n for n in nodes.values() if n.is_source]
    dests = [n for n in nodes.values() if n.is_destination]
    if len(sources) != 1:
        result.issues.append(DagIssue(None, "graph", "source_count",
                                      f"A pipeline needs exactly one source node (found {len(sources)})."))
    if len(dests) != 1:
        result.issues.append(DagIssue(None, "graph", "destination_count",
                                      f"A pipeline needs exactly one destination node (found {len(dests)})."))
    joins_reported = {i.node for i in fold_issues}
    for n in nodes.values():
        if len(pred[n.id]) > 1 and n.id not in joins_reported:
            result.issues.append(DagIssue(n.id, "edges", "fan_in", "Steps take one input; use a join step instead."))
        if len(succ[n.id]) > 1:
            result.issues.append(DagIssue(n.id, "edges", "fan_out", "Branching is not supported; chain the steps."))
        if n.is_source and pred[n.id]:
            result.issues.append(DagIssue(n.id, "edges", "source_has_input", "A source cannot have inputs."))
        if n.is_destination and succ[n.id]:
            result.issues.append(DagIssue(n.id, "edges", "destination_has_output", "A destination cannot feed steps."))
    if _has_cycle(nodes, succ):
        result.issues.append(DagIssue(None, "graph", "cycle", "The pipeline graph contains a cycle."))
    if result.issues:
        return result

    order = [sources[0]]
    while succ[order[-1].id]:
        order.append(nodes[succ[order[-1].id][0]])
    unreachable = [k for k in nodes if k not in {n.id for n in order}]
    for k in unreachable:
        result.issues.append(DagIssue(k, "edges", "disconnected", "The node is not connected to the source."))
    if order[-1].id != dests[0].id:
        result.issues.append(DagIssue(order[-1].id, "edges", "no_destination_path",
                                      "The chain from the source does not end at the destination."))
    result.order = order
    if result.issues:
        return result

    # --- semantic checks + schema inference ---------------------------------------------------------
    _infer(result, resolvers)
    return result


def _has_cycle(nodes: dict[str, Node], succ: dict[str, list[str]]) -> bool:
    state: dict[str, int] = {}

    def visit(k: str) -> bool:
        stack = [(k, iter(succ[k]))]
        state[k] = 1
        while stack:
            cur, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[cur] = 2
                stack.pop()
                continue
            if state.get(nxt) == 1:
                return True
            if state.get(nxt) is None:
                state[nxt] = 1
                stack.append((nxt, iter(succ[nxt])))
        return False

    return any(state.get(k) is None and visit(k) for k in nodes)


def _source_schema(node: Node, result: DagValidation, resolvers: Resolvers) -> T.Schema | None:
    cfg = node.config
    if node.type == "source.dataset":
        if resolvers.check_dataset:
            code = resolvers.check_dataset(cfg.dataset_id)
            if code:
                result.issues.append(DagIssue(node.id, "config.dataset_id", code, _DATASET_MSG.get(code, code)))
                return None
        schema = resolvers.dataset_schema(cfg.dataset_id) if resolvers.dataset_schema else None
        if schema is None and resolvers.dataset_schema is not None:
            result.issues.append(DagIssue(node.id, "config.dataset_id", "dataset_not_ready",
                                          "The dataset has no ready version yet."))
            return None
    else:
        if resolvers.check_source:
            code = resolvers.check_source(cfg.data_source_id)
            if code:
                result.issues.append(DagIssue(node.id, "config.data_source_id", code, _SOURCE_MSG.get(code, code)))
                return None
        schema = (resolvers.connector_schema(cfg.data_source_id, cfg.schema_name, cfg.table)
                  if resolvers.connector_schema else None)
    if schema is not None and cfg.columns:
        missing = [c for c in cfg.columns if c not in schema]
        if missing:
            result.issues.append(DagIssue(node.id, "config.columns", "unknown_column",
                                          f"Unknown column(s): {', '.join(missing)}."))
            return None
        schema = {c: schema[c] for c in cfg.columns}
    return schema


_DATASET_MSG = {
    "dataset_not_found": "The dataset does not exist.",
    "dataset_access_denied": "You do not have export access to this dataset.",
    "dataset_not_ready": "The dataset has no ready version yet.",
}
_SOURCE_MSG = {
    "data_source_not_found": "The data source does not exist.",
    "data_source_not_verified": "The data source has not passed its connection and read-only checks.",
}


def _infer(result: DagValidation, resolvers: Resolvers) -> None:
    source = result.order[0]
    if source.config is None:
        return
    schema = _source_schema(source, result, resolvers)
    result.schemas[source.id] = schema
    ctx = S.StepContext(dataset_schema=resolvers.dataset_schema)
    for node in result.order[1:]:
        if node.is_destination:
            cfg = node.config
            if cfg is not None and cfg.dataset_id is not None and resolvers.check_dataset:
                code = resolvers.check_dataset(cfg.dataset_id)
                if code and code != "dataset_not_ready":
                    result.issues.append(DagIssue(node.id, "config.dataset_id", code, _DATASET_MSG.get(code, code)))
            if schema is not None and not schema:
                result.issues.append(DagIssue(node.id, "", "no_columns", "The pipeline output has no columns."))
            result.schemas[node.id] = schema
            continue
        step = S.STEPS[node.type]
        if node.config is None:
            schema = None
            result.schemas[node.id] = None
            continue
        if node.type == "join" and resolvers.check_dataset:
            code = resolvers.check_dataset(node.config.dataset_id)
            if code:
                result.issues.append(DagIssue(node.id, "config.dataset_id", code, _DATASET_MSG.get(code, code)))
                schema = None
                result.schemas[node.id] = None
                continue
        if schema is None:
            result.schemas[node.id] = None
            continue
        try:
            schema = step.infer(schema, node.config, ctx)
        except S.StepConfigError as exc:
            result.issues.append(DagIssue(node.id, f"config.{exc.field}" if exc.field else "config", exc.code, str(exc)))
            schema = None
        result.schemas[node.id] = schema


def to_validation_error(issues: list[DagIssue]) -> ValidationError:
    """problem+json: `errors: [{field: "dag.nodes.<id>.config.<path>", code, message}]`."""
    tree: dict[str, Any] = {}
    for issue in issues:
        detail = ErrorDetail(issue.message, code=issue.code)
        if issue.node:
            node = tree.setdefault("nodes", {}).setdefault(issue.node, {})
            node.setdefault(issue.field or "node", []).append(detail)
        else:
            tree.setdefault(issue.field or "dag", []).append(detail)
    return ValidationError({"dag": tree})
