"""Tests for ZoneIsolationValidator — zone isolation enforcement.

Covers:
- Default zone resolution
- Cross-zone blocking for non-allowed relations
- Cross-zone allowing for CROSS_ZONE_ALLOWED_RELATIONS
- Subject zone mismatch detection
- ZoneIsolationError attributes
- ZoneIsolationValidator with enforce=False (kill-switch) — functional tests
- is_cross_zone_readable() policy check

Related: Issue #1459 (decomposition), Issue #773 (zone isolation)
"""

from __future__ import annotations

import pytest

from nexus.services.permissions.consistency.zone_manager import (
    ZoneIsolationError,
    ZoneIsolationValidator,
)


class TestZoneIsolationValidatorDefaults:
    """Test zone_id default resolution."""

    def test_none_zone_id_defaults_to_default(self):
        mgr = ZoneIsolationValidator(enforce=True)
        zone_id, subj_z, obj_z, cross = mgr.validate_write_zones(
            zone_id=None, subject_zone_id=None, object_zone_id=None, relation="editor"
        )
        assert zone_id == "default"
        assert subj_z == "default"
        assert obj_z == "default"
        assert cross is False

    def test_empty_zone_id_defaults_to_default(self):
        mgr = ZoneIsolationValidator(enforce=True)
        zone_id, subj_z, obj_z, cross = mgr.validate_write_zones(
            zone_id="", subject_zone_id=None, object_zone_id=None, relation="viewer"
        )
        assert zone_id == "default"
        assert subj_z == "default"
        assert obj_z == "default"

    def test_explicit_zone_id_preserved(self):
        mgr = ZoneIsolationValidator(enforce=True)
        zone_id, subj_z, obj_z, cross = mgr.validate_write_zones(
            zone_id="org_acme", subject_zone_id=None, object_zone_id=None, relation="editor"
        )
        assert zone_id == "org_acme"
        assert subj_z == "org_acme"
        assert obj_z == "org_acme"
        assert cross is False

    def test_subject_zone_defaults_to_zone_id(self):
        mgr = ZoneIsolationValidator(enforce=True)
        _, subj_z, _, _ = mgr.validate_write_zones(
            zone_id="org_x", subject_zone_id=None, object_zone_id=None, relation="member"
        )
        assert subj_z == "org_x"

    def test_object_zone_defaults_to_zone_id(self):
        mgr = ZoneIsolationValidator(enforce=True)
        _, _, obj_z, _ = mgr.validate_write_zones(
            zone_id="org_x", subject_zone_id=None, object_zone_id=None, relation="member"
        )
        assert obj_z == "org_x"


class TestZoneIsolationEnforcement:
    """Test cross-zone blocking and allowing."""

    def test_same_zone_allowed(self):
        mgr = ZoneIsolationValidator(enforce=True)
        zone_id, _, _, cross = mgr.validate_write_zones(
            zone_id="org_a",
            subject_zone_id="org_a",
            object_zone_id="org_a",
            relation="editor",
        )
        assert zone_id == "org_a"
        assert cross is False

    def test_cross_zone_non_allowed_relation_raises(self):
        mgr = ZoneIsolationValidator(enforce=True)
        with pytest.raises(ZoneIsolationError, match="Cannot create cross-zone"):
            mgr.validate_write_zones(
                zone_id="org_a",
                subject_zone_id="org_a",
                object_zone_id="org_b",
                relation="editor",
            )

    def test_cross_zone_allowed_relation_succeeds(self):
        mgr = ZoneIsolationValidator(enforce=True)
        zone_id, subj_z, obj_z, cross = mgr.validate_write_zones(
            zone_id="org_a",
            subject_zone_id="org_a",
            object_zone_id="org_b",
            relation="shared-viewer",
        )
        # Cross-zone shares stored in object's zone
        assert zone_id == "org_b"
        assert subj_z == "org_a"
        assert obj_z == "org_b"
        assert cross is True

    def test_cross_zone_shared_editor_allowed(self):
        mgr = ZoneIsolationValidator(enforce=True)
        zone_id, _, _, cross = mgr.validate_write_zones(
            zone_id="org_a",
            subject_zone_id="org_a",
            object_zone_id="org_b",
            relation="shared-editor",
        )
        assert zone_id == "org_b"
        assert cross is True

    def test_cross_zone_shared_owner_allowed(self):
        mgr = ZoneIsolationValidator(enforce=True)
        zone_id, _, _, cross = mgr.validate_write_zones(
            zone_id="org_a",
            subject_zone_id="org_a",
            object_zone_id="org_b",
            relation="shared-owner",
        )
        assert zone_id == "org_b"
        assert cross is True

    def test_subject_zone_mismatch_raises(self):
        """When subject_zone differs from both zone_id and object_zone, it's a cross-zone violation."""
        mgr = ZoneIsolationValidator(enforce=True)
        with pytest.raises(ZoneIsolationError, match="Cannot create cross-zone"):
            mgr.validate_write_zones(
                zone_id="org_a",
                subject_zone_id="org_b",
                object_zone_id="org_a",
                relation="editor",
            )


