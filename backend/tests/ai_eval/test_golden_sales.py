"""Golden NL->SQL evaluation over sales.csv (testing.golden; questions in golden_sales.yaml). No DB, no network.

* builder questions: the deterministic builder (0 Gemini calls) must produce SQL whose RESULT matches the answer
  computed independently from the CSV. `known_gap` questions are strict xfails (a builder gap reported to AI/ML).
* LLM questions: evaluated by replaying a recorded Gemini cassette through apps.nl2sql.propose when one exists in
  $NEXORA_CASSETTES_DIR or tests/ai_eval/cassettes (`<id>.json` or `nl2sql/<id>.json`; format in testing.golden);
  skipped (with the reason) otherwise. Gemini is never called live.
* The builder's measured accuracy is reported (`-s` prints the table) and guarded by a floor.
"""

from __future__ import annotations

from collections import Counter

import pytest

from testing.golden import (
    Expected,
    builder_sql,
    cassette_for,
    compare,
    evaluate,
    expected_result,
    golden,
    report,
    run_sql,
    sales_rows,
    to_sqlite,
)

QUESTIONS = golden()["questions"]
# Measured on 2026-11-18 (W2): 43 of 48. Measured on main 2026-11-20 (W4, after the W3 builder fixes for g39/g45):
# 45 of 48 answered correctly by the builder alone (93.8%), precision when answered 100%; g46-g48 are declined.
# Raise the floor when the builder improves; never lower it without a reviewed reason.
BUILDER_ACCURACY_FLOOR = 45 / 48


# ------------------------------------------------------------------------------------------------ the set itself
def test_golden_set_is_well_formed():
    assert len(QUESTIONS) >= 40
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids))
    columns = {c["name"] for c in golden()["dataset"]["columns"]}
    kinds = Counter(q["check"]["type"] for q in QUESTIONS)
    assert set(kinds) == {"total", "group", "series", "compare"}, kinds
    for q in QUESTIONS:
        check = q["check"]
        assert isinstance(q["builder"], bool), q["id"]
        assert q["question"].strip(), q["id"]
        for key in ("measure", "by"):
            if key in check:
                assert check[key] in columns, (q["id"], check[key])
        for col in check.get("where") or {}:
            assert col in columns, (q["id"], col)
    assert sum(not q["builder"] for q in QUESTIONS) >= 3, "keep LLM-only questions for cassette evaluation"


def test_expected_answers_match_known_dataset_facts():
    rows = sales_rows()
    assert len(rows) == 5020
    by_id = {q["id"]: q for q in QUESTIONS}
    assert expected_result(by_id["g03"]["check"]).value == 5000  # 20 exact duplicate rows
    regions = expected_result(by_id["g16"]["check"]).mapping
    assert set(regions) == {"North", "South", "East", "West", "Central", None}
    total = expected_result(by_id["g01"]["check"]).value
    assert abs(sum(regions.values()) - total) < 0.01
    months = expected_result(by_id["g34"]["check"]).mapping
    assert len(months) == 24 and min(months) == "2024-12-01" and max(months) == "2026-11-01"
    compare_month = expected_result(by_id["g42"]["check"]).mapping
    assert set(compare_month) == {"current_period", "previous_period"}
    assert abs(compare_month["previous_period"] - expected_result(by_id["g10"]["check"]).value) < 0.01


# ------------------------------------------------------------------------------------------------ the runner
def test_sqlite_translation_matches_postgres_semantics():
    sql = to_sqlite(
        'SELECT DATE_TRUNC(\'week\', "order_date") AS "week", PERCENTILE_CONT(0.5) WITHIN GROUP '
        '(ORDER BY "revenue") AS m FROM "sales" WHERE "order_date" >= DATE \'2026-11-16\' GROUP BY 1'
    )
    lowered = sql.lower()
    assert "pg_date_trunc('week'" in lowered and "pg_percentile_cont(0.5" in lowered
    assert ">= '2026-11-16'" in sql  # DATE literal -> ISO text (the column holds ISO strings)
    columns, rows = run_sql(
        'SELECT DATE_TRUNC(\'week\', "order_date") AS "week", COUNT(*) AS n FROM "sales" '
        "WHERE \"order_date\" >= DATE '2026-11-16' GROUP BY DATE_TRUNC('week', \"order_date\")"
    )
    assert columns == ["week", "n"]
    assert [r[0] for r in rows] == ["2026-11-16"]  # 2026-11-16 is a Monday (Postgres weeks start on Monday)


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ('SELECT "region", SUM("quantity") AS v FROM "sales" GROUP BY "region"', "!="),
        (
            'SELECT "region", SUM("revenue") AS v FROM "sales" WHERE "region" IS NOT NULL GROUP BY "region"',
            "missing",
        ),
        ('SELECT "channel", SUM("revenue") AS v FROM "sales" GROUP BY "channel"', "keys differ"),
    ],
)
def test_compare_rejects_wrong_answers(sql, fragment):
    expected = expected_result({"type": "group", "agg": "sum", "measure": "revenue", "by": "region"})
    problems = compare(expected, *run_sql(sql))
    assert any(fragment in p for p in problems), problems


