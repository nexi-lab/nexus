"""Security-focused unit tests for rebac_manager_enhanced.py (EnhancedReBACManager).

This module covers critical security properties:
- Permission check enforcement (authorized vs unauthorized access)
- Zone isolation (cross-zone access prevention)
- Permission escalation prevention
- Admin fallback behaviour
- Permission bypass when enforcement is disabled
- Empty/null context handling
- Edge cases: empty paths, root path, very long paths
- Graph limit DoS protection (P0-5)
- Consistency level validation
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core.permissions import (
    OperationContext,
    Permission,
    PermissionEnforcer,
)
from nexus.rebac.manager import (
    CheckResult,
    ConsistencyLevel,
    ConsistencyMode,
    ConsistencyRequirement,
    GraphLimitExceeded,
    GraphLimits,
    TraversalStats,
    WriteResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_rebac(allowed_map: dict[tuple, bool] | None = None):
    """Create a mock ReBAC manager that answers permission queries.

    Args:
        allowed_map: dict mapping (subject_tuple, permission, object_tuple, zone_id)
                     to True/False.  If None, all checks return False.
    """
    allowed_map = allowed_map or {}
    rebac = MagicMock()

    def _check(subject, permission, object, zone_id=None):
        key = (subject, permission, object, zone_id or "default")
        return allowed_map.get(key, False)

    rebac.rebac_check.side_effect = _check
    return rebac


# ---------------------------------------------------------------------------
# Permission check enforcement
# ---------------------------------------------------------------------------


class TestPermissionEnforcement:
    """Verify permissions are correctly enforced (allowed/denied)."""

    def test_user_with_read_permission_can_read(self):
        """User with a granted read permission is allowed to read."""
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "read", ("file", "/doc.txt"), "default"): True,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check("/doc.txt", Permission.READ, ctx) is True

    def test_user_without_read_permission_is_denied(self):
        """User without any grant is denied."""
        rebac = _make_mock_rebac({})  # no grants at all
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check("/doc.txt", Permission.READ, ctx) is False

    def test_user_with_read_but_not_write(self):
        """User with read-only grant cannot write."""
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "read", ("file", "/doc.txt"), "default"): True,
                (("user", "alice"), "write", ("file", "/doc.txt"), "default"): False,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check("/doc.txt", Permission.READ, ctx) is True
        assert enforcer.check("/doc.txt", Permission.WRITE, ctx) is False

    def test_user_with_write_permission_can_write(self):
        """User with a granted write permission is allowed."""
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "write", ("file", "/doc.txt"), "default"): True,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check("/doc.txt", Permission.WRITE, ctx) is True

    def test_different_users_have_different_permissions(self):
        """Alice can read but Bob cannot."""
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "read", ("file", "/doc.txt"), "default"): True,
                (("user", "bob"), "read", ("file", "/doc.txt"), "default"): False,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        alice_ctx = OperationContext(user="alice", groups=[])
        bob_ctx = OperationContext(user="bob", groups=[])

        assert enforcer.check("/doc.txt", Permission.READ, alice_ctx) is True
        assert enforcer.check("/doc.txt", Permission.READ, bob_ctx) is False

    @pytest.mark.parametrize("perm", [Permission.READ, Permission.WRITE, Permission.EXECUTE])
    def test_all_permission_types_denied_without_grant(self, perm):
        """All permission types are denied when no grant exists."""
        enforcer = PermissionEnforcer(rebac_manager=_make_mock_rebac({}))
        ctx = OperationContext(user="alice", groups=[])
        assert enforcer.check("/file.txt", perm, ctx) is False


# ---------------------------------------------------------------------------
# Zone isolation
# ---------------------------------------------------------------------------


class TestZoneIsolation:
    """Verify cross-zone access is prevented."""

    def test_user_in_zone_a_cannot_access_zone_b_resource(self):
        """User granted read in zone_a is denied read in zone_b."""
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "read", ("file", "/doc.txt"), "zone_a"): True,
                (("user", "alice"), "read", ("file", "/doc.txt"), "zone_b"): False,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        ctx_a = OperationContext(user="alice", groups=[], zone_id="zone_a")
        ctx_b = OperationContext(user="alice", groups=[], zone_id="zone_b")

        assert enforcer.check("/doc.txt", Permission.READ, ctx_a) is True
        assert enforcer.check("/doc.txt", Permission.READ, ctx_b) is False

    def test_zone_id_forwarded_correctly(self):
        """The zone_id from OperationContext is forwarded to rebac_check."""
        rebac = MagicMock()
        rebac.rebac_check.return_value = True
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        ctx = OperationContext(user="alice", groups=[], zone_id="org_secret")
        enforcer.check("/file.txt", Permission.READ, ctx)

        call_kwargs = rebac.rebac_check.call_args
        zone_arg = call_kwargs.kwargs.get("zone_id") or call_kwargs[0][3]
        assert zone_arg == "org_secret"

    def test_admin_cross_zone_blocked_without_manage_zones(self):
        """Admin from zone_a trying /zone/zone_b/ is blocked without MANAGE_ZONES."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True, rebac_manager=None)
        ctx = OperationContext(
            user="zone_admin",
            groups=[],
            is_admin=True,
            zone_id="zone_a",
            admin_capabilities={"admin:read:*"},
        )
        with pytest.raises(PermissionError, match="Cross-zone access requires MANAGE_ZONES"):
            enforcer.check("/zone/zone_b/secret.txt", Permission.READ, ctx)

    def test_zone_none_defaults_to_default_zone(self):
        """When zone_id is None, 'default' is used."""
        rebac = MagicMock()
        rebac.rebac_check.return_value = True
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        enforcer.check("/file.txt", Permission.READ, ctx)

        call_kwargs = rebac.rebac_check.call_args
        zone_arg = call_kwargs.kwargs.get("zone_id") or call_kwargs[0][3]
        assert zone_arg == "default"


