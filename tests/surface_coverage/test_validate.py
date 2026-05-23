"""Validation rules for the API/RPC surface coverage matrix."""

from pathlib import Path

from scripts.surface_coverage.schema import (
    Operation,
    PerfClass,
    ProfileStatus,
    SurfaceCoverage,
    TransportCell,
)
from scripts.surface_coverage.validate import (
    ValidationFinding,
    format_findings,
    validate_coverage,
)


def _coverage(*ops: Operation) -> SurfaceCoverage:
    return SurfaceCoverage(schema_version=1, modules=[], operations=list(ops))


def _op(
    op_id: str = "filesystem.read",
    *,
    transports: dict[str, TransportCell] | None = None,
    profiles: dict[str, ProfileStatus] | None = None,
    owning_issue: int | None = 4123,
    correctness_test: str | None = "tests/surface_coverage/test_validate.py",
    perf_class: PerfClass | None = PerfClass.NOT_PERF_SENSITIVE,
    perf_link: str | None = "control-plane inventory check; not on request path",
    gap_issue: int | None = None,
) -> Operation:
    return Operation(
        id=op_id,
        module=op_id.split(".", 1)[0],
        summary="surface row",
        transports=transports
        if transports is not None
        else {
            "cli": TransportCell(name="nexus read", source="src/nexus/cli/commands/__init__.py:1")
        },
        profiles=profiles
        if profiles is not None
        else {
            "lite": ProfileStatus.SUPPORTED,
            "sandbox": ProfileStatus.SUPPORTED,
            "full": ProfileStatus.SUPPORTED,
        },
        owning_issue=owning_issue,
        correctness_test=correctness_test,
        perf_class=perf_class,
        perf_link=perf_link,
        gap_issue=gap_issue,
    )


def test_complete_supported_row_has_no_findings() -> None:
    findings = validate_coverage(
        _coverage(_op()),
        repo_root=Path.cwd(),
        check_references=False,
    )
    assert findings == []


def test_supported_row_missing_required_fields_reports_errors() -> None:
    findings = validate_coverage(
        _coverage(
            _op(
                owning_issue=None,
                correctness_test=None,
                perf_class=None,
                perf_link=None,
            )
        ),
        repo_root=Path.cwd(),
        check_references=False,
    )

    assert [(f.code, f.field) for f in findings] == [
        ("supported_missing_owner", "owning_issue"),
        ("supported_missing_test", "correctness_test"),
        ("supported_missing_perf_class", "perf_class"),
        ("supported_missing_perf_link", "perf_link"),
    ]
    assert all(f.severity == "error" for f in findings)


def test_supported_row_whitespace_required_fields_report_errors() -> None:
    findings = validate_coverage(
        _coverage(_op(correctness_test="   ", perf_link="\t\n")),
        repo_root=Path.cwd(),
        check_references=False,
    )

    assert [(f.code, f.field) for f in findings] == [
        ("supported_missing_test", "correctness_test"),
        ("supported_missing_perf_link", "perf_link"),
    ]


def test_missing_profile_key_reports_error() -> None:
    findings = validate_coverage(
        _coverage(
            _op(
                profiles={
                    "lite": ProfileStatus.SUPPORTED,
                    "full": ProfileStatus.SUPPORTED,
                }
            )
        ),
        repo_root=Path.cwd(),
        check_references=False,
    )

    assert [(f.code, f.field, f.operation_id) for f in findings] == [
        ("missing_profile_status", "profiles.sandbox", "filesystem.read")
    ]


def test_missing_needed_row_requires_gap_issue() -> None:
    row = _op(
        transports={},
        profiles={
            "lite": ProfileStatus.MISSING_NEEDED,
            "sandbox": ProfileStatus.MISSING_NEEDED,
            "full": ProfileStatus.MISSING_NEEDED,
        },
        correctness_test=None,
        perf_class=None,
        perf_link=None,
        gap_issue=None,
    )

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=False)

    assert [(f.code, f.field, f.operation_id) for f in findings] == [
        ("gap_missing_issue", "gap_issue", "filesystem.read")
    ]


def test_missing_needed_row_must_exist_in_curated_gap_backlog() -> None:
    row = _op(
        "parsers.list",
        transports={},
        profiles={
            "lite": ProfileStatus.MISSING_NEEDED,
            "sandbox": ProfileStatus.MISSING_NEEDED,
            "full": ProfileStatus.MISSING_NEEDED,
        },
        correctness_test=None,
        perf_class=PerfClass.NOT_PERF_SENSITIVE,
        perf_link="not on a request hot path",
        gap_issue=4187,
    )

    findings = validate_coverage(
        _coverage(row),
        repo_root=Path.cwd(),
        check_references=False,
        curated_missing_operation_ids={"search.grep_section"},
    )

    assert [(f.code, f.field, f.operation_id) for f in findings] == [
        ("missing_needed_not_in_gap_backlog", "gap_issue", "parsers.list")
    ]


