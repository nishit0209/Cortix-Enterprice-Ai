"""Warehouse identifier naming (no DB)."""

import uuid

import pytest

from apps.warehouse import naming
from apps.warehouse.serialization import to_jsonable


def test_production_names(settings):
    settings.WH_SCHEMA_PREFIX = "org_"
    assert naming.schema_name("ab12cd34") == "org_ab12cd34"
    assert naming.reader_role_name("ab12cd34") == "wh_q_ab12cd34"
    assert naming.reader_role_prefix() == "wh_q_"


def test_test_session_names_are_prefix_aware(settings):
    settings.WH_SCHEMA_PREFIX = "t_data_engineering_"
    assert naming.schema_name("ab12cd34") == "t_data_engineering_ab12cd34"
    assert naming.reader_role_name("ab12cd34") == "wh_q_t_data_engineering_ab12cd34"


@pytest.mark.parametrize("bad", ["", "AB12", "ab-12", "ab12cd34; drop", "x" * 17])
def test_invalid_short_ids_are_refused(bad):
    with pytest.raises(naming.InvalidIdentifier):
        naming.schema_name(bad)


def test_invalid_prefix_refused(settings):
    settings.WH_SCHEMA_PREFIX = 'org";'
    with pytest.raises(naming.InvalidIdentifier):
        naming.schema_name("ab12cd34")


def test_dataset_table_name_uses_random_tail_of_uuid():
    ds = uuid.UUID("0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
    assert naming.dataset_table_name(ds, 3) == "ds_0c1d2e3f4a5b_v3"
    with pytest.raises(naming.InvalidIdentifier):
        naming.dataset_table_name(ds, 0)


@pytest.mark.parametrize("ident", ['a"b', "A", "1abc", "a b", "x" * 64, "a;b"])
def test_check_ident_refuses_unsafe(ident):
    with pytest.raises(naming.InvalidIdentifier):
        naming.check_ident(ident)


def test_executor_login_role(settings):
    settings.WH_DATABASE_URL_EXECUTOR = "postgres://wh_executor:pw@localhost:5433/nexora_wh"
    assert naming.executor_login_role() == "wh_executor"


def test_to_jsonable():
    import datetime as dt
    from decimal import Decimal

    assert to_jsonable(Decimal("12.50")) == 12.5
    assert to_jsonable(Decimal("12")) == 12
    assert to_jsonable(Decimal("NaN")) is None
    assert to_jsonable(float("inf")) is None
    assert to_jsonable(dt.date(2024, 1, 2)) == "2024-01-02"
    assert to_jsonable({"a": [Decimal("1.5")]}) == {"a": [1.5]}
    assert to_jsonable(b"\x01\x02") == "0102"
