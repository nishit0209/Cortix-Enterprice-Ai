"""Data-access governance (DATABASE_SCHEMA.md §4). Datasets are referenced by UUID (`dataset_id`), not by
FK: the datasets app lives in a higher layer."""

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.core.tenancy.models import TenantModel

PRINCIPAL_ACL = [("role", "role"), ("user", "user")]
PRINCIPAL_POLICY = [("everyone", "everyone"), ("role", "role"), ("user", "user")]
PRIVILEGES = [("read", "read"), ("query", "query"), ("export", "export"), ("admin", "admin")]
COLUMN_ACTIONS = [("deny", "deny"), ("mask_null", "mask_null"), ("mask_hash", "mask_hash"),
                  ("mask_partial", "mask_partial")]


class DatasetAcl(TenantModel):
    """Grants a role or user a privilege on a dataset. A dataset with no ACL rows is open to every member
    whose role permissions allow the action; once any row exists, access requires a matching grant."""

    dataset_id = models.UUIDField(db_index=True)
    principal_type = models.CharField(max_length=8, choices=PRINCIPAL_ACL)
    principal_id = models.UUIDField()
    privilege = models.CharField(max_length=8, choices=PRIVILEGES)
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["org", "dataset_id", "principal_type", "principal_id", "privilege"], name="dataset_acl_uniq"
            ),
            models.CheckConstraint(condition=models.Q(principal_type__in=["role", "user"]), name="dataset_acl_ptype"),
            models.CheckConstraint(
                condition=models.Q(privilege__in=["read", "query", "export", "admin"]), name="dataset_acl_priv"
            ),
        ]
        indexes = [models.Index(fields=["org", "dataset_id"], name="dataset_acl_org_ds_idx")]


class UserAttribute(TenantModel):
    """Per-user attribute values bound into row policies as `$user.<key>`."""

    user_id = models.UUIDField()
    key = models.CharField(max_length=64)
    values = ArrayField(models.CharField(max_length=200), default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "user_id", "key"], name="user_attribute_uniq")]
        indexes = [models.Index(fields=["org", "user_id"], name="user_attr_org_user_idx")]


class ColumnPolicy(TenantModel):
    dataset_id = models.UUIDField(db_index=True)
    column_name = models.CharField(max_length=128)
    principal_type = models.CharField(max_length=8, choices=PRINCIPAL_POLICY)
    principal_id = models.UUIDField(null=True, blank=True)
    action = models.CharField(max_length=12, choices=COLUMN_ACTIONS)
    params = models.JSONField(default=dict, blank=True)  # e.g. {"keep_last": 4}
    exempt_role_ids = ArrayField(models.UUIDField(), default=list, blank=True)
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["org", "dataset_id", "column_name", "principal_type", "principal_id"],
                name="column_policy_uniq",
                nulls_distinct=False,
            ),
            models.CheckConstraint(
                condition=models.Q(action__in=["deny", "mask_null", "mask_hash", "mask_partial"]),
                name="column_policy_action",
            ),
            models.CheckConstraint(
                condition=(models.Q(principal_type="everyone", principal_id__isnull=True)
                           | (models.Q(principal_type__in=["role", "user"]) & models.Q(principal_id__isnull=False))),
                name="column_policy_principal",
            ),
        ]
        indexes = [models.Index(fields=["org", "dataset_id"], name="column_policy_org_ds_idx")]


class RowPolicy(TenantModel):
    """`predicate` = {"all": [{"col": "region", "op": "in", "value": "$user.regions"}, ...]} (ANDed)."""

    dataset_id = models.UUIDField(db_index=True)
    name = models.CharField(max_length=120)
    principal_type = models.CharField(max_length=8, choices=PRINCIPAL_POLICY)
    principal_id = models.UUIDField(null=True, blank=True)
    predicate = models.JSONField()
    exempt_role_ids = ArrayField(models.UUIDField(), default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(principal_type="everyone", principal_id__isnull=True)
                           | (models.Q(principal_type__in=["role", "user"]) & models.Q(principal_id__isnull=False))),
                name="row_policy_principal",
            ),
        ]
        indexes = [models.Index(fields=["org", "dataset_id"], name="row_policy_org_ds_idx")]
