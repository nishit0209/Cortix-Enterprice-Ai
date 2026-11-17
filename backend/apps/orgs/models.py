from django.conf import settings
from django.db import models

from apps.core.tenancy.models import TenantModel, uuid7

AI_DATA_POLICIES = [
    ("synthetic_only", "Synthetic only"),
    ("schema_only", "Schema only"),
    ("aggregates_only", "Aggregates only"),
    ("full", "Full"),
]


class Organization(models.Model):
    """Global table with a self-policy: the app role sees only the org in app.org_id."""

    STATUS = [("active", "active"), ("suspended", "suspended"), ("deleted", "deleted")]
    PLANS = [("free", "free"), ("team", "team"), ("enterprise", "enterprise")]

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=50, unique=True)
    short_id = models.CharField(max_length=8, unique=True)
    status = models.CharField(max_length=16, choices=STATUS, default="active")
    plan = models.CharField(max_length=16, choices=PLANS, default="free")
    region = models.CharField(max_length=16, default="in")
    ai_data_policy = models.CharField(
        max_length=20, choices=AI_DATA_POLICIES, default="synthetic_only", db_default="synthetic_only"
    )
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ai_data_policy__in=[p for p, _ in AI_DATA_POLICIES]), name="org_ai_policy_valid"
            )
        ]

    def __str__(self) -> str:
        return self.slug


class Permission(models.Model):
    """Global permission catalog (seeded from permissions_catalog.PERMISSIONS)."""

    codename = models.CharField(max_length=60, unique=True)
    description = models.CharField(max_length=200)

    def __str__(self) -> str:
        return self.codename


class Role(TenantModel):
    key = models.CharField(max_length=40)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=200, blank=True, default="")
    is_system = models.BooleanField(default=False)
    permissions = models.ManyToManyField(Permission, through="RolePermission", related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "key"], name="role_org_key_uniq")]

    def __str__(self) -> str:
        return self.key


class RolePermission(TenantModel):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "role", "permission"], name="roleperm_org_role_perm_uniq")
        ]


class Membership(TenantModel):
    STATUS = [("active", "active"), ("suspended", "suspended")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.ForeignKey(Role, on_delete=models.RESTRICT, related_name="memberships")
    status = models.CharField(max_length=16, choices=STATUS, default="active")
    invited_by = models.UUIDField(null=True, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "user"], name="membership_org_user_uniq")]
