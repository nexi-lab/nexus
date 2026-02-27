"""Hypothesis property-based tests for delegation anti-escalation invariant (Issue #1618).

The core invariant: for ALL inputs, derived_grants ⊆ parent_grants (by object_id).
This holds regardless of mode, scope_prefix, remove/add/readonly lists.

Uses Hypothesis to generate random inputs and verify the invariant holds.
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.bricks.delegation.derivation import (
    derive_grants,
    validate_scope_prefix,
)
from nexus.bricks.delegation.errors import (
    EscalationError,
    InvalidPrefixError,
)
from nexus.bricks.delegation.models import DelegationMode

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate absolute paths like /workspace/proj/file.py
_path_segment = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
    min_size=1,
    max_size=10,
)
_absolute_path = st.builds(
    lambda segs: "/" + "/".join(segs),
    st.lists(_path_segment, min_size=1, max_size=5),
)

_relation = st.sampled_from(["direct_editor", "direct_viewer"])
_grant = st.tuples(_relation, _absolute_path)
_grant_list = st.lists(_grant, min_size=0, max_size=50)

_mode = st.sampled_from([DelegationMode.COPY, DelegationMode.CLEAN, DelegationMode.SHARED])

# ---------------------------------------------------------------------------
# Anti-escalation invariant
# ---------------------------------------------------------------------------


class TestAntiEscalationInvariant:
    """The derived grants must be a subset of parent grants (by object_id)."""

    @given(parent_grants=_grant_list)
    @settings(max_examples=200, deadline=None)
    def test_copy_mode_subset(self, parent_grants):
        """COPY mode: derived object_ids ⊆ parent object_ids."""
        result = derive_grants(parent_grants, DelegationMode.COPY)
        parent_ids = {obj_id for _, obj_id in parent_grants}
        derived_ids = {g.object_id for g in result}
        assert derived_ids <= parent_ids

    @given(parent_grants=_grant_list)
    @settings(max_examples=200, deadline=None)
    def test_shared_mode_subset(self, parent_grants):
        """SHARED mode: derived object_ids ⊆ parent object_ids."""
        result = derive_grants(parent_grants, DelegationMode.SHARED)
        parent_ids = {obj_id for _, obj_id in parent_grants}
        derived_ids = {g.object_id for g in result}
        assert derived_ids <= parent_ids

    @given(parent_grants=_grant_list)
    @settings(max_examples=200, deadline=None)
    def test_clean_mode_subset_with_valid_adds(self, parent_grants):
        """CLEAN mode with valid add_grants: derived ⊆ parent."""
        if not parent_grants:
            result = derive_grants(parent_grants, DelegationMode.CLEAN)
            assert result == []
            return

        # Pick a random subset of parent grant IDs as add_grants
        parent_ids = list({obj_id for _, obj_id in parent_grants})
        # Use first few as add_grants
        add_grants = parent_ids[: min(3, len(parent_ids))]

        result = derive_grants(parent_grants, DelegationMode.CLEAN, add_grants=add_grants)
        derived_ids = {g.object_id for g in result}
        assert derived_ids <= set(parent_ids)

    @given(parent_grants=_grant_list, extra_path=_absolute_path)
    @settings(max_examples=100, deadline=None)
    def test_clean_mode_escalation_detected(self, parent_grants, extra_path):
        """CLEAN mode: adding a path not in parent raises EscalationError."""
        parent_ids = {obj_id for _, obj_id in parent_grants}
        if extra_path in parent_ids:
            return  # Skip if the extra path happens to be in parent

        with pytest.raises(EscalationError):
            derive_grants(parent_grants, DelegationMode.CLEAN, add_grants=[extra_path])


# ---------------------------------------------------------------------------
# Privilege monotonicity: readonly_paths can only downgrade, never upgrade
# ---------------------------------------------------------------------------


class TestPrivilegeMonotonicity:
    """Readonly paths can only reduce privilege, never increase."""

    @given(parent_grants=_grant_list)
    @settings(max_examples=200, deadline=None)
    def test_readonly_never_upgrades(self, parent_grants):
        """After applying readonly_paths, no relation should be higher than parent."""
        # Build parent relation map (highest privilege per object)
        parent_map: dict[str, str] = {}
        for relation, obj_id in parent_grants:
            existing = parent_map.get(obj_id)
            if existing is None or _rank(relation) > _rank(existing):
                parent_map[obj_id] = relation

        # Use all parent paths as readonly
        readonly_paths = list(parent_map.keys())

        result = derive_grants(parent_grants, DelegationMode.COPY, readonly_paths=readonly_paths)

        for grant in result:
            parent_relation = parent_map.get(grant.object_id)
            if parent_relation is not None:
                assert _rank(grant.relation) <= _rank(parent_relation), (
                    f"Derived relation {grant.relation} exceeds parent {parent_relation} "
                    f"for {grant.object_id}"
                )


# ---------------------------------------------------------------------------
# Scope prefix filtering
# ---------------------------------------------------------------------------


class TestScopePrefixProperty:
    """All derived grants must match the scope prefix (when set)."""

    @given(parent_grants=_grant_list, prefix=_absolute_path)
    @settings(max_examples=200, deadline=None)
    def test_all_derived_match_prefix(self, parent_grants, prefix):
        """With scope_prefix set, all derived grants match the prefix."""
        result = derive_grants(parent_grants, DelegationMode.COPY, scope_prefix=prefix)
        normalized_prefix = prefix.rstrip("/") + "/"
        for grant in result:
            assert grant.object_id.startswith(
                normalized_prefix
            ) or grant.object_id == prefix.rstrip("/"), (
                f"Grant {grant.object_id} does not match prefix {prefix}"
            )

    @given(parent_grants=_grant_list)
    @settings(max_examples=100, deadline=None)
    def test_no_prefix_returns_all_parents(self, parent_grants):
        """Without scope_prefix, SHARED returns all parent grants."""
        result = derive_grants(parent_grants, DelegationMode.SHARED)
        parent_ids = {obj_id for _, obj_id in parent_grants}
        derived_ids = {g.object_id for g in result}
        # SHARED with no prefix should return all unique parent IDs
        assert derived_ids == parent_ids


# ---------------------------------------------------------------------------
# Remove grants reduces set
# ---------------------------------------------------------------------------


class TestRemoveGrantsProperty:
    """Remove grants can only reduce the derived set."""

    @given(parent_grants=_grant_list)
    @settings(max_examples=200, deadline=None)
    def test_remove_reduces_set(self, parent_grants):
        """Derived with remove_grants ⊆ derived without remove_grants."""
        parent_ids = list({obj_id for _, obj_id in parent_grants})
        if not parent_ids:
            return

        # Remove first path
        remove = [parent_ids[0]]

        result_full = derive_grants(parent_grants, DelegationMode.COPY)
        result_removed = derive_grants(parent_grants, DelegationMode.COPY, remove_grants=remove)

        full_ids = {g.object_id for g in result_full}
        removed_ids = {g.object_id for g in result_removed}
        assert removed_ids <= full_ids


# ---------------------------------------------------------------------------
# validate_scope_prefix property
# ---------------------------------------------------------------------------


class TestPrefixValidationProperty:
    """Property tests for scope_prefix validation."""

    @given(path=_absolute_path)
    @settings(max_examples=100, deadline=None)
    def test_valid_absolute_paths_accepted(self, path):
        """Generated absolute paths should be accepted."""
        # Our path strategy generates paths like /seg1/seg2/... which are valid
        validate_scope_prefix(path)  # Should not raise

    @given(
        path=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-/."),
            min_size=1,
            max_size=20,
        ).filter(lambda p: not p.startswith("/"))
    )
    @settings(max_examples=50, deadline=None, database=None)
    def test_relative_paths_rejected(self, path):
        """All non-absolute paths should be rejected."""
        with pytest.raises(InvalidPrefixError):
            validate_scope_prefix(path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rank(relation: str) -> int:
    """Rank relations by privilege level."""
    if "editor" in relation:
        return 2
    if "viewer" in relation:
        return 1
    return 0
