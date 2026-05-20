"""Warn-only freshness + render + schema CI gate for the surface coverage map.

This test always passes in v1 — it emits warnings when drift is detected.
It will be promoted to hard-fail in a follow-up issue (likely #4139) once
subissues catch up filling per-row content.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from scripts.gen_api_surface_coverage import generate_coverage
from scripts.surface_coverage.render import render_html
from scripts.surface_coverage.schema import dump_yaml, load_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COVERAGE_YAML = _REPO_ROOT / "docs/surface-coverage/api-rpc-surface-coverage.yaml"
_COVERAGE_HTML = _REPO_ROOT / "docs/surface-coverage/api-rpc-surface-coverage.html"


@pytest.fixture(scope="module")
def existing_coverage():
    if not _COVERAGE_YAML.exists():
        pytest.skip("no coverage YAML committed yet")
    return load_yaml(_COVERAGE_YAML)


def test_schema_validity(existing_coverage):
    # load_yaml already enforces schema; reaching here means it parsed.
    assert existing_coverage.schema_version == 1


def test_freshness(tmp_path: Path, existing_coverage):
    """Re-extract; warn if new surfaces appeared in code but not in committed YAML."""
    out = tmp_path / "fresh.yaml"
    dump_yaml(existing_coverage, out)
    fresh = generate_coverage(repo_root=_REPO_ROOT, output=out, overrides=None)

    committed_ids = {op.id for op in existing_coverage.operations}
    fresh_ids = {op.id for op in fresh.operations}

    new_in_code = fresh_ids - committed_ids
    if new_in_code:
        warnings.warn(
            "api-rpc-surface-coverage drift: new surfaces in code not committed:\n"
            + "\n".join(f"  + {op_id}" for op_id in sorted(new_in_code))
            + "\n  Run: uv run python scripts/gen_api_surface_coverage.py"
            + "\n  Then commit the updated YAML and re-render HTML."
            + "\n  This is warn-only in v1.",
            stacklevel=2,
        )


def test_render_determinism(existing_coverage):
    """Re-render committed YAML; warn if output differs from committed HTML."""
    if not _COVERAGE_HTML.exists():
        pytest.skip("no coverage HTML committed yet")
    rendered = render_html(existing_coverage)
    committed = _COVERAGE_HTML.read_text()
    if rendered != committed:
        warnings.warn(
            "api-rpc-surface-coverage drift: committed HTML differs from re-render.\n"
            "  Run: uv run python scripts/render_api_surface_coverage.py\n"
            "  Then commit the updated HTML.\n"
            "  This is warn-only in v1.",
            stacklevel=2,
        )
