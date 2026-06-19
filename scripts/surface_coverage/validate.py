"""Validation rules for the committed API/RPC surface coverage matrix."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from scripts.surface_coverage.schema import (
    PROFILE_KEYS,
    Operation,
    PerfClass,
    ProfileStatus,
    SurfaceCoverage,
)


@dataclass(frozen=True)
class ValidationFinding:
    """A deterministic matrix validation finding."""

    code: str
    operation_id: str | None
    field: str | None
    severity: str
    message: str


_GAP_REQUIRED_STATUSES = {
    ProfileStatus.MISSING_NEEDED,
    ProfileStatus.UNAVAILABLE,
    ProfileStatus.DEPRECATED,
}

_CODE_SORT_ORDER = {
    "missing_profile_status": 0,
    "supported_missing_owner": 1,
    "supported_missing_test": 2,
    "supported_missing_perf_class": 3,
    "supported_missing_perf_link": 4,
    "gap_missing_issue": 5,
    "missing_needed_not_in_gap_backlog": 6,
    "implemented_surface_marked_missing_needed": 7,
}

_REPO_REFERENCE_RE = re.compile(
    r"(?P<path>(?:tests|src|scripts|docs|benchmarks|proto|rust)/[A-Za-z0-9_./-]+)(?::\d+)?"
)


def validate_coverage(
    coverage: SurfaceCoverage,
    *,
    repo_root: Path,
    check_references: bool = True,
    curated_missing_operation_ids: set[str] | None = None,
) -> list[ValidationFinding]:
    """Validate the committed coverage matrix and return sorted findings."""

    findings: list[ValidationFinding] = []
    for op in coverage.operations:
        findings.extend(_validate_profile_keys(op))
        findings.extend(_validate_supported_completeness(op))
        findings.extend(_validate_gap_policy(op))
        findings.extend(_validate_missing_needed_backlog(op, curated_missing_operation_ids))
        if check_references:
            findings.extend(_validate_references(op, repo_root=repo_root))

    return sorted(findings, key=_finding_sort_key)


def load_curated_missing_operation_ids(gaps_path: Path) -> set[str]:
    """Load operation ids from the curated missing-surface backlog."""

    if not gaps_path.exists():
        return set()

    import yaml

    doc = yaml.safe_load(gaps_path.read_text(encoding="utf-8")) or {}
    return {str(entry["id"]) for entry in doc.get("missing_operations", [])}


def _validate_profile_keys(op: Operation) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for profile in PROFILE_KEYS:
        if profile not in op.profiles:
            findings.append(
                ValidationFinding(
                    code="missing_profile_status",
                    operation_id=op.id,
                    field=f"profiles.{profile}",
                    severity="error",
                    message=f"operation {op.id} is missing profile status {profile}",
                )
            )
    return findings


def _validate_supported_completeness(op: Operation) -> list[ValidationFinding]:
    if not _has_supported_profile(op):
        return []

    checks = (
        (
            "owning_issue",
            op.owning_issue,
            "supported_missing_owner",
            "supported row needs an owning issue",
        ),
        (
            "correctness_test",
            op.correctness_test,
            "supported_missing_test",
            "supported row needs a correctness test link",
        ),
        (
            "perf_class",
            op.perf_class,
            "supported_missing_perf_class",
            "supported row needs a performance class",
        ),
        (
            "perf_link",
            op.perf_link,
            "supported_missing_perf_link",
            "supported row needs performance evidence or rationale",
        ),
    )
    findings: list[ValidationFinding] = []
    for field, value, code, message in checks:
        if _is_missing_value(value):
            findings.append(
                ValidationFinding(
                    code=code,
                    operation_id=op.id,
                    field=field,
                    severity="error",
                    message=message,
                )
            )
    return findings


def _is_missing_value(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _validate_gap_policy(op: Operation) -> list[ValidationFinding]:
    if (
        any(status in _GAP_REQUIRED_STATUSES for status in op.profiles.values())
        and op.gap_issue is None
    ):
        return [
            ValidationFinding(
                code="gap_missing_issue",
                operation_id=op.id,
                field="gap_issue",
                severity="error",
                message="missing-needed, unavailable, or deprecated row needs a linked gap issue",
            )
        ]
    return []


def _validate_missing_needed_backlog(
    op: Operation,
    curated_missing_operation_ids: set[str] | None,
) -> list[ValidationFinding]:
    if not _has_all_missing_needed_profiles(op):
        return []

    findings: list[ValidationFinding] = []
    if op.transports:
        findings.append(
            ValidationFinding(
                code="implemented_surface_marked_missing_needed",
                operation_id=op.id,
                field="profiles",
                severity="error",
                message=(
                    "operation has extracted transports but every profile is still "
                    "marked missing_needed"
                ),
            )
        )

    if curated_missing_operation_ids is not None and op.id not in curated_missing_operation_ids:
        findings.append(
            ValidationFinding(
                code="missing_needed_not_in_gap_backlog",
                operation_id=op.id,
                field="gap_issue",
                severity="error",
                message="all-profile missing_needed row is not present in api-rpc-surface-gaps.yaml",
            )
        )

    return findings


def _validate_references(op: Operation, *, repo_root: Path) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if op.correctness_test and not _repo_references_exist(op.correctness_test, repo_root):
        findings.append(
            ValidationFinding(
                code="invalid_test_reference",
                operation_id=op.id,
                field="correctness_test",
                severity="error",
                message=f"correctness_test path does not exist: {op.correctness_test}",
            )
        )

    if (
        op.perf_class in {PerfClass.HOT, PerfClass.HOT_PATH}
        and op.perf_link
        and not _repo_references_exist(op.perf_link, repo_root)
    ):
        findings.append(
            ValidationFinding(
                code="invalid_perf_reference",
                operation_id=op.id,
                field="perf_link",
                severity="error",
                message=(
                    "hot-path perf_link must reference an existing benchmark or "
                    f"guardrail: {op.perf_link}"
                ),
            )
        )
    return findings


def _repo_references_exist(reference: str, repo_root: Path) -> bool:
    paths = _extract_repo_references(reference)
    return bool(paths) and all((repo_root / path).exists() for path in paths)


def _extract_repo_references(reference: str) -> list[str]:
    return [match.group("path") for match in _REPO_REFERENCE_RE.finditer(reference)]


def _has_supported_profile(op: Operation) -> bool:
    return any(status == ProfileStatus.SUPPORTED for status in op.profiles.values())


def _has_all_missing_needed_profiles(op: Operation) -> bool:
    return bool(op.profiles) and all(
        status == ProfileStatus.MISSING_NEEDED for status in op.profiles.values()
    )


def _finding_sort_key(finding: ValidationFinding) -> tuple[str, str, str]:
    # Task 2 adds more codes; unlisted codes sort after known ones by code name.
    return (
        finding.operation_id or "",
        f"{_CODE_SORT_ORDER.get(finding.code, 999):03d}:{finding.code}",
        finding.field or "",
    )


def format_findings(findings: list[ValidationFinding]) -> str:
    """Render findings for assertion failure output."""

    if not findings:
        return "no validation findings"
    return "\n".join(
        f"[{finding.severity}] {finding.operation_id or '-'} "
        f"{finding.code} {finding.field or '-'}: {finding.message}"
        for finding in findings
    )
