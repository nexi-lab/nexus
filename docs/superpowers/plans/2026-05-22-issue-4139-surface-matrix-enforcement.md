# Issue #4139 Surface Matrix Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing #4164 API/RPC surface map scaffold into a deterministic, CI-enforced coverage matrix for #4139.

**Architecture:** Keep `docs/architecture/api-rpc-surface-coverage.yaml` as the single source of truth and add a focused validation layer beside the existing extractor and renderer. Validation returns structured findings, inventory tests hard-fail on untracked drift, and runtime discovery compares `create_app(...).state.exposed_methods` against matrix rows when `nexusd-cluster` is built. Existing incomplete rows are either completed with real owner/test/perf evidence or linked to explicit gap issues.

**Tech Stack:** Python 3.14, pytest, PyYAML, existing `scripts/surface_coverage` dataclasses, FastAPI `create_app`, optional Rust cluster binary build with `cargo build --release -p nexus-cluster --bin nexusd-cluster`.

**Spec:** `docs/superpowers/specs/2026-05-22-issue-4139-surface-matrix-enforcement-design.md`

---

## File Structure

- Create: `scripts/surface_coverage/validate.py`
  - Owns `ValidationFinding`, matrix completeness rules, source-reference checks, finding formatting, and deterministic sorting.
- Create: `scripts/surface_coverage/runtime_discovery.py`
  - Owns optional runtime app construction, `app.state.exposed_methods` extraction, and pure set comparison against matrix transport names.
- Create: `scripts/validate_api_surface_coverage.py`
  - CLI wrapper for local validation reports and future CI diagnostics.
- Create: `tests/architecture/test_validate.py`
  - Unit tests for validator behavior.
- Create: `tests/architecture/test_runtime_discovery.py`
  - Unit tests for runtime comparison plus an optional cluster-binary-backed smoke.
- Create: `tests/architecture/test_gap_backlog.py`
  - Unit tests for curated missing-operation gap issue coverage.
- Modify: `tests/architecture/test_inventory.py`
  - Promote freshness/render/schema checks from warning-only to hard assertions and invoke the validator.
- Modify: `docs/architecture/api-rpc-surface-gaps.yaml`
  - Add gap issue numbers for every curated missing operation.
- Modify: `docs/architecture/api-rpc-surface-coverage.yaml`
  - Add real owner/test/perf fields where evidence exists; link gap issues where evidence is not yet present.
- Modify: `docs/architecture/api-rpc-surface-coverage.html`
  - Regenerated output from the YAML matrix.
- Modify: `docs/architecture/api-rpc-surface-contract.md`
  - Document the hard-fail validation command and runtime-discovery workflow.

---

### Task 1: Validator Core and Supported-Row Completeness

**Files:**
- Create: `scripts/surface_coverage/validate.py`
- Create: `tests/architecture/test_validate.py`

- [ ] **Step 1: Write the failing validator tests**

Create `tests/architecture/test_validate.py` with these initial tests:

```python
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
    profiles: dict[str, ProfileStatus] | None = None,
    owning_issue: int | None = 4123,
    correctness_test: str | None = "tests/architecture/test_validate.py",
    perf_class: PerfClass | None = PerfClass.NOT_PERF_SENSITIVE,
    perf_link: str | None = "control-plane inventory check; not on request path",
    gap_issue: int | None = None,
) -> Operation:
    return Operation(
        id=op_id,
        module=op_id.split(".", 1)[0],
        summary="surface row",
        transports={"cli": TransportCell(name="nexus read", source="src/nexus/cli/commands/__init__.py:1")},
        profiles=profiles
        or {
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


def test_missing_needed_row_requires_gap_issue() -> None:
    row = _op(
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
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/architecture/test_validate.py -v
```

Expected: fail with `ModuleNotFoundError: No module named 'scripts.surface_coverage.validate'`.

- [ ] **Step 3: Implement the minimal validator**

Create `scripts/surface_coverage/validate.py`:

