"""Unit tests for delegation derivation pure functions (Issue #1271, #1618).

Tests derive_grants(), validate_scope_prefix(), and helper functions.
All functions are pure (no I/O), so these tests are fast and isolated.
"""

import pytest

from nexus.bricks.delegation.derivation import (
    GrantSpec,
    derive_grants,
    validate_scope_prefix,
)
from nexus.bricks.delegation.models import DelegationMode

# ---------------------------------------------------------------------------
# validate_scope_prefix
# ---------------------------------------------------------------------------


class TestValidateScopePrefix:
    """Issue 16A: scope_prefix boundary validation."""

    def test_none_is_valid(self):
        validate_scope_prefix(None)  # Should not raise

    def test_absolute_path_is_valid(self):
        validate_scope_prefix("/workspace/proj")

    def test_trailing_slash_is_valid(self):
        validate_scope_prefix("/workspace/proj/")

    def test_root_slash_is_valid(self):
        validate_scope_prefix("/")

    def test_empty_string_raises(self):
        with pytest.raises(Exception, match="empty"):
            validate_scope_prefix("")

    def test_relative_path_raises(self):
        # Use Exception to avoid xdist module double-loading identity mismatch
        with pytest.raises(Exception, match="absolute"):
            validate_scope_prefix("workspace/proj")

    def test_double_slash_raises(self):
        with pytest.raises(Exception, match="//"):
            validate_scope_prefix("/workspace//proj")

    def test_dot_dot_raises(self):
        # Use Exception to avoid xdist module double-loading identity mismatch
        with pytest.raises(Exception, match="\\.\\."):
            validate_scope_prefix("/workspace/../etc/passwd")

    def test_trailing_dot_dot_raises(self):
        with pytest.raises(Exception, match="\\.\\."):
            validate_scope_prefix("/workspace/..")

    def test_mid_dot_dot_raises(self):
        with pytest.raises(Exception, match="\\.\\."):
            validate_scope_prefix("/workspace/../secret")


# ---------------------------------------------------------------------------
# derive_grants — COPY mode
# ---------------------------------------------------------------------------


class TestDeriveCopy:
    """derive_grants with DelegationMode.COPY."""

    def test_copy_returns_all_parent_grants(self):
        grants = [
            ("direct_editor", "/a.py"),
            ("direct_viewer", "/b.py"),
        ]
        result = derive_grants(grants, DelegationMode.COPY)
        ids = {g.object_id for g in result}
        assert ids == {"/a.py", "/b.py"}

    def test_copy_preserves_relations(self):
        grants = [
            ("direct_editor", "/a.py"),
            ("direct_viewer", "/b.py"),
        ]
        result = derive_grants(grants, DelegationMode.COPY)
        relation_map = {g.object_id: g.relation for g in result}
        assert relation_map["/a.py"] == "direct_editor"
        assert relation_map["/b.py"] == "direct_viewer"

    def test_copy_with_remove_grants(self):
        grants = [
            ("direct_editor", "/a.py"),
            ("direct_editor", "/b.py"),
            ("direct_viewer", "/c.py"),
        ]
        result = derive_grants(grants, DelegationMode.COPY, remove_grants=["/b.py"])
        ids = {g.object_id for g in result}
        assert "/b.py" not in ids
        assert ids == {"/a.py", "/c.py"}

    def test_copy_with_readonly_paths(self):
        grants = [
            ("direct_editor", "/a.py"),
            ("direct_editor", "/b.py"),
        ]
        result = derive_grants(grants, DelegationMode.COPY, readonly_paths=["/a.py"])
        relation_map = {g.object_id: g.relation for g in result}
        assert relation_map["/a.py"] == "direct_viewer"  # downgraded
        assert relation_map["/b.py"] == "direct_editor"  # unchanged

    def test_copy_readonly_does_not_affect_viewer(self):
        """Readonly_paths on a viewer grant keeps it as viewer."""
        grants = [("direct_viewer", "/readonly.py")]
        result = derive_grants(grants, DelegationMode.COPY, readonly_paths=["/readonly.py"])
        assert result[0].relation == "direct_viewer"

    def test_copy_with_scope_prefix(self):
        grants = [
            ("direct_editor", "/workspace/proj/a.py"),
            ("direct_editor", "/workspace/other/b.py"),
        ]
        result = derive_grants(grants, DelegationMode.COPY, scope_prefix="/workspace/proj")
        ids = {g.object_id for g in result}
        assert ids == {"/workspace/proj/a.py"}

    def test_copy_empty_parent_grants(self):
        result = derive_grants([], DelegationMode.COPY)
        assert result == []

    def test_copy_all_removed(self):
        grants = [("direct_editor", "/a.py")]
        result = derive_grants(grants, DelegationMode.COPY, remove_grants=["/a.py"])
        assert result == []


# ---------------------------------------------------------------------------
# derive_grants — CLEAN mode
# ---------------------------------------------------------------------------


