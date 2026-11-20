from django.db import models

from apps.core.tenancy.models import TenantModel

SOURCE_TYPES = [("postgresql", "postgresql")]


class Credential(TenantModel):
    """An envelope-encrypted secret (AES-256-GCM, DEK wrapped by the KEK; AAD 'org:<id>:credential:<id>').

    Never serialized by any API and never logged. Only `apps.connectors.services` decrypts it, inside workers.
    """

    KINDS = [(k, k) for k in ("db_password", "api_token")]

    kind = models.CharField(max_length=20, choices=KINDS, default="db_password")
    blob = models.JSONField()                      # {"v","kek_id","ct","nonce","wdek","wnonce"} from core.crypto
    kek_id = models.CharField(max_length=40)
    aad = models.CharField(max_length=200)
    rotated_at = models.DateTimeField(null=True, blank=True)

    def __repr__(self) -> str:
        return f"<Credential {self.pk} kind={self.kind}>"

    def __str__(self) -> str:
        return self.__repr__()


class DataSource(TenantModel):
    STATUS = [(s, s) for s in ("pending", "active", "error", "disabled")]

    name = models.CharField(max_length=200)
    type = models.CharField(max_length=20, choices=SOURCE_TYPES)
    config = models.JSONField(default=dict)        # non-secret: host, port, dbname, sslmode, schema
    username = models.CharField(max_length=128, blank=True, default="")
    credential = models.ForeignKey(Credential, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=16, choices=STATUS, default="pending")
    verified_readonly = models.BooleanField(default=False)
    resolved_ip = models.GenericIPAddressField(null=True, blank=True)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_result = models.JSONField(null=True, blank=True)
    last_error = models.JSONField(null=True, blank=True)   # {code, message}
    last_job_id = models.UUIDField(null=True, blank=True)
    catalog = models.JSONField(null=True, blank=True)      # last introspection (schemas -> tables -> columns)
    introspected_at = models.DateTimeField(null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "name"], name="datasource_org_name_uniq")]
        indexes = [models.Index(fields=["org", "-created_at"], name="datasource_org_time_idx")]

    def __str__(self) -> str:
        return self.name


class SourceImport(TenantModel):
    """One import of an external table into a dataset (full, or incremental by a cursor column)."""

    STATUS = [(s, s) for s in ("queued", "running", "succeeded", "failed")]
    MODES = [(m, m) for m in ("full", "incremental")]

    data_source = models.ForeignKey(DataSource, on_delete=models.CASCADE, related_name="imports")
    schema_name = models.CharField(max_length=63)
    table_name = models.CharField(max_length=63)
    columns = models.JSONField(null=True, blank=True)
    mode = models.CharField(max_length=16, choices=MODES, default="full")
    cursor_column = models.CharField(max_length=63, blank=True, default="")
    cursor_value = models.TextField(blank=True, default="")      # last imported cursor value (text form)
    dataset_id = models.UUIDField(null=True, blank=True)
    dataset_name = models.CharField(max_length=200)
    classification = models.CharField(max_length=16, default="internal")
    status = models.CharField(max_length=16, choices=STATUS, default="queued")
    rows = models.BigIntegerField(null=True, blank=True)
    job_id = models.UUIDField(null=True, blank=True)
    ingest_job_id = models.UUIDField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["org", "data_source", "-created_at"], name="srcimport_org_src_idx"),
            models.Index(fields=["org", "data_source", "schema_name", "table_name"], name="srcimport_org_tbl_idx"),
        ]
