"""Property-based and parametrized tests for derive_grants() (Issue #1271).

Tests the core anti-escalation invariant and all delegation modes
using Hypothesis property-based testing and pytest parametrization.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.services.delegation.derivation import (
    MAX_DELEGATABLE_GRANTS,
    GrantSpec,
    derive_grants,
)
from nexus.services.delegation.errors import (
    EscalationError,
    TooManyGrantsError,
)
from nexus.services.delegation.models import DelegationMode

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Generate realistic file paths
path_chars = st.sampled_from(list("abcdefghijklmnopqrstuvwxyz0123456789_-."))
path_segment = st.text(path_chars, min_size=1, max_size=10)
file_path = st.builds(
    lambda segments: "/" + "/".join(segments),
    st.lists(path_segment, min_size=1, max_size=5),
)

# Generate a relation
relation_st = st.sampled_from(["direct_editor", "direct_viewer"])

# Generate a parent grant: (relation, object_id)
parent_grant_st = st.tuples(relation_st, file_path)

# Generate a list of unique parent grants
parent_grants_st = st.lists(parent_grant_st, min_size=0, max_size=50, unique_by=lambda x: x[1])


# ---------------------------------------------------------------------------
# Property tests: Anti-escalation invariant
# ---------------------------------------------------------------------------


class TestAntiEscalationInvariant:
    """For ALL modes: derived grants ⊆ parent grants (by object_id)."""

    @given(parent_grants=parent_grants_st)
    @settings(max_examples=100)
    def test_copy_mode_subset(self, parent_grants: list[tuple[str, str]]):
        """COPY mode: child object_ids are subset of parent object_ids."""
        parent_ids = {obj_id for _, obj_id in parent_grants}
        result = derive_grants(parent_grants, DelegationMode.COPY)
        child_ids = {g.object_id for g in result}
        assert child_ids <= parent_ids

    @given(parent_grants=parent_grants_st)
    @settings(max_examples=100)
    def test_shared_mode_subset(self, parent_grants: list[tuple[str, str]]):
        """SHARED mode: child object_ids are subset of parent object_ids."""
        parent_ids = {obj_id for _, obj_id in parent_grants}
        result = derive_grants(parent_grants, DelegationMode.SHARED)
        child_ids = {g.object_id for g in result}
        assert child_ids <= parent_ids

    @given(parent_grants=parent_grants_st)
    @settings(max_examples=100)
    def test_clean_mode_empty_add(self, parent_grants: list[tuple[str, str]]):
        """CLEAN mode with no add_grants produces empty result."""
        result = derive_grants(parent_grants, DelegationMode.CLEAN)
        assert result == []

    @given(parent_grants=parent_grants_st)
    @settings(max_examples=100)
    def test_clean_mode_subset_add(self, parent_grants: list[tuple[str, str]]):
        """CLEAN mode: can only add grants that parent has."""
        if not parent_grants:
            return
        # Pick a subset of parent object_ids to add
        parent_ids = [obj_id for _, obj_id in parent_grants]
        add = parent_ids[: len(parent_ids) // 2]
        result = derive_grants(parent_grants, DelegationMode.CLEAN, add_grants=add)
        child_ids = {g.object_id for g in result}
        assert child_ids <= set(parent_ids)


class TestCopyModeMonotonicity:
    """COPY mode: removing more grants → fewer or equal child grants."""

    @given(parent_grants=parent_grants_st)
    @settings(max_examples=100)
    def test_more_removals_fewer_grants(self, parent_grants: list[tuple[str, str]]):
        parent_ids = [obj_id for _, obj_id in parent_grants]
        if len(parent_ids) < 2:
            return

        # Small removal set
        small_remove = parent_ids[:1]
        # Larger removal set
        large_remove = parent_ids[: len(parent_ids) // 2 + 1]

        result_small = derive_grants(parent_grants, DelegationMode.COPY, remove_grants=small_remove)
        result_large = derive_grants(parent_grants, DelegationMode.COPY, remove_grants=large_remove)

        assert len(result_large) <= len(result_small)


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------


class TestCopyMode:
    def test_empty_parent(self):
        """Empty parent grants → empty child grants."""
        result = derive_grants([], DelegationMode.COPY)
        assert result == []

    def test_full_copy(self):
        """Full copy without removals returns all parent grants."""
        grants = [
            ("direct_editor", "/a/b.txt"),
            ("direct_viewer", "/a/c.txt"),
        ]
        result = derive_grants(grants, DelegationMode.COPY)
        assert len(result) == 2
        ids = {g.object_id for g in result}
        assert ids == {"/a/b.txt", "/a/c.txt"}

    def test_remove_grants(self):
        """Remove specific paths from copy."""
        grants = [
            ("direct_editor", "/a/b.txt"),
            ("direct_editor", "/a/c.txt"),
            ("direct_viewer", "/a/d.txt"),
        ]
        result = derive_grants(grants, DelegationMode.COPY, remove_grants=["/a/c.txt"])
        ids = {g.object_id for g in result}
        assert "/a/c.txt" not in ids
        assert "/a/b.txt" in ids
        assert "/a/d.txt" in ids

    def test_readonly_downgrade(self):
        """Readonly paths are downgraded from editor to viewer."""
        grants = [
            ("direct_editor", "/a/secret.txt"),
            ("direct_editor", "/a/normal.txt"),
        ]
        result = derive_grants(
            grants,
            DelegationMode.COPY,
            readonly_paths=["/a/secret.txt"],
        )
        by_id = {g.object_id: g for g in result}
        assert "viewer" in by_id["/a/secret.txt"].relation
        assert "editor" in by_id["/a/normal.txt"].relation

    def test_scope_prefix_filter(self):
        """Scope prefix filters grants to matching paths."""
        grants = [
            ("direct_editor", "/workspace/proj/a.txt"),
            ("direct_editor", "/workspace/proj/b.txt"),
            ("direct_viewer", "/workspace/other/c.txt"),
        ]
        result = derive_grants(
            grants,
            DelegationMode.COPY,
            scope_prefix="/workspace/proj",
        )
        ids = {g.object_id for g in result}
        assert ids == {"/workspace/proj/a.txt", "/workspace/proj/b.txt"}

    def test_readonly_viewer_unchanged(self):
        """Readonly on a viewer path doesn't change the relation."""
        grants = [("direct_viewer", "/a/b.txt")]
        result = derive_grants(
            grants,
            DelegationMode.COPY,
            readonly_paths=["/a/b.txt"],
        )
        assert len(result) == 1
        assert result[0].relation == "direct_viewer"