# ---------------------------------------------------------------------------
# Permission escalation prevention
# ---------------------------------------------------------------------------


class TestPermissionEscalation:
    """Verify users cannot escalate their own permissions."""

    def test_viewer_cannot_perform_write_via_read_grant(self):
        """A read-only grant does not imply write."""
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "read", ("file", "/doc.txt"), "default"): True,
                (("user", "alice"), "write", ("file", "/doc.txt"), "default"): False,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check("/doc.txt", Permission.WRITE, ctx) is False

    def test_reader_cannot_execute(self):
        """A read-only grant does not imply execute."""
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "read", ("file", "/script.sh"), "default"): True,
                (("user", "alice"), "execute", ("file", "/script.sh"), "default"): False,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check("/script.sh", Permission.EXECUTE, ctx) is False

    def test_nonadmin_cannot_bypass_by_setting_is_admin(self):
        """Setting is_admin=True is not enough; capabilities and bypass must also be on."""
        enforcer = PermissionEnforcer(
            allow_admin_bypass=False,  # bypass off
            rebac_manager=_make_mock_rebac({}),
        )
        ctx = OperationContext(
            user="mallory",
            groups=[],
            is_admin=True,  # claims admin
            admin_capabilities={"admin:read:*"},
        )
        # Bypass OFF => falls to ReBAC => denied (no grants)
        assert enforcer.check("/secret.txt", Permission.READ, ctx) is False

    def test_admin_without_write_capability_cannot_write(self):
        """Admin with only read capability cannot write even with bypass ON."""
        enforcer = PermissionEnforcer(
            allow_admin_bypass=True,
            rebac_manager=_make_mock_rebac({}),
        )
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},  # read only
        )
        # Missing admin:write:* => falls to ReBAC => denied
        assert enforcer.check("/file.txt", Permission.WRITE, ctx) is False


# ---------------------------------------------------------------------------
# Admin fallback behaviour
# ---------------------------------------------------------------------------


