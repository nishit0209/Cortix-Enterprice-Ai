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


class Invitation(TenantModel):
    """Pending invitation. Only sha256(token) is stored; the token travels in the email link only."""

    email = models.EmailField(max_length=254)  # stored lower-cased
    role = models.ForeignKey(Role, on_delete=models.RESTRICT, related_name="+")
    token_hash = models.BinaryField(unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.UUIDField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.UUIDField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["org", "email"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="invitation_org_email_pending_uniq",
            )
        ]
        indexes = [models.Index(fields=["org", "-created_at"], name="invitation_org_time_idx")]

    @property
    def status(self) -> str:
        from django.utils import timezone

        if self.accepted_at is not None:
            return "accepted"
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= timezone.now():
            return "expired"
        return "pending"


CONSENT_POLICIES = [p for p in AI_DATA_POLICIES if p[0] != "synthetic_only"]


class AIPolicyConsent(TenantModel):
    """Append-only consent ledger (UPDATE/DELETE blocked by trigger + revoked grants).

    A policy above synthetic_only is effective only while the LATEST row for (org, policy) is a
    `grant` for the CURRENT consent text version (and the Gemini tier allows the policy).
    """

    ACTIONS = [("grant", "grant"), ("revoke", "revoke")]
    TIERS = [("free", "free"), ("paid", "paid"), ("vertex", "vertex")]

    action = models.CharField(max_length=8, choices=ACTIONS)
    policy = models.CharField(max_length=20, choices=CONSENT_POLICIES)
    consent_text_version = models.CharField(max_length=40)
    gemini_tier = models.CharField(max_length=8, choices=TIERS)
    actor_id = models.UUIDField()
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["org", "policy", "-created_at"], name="aiconsent_org_policy_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(action__in=["grant", "revoke"]), name="aiconsent_action_valid"),
            models.CheckConstraint(
                condition=models.Q(policy__in=[p for p, _ in CONSENT_POLICIES]), name="aiconsent_policy_valid"
            ),
        ]
