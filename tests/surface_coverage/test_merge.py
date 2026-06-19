"""Merge new extraction with committed YAML, preserving human-filled fields."""

from scripts.surface_coverage.merge import merge_coverage
from scripts.surface_coverage.schema import (
    Module,
    Operation,
    PerfClass,
    ProfileStatus,
    SurfaceCoverage,
    TransportCell,
)


def _profiles_all_supported():
    return {
        "lite": ProfileStatus.SUPPORTED,
        "sandbox": ProfileStatus.SUPPORTED,
        "full": ProfileStatus.SUPPORTED,
    }


def _op(id_, module, summary="", **overrides):
    base = Operation(
        id=id_,
        module=module,
        summary=summary,
        transports={},
        profiles=_profiles_all_supported(),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_merge_preserves_human_fields():
    existing_op = _op(
        "fs.read",
        "vfs",
        usage_example="nexus fs read /path",
        correctness_test="tests/test_fs.py:42",
        perf_class=PerfClass.HOT,
        perf_link="bench/test_fs_read.py:10",
        owning_issue=4123,
    )
    existing = SurfaceCoverage(
        schema_version=1,
        modules=[Module("vfs", "VFS", "")],
        operations=[existing_op],
    )
    # extractor re-discovers fs.read with refreshed transport info + extractor summary
    fresh_op = _op(
        "fs.read",
        "vfs",
        summary="extractor docstring",
        transports={"cli": TransportCell("nexus fs read", "src/x.py:99")},
    )
    fresh = SurfaceCoverage(
        schema_version=1,
        modules=[Module("vfs", "VFS", "")],
        operations=[fresh_op],
    )

    merged = merge_coverage(existing=existing, fresh=fresh)
    op = next(o for o in merged.operations if o.id == "fs.read")

    # transports refreshed from extractor
    assert op.transports["cli"].source == "src/x.py:99"
    # human fields preserved
    assert op.usage_example == "nexus fs read /path"
    assert op.correctness_test == "tests/test_fs.py:42"
    assert op.perf_class == PerfClass.HOT
    assert op.perf_link == "bench/test_fs_read.py:10"
    assert op.owning_issue == 4123
    # summary: human override wins if non-empty; else extractor
    assert op.summary == "extractor docstring"  # existing summary was ""


def test_merge_adds_new_operations():
    existing = SurfaceCoverage(
        schema_version=1,
        modules=[],
        operations=[_op("fs.read", "vfs")],
    )
    fresh = SurfaceCoverage(
        schema_version=1,
        modules=[],
        operations=[_op("fs.read", "vfs"), _op("fs.write", "vfs")],
    )
    merged = merge_coverage(existing=existing, fresh=fresh)
    assert {o.id for o in merged.operations} == {"fs.read", "fs.write"}


def test_merge_flags_stale_rows():
    existing = SurfaceCoverage(
        schema_version=1,
        modules=[],
        operations=[_op("fs.read", "vfs"), _op("fs.deprecated", "vfs", owning_issue=999)],
    )
    fresh = SurfaceCoverage(
        schema_version=1,
        modules=[],
        operations=[_op("fs.read", "vfs")],
    )
    merged = merge_coverage(existing=existing, fresh=fresh)
    # stale op preserved in operations but added to stale_rows
    assert {o.id for o in merged.operations} == {"fs.read", "fs.deprecated"}
    assert any(s.operation_id == "fs.deprecated" for s in merged.stale_rows)


def test_merge_human_summary_wins():
    existing = _op("fs.read", "vfs", summary="human-curated description")
    fresh = _op("fs.read", "vfs", summary="extractor docstring")
    merged = merge_coverage(
        existing=SurfaceCoverage(1, [], [existing]),
        fresh=SurfaceCoverage(1, [], [fresh]),
    )
    op = next(o for o in merged.operations if o.id == "fs.read")
    assert op.summary == "human-curated description"


def test_merge_preserves_non_default_profile_statuses():
    existing = _op("fs.read", "vfs")
    existing.profiles["sandbox"] = ProfileStatus.UNAVAILABLE
    fresh = _op("fs.read", "vfs")

    merged = merge_coverage(
        existing=SurfaceCoverage(1, [], [existing]),
        fresh=SurfaceCoverage(1, [], [fresh]),
    )

    op = next(o for o in merged.operations if o.id == "fs.read")
    assert op.profiles["sandbox"] == ProfileStatus.UNAVAILABLE


def test_merge_promotes_missing_needed_gap_when_surface_is_extracted():
    existing = _op(
        "parsers.list",
        "parsers",
        profiles={
            "lite": ProfileStatus.MISSING_NEEDED,
            "sandbox": ProfileStatus.MISSING_NEEDED,
            "full": ProfileStatus.MISSING_NEEDED,
        },
        gap_issue=4187,
        owning_issue=4135,
    )
    fresh = _op(
        "parsers.list",
        "parsers",
        transports={"cli": TransportCell("nexus parsers list", "src/nexus/cli/parsers.py:1")},
    )

    merged = merge_coverage(
        existing=SurfaceCoverage(1, [], [existing]),
        fresh=SurfaceCoverage(1, [], [fresh]),
    )

    op = next(o for o in merged.operations if o.id == "parsers.list")
    assert op.transports["cli"].name == "nexus parsers list"
    assert all(status == ProfileStatus.SUPPORTED for status in op.profiles.values())
    assert op.gap_issue == 4187
    assert op.owning_issue == 4135


def test_merge_fills_missing_issue_links_from_fresh_rows():
    existing = _op("raft.cluster_status", "raft")
    fresh = _op("raft.cluster_status", "raft", gap_issue=4204, owning_issue=4138)

    merged = merge_coverage(
        existing=SurfaceCoverage(1, [], [existing]),
        fresh=SurfaceCoverage(1, [], [fresh]),
    )

    op = next(o for o in merged.operations if o.id == "raft.cluster_status")
    assert op.gap_issue == 4204
    assert op.owning_issue == 4138
