from __future__ import annotations

from rest_framework import serializers

from apps.orgs.models import Membership, Role

from .models import ColumnPolicy, DatasetAcl, RetentionPolicy, RowPolicy
from .services import COLUMN_RE, validate_predicate


def _validate_principal(principal_type: str, principal_id, *, allow_everyone: bool) -> None:
    if principal_type == "everyone":
        if not allow_everyone:
            raise serializers.ValidationError({"principal_type": ["Must be role or user."]})
        if principal_id is not None:
            raise serializers.ValidationError({"principal_id": ["Must be empty for principal_type=everyone."]})
        return
    if principal_id is None:
        raise serializers.ValidationError({"principal_id": ["This field is required."]})
    if principal_type == "role" and not Role.objects.filter(id=principal_id).exists():
        raise serializers.ValidationError({"principal_id": ["Unknown role."]})
    if principal_type == "user" and not Membership.objects.filter(user_id=principal_id).exists():
        raise serializers.ValidationError({"principal_id": ["This user is not a member of the organization."]})


def _validate_exempt_roles(role_ids) -> list:
    role_ids = list(dict.fromkeys(role_ids or []))
    if role_ids:
        found = set(Role.objects.filter(id__in=role_ids).values_list("id", flat=True))
        missing = [str(r) for r in role_ids if r not in found]
        if missing:
            raise serializers.ValidationError({"exempt_role_ids": [f"Unknown role(s): {', '.join(missing)}."]})
    return role_ids


class DatasetAclSerializer(serializers.ModelSerializer):
    class Meta:
        model = DatasetAcl
        fields = ["id", "dataset_id", "principal_type", "principal_id", "privilege", "created_by", "created_at"]
        read_only_fields = ["id", "dataset_id", "created_by", "created_at"]

    def validate(self, attrs):
        _validate_principal(attrs["principal_type"], attrs.get("principal_id"), allow_everyone=False)
        return attrs


class ColumnPolicySerializer(serializers.ModelSerializer):
    exempt_role_ids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=50)
    params = serializers.DictField(required=False)

    class Meta:
        model = ColumnPolicy
        fields = ["id", "dataset_id", "column_name", "principal_type", "principal_id", "action", "params",
                  "exempt_role_ids", "created_by", "created_at", "updated_at"]
        read_only_fields = ["id", "dataset_id", "created_by", "created_at", "updated_at"]
        validators: list = []  # uniqueness is enforced below (scoped to the dataset) and by the DB

    def validate_column_name(self, value: str) -> str:
        if not COLUMN_RE.match(value):
            raise serializers.ValidationError("Invalid column name.")
        return value

    def validate(self, attrs):
        inst = self.instance
        ptype = attrs.get("principal_type", inst.principal_type if inst else None)
        pid = attrs.get("principal_id", inst.principal_id if inst else None)
        _validate_principal(ptype, pid, allow_everyone=True)
        action = attrs.get("action", inst.action if inst else None)
        params = attrs.get("params", inst.params if inst else {}) or {}
        if action == "mask_partial":
            keep = params.get("keep_last", 4)
            if not isinstance(keep, int) or isinstance(keep, bool) or not 0 <= keep <= 64:
                raise serializers.ValidationError({"params": ["keep_last must be an integer between 0 and 64."]})
            attrs["params"] = {"keep_last": keep}
        elif params:
            raise serializers.ValidationError({"params": [f"{action} takes no parameters."]})
        else:
            attrs["params"] = {}
        if "exempt_role_ids" in attrs:
            attrs["exempt_role_ids"] = _validate_exempt_roles(attrs["exempt_role_ids"])
        column = attrs.get("column_name", inst.column_name if inst else None)
        dup = ColumnPolicy.objects.filter(dataset_id=self.context["dataset_id"], column_name=column,
                                          principal_type=ptype, principal_id=pid)
        if inst is not None:
            dup = dup.exclude(id=inst.id)
        if dup.exists():
            raise serializers.ValidationError(
                {"column_name": ["A policy for this column and principal already exists."]}, code="duplicate"
            )
        return attrs