class TestCleanMode:
    def test_empty_add(self):
        """Clean mode with no add_grants returns empty."""
        grants = [("direct_editor", "/a.txt")]
        result = derive_grants(grants, DelegationMode.CLEAN)
        assert result == []

    def test_valid_add(self):
        """Clean mode with valid add_grants returns those grants."""
        grants = [
            ("direct_editor", "/a.txt"),
            ("direct_viewer", "/b.txt"),
        ]
        result = derive_grants(grants, DelegationMode.CLEAN, add_grants=["/a.txt"])
        assert len(result) == 1
        assert result[0].object_id == "/a.txt"
        assert result[0].relation == "direct_editor"

    def test_escalation_error(self):
        """Clean mode with grants not in parent raises EscalationError."""
        grants = [("direct_editor", "/a.txt")]
        with pytest.raises(EscalationError, match="not held by parent"):
            derive_grants(
                grants,
                DelegationMode.CLEAN,
                add_grants=["/not_in_parent.txt"],
            )

    def test_partial_escalation(self):
        """Even one invalid grant raises EscalationError."""
        grants = [("direct_editor", "/a.txt")]
        with pytest.raises(EscalationError):
            derive_grants(
                grants,
                DelegationMode.CLEAN,
                add_grants=["/a.txt", "/nope.txt"],
            )

    def test_scope_prefix_with_add(self):
        """Clean mode respects scope_prefix filter on add_grants."""
        grants = [
            ("direct_editor", "/proj/a.txt"),
            ("direct_editor", "/other/b.txt"),
        ]
        result = derive_grants(
            grants,
            DelegationMode.CLEAN,
            add_grants=["/proj/a.txt", "/other/b.txt"],
            scope_prefix="/proj",
        )
        ids = {g.object_id for g in result}
        assert ids == {"/proj/a.txt"}


class TestSharedMode:
    def test_returns_all(self):
        """Shared mode returns all parent grants."""
        grants = [
            ("direct_editor", "/a.txt"),
            ("direct_viewer", "/b.txt"),
        ]
        result = derive_grants(grants, DelegationMode.SHARED)
        assert len(result) == 2

    def test_scope_prefix(self):
        """Shared mode with scope_prefix filters results."""
        grants = [
            ("direct_editor", "/proj/a.txt"),
            ("direct_viewer", "/other/b.txt"),
        ]
        result = derive_grants(grants, DelegationMode.SHARED, scope_prefix="/proj")
        assert len(result) == 1
        assert result[0].object_id == "/proj/a.txt"

    def test_empty_parent(self):
        """Shared mode with empty parent returns empty."""
        result = derive_grants([], DelegationMode.SHARED)
        assert result == []


class TestMaxGrantsCap:
    def test_exactly_max(self):
        """Exactly MAX_DELEGATABLE_GRANTS is allowed."""
        grants = [("direct_viewer", f"/file_{i}.txt") for i in range(MAX_DELEGATABLE_GRANTS)]
        result = derive_grants(grants, DelegationMode.COPY)
        assert len(result) == MAX_DELEGATABLE_GRANTS

    def test_exceeds_max(self):
        """Exceeding MAX_DELEGATABLE_GRANTS raises TooManyGrantsError."""
        grants = [("direct_viewer", f"/file_{i}.txt") for i in range(MAX_DELEGATABLE_GRANTS + 1)]
        with pytest.raises(TooManyGrantsError):
            derive_grants(grants, DelegationMode.COPY)

    def test_just_under_max(self):
        """MAX_DELEGATABLE_GRANTS - 1 is allowed."""
        grants = [("direct_viewer", f"/file_{i}.txt") for i in range(MAX_DELEGATABLE_GRANTS - 1)]
        result = derive_grants(grants, DelegationMode.COPY)
        assert len(result) == MAX_DELEGATABLE_GRANTS - 1


class TestGrantSpecOutput:
    def test_output_type(self):
        """All results are GrantSpec instances."""
        grants = [("direct_editor", "/a.txt")]
        result = derive_grants(grants, DelegationMode.COPY)
        assert all(isinstance(g, GrantSpec) for g in result)

    def test_object_type_is_file(self):
        """All GrantSpec objects have object_type='file'."""
        grants = [("direct_editor", "/a.txt"), ("direct_viewer", "/b.txt")]
        result = derive_grants(grants, DelegationMode.COPY)
        assert all(g.object_type == "file" for g in result)

    def test_duplicate_parent_grants_highest_privilege(self):
        """When parent has duplicate object_ids, highest privilege wins."""
        grants = [
            ("direct_viewer", "/a.txt"),
            ("direct_editor", "/a.txt"),
        ]
        result = derive_grants(grants, DelegationMode.COPY)
        assert len(result) == 1
        assert "editor" in result[0].relation
