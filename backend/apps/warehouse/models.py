from django.db import models

from apps.core.tenancy.models import TenantModel


class WhOrgBinding(TenantModel):
    """Warehouse provisioning state of one org: its schema and reader role in `nexora_wh`."""

    STATUS = [("ready", "ready"), ("suspended", "suspended")]

    wh_schema = models.CharField(max_length=63)
    reader_role = models.CharField(max_length=63)
    status = models.CharField(max_length=16, choices=STATUS, default="ready")
    provisioned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org"], name="wh_binding_org_uniq"),
            models.UniqueConstraint(fields=["org", "wh_schema"], name="wh_binding_org_schema_uniq"),
        ]

    def __str__(self) -> str:
        return self.wh_schema
