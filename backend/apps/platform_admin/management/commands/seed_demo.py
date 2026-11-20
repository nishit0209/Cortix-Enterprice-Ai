"""manage.py seed_demo [--email EMAIL] [--password PASSWORD] [--org-name NAME] [--reset]

Creates (idempotently) a demo user and org, ingests the synthetic CSVs of testdata/generated/ through the real
dataset services, and builds a sample conversation and the default dashboard through the real BI services.
See apps.platform_admin.demo for the details. Prints the login credentials and a summary.
"""

from __future__ import annotations

from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.platform_admin import demo


class Command(BaseCommand):
    help = "Create a demo user + org and load the synthetic demo data through the real services (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--email", default=demo.DEFAULT_EMAIL, help=f"Demo login (default {demo.DEFAULT_EMAIL}).")
        parser.add_argument("--password", default=None,
                            help="Password to set. Without it a new user gets a generated password (printed); an "
                                 "existing user keeps theirs.")
        parser.add_argument("--org-name", "--org", dest="org_name", default=demo.DEFAULT_ORG_NAME,
                            help=f"Demo organization name (default '{demo.DEFAULT_ORG_NAME}').")
        parser.add_argument("--reset", action="store_true",
                            help="Remove the previously seeded demo datasets, dashboard and conversation first.")
        parser.add_argument("--data-dir", default=None,
                            help="Directory with the synthetic CSVs (default testdata/generated/).")

    def handle(self, *args, **opts):
        try:
            result = demo.seed_demo(
                email=opts["email"], password=opts["password"], org_name=opts["org_name"], reset=opts["reset"],
                directory=Path(opts["data_dir"]) if opts["data_dir"] else None,
                progress=lambda msg: self.stdout.write(msg),
            )
        except ValidationError as exc:
            raise CommandError("The password was rejected: " + " ".join(exc.messages)) from exc
        except demo.SeedError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(demo.format_summary(result))
        if result.warnings:
            self.stdout.write(self.style.WARNING(f"Finished with {len(result.warnings)} warning(s)."))
        else:
            self.stdout.write(self.style.SUCCESS("Demo data is ready."))
