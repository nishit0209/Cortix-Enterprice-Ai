"""/semantic-models adapter (pure): UI shape aliases and changed-fields-only PATCH translation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from rest_framework.exceptions import ValidationError

from apps.semantic import model_views

PAYLOAD = {
    "id": "m1", "dataset_id": "d1", "status": "ready", "name": "Sales", "description": "",
    "tables": [{
        "name": "sales", "display_name": "Sales", "description": "Orders", "synonyms": ["orders"],
        "columns": [
            {"name": "region", "display_name": "Region", "description": "", "synonyms": ["zone"], "role": "dimension",
             "default_agg": None, "exposed_to_ai": True, "is_pii": False},
            {"name": "revenue", "display_name": "Revenue", "description": "", "synonyms": [], "role": "measure",
             "default_agg": "sum", "exposed_to_ai": True, "is_pii": False},
            {"name": "email", "display_name": "Email", "description": "", "synonyms": [], "role": "pii",
             "default_agg": None, "exposed_to_ai": False, "is_pii": True},
        ],
    }],
    "metrics": [],
}


@pytest.fixture
def model(monkeypatch):
    import copy

    monkeypatch.setattr(model_views.services, "model_payload", lambda m: copy.deepcopy(PAYLOAD))
    return SimpleNamespace(description="", status="ready", name="Sales")


def _ui_table(**overrides):
    cols = [{"column_name": c["name"], **{k: c[k] for k in ("display_name", "description", "synonyms", "role",
                                                             "default_agg", "exposed_to_ai")}}
            for c in PAYLOAD["tables"][0]["columns"]]
    table = {"alias": "sales", "display_name": "Sales", "description": "Orders", "synonyms": ["orders"],
             "columns": cols}
    table.update(overrides)
    return table


def test_unchanged_ui_table_produces_no_edits(model):
    assert model_views.ui_patch_to_update(model, {"tables": [_ui_table()]}) == {"tables": [], "columns": []}


def test_only_changed_fields_are_sent(model):
    table = _ui_table(description="All orders")
    table["columns"][1]["synonyms"] = ["sales", "turnover"]
    table["columns"][0]["role"] = "attribute"  # UI alias of dimension: no change
    out = model_views.ui_patch_to_update(model, {"tables": [table], "description": "Model notes"})
    assert out == {
        "description": "Model notes",
        "tables": [{"name": "sales", "description": "All orders"}],
        "columns": [{"name": "revenue", "table": "sales", "synonyms": ["sales", "turnover"]}],
    }


def test_role_alias_maps_attribute_to_dimension(model):
    table = _ui_table()
    table["columns"][1]["role"] = "attribute"
    out = model_views.ui_patch_to_update(model, {"tables": [table]})
    assert out["columns"] == [{"name": "revenue", "table": "sales", "role": "dimension"}]


def test_unknown_table_or_column_is_a_400(model):
    with pytest.raises(ValidationError):
        model_views.ui_patch_to_update(model, {"tables": [_ui_table(alias="nope")]})
    table = _ui_table()
    table["columns"].append({"column_name": "ghost"})
    with pytest.raises(ValidationError):
        model_views.ui_patch_to_update(model, {"tables": [table]})


def test_ui_payload_adds_alias_column_name_and_published_status(model):
    body = model_views.ui_payload(model)
    assert body["tables"][0]["alias"] == "sales"
    assert [c["column_name"] for c in body["tables"][0]["columns"]] == ["region", "revenue", "email"]
    assert body["status"] == "published" and body["model_status"] == "ready"
    model.status = "failed"
    assert model_views.ui_payload(model)["status"] == "failed"


def test_ui_serializers_accept_the_frontend_shapes():
    ser = model_views.UiModelPatchSerializer(data={"tables": [_ui_table()]})
    assert ser.is_valid(), ser.errors
    bad = _ui_table()
    bad["columns"][0]["role"] = "owner"
    assert not model_views.UiModelPatchSerializer(data={"tables": [bad]}).is_valid()
    assert model_views.UiModelCreateSerializer(data={"dataset_id": "0190a0c4-0000-7000-8000-000000000001"}).is_valid()
