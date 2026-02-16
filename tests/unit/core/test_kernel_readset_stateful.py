"""Hypothesis stateful test for ReadSetRegistry (Issue #1303).

Uses RuleBasedStateMachine to model the ReadSetRegistry lifecycle:
  - register: Add read sets with file/directory entries
  - unregister: Remove read sets
  - query_affected: Check which queries are affected by a write
  - cleanup: Remove expired read sets

Invariants checked after every rule:
  1. Registry count matches model count
  2. No phantom queries: every returned query_id exists in registry
  3. Zone isolation: zone-filtered results only include matching zones
"""

from __future__ import annotations

import os

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
)

from nexus.core.read_set import AccessType, ReadSet, ReadSetRegistry

# ---------------------------------------------------------------------------
# Strategies for stateful testing
# ---------------------------------------------------------------------------

_ZONE_IDS = st.sampled_from(["zone_a", "zone_b", "zone_c"])
_PATHS = st.sampled_from(
    [
        "/inbox/a.txt",
        "/inbox/b.txt",
        "/inbox/c.txt",
        "/docs/readme.md",
        "/docs/guide.pdf",
        "/shared/data.csv",
        "/shared/config.json",
        "/workspace/project/main.py",
        "/workspace/project/test.py",
    ]
)


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------


class ReadSetRegistryStateMachine(RuleBasedStateMachine):
    """Stateful test for ReadSetRegistry lifecycle operations."""

    registered_ids = Bundle("registered_ids")

    def __init__(self) -> None:
        super().__init__()
        self.registry = ReadSetRegistry()
        # Model: mirrors what the registry should contain
        self.model: dict[str, ReadSet] = {}
        self._next_id = 0

    # ----- Rules -----

    @rule(
        target=registered_ids,
        zone_id=_ZONE_IDS,
        paths=st.lists(_PATHS, min_size=1, max_size=5),
        include_dirs=st.booleans(),
    )
    def register_read_set(self, zone_id: str, paths: list[str], include_dirs: bool) -> str:
        """Register a new read set with file and optionally directory entries."""
        query_id = f"q_{self._next_id}"
        self._next_id += 1

        rs = ReadSet(query_id=query_id, zone_id=zone_id)
        for path in paths:
            rs.record_read("file", path, revision=10)
        if include_dirs:
            # Also read the parent directory of the first path
            parent = "/".join(paths[0].rstrip("/").split("/")[:-1]) + "/"
            if parent and parent != "/":
                rs.record_read("directory", parent, revision=5, access_type=AccessType.LIST)

        self.registry.register(rs)
        self.model[query_id] = rs
        return query_id

    @rule(query_id=registered_ids)
    def unregister_read_set(self, query_id: str) -> None:
        """Unregister a read set."""
        self.registry.unregister(query_id)
        self.model.pop(query_id, None)

    @rule(
        write_path=_PATHS,
        write_revision=st.integers(min_value=1, max_value=100),
    )
    def query_affected(self, write_path: str, write_revision: int) -> None:
        """Query affected read sets and verify against model."""
        affected = self.registry.get_affected_queries(write_path, write_revision)

        # Every returned query_id must exist in the registry
        for qid in affected:
            assert qid in self.model, f"Phantom query: {qid} returned but not in model"

        # Every model read set that SHOULD be affected must be returned
        for qid, rs in self.model.items():
            if rs.overlaps_with_write(write_path, write_revision):
                assert qid in affected, (
                    f"Missed overlap: {qid} should be affected by "
                    f"write to {write_path}@{write_revision}"
                )

    @rule(
        write_path=_PATHS,
        write_revision=st.integers(min_value=1, max_value=100),
        zone_id=_ZONE_IDS,
    )
    def query_affected_with_zone_filter(
        self, write_path: str, write_revision: int, zone_id: str
    ) -> None:
        """Query affected with zone filter — must be subset of unfiltered."""
        unfiltered = self.registry.get_affected_queries(write_path, write_revision)
        filtered = self.registry.get_affected_queries(write_path, write_revision, zone_id=zone_id)

        assert filtered.issubset(unfiltered)

        # All filtered results must have the correct zone
        for qid in filtered:
            rs = self.model.get(qid)
            assert rs is not None
            assert rs.zone_id == zone_id

    @precondition(lambda self: len(self.model) > 0)
    @rule()
    def re_register_existing(self) -> None:
        """Re-registering with the same query_id updates the entry."""
        query_id = next(iter(self.model))
        old_rs = self.model[query_id]

        # Create a new read set with the same ID but different entries
        new_rs = ReadSet(query_id=query_id, zone_id=old_rs.zone_id)
        new_rs.record_read("file", "/replaced/file.txt", revision=99)

        self.registry.register(new_rs)
        self.model[query_id] = new_rs

    @precondition(lambda self: len(self.model) > 0)
    @rule()
    def get_queries_for_zone(self) -> None:
        """get_queries_for_zone returns correct set."""
        # Pick a zone that has at least one read set
        zone_counts: dict[str, set[str]] = {}
        for qid, rs in self.model.items():
            zone_counts.setdefault(rs.zone_id, set()).add(qid)

        for zone_id, expected_ids in zone_counts.items():
            actual_ids = self.registry.get_queries_for_zone(zone_id)
            assert actual_ids == expected_ids, (
                f"Zone {zone_id}: expected {expected_ids}, got {actual_ids}"
            )

    # ----- Invariants (checked after every rule) -----

    @invariant()
    def count_matches_model(self) -> None:
        """Registry length equals model length."""
        assert len(self.registry) == len(self.model), (
            f"Count mismatch: registry={len(self.registry)}, model={len(self.model)}"
        )

    @invariant()
    def all_model_entries_retrievable(self) -> None:
        """Every query_id in the model is retrievable from the registry."""
        for query_id in self.model:
            rs = self.registry.get_read_set(query_id)
            assert rs is not None, f"Model has {query_id} but registry.get_read_set returned None"

    @invariant()
    def no_extra_entries_in_registry(self) -> None:
        """Registry stats should not show more read sets than the model."""
        stats = self.registry.get_stats()
        assert stats["read_sets_count"] == len(self.model)


# ---------------------------------------------------------------------------
# Expose to pytest
# ---------------------------------------------------------------------------

# Inherit from active Hypothesis profile, override stateful-specific settings
_profile = os.getenv("HYPOTHESIS_PROFILE", "dev")
_base = settings.get_profile(_profile)
_step_count = {"dev": 30, "ci": 50, "thorough": 100}.get(_profile, 30)

ReadSetRegistryStateMachine.TestCase.settings = settings(
    _base,
    stateful_step_count=_step_count,
    deadline=None,
)

TestReadSetRegistryStateful = ReadSetRegistryStateMachine.TestCase
