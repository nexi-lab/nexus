"""Schema dataclasses + YAML round-trip."""

from pathlib import Path

import pytest

from scripts.surface_coverage.schema import (
    Module,
    Operation,
    PerfClass,
    ProfileStatus,
    SurfaceCoverage,
    TransportCell,
    dump_yaml,
    load_yaml,
)


def test_profile_status_enum_values():
    assert ProfileStatus.SUPPORTED.value == "supported"
    assert ProfileStatus.UNAVAILABLE.value == "unavailable"
    assert ProfileStatus.ADMIN_ONLY.value == "admin_only"
    assert ProfileStatus.DEPRECATED.value == "deprecated"
    assert ProfileStatus.MISSING_NEEDED.value == "missing_needed"


def test_perf_class_enum_values():
    assert PerfClass.HOT.value == "hot"
    assert PerfClass.SETUP.value == "setup"
    assert PerfClass.CONTROL.value == "control"
    assert PerfClass.NOT_PERF_SENSITIVE.value == "not_perf_sensitive"


def test_round_trip_minimal(tmp_yaml: Path):
    coverage = SurfaceCoverage(
        schema_version=1,
        modules=[Module(id="vfs", name="VFS", description="d", depends_on=[])],
        operations=[
            Operation(
                id="fs.read",
                module="vfs",
                summary="Read bytes",
                transports={
                    "cli": TransportCell(name="nexus fs read", source="src/x.py:1"),
                },
                profiles={
                    "lite": ProfileStatus.SUPPORTED,
                    "sandbox": ProfileStatus.SUPPORTED,
                    "full": ProfileStatus.SUPPORTED,
                },
            )
        ],
        parity_warnings=[],
        unmapped_surfaces=[],
        stale_rows=[],
    )
    dump_yaml(coverage, tmp_yaml)
    reloaded = load_yaml(tmp_yaml)
    assert reloaded == coverage


def test_load_yaml_rejects_unknown_profile_status(tmp_yaml: Path):
    tmp_yaml.write_text(
        "schema_version: 1\n"
        "modules: []\n"
        "operations:\n"
        "  - id: x.y\n"
        "    module: x\n"
        "    summary: s\n"
        "    transports: {}\n"
        "    profiles: {lite: bogus, sandbox: supported, full: supported}\n"
        "parity_warnings: []\n"
        "unmapped_surfaces: []\n"
        "stale_rows: []\n"
    )
    with pytest.raises(ValueError, match="bogus"):
        load_yaml(tmp_yaml)
