"""Hard-fail freshness + render + schema CI gate for the surface coverage map."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.gen_api_surface_coverage import generate_coverage
from scripts.surface_coverage.paths import COVERAGE_HTML, COVERAGE_YAML, GAPS_YAML, REPO_ROOT
from scripts.surface_coverage.render import render_html
from scripts.surface_coverage.runtime_discovery import (
    RUNTIME_BUILD_COMMAND,
    compare_runtime_exposed_methods,
    discover_runtime_exposed_methods,
    matrix_rpc_method_names,
    resolve_runtime_kernel_binary,
)
from scripts.surface_coverage.schema import dump_yaml, load_yaml
from scripts.surface_coverage.validate import (
    format_findings,
    load_curated_missing_operation_ids,
    validate_coverage,
)


@pytest.fixture(scope="module")
def existing_coverage():
    if not COVERAGE_YAML.exists():
        pytest.skip("no coverage YAML committed yet")
    return load_yaml(COVERAGE_YAML)


def test_schema_validity(existing_coverage):
    assert existing_coverage.schema_version == 1


def test_freshness(tmp_path: Path, existing_coverage):
    """Re-extract; fail if new surfaces appeared in code but not committed."""

    out = tmp_path / "fresh.yaml"
    dump_yaml(existing_coverage, out)
    fresh = generate_coverage(repo_root=REPO_ROOT, output=out, overrides=None)

    committed_ids = {op.id for op in existing_coverage.operations}
    fresh_ids = {op.id for op in fresh.operations}
    new_in_code = sorted(fresh_ids - committed_ids)
    stale_ids = sorted(row.operation_id for row in fresh.stale_rows)
    regenerated_yaml = out.read_text(encoding="utf-8")
    committed_yaml = COVERAGE_YAML.read_text(encoding="utf-8")

    assert not new_in_code, (
        "api-rpc-surface-coverage drift: new surfaces in code not committed:\n"
        + "\n".join(f"  + {op_id}" for op_id in new_in_code)
        + "\nRun: uv run python scripts/gen_api_surface_coverage.py"
        + "\nThen commit the updated YAML and re-render HTML."
    )
    assert not stale_ids, (
        "api-rpc-surface-coverage drift: committed YAML contains stale surfaces "
        "not detected by the extractor:\n"
        + "\n".join(f"  - {op_id}" for op_id in stale_ids)
        + "\nRemove stale operations from docs/surface-coverage/api-rpc-surface-coverage.yaml."
        + "\nThen run: uv run python scripts/gen_api_surface_coverage.py"
        + "\nThen commit the updated YAML."
    )
    assert regenerated_yaml == committed_yaml, (
        "api-rpc-surface-coverage drift: regenerated YAML differs from committed YAML.\n"
        "Run: uv run python scripts/gen_api_surface_coverage.py\n"
        "Then commit the updated YAML."
    )
    if COVERAGE_HTML.exists():
        assert render_html(fresh) == COVERAGE_HTML.read_text(), (
            "api-rpc-surface-coverage drift: committed HTML differs from regenerated YAML.\n"
            "Run: uv run python scripts/render_api_surface_coverage.py\n"
            "The local HTML is generated and should not be committed."
        )


def test_render_determinism(existing_coverage):
    """Re-render committed YAML; fail if output differs from committed HTML."""

    if not COVERAGE_HTML.exists():
        pytest.skip("no coverage HTML committed yet")
    rendered = render_html(existing_coverage)
    committed = COVERAGE_HTML.read_text()
    assert rendered == committed, (
        "api-rpc-surface-coverage drift: committed HTML differs from re-render.\n"
        "Run: uv run python scripts/render_api_surface_coverage.py\n"
        "The local HTML is generated and should not be committed."
    )


def test_matrix_validation(existing_coverage):
    findings = validate_coverage(
        existing_coverage,
        repo_root=REPO_ROOT,
        curated_missing_operation_ids=load_curated_missing_operation_ids(GAPS_YAML),
    )
    errors = [finding for finding in findings if finding.severity == "error"]
    assert not errors, format_findings(errors)


def test_no_stale_rows(existing_coverage):
    stale_ids = sorted(row.operation_id for row in existing_coverage.stale_rows)
    assert not stale_ids, (
        "api-rpc-surface-coverage contains stale committed rows:\n"
        + "\n".join(f"  - {op_id}" for op_id in stale_ids)
        + "\nRun: uv run python scripts/gen_api_surface_coverage.py"
    )


def test_runtime_discovery_matches_matrix(tmp_path: Path, existing_coverage):
    kernel_binary = resolve_runtime_kernel_binary(repo_root=REPO_ROOT)
    if kernel_binary is None:
        pytest.skip(f"requires runtime build: {RUNTIME_BUILD_COMMAND}")

    runtime_methods = discover_runtime_exposed_methods(data_dir=tmp_path)
    matrix_methods = matrix_rpc_method_names(existing_coverage, profile="sandbox")
    findings = compare_runtime_exposed_methods(
        matrix_methods=matrix_methods,
        runtime_methods=runtime_methods,
    )
    errors = [finding for finding in findings if finding.severity == "error"]
    assert not errors, format_findings(errors)
