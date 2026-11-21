"""Isolation-matrix factories for apps.ml routes (see backend/testing/isolation.py): real org-A ML resources."""

from __future__ import annotations

from uuid import uuid4

from testing.isolation import once

# a distinctive number: the matrix treats every URL kwarg value as an identifier that must not leak
PROBE_VERSION = 730_519


def _resources(world) -> dict:
    def build():
        from apps.ml.models import Experiment, MLModel, MLProject, ModelVersion

        with world.tenant():
            project = MLProject.objects.create(name="Isolation probe project", dataset_id=uuid4(), target="y",
                                               task_type="classification", features=["a"],
                                               created_by=world.owner.pk)
            exp = Experiment.objects.create(project=project, algorithm="logistic_regression",
                                            task_type="classification", status="succeeded",
                                            created_by=world.owner.pk)
            model = MLModel.objects.create(project=project, name=f"Isolation probe {world.slug}",
                                           task_type="classification", target="y", created_by=world.owner.pk)
            ModelVersion.objects.create(model=model, version=PROBE_VERSION, experiment=exp, algorithm=exp.algorithm,
                                        artifact_key=f"orgs/{world.id}/ml/models/{model.id}/{PROBE_VERSION}.joblib",
                                        artifact_sha256="0" * 64, created_by=world.owner.pk)
        return {"project": project, "experiment": exp, "model": model}

    return once(world, "ml", build)


def _project(world) -> dict[str, str]:
    return {"pk": str(_resources(world)["project"].id)}


def _experiment(world) -> dict[str, str]:
    return {"pk": str(_resources(world)["experiment"].id)}


def _model(world) -> dict[str, str]:
    return {"pk": str(_resources(world)["model"].id)}


ISOLATION_FACTORIES = {
    **{f"ml-project-{n}": _project for n in ("detail", "eda", "experiments", "recommendations", "snapshots")},
    **{f"ml-experiment-{n}": _experiment for n in ("detail", "register")},
    **{f"ml-model-{n}": _model for n in ("detail", "batch-predictions", "monitoring", "predict", "versions")},
    "ml-model-promote": lambda world: {**_model(world), "version": str(PROBE_VERSION)},
}
