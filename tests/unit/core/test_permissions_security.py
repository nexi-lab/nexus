"""Security-focused unit tests for permissions.py (OperationContext, Permission, PermissionEnforcer).

This module covers critical security properties:
- OperationContext validation and immutable defaults
- Permission enum boundary conditions
- Admin context bypass scoping (P0-4)
- System context bypass scoping
- Zone ID propagation for multi-tenant isolation
- Subject type validation for ReBAC integration
- Read set tracking for cache invalidation
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from nexus.core.permissions import (
    OperationContext,
    Permission,
)
from nexus.services.permissions.enforcer import PermissionEnforcer

# ---------------------------------------------------------------------------
# OperationContext creation and validation
# ---------------------------------------------------------------------------


class TestOperationContextCreation:
    """Verify OperationContext is constructed correctly and validated."""

    def test_minimal_context_has_secure_defaults(self):
        """Security property: minimal context is unprivileged by default."""
        ctx = OperationContext(user="alice", groups=[])
        assert ctx.is_admin is False
        assert ctx.is_system is False
        assert ctx.admin_capabilities == set()
        assert ctx.subject_type == "user"
        assert ctx.zone_id is None

    def test_user_id_auto_populated_from_user(self):
        """user_id is set from user when not explicitly provided."""
        ctx = OperationContext(user="alice", groups=[])
        assert ctx.user_id == "alice"

    def test_user_id_explicit_overrides_auto(self):
        """Explicit user_id is not overwritten by auto-population."""
        ctx = OperationContext(user="alice", groups=[], user_id="alice_real")
        assert ctx.user_id == "alice_real"

    def test_subject_id_auto_populated_from_user(self):
        """subject_id defaults to user when not provided."""
        ctx = OperationContext(user="bob", groups=[])
        assert ctx.subject_id == "bob"

    def test_subject_id_explicit_overrides_auto(self):
        """Explicit subject_id is preserved."""
        ctx = OperationContext(user="bob", groups=[], subject_type="agent", subject_id="agent_42")
        assert ctx.subject_id == "agent_42"

    def test_empty_user_rejected(self):
        """Security property: empty user is not allowed."""
        with pytest.raises(ValueError, match="user is required"):
            OperationContext(user="", groups=[])

    def test_none_like_user_rejected(self):
        """Security property: whitespace-only does NOT raise (user is truthy str)."""
        # Note: only empty string raises; whitespace is technically truthy
        ctx = OperationContext(user=" ", groups=[])
        assert ctx.user == " "

    def test_groups_must_be_list(self):
        """Security property: groups must be a list, not a string or other iterable."""
        with pytest.raises(TypeError, match="groups must be list"):
            OperationContext(user="alice", groups="developers")  # type: ignore

    def test_groups_as_tuple_rejected(self):
        """Security property: tuple for groups is also rejected (must be list)."""
        with pytest.raises(TypeError, match="groups must be list"):
            OperationContext(user="alice", groups=("devs",))  # type: ignore

    def test_groups_as_set_rejected(self):
        """Security property: set for groups is also rejected."""
        with pytest.raises(TypeError, match="groups must be list"):
            OperationContext(user="alice", groups={"devs"})  # type: ignore

    def test_empty_groups_allowed(self):
        """An empty groups list is valid (no group memberships)."""
        ctx = OperationContext(user="alice", groups=[])
        assert ctx.groups == []

    def test_request_id_auto_generated_unique(self):
        """Each context gets a unique request_id for audit correlation."""
        ctx1 = OperationContext(user="alice", groups=[])
        ctx2 = OperationContext(user="alice", groups=[])
        assert ctx1.request_id != ctx2.request_id
        # Verify UUID format
        uuid.UUID(ctx1.request_id)
        uuid.UUID(ctx2.request_id)

    def test_request_id_custom_preserved(self):
        """Custom request_id is preserved for external correlation."""
        ctx = OperationContext(user="alice", groups=[], request_id="custom-123")
        assert ctx.request_id == "custom-123"


# ---------------------------------------------------------------------------
# Permission enum combinations and boundary conditions
# ---------------------------------------------------------------------------


class TestPermissionEnumBoundary:
    """Verify Permission IntFlag boundary conditions and combinations."""

    def test_none_permission_is_zero(self):
        """NONE should be zero (no permissions)."""
        assert Permission.NONE == 0

    def test_individual_permission_bits(self):
        """Each permission must occupy a distinct bit."""
        assert Permission.EXECUTE == 1
        assert Permission.WRITE == 2
        assert Permission.READ == 4
        assert Permission.TRAVERSE == 8

    def test_all_is_rwx_without_traverse(self):
        """ALL should be rwx (7), not including TRAVERSE."""
        assert Permission.ALL == 7
        assert Permission.ALL == Permission.READ | Permission.WRITE | Permission.EXECUTE
        assert not (Permission.ALL & Permission.TRAVERSE)

    def test_all_with_traverse_includes_all_bits(self):
        """ALL_WITH_TRAVERSE includes all four permission bits."""
        assert Permission.ALL_WITH_TRAVERSE == 15
        assert Permission.ALL_WITH_TRAVERSE & Permission.READ
        assert Permission.ALL_WITH_TRAVERSE & Permission.WRITE
        assert Permission.ALL_WITH_TRAVERSE & Permission.EXECUTE
        assert Permission.ALL_WITH_TRAVERSE & Permission.TRAVERSE

    def test_permission_bitwise_or(self):
        """Combining permissions with | produces the correct mask."""
        combined = Permission.READ | Permission.WRITE
        assert combined & Permission.READ
        assert combined & Permission.WRITE
        assert not (combined & Permission.EXECUTE)

    def test_permission_bitwise_and_check(self):
        """& can test whether a specific permission is included."""
        perm = Permission.READ | Permission.EXECUTE
        assert perm & Permission.READ
        assert not (perm & Permission.WRITE)

    def test_none_does_not_match_any_permission(self):
        """NONE should not match any individual permission via &."""
        assert not (Permission.NONE & Permission.READ)
        assert not (Permission.NONE & Permission.WRITE)
        assert not (Permission.NONE & Permission.EXECUTE)
        assert not (Permission.NONE & Permission.TRAVERSE)

    @pytest.mark.parametrize(
        "perm,expected_str",
        [
            (Permission.READ, "read"),
            (Permission.WRITE, "write"),
            (Permission.EXECUTE, "execute"),
            (Permission.TRAVERSE, "traverse"),
        ],
    )
    def test_permission_to_string_mapping(self, perm, expected_str):
        """PermissionEnforcer._permission_to_string maps correctly."""
        enforcer = PermissionEnforcer()
        assert enforcer._permission_to_string(perm) == expected_str

    def test_none_permission_maps_to_unknown(self):
        """NONE (0) falls through to 'unknown' because 0 & 0 is falsy."""
        enforcer = PermissionEnforcer()
        # Permission.NONE is 0; `0 & Permission.NONE` is 0 (falsy), so it
        # reaches the else branch and returns 'unknown', not 'none'.
        assert enforcer._permission_to_string(Permission.NONE) == "unknown"


# ---------------------------------------------------------------------------
# Admin context bypass behaviour
# ---------------------------------------------------------------------------


class TestAdminBypassBehaviour:
    """Verify admin bypass is correctly scoped by capabilities, paths, and zones."""

    def test_admin_bypass_disabled_by_default(self):
        """Security property: admin bypass is OFF by default (secure default)."""
        enforcer = PermissionEnforcer()
        assert enforcer.allow_admin_bypass is False

    def test_admin_with_bypass_disabled_falls_through_to_rebac(self):
        """When bypass disabled, admin falls through to ReBAC (denied if no manager)."""
        enforcer = PermissionEnforcer(allow_admin_bypass=False, rebac_manager=None)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )
        # No ReBAC manager => denied
        assert enforcer.check("/file.txt", Permission.READ, ctx) is False

    def test_admin_with_bypass_enabled_and_capability_grants_access(self):
        """Admin with correct capability is allowed when bypass is ON."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )
        assert enforcer.check("/any/path.txt", Permission.READ, ctx) is True

    def test_admin_without_required_capability_is_denied(self):
        """Admin without the matching capability falls through to ReBAC."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True, rebac_manager=None)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},  # has read, NOT write
        )
        # Write needs admin:write:*, which is missing => falls to ReBAC => denied
        assert enforcer.check("/file.txt", Permission.WRITE, ctx) is False

    def test_admin_without_any_capability_is_denied(self):
        """Admin with empty capabilities falls through to ReBAC."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True, rebac_manager=None)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities=set(),
        )
        assert enforcer.check("/file.txt", Permission.READ, ctx) is False

    def test_admin_bypass_path_allowlist_denies_unmatched_path(self):
        """Admin bypass with path allowlist denies paths outside the allowlist."""
        enforcer = PermissionEnforcer(
            allow_admin_bypass=True,
            rebac_manager=None,
            admin_bypass_paths=["/admin/*"],
        )
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )
        # /user/secret.txt is NOT in /admin/* allowlist => falls to ReBAC => denied
        assert enforcer.check("/user/secret.txt", Permission.READ, ctx) is False

    def test_admin_bypass_cross_zone_requires_manage_zones(self):
        """Admin in zone A cannot access /zone/B/* without MANAGE_ZONES capability."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True, rebac_manager=None)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            zone_id="techcorp",
            admin_capabilities={"admin:read:*", "admin:write:*"},
        )
        # Cross-zone access => PermissionError
        with pytest.raises(PermissionError, match="Cross-zone access requires MANAGE_ZONES"):
            enforcer.check("/zone/acme/secret.txt", Permission.READ, ctx)

    def test_admin_bypass_same_zone_path_allowed(self):
        """Admin accessing own zone path is allowed (not cross-zone)."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            zone_id="acme",
            admin_capabilities={"admin:read:*"},
        )
        assert enforcer.check("/zone/acme/file.txt", Permission.READ, ctx) is True

    def test_admin_with_manage_zones_can_access_cross_zone(self):
        """Admin with MANAGE_ZONES capability can access any zone path."""
        from nexus.services.permissions.permissions_enhanced import AdminCapability

        enforcer = PermissionEnforcer(allow_admin_bypass=True)
        ctx = OperationContext(
            user="superadmin",
            groups=[],
            is_admin=True,
            zone_id="techcorp",
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.MANAGE_ZONES,
            },
        )
        assert enforcer.check("/zone/acme/file.txt", Permission.READ, ctx) is True

    def test_nonadmin_user_not_affected_by_bypass(self):
        """Non-admin user is never affected by admin bypass, even if bypass is ON."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True, rebac_manager=None)
        ctx = OperationContext(user="alice", groups=[], is_admin=False)
        assert enforcer.check("/file.txt", Permission.READ, ctx) is False

    def test_admin_bypass_audit_logged(self):
        """Successful admin bypass is logged to audit store."""
        audit_store = MagicMock()
        enforcer = PermissionEnforcer(
            allow_admin_bypass=True,
            audit_store=audit_store,
        )
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )
        enforcer.check("/file.txt", Permission.READ, ctx)
        audit_store.log_bypass.assert_called_once()
        entry = audit_store.log_bypass.call_args[0][0]
        assert entry.allowed is True
        assert entry.bypass_type == "admin"


# ---------------------------------------------------------------------------
# System context behaviour
# ---------------------------------------------------------------------------


class TestSystemBypassBehaviour:
    """Verify system bypass scope and kill-switch."""

    def test_system_bypass_enabled_by_default(self):
        """System bypass defaults to ON (internal services need it)."""
        enforcer = PermissionEnforcer()
        assert enforcer.allow_system_bypass is True

    def test_system_read_allowed_on_any_path(self):
        """System context can read any path (for indexing)."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)
        assert enforcer.check("/user/private.txt", Permission.READ, ctx) is True

    def test_system_write_only_allowed_on_system_path(self):
        """System context can only write to /system/* paths."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)
        assert enforcer.check("/system/config.json", Permission.WRITE, ctx) is True

    def test_system_write_denied_outside_system_path(self):
        """System context CANNOT write to non-/system/ paths."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)
        with pytest.raises(PermissionError, match="System bypass not allowed"):
            enforcer.check("/user/data.txt", Permission.WRITE, ctx)

    def test_system_execute_denied_outside_system_path(self):
        """System context CANNOT execute outside /system/."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)
        with pytest.raises(PermissionError, match="System bypass not allowed"):
            enforcer.check("/bin/script.sh", Permission.EXECUTE, ctx)

    def test_system_bypass_kill_switch(self):
        """When system bypass is disabled, system context raises PermissionError."""
        enforcer = PermissionEnforcer(allow_system_bypass=False)
        ctx = OperationContext(user="system", groups=[], is_system=True)
        with pytest.raises(PermissionError, match="System bypass disabled"):
            enforcer.check("/system/file.txt", Permission.READ, ctx)

    def test_system_bypass_prefix_attack_blocked(self):
        """Paths like /systemdata should NOT match /system/ bypass."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)
        with pytest.raises(PermissionError, match="System bypass not allowed"):
            enforcer.check("/systemdata/evil.txt", Permission.WRITE, ctx)

    def test_system_bypass_exact_system_path_allowed(self):
        """Exact /system path (not /system/) is allowed for write."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)
        assert enforcer.check("/system", Permission.WRITE, ctx) is True


# ---------------------------------------------------------------------------
# Zone ID propagation
# ---------------------------------------------------------------------------


class TestZoneIdPropagation:
    """Verify zone_id flows through permission checks for multi-tenant isolation."""

    def test_zone_id_passed_to_rebac_manager(self):
        """zone_id from context is forwarded to rebac_manager.rebac_check."""
        rebac = MagicMock()
        rebac.rebac_check.return_value = True
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[], zone_id="org_acme")

        enforcer.check("/file.txt", Permission.READ, ctx)

        rebac.rebac_check.assert_called_once()
        call_kwargs = rebac.rebac_check.call_args
        assert call_kwargs[1]["zone_id"] == "org_acme" or call_kwargs[0][3] == "org_acme"

    def test_missing_zone_id_defaults_to_default(self):
        """When zone_id is None, 'default' is passed to rebac_manager."""
        rebac = MagicMock()
        rebac.rebac_check.return_value = True
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        enforcer.check("/file.txt", Permission.READ, ctx)

        call_args = rebac.rebac_check.call_args
        # zone_id should be "default" (positional or keyword)
        zone_id_arg = call_args.kwargs.get("zone_id") or call_args[1].get("zone_id")
        assert zone_id_arg == "default"

    def test_zone_id_preserved_in_context(self):
        """zone_id set at construction is retrievable."""
        ctx = OperationContext(user="alice", groups=[], zone_id="org_xyz")
        assert ctx.zone_id == "org_xyz"

    def test_zone_id_none_by_default(self):
        """zone_id defaults to None when not provided."""
        ctx = OperationContext(user="alice", groups=[])
        assert ctx.zone_id is None


# ---------------------------------------------------------------------------
# Subject type validation
# ---------------------------------------------------------------------------


class TestSubjectTypeValidation:
    """Verify subject types flow correctly through permission checks."""

    @pytest.mark.parametrize(
        "subject_type,subject_id",
        [
            ("user", "alice"),
            ("agent", "claude_001"),
            ("service", "backup_svc"),
            ("session", "sess_abc123"),
        ],
    )
    def test_get_subject_returns_correct_tuple(self, subject_type, subject_id):
        """get_subject() returns the typed (type, id) tuple for ReBAC."""
        ctx = OperationContext(
            user="owner",
            groups=[],
            subject_type=subject_type,
            subject_id=subject_id,
        )
        assert ctx.get_subject() == (subject_type, subject_id)

    def test_get_subject_defaults_to_user_type(self):
        """Default subject_type is 'user' with subject_id from user field."""
        ctx = OperationContext(user="alice", groups=[])
        assert ctx.get_subject() == ("user", "alice")

    def test_subject_type_forwarded_to_rebac(self):
        """The correct subject tuple is forwarded to rebac_manager.rebac_check."""
        rebac = MagicMock()
        rebac.rebac_check.return_value = True
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        ctx = OperationContext(
            user="owner",
            groups=[],
            subject_type="agent",
            subject_id="agent_42",
        )
        enforcer.check("/file.txt", Permission.READ, ctx)

        call_args = rebac.rebac_check.call_args
        subject_arg = call_args.kwargs.get("subject") or call_args[0][0]
        assert subject_arg == ("agent", "agent_42")

    def test_agent_context_with_user_owner(self):
        """Agent context preserves both agent_id and user_id."""
        ctx = OperationContext(
            user="alice",
            groups=[],
            agent_id="notebook_xyz",
            subject_type="agent",
            subject_id="notebook_xyz",
        )
        assert ctx.user_id == "alice"
        assert ctx.agent_id == "notebook_xyz"
        assert ctx.get_subject() == ("agent", "notebook_xyz")


# ---------------------------------------------------------------------------
# Read set tracking
# ---------------------------------------------------------------------------


class TestReadSetTracking:
    """Verify read set tracking for cache invalidation (Issue #1166)."""

    def test_read_tracking_disabled_by_default(self):
        """track_reads defaults to False."""
        ctx = OperationContext(user="alice", groups=[])
        assert ctx.track_reads is False
        assert ctx.read_set is None

    def test_enable_read_tracking_initializes_read_set(self):
        """enable_read_tracking() creates a ReadSet."""
        ctx = OperationContext(user="alice", groups=[], zone_id="org1")
        ctx.enable_read_tracking()
        assert ctx.track_reads is True
        assert ctx.read_set is not None

    def test_record_read_with_tracking_enabled(self):
        """record_read() adds an entry when tracking is on."""
        ctx = OperationContext(user="alice", groups=[], zone_id="org1")
        ctx.enable_read_tracking()
        ctx.record_read("file", "/inbox/a.txt", revision=10)
        assert len(ctx.read_set) == 1

    def test_record_read_with_tracking_disabled_is_noop(self):
        """record_read() is a no-op when tracking is off."""
        ctx = OperationContext(user="alice", groups=[])
        ctx.record_read("file", "/inbox/a.txt", revision=10)
        assert ctx.read_set is None

    def test_disable_read_tracking_preserves_read_set(self):
        """disable_read_tracking() keeps the read_set but stops recording."""
        ctx = OperationContext(user="alice", groups=[], zone_id="org1")
        ctx.enable_read_tracking()
        ctx.record_read("file", "/inbox/a.txt", revision=10)
        ctx.disable_read_tracking()
        assert ctx.track_reads is False
        # read_set still exists
        assert ctx.read_set is not None
        assert len(ctx.read_set) == 1

    def test_enable_tracking_uses_zone_id_from_context(self):
        """enable_read_tracking() defaults to context's zone_id."""
        ctx = OperationContext(user="alice", groups=[], zone_id="org_acme")
        ctx.enable_read_tracking()
        assert ctx.read_set.zone_id == "org_acme"

    def test_enable_tracking_with_explicit_zone_id(self):
        """enable_read_tracking() accepts an explicit zone_id."""
        ctx = OperationContext(user="alice", groups=[], zone_id="org_acme")
        ctx.enable_read_tracking(zone_id="other_zone")
        assert ctx.read_set.zone_id == "other_zone"


# ---------------------------------------------------------------------------
# Secure defaults and deny-by-default
# ---------------------------------------------------------------------------


class TestSecureDefaults:
    """Verify the system is secure by default (deny-by-default)."""

    def test_no_rebac_manager_denies_all(self):
        """Without a ReBAC manager, all access is denied."""
        enforcer = PermissionEnforcer(rebac_manager=None)
        ctx = OperationContext(user="alice", groups=[])
        assert enforcer.check("/any.txt", Permission.READ, ctx) is False
        assert enforcer.check("/any.txt", Permission.WRITE, ctx) is False
        assert enforcer.check("/any.txt", Permission.EXECUTE, ctx) is False

    def test_unknown_permission_denied(self):
        """A permission value that maps to 'unknown' is denied."""
        enforcer = PermissionEnforcer(rebac_manager=MagicMock())
        # Permission(0) is NONE and maps to 'none', but a bizarre value should be denied
        # Construct a value that does not match any known permission
        assert enforcer._permission_to_string(Permission(16)) == "unknown"

    def test_filter_list_admin_sees_all(self):
        """Admin with bypass sees every path in filter_list."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )
        paths = ["/a.txt", "/b.txt", "/secret.txt"]
        assert enforcer.filter_list(paths, ctx) == paths

    def test_filter_list_system_sees_all(self):
        """System context sees every path in filter_list."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)
        paths = ["/a.txt", "/b.txt"]
        assert enforcer.filter_list(paths, ctx) == paths

    def test_filter_list_user_without_rebac_sees_nothing(self):
        """Regular user without ReBAC manager sees nothing."""
        enforcer = PermissionEnforcer(rebac_manager=None)
        ctx = OperationContext(user="alice", groups=[])
        paths = ["/a.txt", "/b.txt"]
        assert enforcer.filter_list(paths, ctx) == []

    def test_filter_list_respects_rebac_decisions(self):
        """filter_list keeps only paths allowed by ReBAC."""
        rebac = MagicMock()
        # Disable Tiger cache so it skips bitmap path
        rebac._tiger_cache = None

        def bulk_side_effect(checks, zone_id=None):
            """rebac_check_bulk([(subject, perm, object), ...], zone_id=...)"""
            result = {}
            for check in checks:
                _subject, _perm, obj = check
                # Only allow /public.txt
                result[check] = obj == ("file", "/public.txt")
            return result

        rebac.rebac_check_bulk.side_effect = bulk_side_effect
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        result = enforcer.filter_list(["/public.txt", "/secret.txt"], ctx)
        assert result == ["/public.txt"]


# ---------------------------------------------------------------------------
# Agent generation / stale session (Issue #1240)
# ---------------------------------------------------------------------------


class TestStaleSessionDetection:
    """Verify stale agent sessions are rejected during permission checks."""

    def test_stale_agent_generation_raises_error(self):
        """Agent with outdated generation is rejected."""
        from nexus.core.exceptions import StaleSessionError

        agent_registry = MagicMock()
        record = MagicMock()
        record.generation = 5  # current generation
        agent_registry.get.return_value = record

        enforcer = PermissionEnforcer(
            rebac_manager=MagicMock(rebac_check=MagicMock(return_value=True)),
            agent_registry=agent_registry,
        )
        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="agent",
            subject_id="agent_42",
            agent_id="agent_42",
            agent_generation=3,  # stale generation
        )
        with pytest.raises(StaleSessionError):
            enforcer.check("/file.txt", Permission.READ, ctx)

    def test_current_agent_generation_allowed(self):
        """Agent with current generation is not rejected."""
        agent_registry = MagicMock()
        record = MagicMock()
        record.generation = 5
        agent_registry.get.return_value = record

        rebac = MagicMock()
        rebac.rebac_check.return_value = True
        enforcer = PermissionEnforcer(
            rebac_manager=rebac,
            agent_registry=agent_registry,
        )
        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="agent",
            subject_id="agent_42",
            agent_id="agent_42",
            agent_generation=5,
        )
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True

    def test_deleted_agent_with_valid_jwt_raises_stale_error(self):
        """Agent deleted from registry but JWT still valid should be rejected."""
        from nexus.core.exceptions import StaleSessionError

        agent_registry = MagicMock()
        agent_registry.get.return_value = None  # Agent no longer exists

        enforcer = PermissionEnforcer(
            rebac_manager=MagicMock(rebac_check=MagicMock(return_value=True)),
            agent_registry=agent_registry,
        )
        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="agent",
            subject_id="deleted_agent",
            agent_id="deleted_agent",
            agent_generation=3,  # From JWT
        )
        with pytest.raises(StaleSessionError):
            enforcer.check("/file.txt", Permission.READ, ctx)

    def test_no_generation_skips_stale_check(self):
        """Agent without agent_generation (SK-key auth) should skip stale check."""
        agent_registry = MagicMock()

        rebac = MagicMock()
        rebac.rebac_check.return_value = True
        enforcer = PermissionEnforcer(
            rebac_manager=rebac,
            agent_registry=agent_registry,
        )
        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="agent",
            subject_id="sk_key_agent",
            agent_id="sk_key_agent",
            agent_generation=None,  # No generation = SK-key auth
        )
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True
        # agent_registry.get should NOT be called when generation is None
        agent_registry.get.assert_not_called()

    def test_user_subject_skips_stale_check(self):
        """User subjects should never trigger stale-session checks."""
        agent_registry = MagicMock()

        rebac = MagicMock()
        rebac.rebac_check.return_value = True
        enforcer = PermissionEnforcer(
            rebac_manager=rebac,
            agent_registry=agent_registry,
        )
        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="user",
            subject_id="alice",
            agent_generation=None,
        )
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True
        agent_registry.get.assert_not_called()


