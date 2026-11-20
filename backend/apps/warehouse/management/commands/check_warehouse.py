"""`manage.py check_warehouse`: verify the local warehouse roles (PostgreSQL 16/17, scripts/bootstrap_local_db.py).

Connects as wh_loader (WH_DATABASE_URL_LOADER) and reads only the role catalog. Exit code 1 when a problem is found.
`--provision-test` additionally creates and drops a throwaway schema + reader role (prefix `t_check_`) to prove the
loader can create a role, grant it to the executor, and that the executor can `SET ROLE` to it and read a table.
"""

from __future__ import annotations

import secrets

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check the warehouse roles and privileges (wh_loader / wh_executor / per-org reader roles)."

    def add_arguments(self, parser):
        parser.add_argument("--provision-test", action="store_true",
                            help="also create, use and drop a throwaway schema + reader role")

    def handle(self, *args, **options):
        from apps.warehouse import services
        from apps.warehouse.exceptions import WarehouseError

        try:
            report = services.check_setup()
        except WarehouseError as exc:
            raise CommandError(f"Cannot connect to the warehouse as wh_loader: {exc.detail}") from exc
        version = report.server_version
        self.stdout.write(f"PostgreSQL server_version_num: {version}")
        for problem in report.problems:
            self.stdout.write(self.style.ERROR(f"  problem: {problem}"))
        if options["provision_test"] and report.ok:
            self._provision_test()
        if not report.ok:
            raise CommandError(f"{len(report.problems)} warehouse setup problem(s) found.")
        self.stdout.write(self.style.SUCCESS("Warehouse roles OK."))

    def _provision_test(self) -> None:
        import psycopg
        from psycopg import sql

        from apps.warehouse import naming, services
        from apps.warehouse.db import executor_connection, loader_connection

        tag = secrets.token_hex(4)
        schema, role = f"t_check_{tag}", f"wh_q_t_check_{tag}"
        executor = naming.executor_login_role()
        ident = sql.Identifier
        try:
            services._provision_objects(schema, role, executor)
            with loader_connection(autocommit=True) as conn:
                conn.execute(sql.SQL("CREATE TABLE {}.probe (v integer)").format(ident(schema)))
                conn.execute(sql.SQL("INSERT INTO {}.probe VALUES (1)").format(ident(schema)))
            with executor_connection() as conn:
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(ident(role)))
                value = conn.execute(sql.SQL("SELECT v FROM {}.probe").format(ident(schema))).fetchone()[0]
                conn.rollback()
            if value != 1:
                raise CommandError("The executor could not read through the reader role.")
            self.stdout.write(self.style.SUCCESS(f"  provision test OK ({schema} / {role})"))
        except psycopg.Error as exc:
            raise CommandError(f"Provision test failed: {exc.diag.message_primary or exc}") from exc
        finally:
            with loader_connection(autocommit=True) as conn:
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(ident(schema)))
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident(role)))
