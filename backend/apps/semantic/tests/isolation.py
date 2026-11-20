"""Isolation-matrix factories (testing/isolation.py) for the semantic routes (dataset_id comes from datasets)."""

from __future__ import annotations


def _metric(world):
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
    return {"dataset_id": str(dataset.id), "metric_id": str(metric.id)}


ISOLATION_FACTORIES = {"dataset-metric": _metric}
