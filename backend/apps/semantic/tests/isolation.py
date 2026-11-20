"""Isolation-matrix factories (testing/isolation.py) for the semantic routes (dataset_id comes from datasets)."""

from __future__ import annotations


def _model_and_metric(world):
    from apps.datasets.tests.isolation import dataset_for
    from apps.semantic.models import MetricDefinition, SemanticModel

    dataset = dataset_for(world)
    metric = world.resources.get("metric")
    if metric is None:
        with world.tenant():
            model = SemanticModel.objects.create(org_id=world.id, dataset=dataset, name=dataset.name)
            metric = MetricDefinition.objects.create(org_id=world.id, model=model, table_name="isolation",
                                                     key="row_count", name="Row count", expression="COUNT(*)")
        world.resources["metric"] = metric
    return dataset, metric


def _metric(world):
    dataset, metric = _model_and_metric(world)
    return {"dataset_id": str(dataset.id), "metric_id": str(metric.id)}


def _model_pk(world):
    _dataset, metric = _model_and_metric(world)
    return {"pk": str(metric.model_id)}


def _model_metric(world):
    _dataset, metric = _model_and_metric(world)
    return {"pk": str(metric.model_id), "metric_id": str(metric.id)}


ISOLATION_FACTORIES = {
    "dataset-metric": _metric,
    "semantic-model-detail": _model_pk,
    "semantic-model-generate": _model_pk,
    "semantic-model-publish": _model_pk,
    "semantic-model-metrics": _model_pk,
    "semantic-model-metric": _model_metric,
}