class TestAdminBehavior:
    """Verify admin bypass behavior is correct and scoped."""

    def test_admin_bypass_off_uses_rebac(self):
        """With bypass off, admins go through normal ReBAC."""
        rebac = _make_mock_rebac(
            {
                (("user", "admin"), "read", ("file", "/file.txt"), "default"): True,
            }
        )
        enforcer = PermissionEnforcer(allow_admin_bypass=False, rebac_manager=rebac)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True

    def test_admin_bypass_on_requires_capability(self):
        """Admin bypass ON but empty capabilities => falls to ReBAC."""
        rebac = _make_mock_rebac({})
        enforcer = PermissionEnforcer(allow_admin_bypass=True, rebac_manager=rebac)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities=set(),
        )
        assert enforcer.check("/file.txt", Permission.READ, ctx) is False

    def test_admin_bypass_path_allowlist(self):
        """Admin bypass with path allowlist only grants within allowlist."""
        enforcer = PermissionEnforcer(
            allow_admin_bypass=True,
            rebac_manager=_make_mock_rebac({}),
            admin_bypass_paths=["/admin/*"],
        )
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )
        # /admin/config.json matches allowlist
        assert enforcer.check("/admin/config.json", Permission.READ, ctx) is True
        # /user/data.txt does NOT match allowlist => falls to ReBAC => denied
        assert enforcer.check("/user/data.txt", Permission.READ, ctx) is False


# ---------------------------------------------------------------------------
# Permission bypass when enforcement is disabled (no ReBAC manager)
# ---------------------------------------------------------------------------


