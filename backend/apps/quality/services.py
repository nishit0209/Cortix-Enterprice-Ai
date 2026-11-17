"""Public service API of apps.quality. Other apps import only from here.

    ensure_suggested_rules(dataset, columns, profile)        -> list[QualityRule]  (once per dataset)
    load_rule_specs(dataset_id)                              -> list[RuleSpec]
    compute(frame, columns, profile, specs, ...)             -> QualityComputation (pure, no DB)
    persist(version, computation, run_id=None)               -> QualityReport
    evaluate_version(version, frame=None)                    -> QualityReport (all of the above)
    report_payload(version)                                  -> dict (API shape)
    create_rule / update_rule / delete_rule                  (validated, audited)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import polars as pl

from apps.core import audit
from apps.core.tenancy import require_org_id, tenant_context

from . import engine
from .duplicates import find_duplicates
from .engine import EvalContext, RuleOutcome, RuleSpec
from .models import QualityReport, QualityResult, QualityRule

KEY_NAME_HINTS = ("id", "_id", "_key", "_code", "_no")


@dataclass
class QualityComputation:
    outcomes: list[tuple[RuleSpec, RuleOutcome]]
    duplicates: dict[str, Any]
    dimensions: dict[str, float]
    score: float
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking_failures(self) -> list[str]:
        return [s.id for s, o in self.outcomes if s.is_blocking and not o.passed and s.id]


# ---------------------------------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------------------------------
def _spec(rule: QualityRule) -> RuleSpec:
    return RuleSpec(
        rule_type=rule.rule_type, column=rule.column_name, params=rule.params or {}, severity=rule.severity,
        is_blocking=rule.is_blocking, id=str(rule.id),
    )


def load_rule_specs(dataset_id) -> list[RuleSpec]:
    return [_spec(r) for r in QualityRule.objects.filter(dataset_id=dataset_id, is_active=True).order_by("created_at")]


def ensure_suggested_rules(dataset, columns: Sequence[Mapping[str, Any]], profile: Mapping[str, Any]) -> list[QualityRule]:
    """Create profile-based suggestions the first time a dataset gets a ready version."""
    if QualityRule.objects.filter(dataset_id=dataset.id, origin="suggested_profile").exists():
        return []
    created = []
    for spec in engine.suggest_rules(columns, profile):
        created.append(
            QualityRule.objects.create(
                dataset_id=dataset.id, column_name=spec.column, rule_type=spec.rule_type, params=dict(spec.params),
                severity=spec.severity, is_blocking=False, is_active=True, origin="suggested_profile",
                description=f"Suggested from the profile of version {profile.get('version', 1)}",
            )
        )
    return created


def create_rule(dataset, *, column_types: Mapping[str, str], data: Mapping[str, Any], user_id=None, request=None) -> QualityRule:
    spec = RuleSpec(
        rule_type=data.get("rule_type", ""), column=data.get("column") or None, params=data.get("params") or {},
        severity=data.get("severity", "warn"), is_blocking=bool(data.get("is_blocking", False)),
    )
    params = engine.validate_rule(spec, column_types)
    rule = QualityRule.objects.create(
        dataset_id=dataset.id, column_name=spec.column, rule_type=spec.rule_type, params=params,
        severity=spec.severity, is_blocking=spec.is_blocking, is_active=bool(data.get("is_active", True)),
        origin="manual", description=str(data.get("description") or "")[:300], created_by=user_id,
    )
    audit.record("quality_rule.create", target=rule, request=request,
                 metadata={"dataset_id": str(dataset.id), "rule_type": rule.rule_type, "column": rule.column_name})
    return rule


def update_rule(rule: QualityRule, *, column_types: Mapping[str, str], data: Mapping[str, Any], request=None) -> QualityRule:
    changes: dict[str, Any] = {}
    for fld in ("severity", "is_blocking", "is_active", "description"):
        if fld in data:
            changes[fld] = data[fld]
    if "params" in data or "column" in data:
        spec = RuleSpec(
            rule_type=rule.rule_type, column=data.get("column", rule.column_name), params=data.get("params", rule.params),
            severity=data.get("severity", rule.severity),
        )
        changes["params"] = engine.validate_rule(spec, column_types)
        changes["column_name"] = spec.column
    elif "severity" in changes and changes["severity"] not in engine.SEVERITY_WEIGHTS:
        raise engine.RuleValidationError({"severity": "Must be info, warn or error."})
    before = {k: getattr(rule, k) for k in changes}
    for k, v in changes.items():
        setattr(rule, k, v)
    rule.save()
    audit.record("quality_rule.update", target=rule, request=request,
                 changes={"before": before, "after": {k: getattr(rule, k) for k in changes}})
    return rule


def delete_rule(rule: QualityRule, *, request=None) -> None:
    audit.record("quality_rule.delete", target=rule, request=request,
                 metadata={"rule_type": rule.rule_type, "column": rule.column_name})
    rule.delete()


# ---------------------------------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------------------------------
def _key_columns(columns: Sequence[Mapping[str, Any]], profile: Mapping[str, Any], specs: Sequence[RuleSpec]) -> list[str]:
    keys: list[str] = []
    candidate = set(profile.get("candidate_keys") or [])
    unique_rule_cols = {s.column for s in specs if s.rule_type == "unique" and s.column}
    for c in columns:
        name = c["name"]
        if c["data_type"] in ("jsonb", "boolean", "double precision"):
            continue
        is_named_key = name == "id" or name.endswith(KEY_NAME_HINTS[1:])
        if c.get("semantic_type") == "id" or name in unique_rule_cols or (is_named_key and name in candidate):
            keys.append(name)
        elif is_named_key and (c.get("stats") or {}).get("distinct", 0) >= 0.9 * max(1, profile.get("row_count") or 1):
            keys.append(name)  # mostly-unique key columns: duplicates here are exactly what to report
    return keys


def _reference_loader(org_id: UUID):
    def load(dataset_id: UUID, column: str) -> pl.Series:
        from apps.datasets import services as dataset_services

        with tenant_context(org_id):
            frame = dataset_services.load_current_frame(dataset_id, columns=[column])
        return frame[column]

    return load


def compute(
    frame: pl.DataFrame,
    columns: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    specs: Sequence[RuleSpec],
    *,
    org_id: UUID | None = None,
) -> QualityComputation:
    from apps.datasets import services as dataset_services

    column_types = {c["name"]: c["data_type"] for c in columns}
    ctx = EvalContext(
        column_types=column_types,
        pii_columns={c["name"] for c in columns if c.get("is_pii")},
        reference_values=_reference_loader(org_id) if org_id else None,
        type_parser=dataset_services.parse_as_type,
    )
    outcomes = [(s, engine.evaluate_rule(frame, s, ctx)) for s in specs]
    duplicates = find_duplicates(frame, columns, key_columns=_key_columns(columns, profile, specs))
    dimensions = engine.compute_dimensions(
        frame, column_types, outcomes, duplicates, invalid_cells=int(profile.get("invalid_cells") or 0)
    )
    score = engine.compute_score(dimensions, outcomes)
    summary = {
        "rules_total": len(outcomes),
        "rules_passed": sum(1 for _, o in outcomes if o.passed),
        "rules_failed": sum(1 for _, o in outcomes if not o.passed),
        "rules_errored": sum(1 for _, o in outcomes if o.error),
        "blocking_failures": [s.id for s, o in outcomes if s.is_blocking and not o.passed and s.id],
        "row_count": frame.height,
    }
    return QualityComputation(outcomes=outcomes, duplicates=duplicates, dimensions=dimensions, score=score, summary=summary)


def persist(version, computation: QualityComputation, *, run_id=None) -> QualityReport:
    org_id = require_org_id()  # bulk_create bypasses TenantModel.save(): set the org explicitly
    results = []
    for spec, outcome in computation.outcomes:
        if not spec.id:
            continue
        results.append(
            QualityResult(
                org_id=org_id, rule_id=spec.id, dataset_version_id=version.id, run_id=run_id, passed=outcome.passed,
                evaluated_count=outcome.evaluated_count, failed_count=outcome.failed_count,
                sample_failures=outcome.sample_failures or None, error=outcome.error[:300],
            )
        )
    QualityResult.objects.bulk_create(results)
    report, _ = QualityReport.objects.update_or_create(
        dataset_version_id=version.id,
        defaults={
            "score": computation.score,
            "dimensions": computation.dimensions,
            "duplicates": computation.duplicates,
            "summary": computation.summary,
        },
    )
    return report


def evaluate_version(version, frame: pl.DataFrame | None = None, *, run_id=None) -> QualityReport:
    """Re-evaluate the dataset's active rules against a version (reads the warehouse table if needed)."""
    from apps.datasets import services as dataset_services

    org_id = require_org_id()
    columns = dataset_services.version_columns(version)
    if frame is None:
        frame = dataset_services.load_version_frame(version)
    specs = load_rule_specs(version.dataset_id)
    computation = compute(frame, columns, version.profile or {}, specs, org_id=org_id)
    return persist(version, computation, run_id=run_id)