```python
"""Validation rules for the committed API/RPC surface coverage matrix."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.surface_coverage.schema import (
    PROFILE_KEYS,
    Operation,
    ProfileStatus,
    SurfaceCoverage,
)


@dataclass(frozen=True, order=True)
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


def validate_coverage(
    coverage: SurfaceCoverage,
    *,
    repo_root: Path,
    check_references: bool = True,
) -> list[ValidationFinding]:
    """Validate the committed coverage matrix and return sorted findings."""

    findings: list[ValidationFinding] = []
    for op in coverage.operations:
        findings.extend(_validate_profile_keys(op))
        findings.extend(_validate_supported_completeness(op))
        findings.extend(_validate_gap_policy(op))

    return sorted(findings, key=_finding_sort_key)


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
        ("owning_issue", op.owning_issue, "supported_missing_owner", "supported row needs an owning issue"),
        ("correctness_test", op.correctness_test, "supported_missing_test", "supported row needs a correctness test link"),
        ("perf_class", op.perf_class, "supported_missing_perf_class", "supported row needs a performance class"),
        ("perf_link", op.perf_link, "supported_missing_perf_link", "supported row needs performance evidence or rationale"),
    )
    findings: list[ValidationFinding] = []
    for field, value, code, message in checks:
        if value is None or value == "":
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


def _validate_gap_policy(op: Operation) -> list[ValidationFinding]:
    if any(status in _GAP_REQUIRED_STATUSES for status in op.profiles.values()) and op.gap_issue is None:
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


def _has_supported_profile(op: Operation) -> bool:
    return any(status == ProfileStatus.SUPPORTED for status in op.profiles.values())


def _finding_sort_key(finding: ValidationFinding) -> tuple[str, str, str]:
    return (
        finding.operation_id or "",
        finding.code,
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
```

- [ ] **Step 4: Run the validator tests and verify they pass**

Run:

```bash
uv run pytest tests/architecture/test_validate.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add scripts/surface_coverage/validate.py tests/architecture/test_validate.py
git commit -m "test(#4139): add surface matrix validator core"
```

---

### Task 2: Reference Validation and Finding Formatting

**Files:**
- Modify: `scripts/surface_coverage/validate.py`
- Modify: `tests/architecture/test_validate.py`

- [ ] **Step 1: Add failing tests for repository references**

Append these tests to `tests/architecture/test_validate.py`:

```python
def test_correctness_test_path_must_exist_when_reference_checks_enabled(tmp_path: Path) -> None:
    row = _op(correctness_test="tests/architecture/missing_surface_test.py:10")

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert [(f.code, f.field) for f in findings] == [
        ("invalid_test_reference", "correctness_test")
    ]


def test_correctness_test_accepts_existing_repo_path() -> None:
    row = _op(correctness_test="tests/architecture/test_validate.py:1")

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert findings == []


def test_hot_perf_link_must_reference_existing_path() -> None:
    row = _op(
        perf_class=PerfClass.HOT,
        perf_link="benchmarks/missing_surface_bench.py:1",
    )

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert [(f.code, f.field) for f in findings] == [
        ("invalid_perf_reference", "perf_link")
    ]


def test_not_perf_sensitive_accepts_text_rationale() -> None:
    row = _op(
        perf_class=PerfClass.NOT_PERF_SENSITIVE,
        perf_link="control-plane command invoked by humans; timing is not a request-path constraint",
    )

    findings = validate_coverage(_coverage(row), repo_root=Path.cwd(), check_references=True)

    assert findings == []
```

- [ ] **Step 2: Run the tests and verify the new cases fail**

Run:

```bash
uv run pytest tests/architecture/test_validate.py -v
```

Expected: fail because `validate_coverage` does not yet validate references.

- [ ] **Step 3: Implement reference validation**

Update `scripts/surface_coverage/validate.py` by importing `PerfClass` and adding reference checks:

```python
from scripts.surface_coverage.schema import (
    PROFILE_KEYS,
    Operation,
    PerfClass,
    ProfileStatus,
    SurfaceCoverage,
)
```

Add this block inside `validate_coverage` after `_validate_gap_policy(op)`:

```python
        if check_references:
            findings.extend(_validate_references(op, repo_root=repo_root))
```

Add these helper functions:

```python
def _validate_references(op: Operation, *, repo_root: Path) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if op.correctness_test and not _repo_reference_exists(op.correctness_test, repo_root):
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
        and not _repo_reference_exists(op.perf_link, repo_root)
    ):
        findings.append(
            ValidationFinding(
                code="invalid_perf_reference",
                operation_id=op.id,
                field="perf_link",
                severity="error",
                message=f"hot-path perf_link must reference an existing benchmark or guardrail: {op.perf_link}",
            )
        )
    return findings


def _repo_reference_exists(reference: str, repo_root: Path) -> bool:
    path_part = reference.split(":", 1)[0]
    if not path_part:
        return False
    return (repo_root / path_part).exists()
```