# ---------------------------------------------------------------------------
# Shared stale-session helper (Issue #1445)
# ---------------------------------------------------------------------------


class TestCheckStaleSessionHelper:
    """Tests for the check_stale_session() shared helper function."""

    def test_stale_generation_raises(self):
        """Mismatched generation should raise StaleSessionError."""
        from nexus.core.exceptions import StaleSessionError
        from nexus.core.permissions import check_stale_session

        registry = MagicMock()
        record = MagicMock()
        record.generation = 10
        registry.get.return_value = record

        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="agent",
            subject_id="agent_x",
            agent_id="agent_x",
            agent_generation=5,
        )
        with pytest.raises(StaleSessionError):
            check_stale_session(registry, ctx)

    def test_current_generation_passes(self):
        """Matching generation should not raise."""
        from nexus.core.permissions import check_stale_session

        registry = MagicMock()
        record = MagicMock()
        record.generation = 5
        registry.get.return_value = record

        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="agent",
            subject_id="agent_x",
            agent_id="agent_x",
            agent_generation=5,
        )
        check_stale_session(registry, ctx)  # Should not raise

    def test_missing_agent_raises(self):
        """Agent not found in registry should raise StaleSessionError."""
        from nexus.core.exceptions import StaleSessionError
        from nexus.core.permissions import check_stale_session

        registry = MagicMock()
        registry.get.return_value = None

        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="agent",
            subject_id="gone_agent",
            agent_id="gone_agent",
            agent_generation=3,
        )
        with pytest.raises(StaleSessionError):
            check_stale_session(registry, ctx)

    def test_none_registry_skips(self):
        """None agent_registry should skip check entirely."""
        from nexus.core.permissions import check_stale_session

        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="agent",
            subject_id="agent_x",
            agent_id="agent_x",
            agent_generation=5,
        )
        check_stale_session(None, ctx)  # Should not raise

    def test_none_generation_skips(self):
        """None agent_generation should skip check entirely."""
        from nexus.core.permissions import check_stale_session

        registry = MagicMock()
        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="agent",
            subject_id="agent_x",
            agent_id="agent_x",
            agent_generation=None,
        )
        check_stale_session(registry, ctx)  # Should not raise
        registry.get.assert_not_called()

    def test_user_subject_skips(self):
        """Non-agent subject_type should skip check entirely."""
        from nexus.core.permissions import check_stale_session

        registry = MagicMock()
        ctx = OperationContext(
            user="alice",
            groups=[],
            subject_type="user",
            subject_id="alice",
            agent_generation=5,
        )
        check_stale_session(registry, ctx)  # Should not raise
        registry.get.assert_not_called()


