"""DAG validation: structure, node configs, graph shape and static schema inference (pure, no DB)."""

import uuid

from apps.pipelines import types as T
from apps.pipelines.dag import Resolvers, to_validation_error, validate_dag

DS = str(uuid.uuid4())
OTHER = str(uuid.uuid4())
SRC = str(uuid.uuid4())


def node(id_, type_, **config):
    return {"id": id_, "type": type_, "config": config}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"from": a["id"], "to": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def linear(*steps, dest=None):
    return chain(node("src", "source.dataset", dataset_id=DS), *steps, dest or node("dst", "destination.dataset", name="Out"))


SCHEMAS = {DS: {"id": T.BIGINT, "city": T.TEXT, "amount": T.DOUBLE}, OTHER: {"city": T.TEXT, "state": T.TEXT}}
RESOLVERS = Resolvers(dataset_schema=lambda i: SCHEMAS.get(str(i)), check_dataset=lambda i: None
                      if str(i) in SCHEMAS else "dataset_not_found")


def codes(result):
    return {(i.node, i.code) for i in result.issues}


def test_valid_linear_pipeline_with_schema_inference():
    dag = linear(node("t", "trim_whitespace"), node("r", "rename", mapping={"city": "town"}),
                 node("j", "join", dataset_id=OTHER, on=[{"left": "town", "right": "city"}]),
                 node("d", "derive", name="amount2", expression={"op": "mul", "args": [{"col": "amount"}, {"lit": 2}]}))
    result = validate_dag(dag, RESOLVERS)
    assert result.ok, result.issues
    assert [n.id for n in result.order] == ["src", "t", "r", "j", "d", "dst"]
    assert result.schemas["dst"] == {"id": T.BIGINT, "town": T.TEXT, "amount": T.DOUBLE, "state": T.TEXT,
                                     "amount2": T.DOUBLE}
    payload = result.payload()
    assert payload["valid"] and payload["order"][0] == "src"


def test_edges_may_be_pairs_and_nodes_unordered():
    dag = {"nodes": [node("dst", "destination.dataset", name="Out"), node("src", "source.dataset", dataset_id=DS)],
           "edges": [["src", "dst"]]}
    assert validate_dag(dag).ok


def test_structure_errors():
    assert not validate_dag({"nodes": []}).ok
    assert not validate_dag({"nodes": [node("a", "source.dataset", dataset_id=DS)], "extra": 1}).ok
    bad_id = validate_dag(chain(node("bad id!", "source.dataset", dataset_id=DS), node("dst", "destination.dataset",
                                                                                         name="x")))
    assert not bad_id.ok


def test_unknown_node_type_and_bad_config_reported_per_node():
    dag = linear(node("x", "python_udf", code="import os"), node("f", "filter", conditions=[{"column": "a"}]))
    result = validate_dag(dag)
    got = codes(result)
    assert ("x", "unknown_node_type") in got
    assert any(n == "f" for n, _ in got)


def test_source_and_destination_counts():
    two_sources = {"nodes": [node("a", "source.dataset", dataset_id=DS), node("b", "source.dataset", dataset_id=DS),
                             node("dst", "destination.dataset", name="x")], "edges": [["a", "dst"], ["b", "dst"]]}
    got = codes(validate_dag(two_sources))
    assert (None, "source_count") in got and ("dst", "fan_in") in got
    no_dest = chain(node("a", "source.dataset", dataset_id=DS), node("t", "trim_whitespace"))
    assert (None, "destination_count") in codes(validate_dag(no_dest))


def test_cycle_detected():
    dag = {"nodes": [node("src", "source.dataset", dataset_id=DS), node("a", "trim_whitespace"),
                     node("b", "trim_whitespace"), node("dst", "destination.dataset", name="x")],
           "edges": [["src", "a"], ["a", "b"], ["b", "a"]]}
    got = codes(validate_dag(dag))
    assert (None, "cycle") in got or ("a", "fan_in") in got


def test_pure_cycle_without_fan_in():
    dag = {"nodes": [node("src", "source.dataset", dataset_id=DS), node("a", "trim_whitespace"),
                     node("b", "trim_whitespace"), node("dst", "destination.dataset", name="x")],
           "edges": [["src", "dst"], ["a", "b"], ["b", "a"]]}
    assert (None, "cycle") in codes(validate_dag(dag))


