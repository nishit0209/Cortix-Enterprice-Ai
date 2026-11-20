"""Signals of apps.datasets (same-layer listeners such as apps.semantic subscribe in their AppConfig.ready()).

dataset_version_published(sender=Dataset, org_id=UUID, dataset_id=UUID, version_id=UUID)
    Sent inside the org's tenant context right after a version became the dataset's CURRENT version (ingestion
    succeeded and no blocking quality rule failed). Receivers must be idempotent, must not raise for data
    problems, and should run their writes in a savepoint (`transaction.atomic()`); errors are logged and never
    fail the ingestion.
"""

from django.dispatch import Signal

dataset_version_published = Signal()
