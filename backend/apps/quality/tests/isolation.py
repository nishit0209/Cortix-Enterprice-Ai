"""Isolation-matrix factories (testing/isolation.py) for the quality routes (dataset_id comes from datasets)."""

from __future__ import annotations


def _rule(world):
    from apps.datasets.tests.isolation import dataset_for
    from apps.quality.models import QualityRule

    dataset = dataset_for(world)
    rule = world.resources.get("quality_rule")
    if rule is None:
        with world.tenant():
            rule = QualityRule.objects.create(org_id=world.id, dataset=dataset, rule_type="not_null",
                                              column_name="isolation")
        world.resources["quality_rule"] = rule
    return {"dataset_id": str(dataset.id), "rule_id": str(rule.id)}


ISOLATION_FACTORIES = {"dataset-quality-rule": _rule}