- [ ] **Step 4: Run the validator tests and verify they pass**

Run:

```bash
uv run pytest tests/architecture/test_validate.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add scripts/surface_coverage/validate.py tests/architecture/test_validate.py
git commit -m "test(#4139): validate surface matrix references"
```

---

### Task 3: Runtime Discovery Comparison

**Files:**
- Create: `scripts/surface_coverage/runtime_discovery.py`
- Create: `tests/architecture/test_runtime_discovery.py`

- [ ] **Step 1: Write failing runtime comparison tests**

Create `tests/architecture/test_runtime_discovery.py`:

```python
"""Runtime discovery checks for create_app(...).state.exposed_methods."""

from pathlib import Path

import pytest

from scripts.surface_coverage.runtime_discovery import (
    RUNTIME_BUILD_COMMAND,
    compare_runtime_exposed_methods,
    discover_runtime_exposed_methods,
    matrix_rpc_method_names,
)
from scripts.surface_coverage.schema import (
    Operation,
    ProfileStatus,
    SurfaceCoverage,
    TransportCell,
)


def _coverage(*ops: Operation) -> SurfaceCoverage:
    return SurfaceCoverage(schema_version=1, modules=[], operations=list(ops))


def _rpc_op(op_id: str, transport: str, name: str) -> Operation:
    return Operation(
        id=op_id,
        module=op_id.split(".", 1)[0],
        summary="runtime row",
        transports={transport: TransportCell(name=name, source="src/x.py:1")},
        profiles={
            "lite": ProfileStatus.SUPPORTED,
            "sandbox": ProfileStatus.SUPPORTED,
            "full": ProfileStatus.SUPPORTED,
        },
    )


def test_matrix_rpc_method_names_uses_grpc_expose_and_call_cells() -> None:
    coverage = _coverage(
        _rpc_op("filesystem.read", "grpc_expose", "read_file"),
        _rpc_op("filesystem.write", "grpc_call", "write"),
        _rpc_op("filesystem.cli", "cli", "nexus write"),
    )

    assert matrix_rpc_method_names(coverage) == {"read_file", "write"}


def test_compare_runtime_exposed_methods_reports_runtime_only_methods() -> None:
    findings = compare_runtime_exposed_methods(
        matrix_methods={"read", "write"},
        runtime_methods={"read", "write", "runtime_only"},
    )

    assert [(f.code, f.operation_id, f.message) for f in findings] == [
        ("runtime_exposed_method_missing_matrix_row", "runtime_only", "runtime method is not represented by any grpc_expose or grpc_call matrix row")
    ]


def test_compare_runtime_exposed_methods_reports_missing_runtime_methods() -> None:
    findings = compare_runtime_exposed_methods(
        matrix_methods={"read", "write"},
        runtime_methods={"read"},
    )

    assert [(f.code, f.operation_id) for f in findings] == [
        ("matrix_rpc_method_missing_runtime_discovery", "write")
    ]


def test_discover_runtime_exposed_methods_skips_without_runtime(tmp_path: Path) -> None:
    kernel_binary = resolve_runtime_kernel_binary(repo_root=Path.cwd())
    if kernel_binary is None:
        pytest.skip(f"requires runtime build: {RUNTIME_BUILD_COMMAND}")
    methods = discover_runtime_exposed_methods(data_dir=tmp_path)
    assert isinstance(methods, set)
    assert methods, "runtime discovery should return at least one exposed method"
```

- [ ] **Step 2: Run the runtime tests and verify the pure tests fail**

Run:

```bash
uv run pytest tests/architecture/test_runtime_discovery.py -v
```

Expected: fail with `ModuleNotFoundError: No module named 'scripts.surface_coverage.runtime_discovery'`.

- [ ] **Step 3: Implement runtime discovery**

Create `scripts/surface_coverage/runtime_discovery.py`:

```python
"""Runtime discovery for FastAPI exposed RPC methods."""

from __future__ import annotations

from pathlib import Path

from scripts.surface_coverage.schema import SurfaceCoverage
from scripts.surface_coverage.validate import ValidationFinding

RUNTIME_BUILD_COMMAND = "cargo build --release -p nexus-cluster --bin nexusd-cluster"


def matrix_rpc_method_names(coverage: SurfaceCoverage) -> set[str]:
    """Return matrix method names expected to appear in runtime RPC discovery."""

    methods: set[str] = set()
    for op in coverage.operations:
        for transport in ("grpc_expose", "grpc_call"):
            cell = op.transports.get(transport)
            if cell is not None:
                methods.add(cell.name)
    return methods


def compare_runtime_exposed_methods(
    *,
    matrix_methods: set[str],
    runtime_methods: set[str],
) -> list[ValidationFinding]:
    """Compare committed matrix RPC names to runtime-discovered RPC names."""

    findings: list[ValidationFinding] = []
    for method in sorted(runtime_methods - matrix_methods):
        findings.append(
            ValidationFinding(
                code="runtime_exposed_method_missing_matrix_row",
                operation_id=method,
                field="transports.grpc_expose",
                severity="error",
                message="runtime method is not represented by any grpc_expose or grpc_call matrix row",
            )
        )
    for method in sorted(matrix_methods - runtime_methods):
        findings.append(
            ValidationFinding(
                code="matrix_rpc_method_missing_runtime_discovery",
                operation_id=method,
                field="app.state.exposed_methods",
                severity="error",
                message="matrix grpc_expose/grpc_call method was not discovered from create_app(...).state.exposed_methods",
            )
        )
    return findings


def discover_runtime_exposed_methods(*, data_dir: Path) -> set[str]:
    """Build a minimal Nexus app and return app.state.exposed_methods keys."""

    import nexus
    from nexus.server.fastapi_server import create_app

    nx = nexus.connect(config={"profile": "sandbox", "data_dir": str(data_dir)})
    try:
        app = create_app(nexus_fs=nx)
        exposed = getattr(app.state, "exposed_methods", {})
        return set(exposed)
    finally:
        close = getattr(nx, "close", None)
        if callable(close):
            close()
```

- [ ] **Step 4: Run the runtime tests**

Run:

```bash
uv run pytest tests/architecture/test_runtime_discovery.py -v
```

Expected: pure comparison tests pass. The cluster-binary-backed test either passes or skips with a reason containing `cargo build --release -p nexus-cluster --bin nexusd-cluster`.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add scripts/surface_coverage/runtime_discovery.py tests/architecture/test_runtime_discovery.py
git commit -m "test(#4139): compare runtime RPC discovery with matrix"
```

---

### Task 4: Validation CLI

**Files:**
- Create: `scripts/validate_api_surface_coverage.py`
- Modify: `tests/architecture/test_validate.py`

- [ ] **Step 1: Add failing CLI smoke test**

Append this test to `tests/architecture/test_validate.py`:

```python
def test_cli_formatter_returns_zero_for_clean_matrix(tmp_path: Path) -> None:
    from scripts.validate_api_surface_coverage import validate_file
    from scripts.surface_coverage.schema import dump_yaml

    path = tmp_path / "coverage.yaml"
    dump_yaml(_coverage(_op()), path)

    assert validate_file(path=path, repo_root=Path.cwd(), check_references=False) == []