def test_missing_needed_row_with_real_transport_reports_error() -> None:
    row = _op(
        "parsers.list",
        transports={"cli": TransportCell(name="nexus parsers list", source="src/nexus/cli/x.py:1")},
        profiles={
            "lite": ProfileStatus.MISSING_NEEDED,
            "sandbox": ProfileStatus.MISSING_NEEDED,
            "full": ProfileStatus.MISSING_NEEDED,
        },
        correctness_test=None,
        perf_class=PerfClass.NOT_PERF_SENSITIVE,
        perf_link="not on a request hot path",
        gap_issue=4187,
    )

    findings = validate_coverage(
        _coverage(row),
        repo_root=Path.cwd(),
        check_references=False,
        curated_missing_operation_ids={"parsers.list"},
    )

    assert [(f.code, f.field, f.operation_id) for f in findings] == [
        ("implemented_surface_marked_missing_needed", "profiles", "parsers.list")
    ]


def test_findings_are_deterministically_sorted() -> None:
    findings = validate_coverage(
        _coverage(
            _op("search.grep", owning_issue=None, correctness_test=None),
            _op("filesystem.read", owning_issue=None, correctness_test=None),
        ),
        repo_root=Path.cwd(),
        check_references=False,
    )

    assert [(f.operation_id, f.code) for f in findings] == sorted(
        (f.operation_id, f.code) for f in findings
    )


def test_format_findings_includes_actionable_rows() -> None:
    rendered = format_findings(
        [
            ValidationFinding(
                code="supported_missing_owner",
                operation_id="filesystem.read",
                field="owning_issue",
                severity="error",
                message="supported row needs an owning issue",
            )
        ]
    )

    assert "filesystem.read" in rendered
    assert "supported_missing_owner" in rendered
    assert "owning_issue" in rendered


def test_correctness_test_path_must_exist_when_reference_checks_enabled(
    tmp_path: Path,
) -> None:
    row = _op(correctness_test="tests/surface_coverage/missing_surface_test.py:10")

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert [(f.code, f.field) for f in findings] == [("invalid_test_reference", "correctness_test")]


def test_correctness_test_accepts_existing_repo_path() -> None:
    row = _op(correctness_test="tests/surface_coverage/test_validate.py:1")

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert findings == []


def test_correctness_test_accepts_multiple_existing_repo_paths() -> None:
    row = _op(
        correctness_test=(
            "tests/surface_coverage/test_validate.py:1; "
            "tests/integration/services/test_connectors_router.py:1"
        )
    )

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert findings == []


def test_correctness_test_reports_missing_repo_path_among_multiple_references() -> None:
    row = _op(
        correctness_test=(
            "tests/surface_coverage/test_validate.py:1; "
            "tests/surface_coverage/missing_surface_test.py:10"
        )
    )

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert [(f.code, f.field) for f in findings] == [("invalid_test_reference", "correctness_test")]


def test_hot_perf_link_must_reference_existing_path() -> None:
    row = _op(
        perf_class=PerfClass.HOT,
        perf_link="benchmarks/missing_surface_bench.py:1",
    )

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert [(f.code, f.field) for f in findings] == [("invalid_perf_reference", "perf_link")]


def test_hot_perf_link_accepts_prose_with_embedded_existing_repo_path() -> None:
    row = _op(
        perf_class=PerfClass.HOT,
        perf_link="version list/diff guardrail in tests/benchmarks/test_lifecycle_surface_latency.py:165",
    )

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert findings == []


def test_hot_perf_link_reports_missing_embedded_repo_path() -> None:
    row = _op(
        perf_class=PerfClass.HOT,
        perf_link="version list/diff guardrail in tests/benchmarks/missing_surface_latency.py:165",
    )

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert [(f.code, f.field) for f in findings] == [("invalid_perf_reference", "perf_link")]


def test_not_perf_sensitive_accepts_text_rationale() -> None:
    row = _op(
        perf_class=PerfClass.NOT_PERF_SENSITIVE,
        perf_link="control-plane command invoked by humans; timing is not a request-path constraint",
    )

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert findings == []


def test_cli_formatter_returns_zero_for_clean_matrix(tmp_path: Path) -> None:
    from scripts.surface_coverage.schema import dump_yaml
    from scripts.validate_api_surface_coverage import validate_file

    path = tmp_path / "coverage.yaml"
    dump_yaml(_coverage(_op()), path)

    assert validate_file(path=path, repo_root=Path.cwd(), check_references=False) == []