class RowPolicySerializer(serializers.ModelSerializer):
    exempt_role_ids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=50)

    class Meta:
        model = RowPolicy
        fields = ["id", "dataset_id", "name", "principal_type", "principal_id", "predicate", "exempt_role_ids",
                  "is_active", "created_by", "created_at", "updated_at"]
        read_only_fields = ["id", "dataset_id", "created_by", "created_at", "updated_at"]

    def validate_predicate(self, value):
        try:
            return validate_predicate(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        inst = self.instance
        ptype = attrs.get("principal_type", inst.principal_type if inst else None)
        pid = attrs.get("principal_id", inst.principal_id if inst else None)
        _validate_principal(ptype, pid, allow_everyone=True)
        if "exempt_role_ids" in attrs:
            attrs["exempt_role_ids"] = _validate_exempt_roles(attrs["exempt_role_ids"])
        return attrs


class UserAttributesSerializer(serializers.Serializer):
    attributes = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField(max_length=200, allow_blank=False), max_length=500),
        allow_empty=True,
    )

    def validate_attributes(self, value: dict) -> dict:
        from .services import BUILTIN_ATTRS, USER_REF

        if len(value) > 50:
            raise serializers.ValidationError("At most 50 attributes per user.")
        for key in value:
            if not USER_REF.match(f"$user.{key}"):
                raise serializers.ValidationError(f"Invalid attribute key {key!r} (use lowercase snake_case).")
            if key in BUILTIN_ATTRS:
                raise serializers.ValidationError(f"{key!r} is a reserved built-in attribute.")
        return {k: list(dict.fromkeys(v)) for k, v in value.items()}


class PolicyPreviewSerializer(serializers.Serializer):
    user_id = serializers.UUIDField()
    tables = serializers.DictField(child=serializers.CharField(max_length=200), required=False)
    columns_by_table = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField(max_length=128), max_length=2000), required=False
    )
    privilege = serializers.ChoiceField(choices=["read", "query", "export", "admin"], default="query")
    # W4: also run a small sample query AS the member (impersonated policy AND the caller's own policy)
    sample = serializers.BooleanField(default=False)
    limit = serializers.IntegerField(min_value=1, max_value=50, default=20)

    def validate(self, attrs):
        if attrs.get("sample") and "tables" in attrs:
            raise serializers.ValidationError({"sample": ["A sample runs on the dataset's own tables; omit tables."]})
        if ("tables" in attrs) != ("columns_by_table" in attrs):
            raise serializers.ValidationError("Provide both tables and columns_by_table, or neither.")
        if "tables" in attrs and set(attrs["columns_by_table"]) - set(attrs["tables"]):
            raise serializers.ValidationError({"columns_by_table": ["Keys must be logical tables listed in tables."]})
        return attrs


class RetentionPolicySerializer(serializers.ModelSerializer):
    """`target` must be a registered retention target; `mode=enforce` needs a completed dry run first (409)."""

    class Meta:
        model = RetentionPolicy
        fields = ["id", "name", "target", "retain_days", "action", "mode", "is_active", "last_dry_run_at",
                  "last_dry_run", "last_applied_at", "last_result", "created_by", "created_at", "updated_at"]
        read_only_fields = ["id", "last_dry_run_at", "last_dry_run", "last_applied_at", "last_result", "created_by",
                            "created_at", "updated_at"]
        validators: list = []  # the (org, target) uniqueness is answered as 409 by the view

    def validate_retain_days(self, value: int) -> int:
        from .retention import MAX_RETAIN_DAYS

        if not 1 <= value <= MAX_RETAIN_DAYS:
            raise serializers.ValidationError(f"Must be between 1 and {MAX_RETAIN_DAYS} days.")
        return value

    def validate_target(self, value: str) -> str:
        from .retention import NEVER_TARGETS, get_target

        if self.instance is not None and value != self.instance.target:
            raise serializers.ValidationError("The target of a policy cannot be changed; create a new policy.")
        if value in NEVER_TARGETS:
            raise serializers.ValidationError("Append-only evidence (audit, access history, query audit, AI logs) "
                                              "is never deleted by retention.")
        if get_target(value) is None:
            raise serializers.ValidationError("Unsupported retention target.")
        return value

    def validate(self, attrs):
        from .retention import get_target

        inst = self.instance
        target = get_target(attrs.get("target", inst.target if inst else ""))
        action = attrs.get("action", inst.action if inst else "delete")
        if target is not None and action not in target.actions:
            raise serializers.ValidationError({"action": [f"{target.key} supports: {', '.join(target.actions)}."]})
        return attrs