```

- [ ] **Step 2: Run the focused tests and verify the new CLI import fails**

Run:

```bash
uv run pytest tests/architecture/test_validate.py::test_cli_formatter_returns_zero_for_clean_matrix -v
```

Expected: fail with `ModuleNotFoundError: No module named 'scripts.validate_api_surface_coverage'`.

- [ ] **Step 3: Implement the validation CLI wrapper**

Create `scripts/validate_api_surface_coverage.py`:

```python
#!/usr/bin/env python3
"""Validate docs/architecture/api-rpc-surface-coverage.yaml."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.surface_coverage.schema import load_yaml  # noqa: E402
from scripts.surface_coverage.validate import (  # noqa: E402
    ValidationFinding,
    format_findings,
    validate_coverage,
)


def validate_file(
    *,
    path: Path,
    repo_root: Path,
    check_references: bool = True,
) -> list[ValidationFinding]:
    coverage = load_yaml(path)
    return validate_coverage(
        coverage,
        repo_root=repo_root,
        check_references=check_references,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--coverage",
        type=Path,
        default=Path("docs/architecture/api-rpc-surface-coverage.yaml"),
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
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
```

- [ ] **Step 4: Run the CLI-focused tests**

Run:

```bash
uv run pytest tests/architecture/test_validate.py -v
```

Expected: validator unit tests pass.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git add scripts/validate_api_surface_coverage.py tests/architecture/test_validate.py
git commit -m "test(#4139): add surface matrix validation CLI"
```

---

### Task 5: Curated Missing-Operation Gap Backlog

**Files:**
- Create: `tests/architecture/test_gap_backlog.py`
- Modify: `docs/architecture/api-rpc-surface-gaps.yaml`
- Regenerate: `docs/architecture/api-rpc-surface-coverage.yaml`
- Regenerate: `docs/architecture/api-rpc-surface-coverage.html`

- [ ] **Step 1: Write failing gap backlog test**

Create `tests/architecture/test_gap_backlog.py`:

```python
"""Curated missing operation backlog validation."""

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GAPS = _REPO_ROOT / "docs/architecture/api-rpc-surface-gaps.yaml"


def test_every_missing_operation_has_gap_issue_and_owner() -> None:
    doc = yaml.safe_load(_GAPS.read_text(encoding="utf-8"))
    missing = doc.get("missing_operations", [])

    without_gap_issue = [entry["id"] for entry in missing if entry.get("gap_issue") is None]
    without_owner = [entry["id"] for entry in missing if entry.get("owning_issue") is None]

    assert without_gap_issue == []
    assert without_owner == []
```

- [ ] **Step 2: Run the gap backlog test and verify it fails**

Run:

```bash
uv run pytest tests/architecture/test_gap_backlog.py -v
```

Expected: fail listing current missing operations without `gap_issue` or `owning_issue`.

- [ ] **Step 3: Create gap issues for missing operations without issue links**

For each failing entry in `docs/architecture/api-rpc-surface-gaps.yaml`, create or locate a GitHub gap issue. Use the connected GitHub app when available. If the connector fails to write, use `/opt/homebrew/bin/gh` or `/Users/tafeng/.local/bin/gh` after verifying auth.

Issue body template for each missing operation:

```markdown
Parent matrix: #4139

## Missing surface

Use the operation id from the failing YAML entry.

## Request/response or CLI shape

Describe the expected external shape from `docs/architecture/api-rpc-surface-gaps.yaml`.

## Tests required

- positive flow
- expected failure
- auth or permission denied when applicable
- profile unavailable behavior
- stable unsupported error shape until implementation lands

## Benchmark expectation

Use the `wanted_why` and module context from `docs/architecture/api-rpc-surface-gaps.yaml` to classify this row as setup, control, hot, or not performance-sensitive.

## Docs location

Update `docs/architecture/api-rpc-surface-coverage.yaml` and the relevant profile story issue.
```

- [ ] **Step 4: Update the gap YAML with returned issue numbers**

Edit `docs/architecture/api-rpc-surface-gaps.yaml` so every `missing_operations` entry has both:

```yaml
gap_issue: 4188
owning_issue: 4138
```

Use these owner defaults unless a better child issue already owns the surface:

```text
search.* -> 4135
parsers.* -> 4135
raft.* -> 4138
hub.* -> 4138
approvals.* -> 4138
archive.* -> 4138
upload.* -> 4138
portability.* -> 4138
fuse.* -> 4133
```

- [ ] **Step 5: Regenerate YAML and HTML**

Run:

```bash
uv run python scripts/gen_api_surface_coverage.py
uv run python scripts/render_api_surface_coverage.py
```

Expected: generated YAML preserves human fields and imports updated gap issue links into missing-needed rows; HTML changes only from regenerated matrix data.

- [ ] **Step 6: Run the gap test**

Run:

```bash
uv run pytest tests/architecture/test_gap_backlog.py -v
```

Expected: pass.

- [ ] **Step 7: Commit Task 5**

Run:

```bash
git add docs/architecture/api-rpc-surface-gaps.yaml docs/architecture/api-rpc-surface-coverage.yaml docs/architecture/api-rpc-surface-coverage.html tests/architecture/test_gap_backlog.py
git commit -m "test(#4139): require gap issues for missing surfaces"
```

---

### Task 6: Matrix Data Migration for Owner, Correctness, and Performance Fields

**Files:**
- Modify: `docs/architecture/api-rpc-surface-coverage.yaml`
- Modify: `tests/architecture/test_inventory.py`
- Regenerate: `docs/architecture/api-rpc-surface-coverage.html`

- [ ] **Step 1: Generate the current validation report**

Run:

```bash
uv run python scripts/validate_api_surface_coverage.py --coverage docs/architecture/api-rpc-surface-coverage.yaml
```

Expected: nonzero exit with rows missing owner/test/perf/gap fields.

- [ ] **Step 2: Group findings by module and profile story issue**

Use this owner mapping when updating `owning_issue` for existing supported rows:

```text
filesystem.*, grpc typed VFS, core read/write/list/stat rows -> 4123, 4127, or 4133 depending profile/module context
rebac.*, delegation.*, sharing and permission rows -> 4124, 4128, or 4134
search.*, parsers.*, semantic indexing rows -> 4129 or 4135
mcp.*, mount.*, oauth credential and connector rows -> 4131 or 4136
workspace.*, snapshot.*, versioning.*, agent rows -> 4137
admin.*, auth.*, audit.*, events.*, governance.*, federation.*, pay.*, hub.*, raft.*, daemon.* -> 4125 or 4138
profile gate and startup rows -> 4122, 4126, or 4132
```

- [ ] **Step 3: Add real correctness test links where existing tests prove the row**

For each supported row, set `correctness_test` only to a real existing test that exercises the surface or a profile story test that asserts the row’s story coverage. Use the exact `path:line` format:

```yaml
correctness_test: tests/e2e/server/test_cli_commands_e2e.py:1
```

For rows without real proof, create or locate a gap issue for test coverage and set `gap_issue`. Do not use a matrix metadata test as a correctness test for product behavior.

- [ ] **Step 4: Add performance classification**

For each supported row, set:

```yaml
perf_class: hot
perf_link: tests/e2e/server/test_exchange_protocol_perf.py:1
```

when it is request-path critical and has benchmark evidence.

For setup or control-plane rows with existing timing or smoke evidence, set:

```yaml
perf_class: control
perf_link: tests/e2e/server/test_operations_e2e.py:1
```

For rows that are explicitly not performance-sensitive, set:

```yaml
perf_class: not_perf_sensitive
perf_link: "operator-invoked inventory path; not on a request or data-plane hot path"
```

- [ ] **Step 5: Re-run validation until no untracked errors remain**

Run:

```bash
uv run python scripts/validate_api_surface_coverage.py --coverage docs/architecture/api-rpc-surface-coverage.yaml
```

Expected: exit 0. Remaining incomplete product coverage is represented by explicit `gap_issue` links rather than hidden empty fields.

- [ ] **Step 6: Regenerate HTML**

Run:

```bash
uv run python scripts/render_api_surface_coverage.py
```

Expected: `docs/architecture/api-rpc-surface-coverage.html` is regenerated from the updated YAML.

- [ ] **Step 7: Promote inventory tests from warnings to assertions**

Replace `tests/architecture/test_inventory.py` with:

```python
"""Hard-fail freshness + render + schema CI gate for the surface coverage map."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.gen_api_surface_coverage import generate_coverage
from scripts.surface_coverage.render import render_html
from scripts.surface_coverage.runtime_discovery import (
    RUNTIME_BUILD_COMMAND,
    compare_runtime_exposed_methods,
    discover_runtime_exposed_methods,
    matrix_rpc_method_names,
)
from scripts.surface_coverage.schema import dump_yaml, load_yaml
from scripts.surface_coverage.validate import format_findings, validate_coverage

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COVERAGE_YAML = _REPO_ROOT / "docs/architecture/api-rpc-surface-coverage.yaml"
_COVERAGE_HTML = _REPO_ROOT / "docs/architecture/api-rpc-surface-coverage.html"


