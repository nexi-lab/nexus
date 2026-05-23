#!/usr/bin/env python3
"""Validate docs/surface-coverage/api-rpc-surface-coverage.yaml."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.surface_coverage.paths import COVERAGE_YAML, GAPS_YAML, REPO_ROOT  # noqa: E402
from scripts.surface_coverage.schema import load_yaml  # noqa: E402
from scripts.surface_coverage.validate import (  # noqa: E402
    ValidationFinding,
    format_findings,
    load_curated_missing_operation_ids,
    validate_coverage,
)


def _default_path_for_repo(path: Path, repo_root: Path) -> Path:
    try:
        relative = path.relative_to(REPO_ROOT)
    except ValueError:
        return path
    return repo_root / relative


def validate_file(
    *,
    path: Path,
    repo_root: Path,
    check_references: bool = True,
    gaps_path: Path | None = None,
) -> list[ValidationFinding]:
    coverage = load_yaml(path)
    resolved_gaps_path = gaps_path or _default_path_for_repo(GAPS_YAML, repo_root)
    return validate_coverage(
        coverage,
        repo_root=repo_root,
        check_references=check_references,
        curated_missing_operation_ids=load_curated_missing_operation_ids(resolved_gaps_path),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--coverage",
        type=Path,
        default=COVERAGE_YAML,
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--gaps",
        type=Path,
        default=None,
        help="curated missing-surface backlog to align with all-profile missing_needed rows",
    )
    parser.add_argument(
        "--skip-reference-checks",
        action="store_true",
        help="skip path existence checks for correctness_test and perf_link",
    )
    args = parser.parse_args(argv)

    findings = validate_file(
        path=args.coverage,
        repo_root=args.repo_root,
        check_references=not args.skip_reference_checks,
        gaps_path=args.gaps,
    )
    errors = [finding for finding in findings if finding.severity == "error"]
    if errors:
        print(format_findings(errors), file=sys.stderr)
        return 1
    if findings:
        print(format_findings(findings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