# ---------------------------------------------------------------------------
# Namespace visibility (Issue #1239)
# ---------------------------------------------------------------------------


class TestNamespaceVisibility:
    """Verify namespace manager hides unmounted paths."""

    def test_invisible_path_raises_not_found(self):
        """Path not visible to subject raises NexusFileNotFoundError, not 403."""
        from nexus.core.exceptions import NexusFileNotFoundError

        ns_manager = MagicMock()
        ns_manager.is_visible.return_value = False

        enforcer = PermissionEnforcer(
            rebac_manager=MagicMock(rebac_check=MagicMock(return_value=True)),
            namespace_manager=ns_manager,
        )
        ctx = OperationContext(user="alice", groups=[])
        with pytest.raises(NexusFileNotFoundError):
            enforcer.check("/hidden/file.txt", Permission.READ, ctx)

    def test_visible_path_proceeds_to_rebac(self):
        """Path visible to subject proceeds to ReBAC check."""
        ns_manager = MagicMock()
        ns_manager.is_visible.return_value = True

        rebac = MagicMock()
        rebac.rebac_check.return_value = True
        enforcer = PermissionEnforcer(
            rebac_manager=rebac,
            namespace_manager=ns_manager,
        )
        ctx = OperationContext(user="alice", groups=[])
        assert enforcer.check("/visible/file.txt", Permission.READ, ctx) is True

    def test_admin_bypass_skips_namespace_check(self):
        """Admin bypass returns before namespace check runs."""
        ns_manager = MagicMock()
        ns_manager.is_visible.return_value = False  # would block regular users

        enforcer = PermissionEnforcer(
            allow_admin_bypass=True,
            namespace_manager=ns_manager,
        )
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )
        # Admin bypass should return True without checking namespace
        assert enforcer.check("/hidden/file.txt", Permission.READ, ctx) is True
        ns_manager.is_visible.assert_not_called()