@pytest.fixture(scope="module")
def existing_coverage():
    if not _COVERAGE_YAML.exists():
        pytest.skip("no coverage YAML committed yet")
    return load_yaml(_COVERAGE_YAML)


def test_schema_validity(existing_coverage):
    assert existing_coverage.schema_version == 1


def test_freshness(tmp_path: Path, existing_coverage):
    """Re-extract; fail if new surfaces appeared in code but not committed."""

    out = tmp_path / "fresh.yaml"
    dump_yaml(existing_coverage, out)
    fresh = generate_coverage(repo_root=_REPO_ROOT, output=out, overrides=None)

    committed_ids = {op.id for op in existing_coverage.operations}
    fresh_ids = {op.id for op in fresh.operations}
    new_in_code = sorted(fresh_ids - committed_ids)

    assert not new_in_code, (
        "api-rpc-surface-coverage drift: new surfaces in code not committed:\n"
        + "\n".join(f"  + {op_id}" for op_id in new_in_code)
        + "\nRun: uv run python scripts/gen_api_surface_coverage.py"
        + "\nThen commit the updated YAML and re-render HTML."
    )


def test_render_determinism(existing_coverage):
    if not _COVERAGE_HTML.exists():
        pytest.skip("no coverage HTML committed yet")
    rendered = render_html(existing_coverage)
    committed = _COVERAGE_HTML.read_text()
    assert rendered == committed, (
        "api-rpc-surface-coverage drift: committed HTML differs from re-render.\n"
        "Run: uv run python scripts/render_api_surface_coverage.py\n"
        "Then commit the updated HTML."
    )


