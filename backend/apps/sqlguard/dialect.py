"""The sqlglot dialect the guard reads and writes.

Identical to sqlglot's postgres dialect except that the modulo operator is generated as ``MOD(a, b)``: the
executed SQL is passed to psycopg with named parameters, where a bare ``%`` would be a placeholder marker.
With string literals bound as parameters and ``%`` refused in identifiers, the only ``%`` left in executed SQL
are the guard's own ``%(pN)s`` placeholders.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.postgres import Postgres


class GuardPostgres(Postgres):
    class Generator(Postgres.Generator):
        TRANSFORMS = {
            **Postgres.Generator.TRANSFORMS,
            exp.Mod: lambda self, e: self.func("MOD", e.this, e.expression),
        }


DIALECT = GuardPostgres
