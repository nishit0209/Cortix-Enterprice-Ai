"""`manage.py seed_demo` (CONTRACT "Local runtime: Demo data").

Unit tests (no database): password generation, CSV discovery and the testdata generator, the eager-runner switch,
the summary and the command wiring, and that every dashboard question is answered by nl2sql's deterministic
builder (no Gemini call is needed). The `django_db` test runs the REAL command end to end (eager): ingestion through
the dataset services, the BI chat pipeline, the dashboard and its live widget data, idempotency and --reset."""

from __future__ import annotations

import re
import shutil
import string
import sys
from datetime import date
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command

from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import client_for
from apps.platform_admin import demo

SEED_EMAIL = "demo.seed@example.test"


# ================================================================================================ unit (no DB)
def test_generated_passwords_are_strong_and_random():
    passwords = {demo.generate_password() for _ in range(50)}
    assert len(passwords) == 50
    for pw in passwords:
        assert len(pw) == 20
        assert any(c in string.ascii_lowercase for c in pw) and any(c in string.ascii_uppercase for c in pw)
        assert any(c in string.digits for c in pw) and any(c in "-_.!@#%" for c in pw)
        assert not any(c.isspace() for c in pw)


def test_csv_discovery_puts_sales_first_and_ignores_adversarial_files(tmp_path):
    for name in ("products.csv", "sales.csv", "churn.csv", "notes.txt"):
        (tmp_path / name).write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "adversarial").mkdir()
    (tmp_path / "adversarial" / "formula_injection.csv").write_text("=1+1\n", encoding="utf-8")
    assert [p.name for p in demo.csv_files(tmp_path)] == ["sales.csv", "churn.csv", "products.csv"]
    assert demo.csv_files(tmp_path / "missing") == []


def test_existing_test_data_is_used_as_is(tmp_path):
    (tmp_path / "sales.csv").write_text("a\n1\n", encoding="utf-8")

    def never(*_a, **_k):
        raise AssertionError("the generator must not run when CSVs exist")

    assert [p.name for p in demo.ensure_test_data(tmp_path, run=never)] == ["sales.csv"]