def test_matrix_validation(existing_coverage):
    findings = validate_coverage(existing_coverage, repo_root=_REPO_ROOT)
    errors = [finding for finding in findings if finding.severity == "error"]
    assert not errors, format_findings(errors)


def test_runtime_discovery_matches_matrix(tmp_path: Path, existing_coverage):
    kernel_binary = resolve_runtime_kernel_binary(repo_root=_REPO_ROOT)
    if kernel_binary is None:
        pytest.skip(f"requires runtime build: {RUNTIME_BUILD_COMMAND}")
    runtime_methods = discover_runtime_exposed_methods(data_dir=tmp_path)
    matrix_methods = matrix_rpc_method_names(existing_coverage)
    findings = compare_runtime_exposed_methods(
        matrix_methods=matrix_methods,
        runtime_methods=runtime_methods,
    )
    errors = [finding for finding in findings if finding.severity == "error"]
    assert not errors, format_findings(errors)
```

- [ ] **Step 8: Run inventory validation**

Run:

```bash
uv run pytest tests/architecture/test_inventory.py::test_matrix_validation -v
```

Expected: pass.

- [ ] **Step 9: Commit Task 6**

Run:

```bash
git add docs/architecture/api-rpc-surface-coverage.yaml docs/architecture/api-rpc-surface-coverage.html tests/architecture/test_inventory.py
git commit -m "docs(#4139): fill surface matrix ownership and coverage fields"
```

---

### Task 7: Runtime Discovery in Inventory Tests

**Files:**
- Modify: `scripts/surface_coverage/runtime_discovery.py`
- Modify: `tests/architecture/test_runtime_discovery.py`
- Modify: `tests/architecture/test_inventory.py`
- Regenerate if needed: `docs/architecture/api-rpc-surface-coverage.yaml`
- Regenerate if needed: `docs/architecture/api-rpc-surface-coverage.html`

- [ ] **Step 1: Run runtime discovery without the cluster binary built**

Run:

```bash
uv run pytest tests/architecture/test_inventory.py::test_runtime_discovery_matches_matrix -v
```

Expected: skip with a reason containing `requires runtime build`.

- [ ] **Step 2: Build `nexusd-cluster`**

Run:

```bash
cargo build --release -p nexus-cluster --bin nexusd-cluster
```

Expected: command exits 0 and writes `target/release/nexusd-cluster`.

- [ ] **Step 3: Run runtime discovery after the build**

Run:

```bash
uv run pytest tests/architecture/test_runtime_discovery.py tests/architecture/test_inventory.py::test_runtime_discovery_matches_matrix -v
```

Expected: pure runtime comparison tests pass. If the cluster-binary-backed comparison fails, inspect the method names in the failure output.

- [ ] **Step 4: Reconcile runtime-only methods**

If runtime exposes a method not present in the matrix, add or repair the matrix row by fixing the extractor, running:

```bash
uv run python scripts/gen_api_surface_coverage.py
uv run python scripts/render_api_surface_coverage.py
```

Then add owner/test/perf/gap fields as required by Task 6.

- [ ] **Step 5: Reconcile matrix-only runtime methods**

If a matrix `grpc_expose` or `grpc_call` cell does not appear at runtime, decide whether the row is stale, deprecated, profile-gated, or missing from runtime wiring. Update the row status and `gap_issue`, or fix runtime wiring in the relevant source module if it is a real regression.

- [ ] **Step 6: Re-run runtime discovery tests**

Run:

```bash
uv run pytest tests/architecture/test_runtime_discovery.py tests/architecture/test_inventory.py::test_runtime_discovery_matches_matrix -v
```

Expected: pass with runtime built.

- [ ] **Step 7: Commit Task 7**

Run:

```bash
git add scripts/surface_coverage/runtime_discovery.py tests/architecture/test_runtime_discovery.py tests/architecture/test_inventory.py docs/architecture/api-rpc-surface-coverage.yaml docs/architecture/api-rpc-surface-coverage.html
git commit -m "test(#4139): include runtime RPC discovery in matrix gate"
```

---

### Task 8: Contract Documentation and Contributor Workflow

**Files:**
- Modify: `docs/architecture/api-rpc-surface-contract.md`

- [ ] **Step 1: Add the hard-fail workflow section**

Edit `docs/architecture/api-rpc-surface-contract.md` and add this section after the existing workflow section:

```markdown
## CI enforcement