class TestDeriveClean:
    """derive_grants with DelegationMode.CLEAN."""

    def test_clean_returns_only_added(self):
        grants = [
            ("direct_editor", "/a.py"),
            ("direct_editor", "/b.py"),
            ("direct_viewer", "/c.py"),
        ]
        result = derive_grants(grants, DelegationMode.CLEAN, add_grants=["/a.py"])
        ids = {g.object_id for g in result}
        assert ids == {"/a.py"}

    def test_clean_preserves_parent_relation(self):
        grants = [("direct_viewer", "/a.py")]
        result = derive_grants(grants, DelegationMode.CLEAN, add_grants=["/a.py"])
        assert result[0].relation == "direct_viewer"

    def test_clean_escalation_raises(self):
        grants = [("direct_editor", "/a.py")]
        # Use Exception to avoid xdist module double-loading identity mismatch
        with pytest.raises(Exception, match="not held by parent"):
            derive_grants(grants, DelegationMode.CLEAN, add_grants=["/secret.py"])

    def test_clean_empty_add_returns_empty(self):
        grants = [("direct_editor", "/a.py")]
        result = derive_grants(grants, DelegationMode.CLEAN, add_grants=[])
        assert result == []

    def test_clean_none_add_returns_empty(self):
        grants = [("direct_editor", "/a.py")]
        result = derive_grants(grants, DelegationMode.CLEAN)
        assert result == []

    def test_clean_with_scope_prefix(self):
        grants = [
            ("direct_editor", "/workspace/proj/a.py"),
            ("direct_editor", "/workspace/other/b.py"),
        ]
        result = derive_grants(
            grants,
            DelegationMode.CLEAN,
            add_grants=["/workspace/proj/a.py", "/workspace/other/b.py"],
            scope_prefix="/workspace/proj",
        )
        ids = {g.object_id for g in result}
        assert ids == {"/workspace/proj/a.py"}


# ---------------------------------------------------------------------------
# derive_grants — SHARED mode
# ---------------------------------------------------------------------------


class TestDeriveShared:
    """derive_grants with DelegationMode.SHARED."""

    def test_shared_returns_all(self):
        grants = [
            ("direct_editor", "/a.py"),
            ("direct_viewer", "/b.py"),
        ]
        result = derive_grants(grants, DelegationMode.SHARED)
        ids = {g.object_id for g in result}
        assert ids == {"/a.py", "/b.py"}

    def test_shared_with_scope_prefix(self):
        grants = [
            ("direct_editor", "/workspace/proj/a.py"),
            ("direct_editor", "/workspace/other/b.py"),
        ]
        result = derive_grants(grants, DelegationMode.SHARED, scope_prefix="/workspace/proj")
        ids = {g.object_id for g in result}
        assert ids == {"/workspace/proj/a.py"}

    def test_shared_empty_parent_grants(self):
        result = derive_grants([], DelegationMode.SHARED)
        assert result == []


# ---------------------------------------------------------------------------
# derive_grants — edge cases
# ---------------------------------------------------------------------------


class TestDeriveEdgeCases:
    """Edge cases and invariants."""

    def test_invalid_mode_raises(self):
        # Use Exception to avoid xdist module double-loading identity mismatch
        with pytest.raises(Exception, match="Unknown delegation mode"):
            derive_grants([], "not_a_mode")  # type: ignore

    def test_max_grants_boundary(self):
        grants = [("direct_viewer", f"/f/{i}") for i in range(1000)]
        result = derive_grants(grants, DelegationMode.COPY)
        assert len(result) == 1000

    def test_over_max_grants_raises(self):
        grants = [("direct_viewer", f"/f/{i}") for i in range(1001)]
        # Use Exception to avoid xdist module double-loading identity mismatch
        with pytest.raises(Exception, match="exceeds maximum"):
            derive_grants(grants, DelegationMode.COPY)

    def test_duplicate_parent_grants_keeps_highest_privilege(self):
        """If parent has both viewer and editor for same object, keep editor."""
        grants = [
            ("direct_viewer", "/a.py"),
            ("direct_editor", "/a.py"),
        ]
        result = derive_grants(grants, DelegationMode.COPY)
        assert len(result) == 1
        assert result[0].relation == "direct_editor"

    def test_grant_spec_fields(self):
        grants = [("direct_editor", "/a.py")]
        result = derive_grants(grants, DelegationMode.COPY)
        assert result[0].object_type == "file"
        assert result[0].object_id == "/a.py"
        assert result[0].relation == "direct_editor"

    def test_grant_spec_frozen(self):
        spec = GrantSpec(object_type="file", object_id="/a.py", relation="direct_editor")
        with pytest.raises(AttributeError):
            spec.relation = "direct_viewer"  # type: ignore

    def test_scope_prefix_validated_in_derive_grants(self):
        """validate_scope_prefix is called inside derive_grants."""
        with pytest.raises(Exception, match="absolute"):
            derive_grants([], DelegationMode.COPY, scope_prefix="relative")