def test_disconnected_and_fan_out():
    disconnected = {"nodes": [node("src", "source.dataset", dataset_id=DS), node("lost", "trim_whitespace"),
                              node("dst", "destination.dataset", name="x")], "edges": [["src", "dst"]]}
    assert ("lost", "disconnected") in codes(validate_dag(disconnected))
    fan_out = {"nodes": [node("src", "source.dataset", dataset_id=DS), node("a", "trim_whitespace"),
                         node("dst", "destination.dataset", name="x")], "edges": [["src", "a"], ["src", "dst"]]}
    assert ("src", "fan_out") in codes(validate_dag(fan_out))


def test_edge_problems():
    dag = linear()
    dag["edges"] += [{"from": "src", "to": "ghost"}, {"from": "src", "to": "src"}, {"from": "src", "to": "dst"}]
    got = {i.code for i in validate_dag(dag).issues}
    assert {"unknown_node", "self_loop", "duplicate_edge"} <= got


def test_destination_needs_exactly_one_target():
    both = linear(dest=node("dst", "destination.dataset", name="x", dataset_id=DS))
    assert not validate_dag(both).ok
    neither = linear(dest=node("dst", "destination.dataset"))
    assert not validate_dag(neither).ok
    attested = linear(dest=node("dst", "destination.dataset", name="x", classification="synthetic"))
    assert not validate_dag(attested).ok  # output classification can never self-attest synthetic/public


def test_column_references_checked_against_inferred_schema():
    dag = linear(node("r", "rename", mapping={"city": "town"}), node("s", "select_columns", columns=["city"]))
    result = validate_dag(dag, RESOLVERS)
    assert ("s", "unknown_column") in codes(result)
    issue = next(i for i in result.issues if i.node == "s")
    assert issue.field == "config.columns"


def test_unknown_dataset_reported_on_source_and_join():
    missing = str(uuid.uuid4())
    dag = chain(node("src", "source.dataset", dataset_id=missing), node("dst", "destination.dataset", name="x"))
    assert ("src", "dataset_not_found") in codes(validate_dag(dag, RESOLVERS))
    join = linear(node("j", "join", dataset_id=missing, on=[{"left": "city", "right": "city"}]))
    assert ("j", "dataset_not_found") in codes(validate_dag(join, RESOLVERS))


def test_access_denied_is_reported():
    denied = Resolvers(dataset_schema=lambda i: SCHEMAS.get(str(i)), check_dataset=lambda i: "dataset_access_denied")
    assert ("src", "dataset_access_denied") in codes(validate_dag(linear(), denied))


def test_connector_source_schema():
    dag = chain(node("src", "source.connector", data_source_id=SRC, table="orders", schema="sales",
                     columns=["id", "total"]),
                node("c", "cast", columns={"total": "float"}),
                node("f", "filter", conditions=[{"column": "total", "op": "gt", "value": 10}]),
                node("dst", "destination.dataset", name="x"))
    resolvers = Resolvers(connector_schema=lambda s, sch, t: {"id": T.TEXT, "total": T.TEXT, "note": T.TEXT},
                          check_source=lambda s: None)
    result = validate_dag(dag, resolvers)
    assert result.ok, result.issues
    assert result.schemas["dst"] == {"id": T.TEXT, "total": T.DOUBLE}
    unverified = Resolvers(check_source=lambda s: "data_source_not_verified")
    assert ("src", "data_source_not_verified") in codes(validate_dag(dag, unverified))


def test_unknown_source_schema_skips_column_checks():
    dag = linear(node("s", "select_columns", columns=["anything"]))
    assert validate_dag(dag).ok  # no resolvers: structure + config only


def test_validation_error_shape():
    dag = linear(node("s", "select_columns", columns=["nope"]))
    result = validate_dag(dag, RESOLVERS)
    err = to_validation_error(result.issues)
    assert "nodes" in err.detail["dag"] and "s" in err.detail["dag"]["nodes"]
    detail = err.detail["dag"]["nodes"]["s"]["config.columns"][0]
    assert detail.code == "unknown_column"