#4139 promotes the matrix from a warn-only artifact to an enforceable contract.
Contributors changing external surfaces must run:

```bash
uv run python scripts/gen_api_surface_coverage.py
uv run python scripts/render_api_surface_coverage.py
uv run python scripts/validate_api_surface_coverage.py
uv run pytest \
  tests/architecture/test_inventory.py \
  tests/architecture/test_validate.py \
  tests/architecture/test_runtime_discovery.py \
  tests/architecture/test_gap_backlog.py \
  tests/architecture/test_merge.py \
  -v
```

The inventory tests fail when:

- a new external surface appears in code but not in the committed matrix,
- the rendered HTML is stale,
- a supported row lacks `owning_issue`, `correctness_test`, `perf_class`, or `perf_link`,
- a missing-needed, unavailable, or deprecated row lacks `gap_issue`,
- runtime-discovered RPC methods diverge from the matrix after `nexusd-cluster` is built.

Runtime discovery requires the cluster kernel binary:

```bash
cargo build --release -p nexus-cluster --bin nexusd-cluster
uv run pytest tests/architecture/test_inventory.py::test_runtime_discovery_matches_matrix -v
```
```

- [ ] **Step 2: Verify docs formatting**

Run:

```bash
uv run python scripts/validate_api_surface_coverage.py
```

Expected: exit 0.

- [ ] **Step 3: Commit Task 8**

Run:

```bash
git add docs/architecture/api-rpc-surface-contract.md
git commit -m "docs(#4139): document enforced surface matrix workflow"
```

---

### Task 9: Final Verification

**Files:**
- No new files expected.

- [ ] **Step 1: Run focused surface-matrix architecture tests**

Run:

```bash
uv run pytest \
  tests/architecture/test_inventory.py \
  tests/architecture/test_validate.py \
  tests/architecture/test_runtime_discovery.py \
  tests/architecture/test_gap_backlog.py \
  tests/architecture/test_merge.py \
  -v
```

Expected: focused matrix architecture tests pass, with runtime-discovery skip only if `nexusd-cluster` is not built.

- [ ] **Step 2: Run generator idempotence**

Run:

```bash
uv run python scripts/gen_api_surface_coverage.py
git diff --exit-code -- docs/architecture/api-rpc-surface-coverage.yaml
```

Expected: both commands exit 0.

- [ ] **Step 3: Run renderer idempotence**

Run:

```bash
uv run python scripts/render_api_surface_coverage.py
git diff --exit-code -- docs/architecture/api-rpc-surface-coverage.html
```

Expected: both commands exit 0.

- [ ] **Step 4: Run validation CLI**

Run:

```bash
uv run python scripts/validate_api_surface_coverage.py
```

Expected: exit 0.

- [ ] **Step 5: Run runtime discovery when feasible**

If the cluster binary is not built, run:

```bash
uv run pytest tests/architecture/test_inventory.py::test_runtime_discovery_matches_matrix -v
```

Expected: skip with the build command in the reason.

If the cluster binary is built, run:

```bash
cargo build --release -p nexus-cluster --bin nexusd-cluster
uv run pytest tests/architecture/test_runtime_discovery.py tests/architecture/test_inventory.py::test_runtime_discovery_matches_matrix -v
```

Expected: pass.

- [ ] **Step 6: Check git state**

Run:

```bash
git status --short
```

Expected: clean worktree.
