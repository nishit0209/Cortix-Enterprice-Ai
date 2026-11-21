"""Customer 360 (API_SPECIFICATION Phase 6, DATABASE_SCHEMA §10). Every model is a TenantModel with RLS."""

from decimal import Decimal

from django.db import models

from apps.core.tenancy.models import TenantModel

MATCH_STATUSES = [(s, s) for s in ("auto_merged", "pending_review", "confirmed", "rejected")]


class CustomerSettings(TenantModel):
    """Per-org identity-resolution configuration (one row per org; defaults in apps.customers.matching)."""

    auto_threshold = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.9000"))
    review_threshold = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.7000"))
    weights = models.JSONField(default=dict, blank=True)        # {email, phone, name, address, dob}
    survivorship = models.JSONField(default=dict, blank=True)   # {field: most_recent|most_frequent|...}
    default_country_code = models.CharField(max_length=4, default="91")
    features_dataset_id = models.UUIDField(null=True, blank=True)  # last "Customer 360 features" publish target

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org"], name="customer_settings_org_uniq")]


class CustomerSource(TenantModel):
    """A dataset mapped to customer fields (kind=profile) or to customer events (kind=event)."""

    KINDS = [("profile", "profile"), ("event", "event")]
    STATUS = [(s, s) for s in ("active", "error")]

    name = models.CharField(max_length=200)
    source_system = models.SlugField(max_length=64)
    kind = models.CharField(max_length=16, choices=KINDS, default="profile")
    dataset_id = models.UUIDField()
    field_mapping = models.JSONField(default=dict)   # profile: {email, phone, name|first_name+last_name, ...} -> column
    event_mapping = models.JSONField(default=dict)   # event: {customer_key, occurred_at, event_type, amount, ...}
    link_source = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    priority = models.PositiveIntegerField(default=100)  # survivorship "source_priority": lower = more trusted
    status = models.CharField(max_length=16, choices=STATUS, default="active")
    last_error = models.JSONField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_version_id = models.UUIDField(null=True, blank=True)
    record_count = models.PositiveIntegerField(default=0)
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "source_system"], name="customer_source_system_uniq")]
        indexes = [models.Index(fields=["org", "kind"], name="customer_source_kind_idx")]


class CustomerSourceRecord(TenantModel):
    source = models.ForeignKey(CustomerSource, on_delete=models.CASCADE, related_name="records")
    source_system = models.CharField(max_length=64)
    source_key = models.CharField(max_length=255)
    dataset_id = models.UUIDField(null=True, blank=True)
    attributes = models.JSONField(default=dict)   # mapped raw values
    normalized = models.JSONField(default=dict)   # matching.normalize_record(...)
    recorded_at = models.DateTimeField(null=True, blank=True)  # mapped created_at (survivorship "most_recent")
    ingested_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "source", "source_key"], name="customer_record_key_uniq")]
        indexes = [models.Index(fields=["org", "source_system", "source_key"], name="customer_record_sys_idx")]


class CustomerProfile(TenantModel):
    """Golden record of one resolved customer."""

    golden = models.JSONField(default=dict)
    survivorship = models.JSONField(default=dict)
    record_count = models.PositiveIntegerField(default=0)
    display_name = models.CharField(max_length=300, blank=True, default="")
    email = models.CharField(max_length=320, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    sources = models.JSONField(default=list)
    event_count = models.PositiveIntegerField(default=0)
    order_count = models.PositiveIntegerField(default=0)
    monetary = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0"))
    first_seen = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    last_order_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["org", "display_name"], name="customer_profile_name_idx"),
            models.Index(fields=["org", "email"], name="customer_profile_email_idx"),
        ]


class IdentityMatch(TenantModel):
    record_a = models.ForeignKey(CustomerSourceRecord, on_delete=models.CASCADE, related_name="+")
    record_b = models.ForeignKey(CustomerSourceRecord, on_delete=models.CASCADE, related_name="+")
    score = models.DecimalField(max_digits=5, decimal_places=4)  # DATABASE_SCHEMA match_probability
    features = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=MATCH_STATUSES)
    reviewed_by = models.UUIDField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "record_a", "record_b"], name="identity_match_pair_uniq")]
        indexes = [models.Index(fields=["org", "status", "-score"], name="identity_match_status_idx")]


class CustomerProfileLink(TenantModel):
    profile = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="links")
    record = models.ForeignKey(CustomerSourceRecord, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "record"], name="customer_link_record_uniq")]


class CustomerEvent(TenantModel):
    """Timeline entry (order, ticket, ...) from a mapped event dataset."""

    source = models.ForeignKey(CustomerSource, on_delete=models.CASCADE, related_name="events")
    record = models.ForeignKey(CustomerSourceRecord, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+")
    profile = models.ForeignKey(CustomerProfile, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="events")
    customer_key = models.CharField(max_length=255)
    event_key = models.CharField(max_length=255)
    event_type = models.CharField(max_length=40)
    channel = models.CharField(max_length=60, blank=True, default="")
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    occurred_at = models.DateTimeField()
    attributes = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "source", "event_key"], name="customer_event_key_uniq")]
        indexes = [models.Index(fields=["org", "profile", "-occurred_at"], name="customer_event_profile_idx")]


class CustomerSegment(TenantModel):
    METHODS = [(m, m) for m in ("rule", "rfm", "kmeans", "hierarchical")]

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    method = models.CharField(max_length=16, choices=METHODS, default="rule")
    definition = models.JSONField(default=dict)
    model_version_id = models.UUIDField(null=True, blank=True)
    size = models.PositiveIntegerField(default=0)
    created_by = models.UUIDField(null=True, blank=True)
    refreshed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "name"], name="customer_segment_name_uniq")]


class CustomerSegmentMember(TenantModel):
    segment = models.ForeignKey(CustomerSegment, on_delete=models.CASCADE, related_name="members")
    profile = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="+")
    attributes = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "segment", "profile"], name="customer_member_uniq")]


class CustomerScore(TenantModel):
    SCORE_TYPES = [(s, s) for s in ("churn", "clv", "next_best_action")]

    profile = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="scores")
    score_type = models.CharField(max_length=24, choices=SCORE_TYPES)
    value = models.JSONField(default=dict)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    explanation = models.JSONField(default=dict, blank=True)
    model_id = models.UUIDField(null=True, blank=True)
    model_version_id = models.UUIDField(null=True, blank=True)
    scored_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["org", "profile", "score_type", "-scored_at"], name="customer_score_idx")]


class ResolutionRun(TenantModel):
    """One identity-resolution job (history + concurrency guard)."""

    STATUS = [(s, s) for s in ("queued", "running", "succeeded", "failed")]

    job_id = models.UUIDField(null=True, blank=True)
    requested_by = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS, default="queued")
    stats = models.JSONField(default=dict, blank=True)
    error = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["org", "-created_at"], name="customer_resolution_run_idx")]
