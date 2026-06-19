"""SSOT for surface-coverage file paths.

Every consumer of the api-rpc-surface coverage artefacts (CLI scripts, internal
renderer/distributor modules, and `tests/surface_coverage/` checks) should
import path constants from this module rather than hard-coding
`docs/surface-coverage/...` strings.

Background — why this module exists:

The coverage family used to live at `docs/architecture/api-rpc-surface-*`, with
its location hard-coded in ~12 places. Moving it to `docs/surface-coverage/`
(PR #4182 commits `2f5189acb`, `3099e3c96`, `6c6b7509d`, `2731a824b`,
`e6094e281`, `cd74a35f8`) required editing every one of those call sites by
hand — patch over patch instead of a systemic fix. Centralising the paths here
turns the next rename / schema-version-suffix / directory move into a one-line
change.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

SURFACE_COVERAGE_DIR: Path = REPO_ROOT / "docs" / "surface-coverage"

COVERAGE_YAML: Path = SURFACE_COVERAGE_DIR / "api-rpc-surface-coverage.yaml"
COVERAGE_HTML: Path = SURFACE_COVERAGE_DIR / "api-rpc-surface-coverage.html"
GAPS_YAML: Path = SURFACE_COVERAGE_DIR / "api-rpc-surface-gaps.yaml"
OVERRIDES_YAML: Path = SURFACE_COVERAGE_DIR / "api-rpc-surface-overrides.yaml"
CONTRACT_MD: Path = SURFACE_COVERAGE_DIR / "api-rpc-surface-contract.md"
VENDOR_MERMAID: Path = SURFACE_COVERAGE_DIR / "_vendor" / "mermaid.min.js"

__all__ = [
    "COVERAGE_HTML",
    "COVERAGE_YAML",
    "CONTRACT_MD",
    "GAPS_YAML",
    "OVERRIDES_YAML",
    "REPO_ROOT",
    "SURFACE_COVERAGE_DIR",
    "VENDOR_MERMAID",
]
