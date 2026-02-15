"""Hypothesis property-based tests for ReadSet kernel invariants (Issue #1303).

Invariants proven:
  1. Serialization roundtrip: from_dict(to_dict(rs)) preserves all entries
  2. Overlap consistency: direct match is subset of directory match
  3. Index consistency: registered entries are always findable
  4. Zone filter correctness: filtered results are subset of unfiltered
  5. Bug regression: overlaps_with_write checks directory containment even
     when direct path match has older revision
"""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st

from nexus.core.read_set import (
    AccessType,
    ReadSet,
    ReadSetEntry,
    ReadSetRegistry,
)
from tests.strategies.kernel import read_set_entry, valid_path

# ---------------------------------------------------------------------------
# Invariant 1: Serialization roundtrip
# ---------------------------------------------------------------------------


class TestReadSetSerializationInvariants:
    """ReadSet serialization roundtrip properties."""

    @given(
        query_id=st.text(min_size=1, max_size=30),
        zone_id=st.text(min_size=1, max_size=30),
        entries=st.lists(read_set_entry(), max_size=20),
    )
    @example(query_id="q1", zone_id="z1", entries=[])
    def test_readset_serialization_roundtrip(
        self,
        query_id: str,
        zone_id: str,
        entries: list[ReadSetEntry],
    ) -> None:
        """from_dict(to_dict(rs)) preserves query_id, zone_id, and all entries."""
        rs = ReadSet(query_id=query_id, zone_id=zone_id, entries=entries)
        data = rs.to_dict()
        restored = ReadSet.from_dict(data)

        assert restored.query_id == rs.query_id
        assert restored.zone_id == rs.zone_id
        assert len(restored.entries) == len(rs.entries)

        for orig, rest in zip(rs.entries, restored.entries):
            assert rest.resource_type == orig.resource_type
            assert rest.resource_id == orig.resource_id
            assert rest.revision == orig.revision

    @given(entry=read_set_entry())
    def test_entry_serialization_roundtrip(self, entry: ReadSetEntry) -> None:
        """from_dict(to_dict(entry)) preserves all fields."""
        data = entry.to_dict()
        restored = ReadSetEntry.from_dict(data)

        assert restored.resource_type == entry.resource_type
        assert restored.resource_id == entry.resource_id
        assert restored.revision == entry.revision
        assert restored.access_type == entry.access_type


# ---------------------------------------------------------------------------
# Invariant 2: Staleness is monotonic
# ---------------------------------------------------------------------------


class TestStalenessInvariants:
    """ReadSetEntry staleness properties."""

    @given(
        revision=st.integers(min_value=0, max_value=1_000_000),
        current=st.integers(min_value=0, max_value=1_000_000),
    )
    def test_staleness_is_consistent(self, revision: int, current: int) -> None:
        """is_stale returns True iff current_revision > entry.revision."""
        entry = ReadSetEntry(
            resource_type="file",
            resource_id="/test.txt",
            revision=revision,
        )
        assert entry.is_stale(current) == (current > revision)

    @given(
        revision=st.integers(min_value=0, max_value=1_000_000),
        bump=st.integers(min_value=1, max_value=1_000),
    )
    def test_staleness_monotonic(self, revision: int, bump: int) -> None:
        """Once stale, always stale at higher revisions."""
        entry = ReadSetEntry(
            resource_type="file",
            resource_id="/test.txt",
            revision=revision,
        )
        stale_rev = revision + bump
        assert entry.is_stale(stale_rev) is True
        # Any revision higher than stale_rev is also stale
        assert entry.is_stale(stale_rev + 1) is True


# ---------------------------------------------------------------------------
# Invariant 3: Overlap detection consistency
# ---------------------------------------------------------------------------