class TestPermissionBypassNoEnforcement:
    """Verify behavior when no ReBAC manager is configured."""

    def test_deny_by_default_without_rebac(self):
        """Without rebac_manager, all non-privileged access is denied."""
        enforcer = PermissionEnforcer(rebac_manager=None)
        ctx = OperationContext(user="alice", groups=[])
        assert enforcer.check("/file.txt", Permission.READ, ctx) is False

    def test_system_bypass_works_without_rebac(self):
        """System bypass works even without rebac_manager."""
        enforcer = PermissionEnforcer(rebac_manager=None)
        ctx = OperationContext(user="system", groups=[], is_system=True)
        assert enforcer.check("/any.txt", Permission.READ, ctx) is True

    def test_admin_bypass_works_without_rebac(self):
        """Admin bypass works even without rebac_manager."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True, rebac_manager=None)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True


# ---------------------------------------------------------------------------
# Empty/null context handling
# ---------------------------------------------------------------------------


class TestEmptyNullContextHandling:
    """Verify edge cases with empty or unusual contexts."""

    def test_empty_user_rejected(self):
        """Empty user string is rejected at construction."""
        with pytest.raises(ValueError, match="user is required"):
            OperationContext(user="", groups=[])

    def test_many_groups_allowed(self):
        """Large number of groups does not break context creation."""
        groups = [f"group_{i}" for i in range(1000)]
        ctx = OperationContext(user="alice", groups=groups)
        assert len(ctx.groups) == 1000

    def test_context_with_all_fields(self):
        """Context with every field set does not break."""
        ctx = OperationContext(
            user="alice",
            groups=["g1", "g2"],
            zone_id="zone1",
            agent_id="agent_x",
            agent_generation=3,
            is_admin=True,
            is_system=False,
            user_id="alice_id",
            subject_type="agent",
            subject_id="agent_x",
            admin_capabilities={"admin:read:*"},
            request_id="req-001",
            backend_path="/mnt/gcs/data",
            virtual_path="/workspace/data",
        )
        assert ctx.user == "alice"
        assert ctx.agent_generation == 3
        assert ctx.backend_path == "/mnt/gcs/data"
        assert ctx.virtual_path == "/workspace/data"


# ---------------------------------------------------------------------------
# Edge cases: paths
# ---------------------------------------------------------------------------


class TestPathEdgeCases:
    """Verify edge cases with various path formats."""

    def test_root_path_check(self):
        """Permission check on root path works."""
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "read", ("file", "/"), "default"): True,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check("/", Permission.READ, ctx) is True

    def test_deeply_nested_path(self):
        """Very deep path does not break permission check."""
        deep_path = "/a/b/c/d/e/f/g/h/i/j/k/l/m/n/o/p/q/r/s/t/u/v/w/x/y/z/file.txt"
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "read", ("file", deep_path), "default"): True,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check(deep_path, Permission.READ, ctx) is True

    def test_path_with_special_characters(self):
        """Path with spaces and unicode does not break."""
        path = "/docs/my file (2).txt"
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "read", ("file", path), "default"): True,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check(path, Permission.READ, ctx) is True

    def test_very_long_path_denied_without_grant(self):
        """Very long path is denied when no grant exists."""
        long_path = "/" + "/".join(["segment"] * 500) + "/file.txt"
        enforcer = PermissionEnforcer(rebac_manager=_make_mock_rebac({}))
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check(long_path, Permission.READ, ctx) is False

    def test_parent_directory_inheritance(self):
        """Permission on parent directory grants access to child file."""
        rebac = MagicMock()

        call_count = 0

        def _check(subject, permission, object, zone_id=None):
            nonlocal call_count
            call_count += 1
            _, path = object
            # Only grant on /workspace/ directory, not directly on file
            return path == "/workspace"

        rebac.rebac_check.side_effect = _check
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        result = enforcer.check("/workspace/file.txt", Permission.READ, ctx)
        assert result is True

    def test_no_parent_inheritance_when_no_parent_grant(self):
        """No parent has a grant => deny the file."""
        rebac = _make_mock_rebac({})
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        assert enforcer.check("/workspace/file.txt", Permission.READ, ctx) is False


# ---------------------------------------------------------------------------
# Graph limits and DoS protection (P0-5)
# ---------------------------------------------------------------------------


class TestGraphLimitProtection:
    """Verify graph limit constants and GraphLimitExceeded behavior."""

    def test_graph_limits_have_sane_values(self):
        """Graph limits should be positive and bounded."""
        assert GraphLimits.MAX_DEPTH > 0
        assert GraphLimits.MAX_FAN_OUT > 0
        assert GraphLimits.MAX_EXECUTION_TIME_MS > 0
        assert GraphLimits.MAX_VISITED_NODES > 0
        assert GraphLimits.MAX_TUPLE_QUERIES > 0

    def test_graph_limit_exceeded_message(self):
        """GraphLimitExceeded carries limit details."""
        exc = GraphLimitExceeded("timeout", 1000, 1500, path=["a", "b"])
        assert exc.limit_type == "timeout"
        assert exc.limit_value == 1000
        assert exc.actual_value == 1500
        assert exc.path == ["a", "b"]
        assert "timeout" in str(exc)

    def test_graph_limit_exceeded_http_error_timeout(self):
        """Timeout limit yields HTTP 503."""
        exc = GraphLimitExceeded("timeout", 1000, 1500)
        err = exc.to_http_error()
        assert err["code"] == 503

    def test_graph_limit_exceeded_http_error_other(self):
        """Non-timeout limit yields HTTP 429."""
        exc = GraphLimitExceeded("depth", 50, 60)
        err = exc.to_http_error()
        assert err["code"] == 429

    def test_graph_limit_exceeded_empty_path(self):
        """GraphLimitExceeded with no path defaults to empty list."""
        exc = GraphLimitExceeded("fan_out", 1000, 2000)
        assert exc.path == []


# ---------------------------------------------------------------------------
# Consistency levels and requirements
# ---------------------------------------------------------------------------


class TestConsistencyValidation:
    """Verify consistency requirement validation."""

    def test_at_least_as_fresh_requires_min_revision(self):
        """AT_LEAST_AS_FRESH mode requires min_revision."""
        with pytest.raises(ValueError, match="min_revision is required"):
            ConsistencyRequirement(mode=ConsistencyMode.AT_LEAST_AS_FRESH)

    def test_at_least_as_fresh_with_revision_ok(self):
        """AT_LEAST_AS_FRESH with min_revision succeeds."""
        req = ConsistencyRequirement(mode=ConsistencyMode.AT_LEAST_AS_FRESH, min_revision=42)
        assert req.min_revision == 42

    def test_minimize_latency_does_not_require_revision(self):
        """MINIMIZE_LATENCY mode does not need min_revision."""
        req = ConsistencyRequirement(mode=ConsistencyMode.MINIMIZE_LATENCY)
        assert req.min_revision is None

    def test_fully_consistent_does_not_require_revision(self):
        """FULLY_CONSISTENT mode does not need min_revision."""
        req = ConsistencyRequirement(mode=ConsistencyMode.FULLY_CONSISTENT)
        assert req.min_revision is None

    def test_to_legacy_level_mapping(self):
        """ConsistencyRequirement maps to correct legacy ConsistencyLevel."""
        assert (
            ConsistencyRequirement(mode=ConsistencyMode.MINIMIZE_LATENCY).to_legacy_level()
            == ConsistencyLevel.EVENTUAL
        )
        assert (
            ConsistencyRequirement(
                mode=ConsistencyMode.AT_LEAST_AS_FRESH, min_revision=1
            ).to_legacy_level()
            == ConsistencyLevel.BOUNDED
        )
        assert (
            ConsistencyRequirement(mode=ConsistencyMode.FULLY_CONSISTENT).to_legacy_level()
            == ConsistencyLevel.STRONG
        )


# ---------------------------------------------------------------------------
# CheckResult and WriteResult data classes
# ---------------------------------------------------------------------------


class TestResultDataClasses:
    """Verify CheckResult and WriteResult carry correct metadata."""

    def test_check_result_allowed(self):
        """CheckResult captures allowed=True."""
        result = CheckResult(
            allowed=True,
            consistency_token="tok_1",
            decision_time_ms=1.5,
            cached=False,
        )
        assert result.allowed is True
        assert result.indeterminate is False

    def test_check_result_denied(self):
        """CheckResult captures allowed=False."""
        result = CheckResult(
            allowed=False,
            consistency_token="tok_2",
            decision_time_ms=0.5,
            cached=True,
            cache_age_ms=100.0,
        )
        assert result.allowed is False
        assert result.cached is True

    def test_check_result_indeterminate(self):
        """CheckResult with indeterminate=True signals a limit-driven denial."""
        exc = GraphLimitExceeded("depth", 50, 60)
        result = CheckResult(
            allowed=False,
            consistency_token="tok_3",
            decision_time_ms=1000.0,
            cached=False,
            indeterminate=True,
            limit_exceeded=exc,
        )
        assert result.indeterminate is True
        assert result.limit_exceeded is not None
        assert result.limit_exceeded.limit_type == "depth"

    def test_write_result_metadata(self):
        """WriteResult carries tuple_id, revision, and consistency_token."""
        wr = WriteResult(
            tuple_id="uuid-123",
            revision=42,
            consistency_token="tok_42",
            written_at_ms=1000.0,
        )
        assert wr.tuple_id == "uuid-123"
        assert wr.revision == 42
        assert wr.consistency_token == "tok_42"


# ---------------------------------------------------------------------------
# Traversal stats
# ---------------------------------------------------------------------------


class TestTraversalStats:
    """Verify TraversalStats defaults."""

    def test_traversal_stats_defaults(self):
        """All counters start at zero."""
        stats = TraversalStats()
        assert stats.queries == 0
        assert stats.nodes_visited == 0
        assert stats.max_depth_reached == 0
        assert stats.cache_hits == 0
        assert stats.cache_misses == 0
        assert stats.duration_ms == 0.0


# ---------------------------------------------------------------------------
# TRAVERSE permission implication
# ---------------------------------------------------------------------------


class TestTraversePermission:
    """Verify TRAVERSE is implied by READ or WRITE."""

    def test_traverse_allowed_when_user_has_read(self):
        """User with READ is allowed to TRAVERSE (implicit)."""
        call_log = []

        def _check(subject, permission, object, zone_id=None):
            call_log.append(permission)
            _, path = object
            return bool(permission == "read" and path == "/dir")

        rebac = MagicMock()
        rebac.rebac_check.side_effect = _check
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        result = enforcer.check("/dir", Permission.TRAVERSE, ctx)
        assert result is True

    def test_traverse_allowed_when_user_has_write(self):
        """User with WRITE is allowed to TRAVERSE (implicit)."""

        def _check(subject, permission, object, zone_id=None):
            _, path = object
            return bool(permission == "write" and path == "/dir")

        rebac = MagicMock()
        rebac.rebac_check.side_effect = _check
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        result = enforcer.check("/dir", Permission.TRAVERSE, ctx)
        assert result is True

    def test_traverse_denied_when_no_read_or_write(self):
        """TRAVERSE is denied when user has neither READ nor WRITE."""
        rebac = _make_mock_rebac({})
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[])

        result = enforcer.check("/dir", Permission.TRAVERSE, ctx)
        assert result is False


# ---------------------------------------------------------------------------
# Audit logging for bypass events
# ---------------------------------------------------------------------------


class TestBypassAuditLogging:
    """Verify that bypass events are logged to the audit store."""

    def test_system_bypass_logged(self):
        """Successful system bypass is logged."""
        audit_store = MagicMock()
        enforcer = PermissionEnforcer(audit_store=audit_store)
        ctx = OperationContext(user="system", groups=[], is_system=True)

        enforcer.check("/system/config.json", Permission.READ, ctx)

        audit_store.log_bypass.assert_called_once()
        entry = audit_store.log_bypass.call_args[0][0]
        assert entry.bypass_type == "system"
        assert entry.allowed is True

    def test_denied_system_bypass_logged(self):
        """Denied system bypass (outside /system) is logged."""
        audit_store = MagicMock()
        enforcer = PermissionEnforcer(audit_store=audit_store)
        ctx = OperationContext(user="system", groups=[], is_system=True)

        with pytest.raises(PermissionError):
            enforcer.check("/user/data.txt", Permission.WRITE, ctx)

        audit_store.log_bypass.assert_called_once()
        entry = audit_store.log_bypass.call_args[0][0]
        assert entry.allowed is False

    def test_admin_bypass_denied_logged(self):
        """Admin bypass denied due to kill switch is logged."""
        audit_store = MagicMock()
        enforcer = PermissionEnforcer(
            allow_admin_bypass=False,
            rebac_manager=_make_mock_rebac({}),
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
        assert entry.allowed is False
        assert "kill_switch" in entry.denial_reason


# ---------------------------------------------------------------------------
# Multiple overlapping security checks
# ---------------------------------------------------------------------------


class TestCombinedSecurityScenarios:
    """Complex adversarial scenarios combining multiple security features."""

    def test_admin_in_wrong_zone_with_read_capability_is_blocked(self):
        """Admin with read:* in zone_a cannot read /zone/zone_b/ files."""
        enforcer = PermissionEnforcer(
            allow_admin_bypass=True,
            rebac_manager=_make_mock_rebac({}),
        )
        ctx = OperationContext(
            user="evil_admin",
            groups=[],
            is_admin=True,
            zone_id="zone_a",
            admin_capabilities={"admin:read:*", "admin:write:*"},
        )
        with pytest.raises(PermissionError, match="Cross-zone"):
            enforcer.check("/zone/zone_b/confidential.txt", Permission.READ, ctx)

    def test_system_context_cannot_write_user_paths_even_with_admin_bypass(self):
        """System context write is scoped to /system/ regardless of admin bypass."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True)
        ctx = OperationContext(
            user="system",
            groups=[],
            is_system=True,
        )
        # System bypass takes precedence (is_system checked before is_admin)
        with pytest.raises(PermissionError, match="System bypass not allowed"):
            enforcer.check("/user/data.txt", Permission.WRITE, ctx)

    def test_non_privileged_user_with_rebac_grant_is_allowed(self):
        """Regular user with a ReBAC grant is allowed (happy path)."""
        rebac = _make_mock_rebac(
            {
                (("user", "alice"), "read", ("file", "/shared/doc.txt"), "org_acme"): True,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=[], zone_id="org_acme")

        assert enforcer.check("/shared/doc.txt", Permission.READ, ctx) is True

    def test_agent_subject_with_grant_is_allowed(self):
        """Agent subjects are checked via ReBAC just like users."""
        rebac = _make_mock_rebac(
            {
                (("agent", "bot_1"), "read", ("file", "/data.csv"), "default"): True,
            }
        )
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(
            user="owner",
            groups=[],
            subject_type="agent",
            subject_id="bot_1",
        )
        assert enforcer.check("/data.csv", Permission.READ, ctx) is True

    def test_agent_subject_without_grant_is_denied(self):
        """Agent subjects without grants are denied."""
        rebac = _make_mock_rebac({})
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(
            user="owner",
            groups=[],
            subject_type="agent",
            subject_id="bot_1",
        )
        assert enforcer.check("/data.csv", Permission.READ, ctx) is False
