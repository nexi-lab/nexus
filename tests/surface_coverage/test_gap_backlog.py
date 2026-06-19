"""Curated missing operation backlog validation."""

import yaml

from scripts.surface_coverage.paths import COVERAGE_YAML, GAPS_YAML
from scripts.surface_coverage.schema import ProfileStatus, load_yaml


def test_every_missing_operation_has_gap_issue_and_owner() -> None:
    doc = yaml.safe_load(GAPS_YAML.read_text(encoding="utf-8"))
    missing = doc.get("missing_operations", [])

    without_gap_issue = [entry["id"] for entry in missing if entry.get("gap_issue") is None]
    without_owner = [entry["id"] for entry in missing if entry.get("owning_issue") is None]

    assert without_gap_issue == []
    assert without_owner == []


def test_committed_missing_needed_rows_match_gap_backlog() -> None:
    doc = yaml.safe_load(GAPS_YAML.read_text(encoding="utf-8"))
    gap_ids = {entry["id"] for entry in doc.get("missing_operations", [])}
    coverage = load_yaml(COVERAGE_YAML)
    missing_needed_ids = {
        op.id
        for op in coverage.operations
        if not op.transports
        and all(status == ProfileStatus.MISSING_NEEDED for status in op.profiles.values())
    }

    assert missing_needed_ids == gap_ids