def test_compare_checks_ranking_order_and_scalars():
    expected = expected_result(
        {"type": "group", "agg": "sum", "measure": "revenue", "by": "region", "top": 3, "order": "desc"}
    )
    asc = 'SELECT "region", SUM("revenue") AS v FROM "sales" GROUP BY "region" ORDER BY v ASC LIMIT 3'
    assert any("keys differ" in p or "ranking" in p for p in compare(expected, *run_sql(asc)))
    assert compare(Expected("scalar", value=10.0), ["v"], [(10.004,)]) == []
    assert compare(Expected("scalar", value=10.0), ["v"], [(10.5,)]) != []
    assert compare(Expected("scalar", value=1.0), ["v"], [(1,), (2,)]) == ["expected one row, got 2"]


def test_cassette_replay_pipeline_runs_through_nl2sql_propose():
    """Self-test of the replay path with a hand-written response (NOT a recording): propose() post-processes it
    (parse, policy column check, LIMIT) and the runner executes and grades the resulting SQL."""
    from testing.golden import cassette_sql

    case = next(q for q in QUESTIONS if q["id"] == "g45")
    crafted = {
        "prompt_key": "nl2sql.generate",
        "response": {
            "sql": "SELECT category, SUM(revenue) AS revenue FROM sales WHERE region = 'North' GROUP BY category",
            "explanation": "Revenue in the North region by category.",
            "assumptions": [],
            "chart_hint": {"type": "bar", "x": "category", "y": ["revenue"]},
            "clarification": None,
        },
    }
    sql = cassette_sql(case["question"], crafted)
    assert sql and "limit" in sql.lower()
    assert compare(expected_result(case["check"]), *run_sql(sql)) == []


# ------------------------------------------------------------------------------------------------ cassette files
# The cassette-file tests use a question the deterministic builder DECLINES (g48: a value filter plus a top-N on
# units), so evaluate() really takes the cassette path. The W3 builder now answers g45 itself, which is why these
# tests no longer use it. If the builder later learns g48 too, `_cassette_case` forces the cassette path explicitly
# (the file-lookup and replay logic under test is independent of the builder).
_CASSETTE_ID = "g48"
_CRAFTED_G48 = {
    "format": "nexora.cassette.v1",
    "id": _CASSETTE_ID,
    "prompt_key": "nl2sql.generate",
    "response": {
        "sql": "SELECT product, SUM(quantity) AS units FROM sales WHERE region = 'West' "
        "GROUP BY product ORDER BY units DESC LIMIT 3",
        "explanation": "The three products with the most units sold in the West region.",
        "assumptions": [],
        "chart_hint": {"type": "bar", "x": "product", "y": ["units"]},
        "clarification": None,
    },
}


def _cassette_case(monkeypatch) -> dict:
    """The golden case the cassette tests replay; the builder is bypassed if it ever starts answering it."""
    import testing.golden as golden_mod

    case = next(q for q in QUESTIONS if q["id"] == _CASSETTE_ID)
    assert case["builder"] is False, "the cassette tests need an LLM-path question"
    if builder_sql(case["question"]) is not None:
        monkeypatch.setattr(golden_mod, "builder_sql", lambda _question: None)
    return case


def test_the_cassette_question_is_declined_by_the_builder_on_main():
    """Documents WHY g48 is the cassette question (measured W4: the builder declines g46-g48, answers g45)."""
    case = next(q for q in QUESTIONS if q["id"] == _CASSETTE_ID)
    assert builder_sql(case["question"]) is None