class TestOverlapInvariants:
    """ReadSet overlap detection properties."""

    @given(
        file_path=valid_path(),
        file_rev=st.integers(min_value=0, max_value=1_000),
        write_rev=st.integers(min_value=0, max_value=2_000),
    )
    def test_direct_overlap_iff_newer_revision(
        self,
        file_path: str,
        file_rev: int,
        write_rev: int,
    ) -> None:
        """Direct path match overlaps iff write revision > read revision."""
        rs = ReadSet(query_id="q1", zone_id="z1")
        rs.record_read("file", file_path, file_rev)

        result = rs.overlaps_with_write(file_path, write_rev)
        assert result == (write_rev > file_rev)

    @given(
        dir_path=valid_path(max_depth=3),
        child_suffix=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1,
            max_size=20,
        ),
        dir_rev=st.integers(min_value=0, max_value=1_000),
        write_rev=st.integers(min_value=1, max_value=2_000),
    )
    def test_directory_containment_always_overlaps(
        self,
        dir_path: str,
        child_suffix: str,
        dir_rev: int,
        write_rev: int,
    ) -> None:
        """Write to file inside a read directory always triggers overlap."""
        dir_normalized = dir_path.rstrip("/") + "/"
        child_path = dir_normalized + child_suffix

        rs = ReadSet(query_id="q1", zone_id="z1")
        rs.record_read("directory", dir_normalized, dir_rev, access_type=AccessType.LIST)

        assert rs.overlaps_with_write(child_path, write_rev) is True

    @given(
        file_path=valid_path(),
        file_rev=st.integers(min_value=10, max_value=1_000),
        dir_rev=st.integers(min_value=0, max_value=9),
    )
    def test_bug_regression_directory_checked_after_direct_miss(
        self,
        file_path: str,
        file_rev: int,
        dir_rev: int,
    ) -> None:
        """Regression: when direct match has older revision, directory check still runs.

        This was a real bug where overlaps_with_write returned False early
        when the direct path match had a newer revision than the write,
        skipping the directory containment check entirely.
        """
        # Create a path that's both directly read AND inside a read directory
        parent = "/".join(file_path.rstrip("/").split("/")[:-1])
        if not parent or parent == "":
            parent = "/"
        parent_dir = parent.rstrip("/") + "/"

        rs = ReadSet(query_id="q1", zone_id="z1")
        rs.record_read("file", file_path, file_rev)  # Read file at high revision
        rs.record_read("directory", parent_dir, dir_rev, access_type=AccessType.LIST)

        # Write at revision between dir_rev and file_rev:
        # - Direct match: write_rev < file_rev → NOT stale for file
        # - Directory: write_rev > dir_rev → IS stale for directory listing
        write_rev = dir_rev + 1
        if write_rev < file_rev:
            # Before bug fix, this returned False incorrectly
            assert rs.overlaps_with_write(file_path, write_rev) is True


# ---------------------------------------------------------------------------
# Invariant 4: Registry zone filter is subset of unfiltered
# ---------------------------------------------------------------------------


class TestRegistryInvariants:
    """ReadSetRegistry consistency properties."""

    @given(
        paths=st.lists(valid_path(), min_size=1, max_size=10),
        zone_ids=st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("L", "N")),
                min_size=1,
                max_size=10,
            ),
            min_size=1,
            max_size=3,
        ),
    )
    @settings(deadline=None)
    def test_zone_filter_is_subset_of_unfiltered(
        self,
        paths: list[str],
        zone_ids: list[str],
    ) -> None:
        """get_affected_queries(zone_id=X) ⊆ get_affected_queries(zone_id=None)."""
        registry = ReadSetRegistry()

        # Register read sets across different zones
        for i, zone_id in enumerate(zone_ids):
            rs = ReadSet(query_id=f"q_{i}", zone_id=zone_id)
            for path in paths:
                rs.record_read("file", path, revision=10)
            registry.register(rs)

        # For any write, filtered results must be subset of unfiltered
        for path in paths:
            unfiltered = registry.get_affected_queries(path, 20)
            for zone_id in zone_ids:
                filtered = registry.get_affected_queries(path, 20, zone_id=zone_id)
                assert filtered.issubset(unfiltered), (
                    f"Filtered {filtered} not subset of unfiltered {unfiltered} "
                    f"for zone_id={zone_id}"
                )

    @given(
        query_id=st.text(min_size=1, max_size=20),
        zone_id=st.text(min_size=1, max_size=10),
        paths=st.lists(valid_path(), min_size=1, max_size=10),
    )
    @settings(deadline=None)
    def test_unregister_removes_completely(
        self,
        query_id: str,
        zone_id: str,
        paths: list[str],
    ) -> None:
        """After unregister, the query_id is never returned by any lookup."""
        registry = ReadSetRegistry()
        rs = ReadSet(query_id=query_id, zone_id=zone_id)
        for path in paths:
            rs.record_read("file", path, revision=10)
        registry.register(rs)
        registry.unregister(query_id)

        # Must not appear in any affected query
        for path in paths:
            affected = registry.get_affected_queries(path, 20)
            assert query_id not in affected

        # Must not be retrievable
        assert registry.get_read_set(query_id) is None

        # Must not be in zone index
        assert query_id not in registry.get_queries_for_zone(zone_id)

    @given(
        n=st.integers(min_value=1, max_value=20),
        zone_id=st.text(min_size=1, max_size=10),
    )
    @settings(deadline=None)
    def test_registry_count_matches_registered(self, n: int, zone_id: str) -> None:
        """len(registry) == number of registered (non-unregistered) read sets."""
        registry = ReadSetRegistry()
        for i in range(n):
            rs = ReadSet(query_id=f"q_{i}", zone_id=zone_id)
            rs.record_read("file", f"/path_{i}.txt", revision=10)
            registry.register(rs)

        assert len(registry) == n
