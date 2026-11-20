"""Isolation-matrix factories (testing/isolation.py) for the datasets routes: real org-A resources."""

from __future__ import annotations


def dataset_for(world):
    from apps.datasets.models import Dataset

    dataset = world.resources.get("dataset")
    if dataset is None:
        with world.tenant():
            dataset = Dataset.objects.create(org_id=world.id, name=f"Isolation {world.slug}",
                                             slug=f"isolation-{world.slug}", status="ready")
        world.resources["dataset"] = dataset
    return dataset


def upload_for(world):
    from apps.datasets.models import Upload

    upload = world.resources.get("upload")
    if upload is None:
        with world.tenant():
            upload = Upload.objects.create(
                org_id=world.id, filename="isolation.csv", content_type="text/csv", file_kind="csv", size_bytes=4,
                sha256="0" * 64, object_key=f"orgs/{world.id}/uploads/isolation/isolation.csv", status="ready",
            )
        world.resources["upload"] = upload
    return upload


def _dataset_pk(world):
    return {"pk": str(dataset_for(world).id)}


ISOLATION_FACTORIES = {
    "upload-detail": lambda world: {"pk": str(upload_for(world).id)},
    "dataset-detail": _dataset_pk,
    "dataset-classification": _dataset_pk,
    "dataset-columns": _dataset_pk,
    "dataset-preview": _dataset_pk,
    "dataset-versions": _dataset_pk,
}
KWARG_FACTORIES = {"dataset_id": lambda world: {"dataset_id": str(dataset_for(world).id)}}