@pytest.mark.parametrize("layout", ["{id}.json", "nl2sql/{id}.json"])
def test_cassettes_dir_env_is_searched_first_in_both_layouts(tmp_path, monkeypatch, layout):
    import json

    from testing.golden import CASSETTES_ENV, cassette_dirs, cassette_path

    case = _cassette_case(monkeypatch)
    path = tmp_path / layout.format(id=_CASSETTE_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_CRAFTED_G48), encoding="utf-8")
    monkeypatch.setenv(CASSETTES_ENV, str(tmp_path))
    assert cassette_dirs()[0] == tmp_path
    assert cassette_path(_CASSETTE_ID) == path
    loaded = cassette_for(_CASSETTE_ID, case["question"])
    assert loaded["response"]["sql"].startswith("SELECT product")
    outcome = evaluate(case, use_cassettes=True)
    assert outcome.source == "cassette" and outcome.correct, outcome.problems
    monkeypatch.delenv(CASSETTES_ENV)
    assert tmp_path not in cassette_dirs()


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ("not json", "JSON"),
        ("[]", "JSON object"),
        ('{"format": "other.v2", "response": {}}', "format"),
        ('{"id": "g46", "response": {"sql": "SELECT 1"}}', "recorded for"),  # the file is g48's
        ('{"question": "a different question", "response": {"sql": "SELECT 1"}}', "stale"),
        ('{"prompt_key": "nl2sql.generate"}', "response"),
        ('{"prompt_key": 7, "response": {"sql": "SELECT 1"}}', "prompt_key"),
    ],
)
def test_a_bad_cassette_is_a_recording_defect_not_a_skip(tmp_path, monkeypatch, payload, fragment):
    from testing.golden import CASSETTES_ENV, CassetteError

    (tmp_path / f"{_CASSETTE_ID}.json").write_text(payload, encoding="utf-8")
    monkeypatch.setenv(CASSETTES_ENV, str(tmp_path))
    case = next(q for q in QUESTIONS if q["id"] == _CASSETTE_ID)
    with pytest.raises(CassetteError, match=fragment):
        cassette_for(_CASSETTE_ID, case["question"])


def test_report_counts_cassette_answers_separately(tmp_path, monkeypatch):
    import json

    from testing.golden import CASSETTES_ENV

    case = next(q for q in QUESTIONS if q["id"] == _CASSETTE_ID)
    (tmp_path / f"{_CASSETTE_ID}.json").write_text(json.dumps(_CRAFTED_G48), encoding="utf-8")
    monkeypatch.setenv(CASSETTES_ENV, str(tmp_path))
    with_cassettes = report(use_cassettes=True)
    builder_only = report(use_cassettes=False)
    if builder_sql(case["question"]) is None:
        assert with_cassettes["cassette_answered"] == 1 and with_cassettes["cassette_correct"] == 1
        assert with_cassettes["correct"] == builder_only["correct"] + 1
    assert with_cassettes["builder_correct"] == builder_only["builder_correct"]


# ------------------------------------------------------------------------------------------------ evaluation
def _params():
    for q in QUESTIONS:
        marks = []
        if q.get("known_gap"):
            marks.append(pytest.mark.xfail(strict=True, reason=f"builder gap: {q['known_gap']}"))
        yield pytest.param(q, id=q["id"], marks=marks)


@pytest.mark.ac("P4-AC2")
@pytest.mark.parametrize("case", list(_params()))
def test_golden_question(case):
    if case["builder"]:
        outcome = evaluate(case)
        assert outcome.source == "builder", f"builder declined {case['question']!r}"
        assert outcome.correct, f"{case['question']!r}: {outcome.problems} (sql: {outcome.sql})"
        return
    if builder_sql(case["question"]) is not None:
        outcome = evaluate(case)  # the builder now answers an LLM question: it must be right, never a guess
        assert outcome.correct, f"builder answered {case['question']!r} wrongly: {outcome.problems}"
        return
    if cassette_for(case["id"], case["question"]) is None:
        pytest.skip(
            f"{case['id']}: needs the LLM path; no recorded cassette (AI/ML records them, never live)"
        )
    outcome = evaluate(case, use_cassettes=True)
    assert outcome.correct, (
        f"cassette answer for {case['question']!r}: {outcome.problems} (sql: {outcome.sql})"
    )


def test_builder_accuracy_report(capsys):
    rep = report(use_cassettes=False)
    lines = [f"{'OK ' if o.correct else '-- '} {o.id} [{o.source}] {o.question}" for o in rep["outcomes"]]
    with capsys.disabled():
        print(
            "\n".join(
                [
                    "",
                    "golden NL->SQL (builder only):",
                    *lines,
                    f"builder accuracy {rep['builder_correct']}/{rep['total']} = {rep['builder_accuracy']:.1%}, "
                    f"precision when answered {rep['precision_when_answered']:.1%}",
                ]
            )
        )
    assert rep["builder_accuracy"] >= BUILDER_ACCURACY_FLOOR - 1e-9
    wrong = [o for o in rep["outcomes"] if o.source == "builder" and not o.correct]
    assert wrong == [], "the builder must decline rather than answer wrongly: " + "; ".join(
        f"{o.id}: {o.problems}" for o in wrong
    )
