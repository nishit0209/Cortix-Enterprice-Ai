"""Global user model (no org_id: users can belong to several organizations via memberships)."""

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from apps.core.tenancy.models import TenantModel, uuid7


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email: str, password: str | None = None, **extra):
        if not email:
            raise ValueError("email is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_platform_admin(self, email: str, password: str, **extra):
        extra.setdefault("is_platform_admin", True)
        return self.create_user(email, password, **extra)

    def get_by_natural_key(self, username):
        return self.get(email__iexact=username)


class User(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    email = models.EmailField(max_length=254, unique=True)  # stored lower-cased by the manager
    full_name = models.CharField(max_length=150, blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_platform_admin = models.BooleanField(default=False)
    mfa_enabled = models.BooleanField(default=False)
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    locale = models.CharField(max_length=16, default="en")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_uniq")]

    def __str__(self) -> str:
        return self.email

    # Minimal compatibility with Django's permission API (Nexora uses org-scoped RBAC instead)
    @property
    def is_staff(self) -> bool:
        return self.is_platform_admin

    @property
    def is_superuser(self) -> bool:
        return False

    def has_perm(self, perm, obj=None) -> bool:
        return False

    def has_module_perms(self, app_label) -> bool:
        return False


class ApiKey(TenantModel):
    """Org-bound programmatic credential: `nxk_<prefix>_<secret>`.

    Only `prefix` (public, globally unique, used for lookup) and `key_hash`
    (HMAC-SHA256(secret, settings.API_KEY_PEPPER)) are stored; the secret is shown once at creation.
    The key acts as its creator, restricted to `scopes` ∩ the creator's current role permissions.
    """

    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=8, unique=True)
    key_hash = models.BinaryField()
    scopes = ArrayField(models.CharField(max_length=60), default=list)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["org", "-created_at"], name="apikey_org_time_idx")]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix})"

    @property
    def status(self) -> str:
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return "expired"
        return "active"