def test_missing_test_data_is_generated_by_the_testdata_script(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        Path(cmd[cmd.index("--out") + 1]).mkdir(parents=True, exist_ok=True)
        (Path(cmd[cmd.index("--out") + 1]) / "sales.csv").write_text("a\n1\n", encoding="utf-8")

    out = tmp_path / "generated"
    assert [p.name for p in demo.ensure_test_data(out, run=fake_run)] == ["sales.csv"]
    ((cmd, kwargs),) = calls
    assert cmd[0] == sys.executable and Path(cmd[1]) == demo.generator_script()
    assert kwargs["check"] is True and Path(kwargs["cwd"]) == demo.generator_script().parent


def test_a_generator_that_produces_nothing_or_is_missing_is_an_error(tmp_path, monkeypatch):
    with pytest.raises(demo.SeedError, match="produced no CSV"):
        demo.ensure_test_data(tmp_path / "empty", run=lambda *_a, **_k: None)
    monkeypatch.setattr(demo, "generator_script", lambda: tmp_path / "nope" / "generate.py")
    with pytest.raises(demo.SeedError, match="generator"):
        demo.ensure_test_data(tmp_path / "empty")


def test_the_real_generator_creates_the_demo_csvs(tmp_path):
    """Runs testdata/generate.py (QA's script, read-only use) through the same code path as the command."""
    files = demo.ensure_test_data(tmp_path / "generated")
    names = [p.name for p in files]
    assert names[0] == demo.SALES_FILE
    assert {"sales.csv", "churn.csv", "customers_crm.csv", "products_supplier_a.csv"} <= set(names)
    assert all(p.parent == tmp_path / "generated" for p in files)  # never the adversarial/ files
    assert files[0].read_text(encoding="utf-8").splitlines()[0].split(",") == [
        "order_id", "order_date", "customer_id", "customer_name", "region", "product", "category", "quantity",
        "unit_price", "discount", "revenue", "channel"]


def test_dataset_names():
    assert demo.dataset_name_for(Path("sales.csv")) == "Sales"
    assert demo.dataset_name_for(Path("customers_crm.csv")) == "Customers (CRM)"
    assert demo.dataset_name_for(Path("store_visits_2026.csv")) == "Store visits 2026"


def test_eager_tasks_switches_the_runner_and_restores_it(settings):
    settings.TASK_RUNNER = "thread"
    with demo.eager_tasks():
        assert settings.TASK_RUNNER == "eager"
    assert settings.TASK_RUNNER == "thread"
    with pytest.raises(RuntimeError), demo.eager_tasks():
        raise RuntimeError("seed failed")
    assert settings.TASK_RUNNER == "thread"


def _result(**overrides) -> demo.SeedResult:
    values = dict(email="demo@nexora.local", password="Gen-Pass_123!abcdEFGH", password_note="generated",
                  user_created=True, org_id=str(uuid4()), org_slug="nexora-demo", org_name="Nexora Demo",
                  org_created=True, ai_data_policy="synthetic_only")
    values.update(overrides)
    return demo.SeedResult(**values)


def test_summary_lists_login_datasets_rows_and_dashboard():
    r = _result(datasets=[
        demo.DatasetResult(file="sales.csv", name="Sales", dataset_id=str(uuid4()), status="ready", rows=5020,
                           columns=12, quality=96.5, action="created"),
        demo.DatasetResult(file="churn.csv", name="Customer churn", status="failed", error="parse_error: bad row",
                           action="created"),
    ], conversation_id="c-1", dashboard_id="d-1", widgets=["Total revenue", "Revenue by region"],
        warnings=["churn.csv: ingestion did not finish (failed; parse_error: bad row)."])
    assert r.total_rows == 5020  # only ready datasets count
    text = demo.format_summary(r)
    assert "Login     : demo@nexora.local" in text and "Password  : Gen-Pass_123!abcdEFGH" in text
    assert "Org       : Nexora Demo (slug 'nexora-demo', created)" in text and "AI policy : synthetic_only" in text
    sales_line = next(line for line in text.splitlines() if line.startswith("Sales "))
    assert sales_line.split() == ["Sales", "ready", "5020", "12", "96.5", "created"]
    assert "Dashboard    : d-1 (2 widgets: Total revenue, Revenue by region)" in text
    assert "WARNING: churn.csv: ingestion did not finish" in text
    kept = demo.format_summary(_result(password=None, password_note="unchanged (use --password to set)",
                                       org_created=False))
    assert "Password  : (unchanged (use --password to set))" in kept and "existing)" in kept


def test_command_wires_its_options_to_the_seed(monkeypatch, tmp_path):
    seen = {}

    def fake_seed(**kwargs):
        seen.update(kwargs)
        kwargs["progress"]("Ingesting sales.csv ...")
        return _result()

    monkeypatch.setattr(demo, "seed_demo", fake_seed)
    out = StringIO()
    call_command("seed_demo", "--email", "x@example.test", "--org", "Acme Demo", "--reset", "--data-dir",
                 str(tmp_path), stdout=out)
    assert {k: seen[k] for k in ("email", "password", "org_name", "reset", "directory")} == {
        "email": "x@example.test", "password": None, "org_name": "Acme Demo", "reset": True, "directory": tmp_path}
    text = out.getvalue()
    assert "Ingesting sales.csv ..." in text and "Login     : demo@nexora.local" in text
    assert "Demo data is ready." in text
    monkeypatch.setattr(demo, "seed_demo", lambda **_k: _result(warnings=["one"]))
    out = StringIO()
    call_command("seed_demo", "--password", "Synthetic-Pass-2026!", stdout=out)
    assert "Finished with 1 warning(s)." in out.getvalue()


@pytest.mark.parametrize(("exc", "message"), [
    (demo.SeedError("No CSV files in x"), "No CSV files in x"),
    (ValidationError(["This password is too common."]), "The password was rejected: This password is too common."),
])
def test_command_errors_are_reported_cleanly(monkeypatch, exc, message):
    def failing(**_k):
        raise exc

    monkeypatch.setattr(demo, "seed_demo", failing)
    with pytest.raises(CommandError, match=re.escape(message)):
        call_command("seed_demo", stdout=StringIO())


# The schema the semantic layer derives for testdata/generated/sales.csv (types as the warehouse load creates them).
SALES_SCHEMA = (
    ("order_id", "text", "id", False), ("order_date", "date", "date", False), ("customer_id", "text", "id", False),
    ("customer_name", "text", "person_name", True), ("region", "text", None, False), ("product", "text", None, False),
    ("category", "text", None, False), ("quantity", "bigint", None, False),
    ("unit_price", "numeric", "currency", False), ("discount", "numeric", "currency", False),
    ("revenue", "numeric", "currency", False), ("channel", "text", None, False),
)


def _sales_context():
    from apps.semantic.generation import infer_role
    from nexora.contracts import QueryPolicy, QuestionContext

    metas = tuple({"name": n, "type": t, "semantic_type": s, "role": infer_role(n, t, s, pii)}
                  for n, t, s, pii in SALES_SCHEMA)
    tables = {"sales": "t_demo.ds_sales_v1"}
    policy = QueryPolicy(org_id=uuid4(), user_id=uuid4(), dataset_id=uuid4(), tables=tables,
                         allowed_columns={"sales": frozenset(m["name"] for m in metas)}, fingerprint="fp-demo")
    return QuestionContext(org_id=policy.org_id, user_id=policy.user_id, dataset_id=policy.dataset_id, policy=policy,
                           history=(), columns_meta={"sales": metas}, metrics=(), dataset_name="Sales",
                           dataset_classification="synthetic")


@pytest.mark.parametrize("spec", demo.DASHBOARD_WIDGETS, ids=[s["question"] for s in demo.DASHBOARD_WIDGETS])
def test_every_dashboard_question_is_answered_by_the_deterministic_builder(spec):
    from apps.nl2sql.builder import build_sql

    result = build_sql(spec["question"], _sales_context(), today=date(2026, 11, 20))
    assert result is not None, spec["question"]
    assert '"revenue"' in result.sql


def test_the_default_dashboard_has_four_to_six_tiles_including_the_required_ones():
    titles = [w["title"] for w in demo.DASHBOARD_WIDGETS]
    assert 4 <= len(titles) <= 6
    assert {"Total revenue", "Revenue by region", "Monthly revenue trend", "Top 10 products by revenue"} <= set(titles)
    assert {w["kind"] for w in demo.DASHBOARD_WIDGETS} == {"kpi", "chart"}


# ================================================================================================ DB: end to end
def _run(*args) -> str:
    out = StringIO()
    call_command("seed_demo", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db(transaction=True, databases="__all__")
def test_seed_demo_end_to_end_idempotent_and_reset(tmp_path, settings, fake_embedder, network_spy):
    """Real services, eager runner, no Gemini key: user + org, sales.csv ingested (profiling, quality, warehouse
    load, semantic layer), a conversation answered by the deterministic builder and a default dashboard whose widget
    data is re-executed live. A re-run changes nothing; --reset rebuilds the demo content."""
    from apps.accounts.models import User
    from apps.bi.models import Conversation, Dashboard, DashboardWidget, Message, SavedQuery
    from apps.datasets.models import Dataset
    from apps.orgs.services import effective_ai_policy, get_current_org, user_orgs

    data = tmp_path / "generated"
    data.mkdir()
    shutil.copy(demo.data_dir() / demo.SALES_FILE, data / demo.SALES_FILE)
    settings.TASK_RUNNER = "thread"  # the command must switch to the eager runner by itself
    settings.GEMINI_API_KEY = ""
    args = ("--data-dir", str(data), "--email", SEED_EMAIL)

    text = _run(*args)
    assert settings.TASK_RUNNER == "thread"
    assert "Demo data is ready." in text and "WARNING" not in text, text
    password = re.search(r"^Password  : (\S+)$", text, re.M).group(1)
    user = User.objects.get(email=SEED_EMAIL)
    assert len(password) == 20 and user.check_password(password)
    (org,) = [o for o in user_orgs(user.pk) if o["name"] == demo.DEFAULT_ORG_NAME]
    assert org["role"] == "owner"
    assert f"(slug '{org['slug']}', created)" in text and "AI policy : synthetic_only" in text

    with tenant_context(org["id"]):
        assert effective_ai_policy(get_current_org()) == "synthetic_only"
        (sales,) = Dataset.objects.exclude(status="deleted")
        assert (sales.status, sales.row_count, sales.column_count, sales.classification) == (
            "ready", 5020, 12, "synthetic")
        assert sales.classification_attested_by == user.pk and sales.quality_score is not None
        conv = Conversation.objects.get()
        assert (conv.title, conv.user_id, conv.dataset_id) == (demo.CONVERSATION_TITLE, user.pk, sales.id)
        assert Message.objects.filter(conversation=conv, role="assistant", status="ok").count() == len(
            demo.DASHBOARD_WIDGETS)
        dash = Dashboard.objects.get()
        assert (dash.title, dash.owner_id, dash.is_default) == (demo.DASHBOARD_TITLE, user.pk, True)
        widgets = list(DashboardWidget.objects.filter(dashboard=dash))
        assert {w.title for w in widgets} == {s["title"] for s in demo.DASHBOARD_WIDGETS}
        assert all(w.saved_query_id for w in widgets) and SavedQuery.objects.count() == len(widgets)
        assert get_current_org().settings[demo.SEED_KEY] == {
            "datasets": {demo.SALES_FILE: str(sales.id)}, "conversation_id": str(conv.id),
            "dashboard_id": str(dash.id)}
    sales_line = next(line for line in text.splitlines() if line.startswith("Sales "))
    assert sales_line.split()[1:3] == ["ready", "5020"]

    client = client_for(user)
    listed = client.get(f"/api/v1/orgs/{org['slug']}/dashboards").json()["items"]
    assert [d["id"] for d in listed] == [str(dash.id)] and listed[0]["widget_count"] == len(widgets)
    for widget in widgets:
        r = client.get(f"/api/v1/orgs/{org['slug']}/dashboards/{dash.id}/widgets/{widget.id}/data")
        assert r.status_code == 200, r.content
        assert r.json()["row_count"] > 0  # live rows from the warehouse, re-executed under the viewer's policy

    again = _run(*args)
    assert "Password  : (unchanged (use --password to set))" in again and "existing)" in again
    with tenant_context(org["id"]):
        assert [d.id for d in Dataset.objects.exclude(status="deleted")] == [sales.id]
        assert Dashboard.objects.get().id == dash.id and Conversation.objects.get().id == conv.id
        assert DashboardWidget.objects.count() == len(widgets) and SavedQuery.objects.count() == len(widgets)

    new_password = "Synthetic-Reset-Pass-2026!"
    _run(*args, "--reset", "--password", new_password)
    user.refresh_from_db()
    assert user.check_password(new_password)
    with tenant_context(org["id"]):
        assert Dataset.objects.get(id=sales.id).status == "deleted"
        (fresh,) = Dataset.objects.exclude(status="deleted")
        assert fresh.id != sales.id and fresh.status == "ready"
        rebuilt = Dashboard.objects.get()
        assert rebuilt.id != dash.id and rebuilt.is_default
        assert not Conversation.objects.filter(id=conv.id).exists() and Conversation.objects.count() == 1
        assert SavedQuery.objects.count() == DashboardWidget.objects.count() == len(widgets)  # old pins removed