class TestZoneIsolationErrorAttributes:
    """Test ZoneIsolationError exception attributes."""

    def test_error_has_subject_zone(self):
        err = ZoneIsolationError("test", "zone_a", "zone_b")
        assert err.subject_zone == "zone_a"
        assert err.object_zone == "zone_b"
        assert str(err) == "test"

    def test_error_with_none_zones(self):
        err = ZoneIsolationError("msg", None, None)
        assert err.subject_zone is None
        assert err.object_zone is None


class TestZoneIsolationValidatorKillSwitch:
    """Test enforce=False bypasses validation — functional tests."""

    def test_enforce_false_flag_set(self):
        mgr = ZoneIsolationValidator(enforce=False)
        assert mgr.enforce is False

    def test_enforce_true_flag_set(self):
        mgr = ZoneIsolationValidator(enforce=True)
        assert mgr.enforce is True

    def test_enforce_false_allows_cross_zone_non_allowed_relation(self):
        """Kill-switch should allow cross-zone writes that would normally be rejected."""
        mgr = ZoneIsolationValidator(enforce=False)
        zone_id, subj_z, obj_z, cross = mgr.validate_write_zones(
            zone_id="org_a",
            subject_zone_id="org_a",
            object_zone_id="org_b",
            relation="editor",  # Not in CROSS_ZONE_ALLOWED_RELATIONS
        )
        # Should NOT raise ZoneIsolationError
        assert zone_id == "org_a"  # Original zone preserved (no cross-zone redirect)
        assert subj_z == "org_a"
        assert obj_z == "org_b"
        assert cross is True

    def test_enforce_false_allows_subject_zone_mismatch(self):
        """Kill-switch should allow subject zone mismatch."""
        mgr = ZoneIsolationValidator(enforce=False)
        zone_id, subj_z, obj_z, cross = mgr.validate_write_zones(
            zone_id="org_a",
            subject_zone_id="org_b",
            object_zone_id="org_a",
            relation="editor",
        )
        # Should NOT raise ZoneIsolationError
        assert zone_id == "org_a"
        assert subj_z == "org_b"
        assert obj_z == "org_a"
        assert cross is True

    def test_enforce_false_still_resolves_defaults(self):
        """Kill-switch should still resolve None zones to defaults."""
        mgr = ZoneIsolationValidator(enforce=False)
        zone_id, subj_z, obj_z, cross = mgr.validate_write_zones(
            zone_id=None,
            subject_zone_id=None,
            object_zone_id=None,
            relation="editor",
        )
        assert zone_id == "default"
        assert subj_z == "default"
        assert obj_z == "default"
        assert cross is False


class TestIsCrossZoneReadable:
    """Test is_cross_zone_readable() policy check."""

    def test_shared_viewer_is_cross_zone_readable(self):
        mgr = ZoneIsolationValidator()
        assert mgr.is_cross_zone_readable("shared-viewer") is True

    def test_shared_editor_is_cross_zone_readable(self):
        mgr = ZoneIsolationValidator()
        assert mgr.is_cross_zone_readable("shared-editor") is True

    def test_shared_owner_is_cross_zone_readable(self):
        mgr = ZoneIsolationValidator()
        assert mgr.is_cross_zone_readable("shared-owner") is True

    def test_editor_is_not_cross_zone_readable(self):
        mgr = ZoneIsolationValidator()
        assert mgr.is_cross_zone_readable("editor") is False

    def test_viewer_is_not_cross_zone_readable(self):
        mgr = ZoneIsolationValidator()
        assert mgr.is_cross_zone_readable("viewer") is False

    def test_member_is_not_cross_zone_readable(self):
        mgr = ZoneIsolationValidator()
        assert mgr.is_cross_zone_readable("member") is False

    def test_owner_is_not_cross_zone_readable(self):
        mgr = ZoneIsolationValidator()
        assert mgr.is_cross_zone_readable("owner") is False

    def test_empty_relation_is_not_cross_zone_readable(self):
        mgr = ZoneIsolationValidator()
        assert mgr.is_cross_zone_readable("") is False