# ---------------------------------------------------------------------------------------------------
# read models
# ---------------------------------------------------------------------------------------------------
def latest_results(version_id) -> dict[str, QualityResult]:
    latest: dict[str, QualityResult] = {}
    for r in QualityResult.objects.filter(dataset_version_id=version_id).order_by("rule_id", "-measured_at"):
        latest.setdefault(str(r.rule_id), r)
    return latest


def report_payload(version) -> dict[str, Any]:
    if version is None:
        return {"status": "pending", "score": None, "dimensions": {}, "rules": [],
                "duplicates": {"exact_rows": None, "key_duplicates": None, "fuzzy_clusters": []}}
    report = QualityReport.objects.filter(dataset_version_id=version.id).first()
    results = latest_results(version.id)
    rules = []
    for rule in QualityRule.objects.filter(dataset_id=version.dataset_id).order_by("created_at"):
        res = results.get(str(rule.id))
        rules.append({
            "id": str(rule.id), "column": rule.column_name, "rule_type": rule.rule_type, "severity": rule.severity,
            "is_blocking": rule.is_blocking, "is_active": rule.is_active, "origin": rule.origin,
            "passed": None if res is None else res.passed,
            "evaluated_count": None if res is None else res.evaluated_count,
            "failed_count": None if res is None else res.failed_count,
            "sample_failures": [] if res is None else (res.sample_failures or []),
            "error": "" if res is None else res.error,
        })
    if report is None:
        return {"status": "pending", "version_id": str(version.id), "score": None, "dimensions": {}, "rules": rules,
                "duplicates": {"exact_rows": None, "key_duplicates": None, "fuzzy_clusters": []}}
    dup = report.duplicates or {}
    return {
        "status": "ready",
        "version_id": str(version.id),
        "generated_at": report.generated_at.isoformat(),
        "score": float(report.score),
        "dimensions": report.dimensions,
        "rules": rules,
        "duplicates": {
            "exact_rows": dup.get("exact_rows", 0),
            "key_duplicates": dup.get("key_duplicates", 0),
            "key_columns": dup.get("key_columns", []),
            "fuzzy_clusters": dup.get("fuzzy_clusters", []),
        },
        "summary": report.summary,
    }


def rule_payload(rule: QualityRule, last: QualityResult | None = None) -> dict[str, Any]:
    return {
        "id": str(rule.id), "column": rule.column_name, "rule_type": rule.rule_type, "params": rule.params,
        "severity": rule.severity, "is_blocking": rule.is_blocking, "is_active": rule.is_active, "origin": rule.origin,
        "description": rule.description, "created_at": rule.created_at.isoformat(),
        "last_result": None if last is None else {
            "passed": last.passed, "evaluated_count": last.evaluated_count, "failed_count": last.failed_count,
            "measured_at": last.measured_at.isoformat(), "dataset_version_id": str(last.dataset_version_id),
        },
    }


def version_score(version_id) -> float | None:
    report = QualityReport.objects.filter(dataset_version_id=version_id).first()
    return float(report.score) if report else None
