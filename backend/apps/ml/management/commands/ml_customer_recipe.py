"""Customer 360 ML recipe: churn + CLV projects (and optionally models) from customers + orders datasets.

    python manage.py ml_customer_recipe --org <slug> --user <email> --customers "Customers" --orders "Orders" \
        [--train] [--promote staging|production|none] [--horizon 180] [--attest]
    python manage.py ml_customer_recipe --org <slug> --user <email> --features <dataset id> [--train]

Datasets are given by id or exact name. Everything runs as the user, through their data policy and permissions
(the user needs ml.manage; promotion needs ml.promote; production promotion creates an approval request).
"""

from __future__ import annotations

import json
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create the Customer 360 churn (classification) and CLV (regression) ML projects; optionally train them."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True, help="Organization slug")
        parser.add_argument("--user", required=True, help="Email of the member the recipe runs as")
        parser.add_argument("--customers", help="Customers dataset id or name")
        parser.add_argument("--orders", help="Orders dataset id or name")
        parser.add_argument("--features", help="An existing feature dataset (skips the feature step)")
        parser.add_argument("--horizon", type=int, default=180, help="CLV horizon in days (default 180)")
        parser.add_argument("--train", action="store_true", help="Train, register and promote the models")
        parser.add_argument("--promote", default="staging", choices=["none", "staging", "production"])
        parser.add_argument("--attest", action="store_true",
                            help="Attest that synthetic/public sources make a synthetic/public feature dataset")

    def handle(self, *args, **opts):
        from apps.accounts.models import User
        from apps.core.notifications import members_with_permission
        from apps.core.tenancy import tenant_context
        from apps.core.tenancy.context import current_actor
        from apps.ml import services
        from apps.orgs.models import Organization

        org = Organization.objects.filter(slug=opts["org"]).first()
        user = User.objects.filter(email__iexact=opts["user"]).first()
        if org is None or user is None:
            raise CommandError("Unknown organization slug or user email.")
        token = current_actor.set(("user", user.pk))
        try:
            with tenant_context(org.id):
                if user.pk not in set(members_with_permission("ml.manage")):
                    raise CommandError(f"{user.email} does not have ml.manage in {org.slug}.")
                ids = {k: self._dataset_id(opts[k]) for k in ("customers", "orders", "features") if opts.get(k)}
            if "features" in ids:
                result = services.train_customer_projects(
                    org_id=org.id, user_id=user.pk, features_dataset_id=ids["features"], horizon_days=opts["horizon"],
                    train=opts["train"], promote_to=opts["promote"])
            else:
                if not ("customers" in ids and "orders" in ids):
                    raise CommandError("Give --customers and --orders (or --features).")
                result = services.customer360_recipe(
                    org_id=org.id, user_id=user.pk, customers_dataset_id=ids["customers"],
                    orders_dataset_id=ids["orders"], horizon_days=opts["horizon"], train=opts["train"],
                    promote_to=opts["promote"], attest=opts["attest"])
        finally:
            current_actor.reset(token)
        self.stdout.write(json.dumps(result, indent=2, default=str))

    @staticmethod
    def _dataset_id(value: str) -> str:
        from apps.datasets.models import Dataset

        try:
            return str(Dataset.objects.get(id=UUID(value)).id)
        except (ValueError, Dataset.DoesNotExist):
            pass
        found = list(Dataset.objects.filter(name=value).values_list("id", flat=True)[:2])
        if len(found) != 1:
            raise CommandError(f"Dataset {value!r}: {'not found' if not found else 'ambiguous name'}.")
        return str(found[0])
