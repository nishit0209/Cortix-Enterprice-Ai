"""Physical naming of warehouse objects in `nexora_wh` (schemas, reader roles, dataset tables).

Production (WH_SCHEMA_PREFIX="org_"):  schema org_<short_id>,  reader role wh_q_<short_id>
Tests      (WH_SCHEMA_PREFIX="t_<suffix>_"): schema t_<suffix>_<short_id>, role wh_q_t_<suffix>_<short_id>

so parallel test sessions never collide with each other or with real org objects.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse
from uuid import UUID

from django.conf import settings

PG_IDENT_MAX = 63
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SHORT_RE = re.compile(r"^[a-z0-9]{1,16}$")


class InvalidIdentifier(ValueError):
    pass


def check_ident(name: str) -> str:
    """Only lower-case ASCII identifiers we generated ourselves are ever used for warehouse objects."""
    if not _IDENT_RE.match(name or ""):
        raise InvalidIdentifier(f"invalid warehouse identifier: {name!r}")
    return name


def _prefix() -> str:
    prefix = settings.WH_SCHEMA_PREFIX
    if not re.match(r"^[a-z][a-z0-9_]{0,30}$", prefix or ""):
        raise InvalidIdentifier(f"invalid WH_SCHEMA_PREFIX: {prefix!r}")
    return prefix


def _short(short_id: str) -> str:
    if not _SHORT_RE.match(short_id or ""):
        raise InvalidIdentifier(f"invalid org short_id: {short_id!r}")
    return short_id


def schema_name(short_id: str) -> str:
    return check_ident(f"{_prefix()}{_short(short_id)}")


def reader_role_name(short_id: str) -> str:
    prefix = _prefix()
    tag = "" if prefix == "org_" else prefix
    return check_ident(f"wh_q_{tag}{_short(short_id)}")


def reader_role_prefix() -> str:
    """Prefix shared by every reader role of this deployment/test session (used by test cleanup)."""
    prefix = _prefix()
    return "wh_q_" + ("" if prefix == "org_" else prefix)


def dataset_table_name(dataset_id: UUID | str, version: int) -> str:
    """ds_<12 random hex chars of the dataset UUIDv7>_v<n> (the UUIDv7 head is a timestamp)."""
    hexid = UUID(str(dataset_id)).hex
    if version < 1:
        raise InvalidIdentifier("version must be >= 1")
    return check_ident(f"ds_{hexid[-12:]}_v{int(version)}")


def executor_login_role() -> str:
    """The login role of WH_DATABASE_URL_EXECUTOR (member of every reader role)."""
    user = urlparse(settings.WH_DATABASE_URL_EXECUTOR).username or ""
    return check_ident(user)
