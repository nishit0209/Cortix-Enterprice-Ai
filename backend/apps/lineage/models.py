from django.db import models

from apps.core.tenancy.models import TenantModel

NODE_TYPES = [
    (t, t)
    for t in (
        "data_source", "upload", "dataset", "dataset_version", "pipeline", "pipeline_version", "pipeline_run",
        "semantic_model", "saved_query", "dashboard", "ml_model_version", "document",
        # W4: producers of publish_frame outputs (ML batch predictions, Customer 360 / Product 360 exports)
        "ml_model", "ml_experiment", "customer_source", "customer_segment", "product_source",
    )
]


class LineageEdge(TenantModel):
    """A directed edge written by the code that caused it (no inference)."""

    from_type = models.CharField(max_length=32, choices=NODE_TYPES)
    from_id = models.UUIDField()
    to_type = models.CharField(max_length=32, choices=NODE_TYPES)
    to_id = models.UUIDField()
    run_id = models.UUIDField(null=True, blank=True)
    label = models.CharField(max_length=120, blank=True, default="")
    transform = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["org", "from_type", "from_id", "to_type", "to_id", "run_id"],
                name="lineage_edge_uniq",
                nulls_distinct=False,
            ),
        ]
        indexes = [
            models.Index(fields=["org", "to_type", "to_id"], name="lineage_org_to_idx"),
            models.Index(fields=["org", "from_type", "from_id"], name="lineage_org_from_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.from_type}:{self.from_id} -> {self.to_type}:{self.to_id}"
