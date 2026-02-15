"""Tests for batch ancestor resolution in PermissionEnforcer (Issue #899).

Covers the adaptive _check_rebac() logic that routes shallow paths (depth <= 2)
through _check_rebac_sequential() and deep paths (depth > 2) through
_check_rebac_batched(), which resolves all checks in a single rebac_check_bulk() call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.core.permissions import OperationContext, Permission, PermissionEnforcer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_rebac_manager() -> MagicMock:
    """Create a mock rebac_manager with the expected interface."""
    mgr = MagicMock()
    mgr.rebac_check = MagicMock(return_value=False)
    mgr.rebac_check_bulk = MagicMock(return_value={})
    mgr.register_boundary_cache_invalidator = MagicMock()
    return mgr


def _make_enforcer(
    rebac_manager: MagicMock | None = None,
    boundary_cache: MagicMock | None = None,
    router: MagicMock | None = None,
) -> PermissionEnforcer:
    """Create a PermissionEnforcer wired to mocks."""
    mgr = rebac_manager or _make_rebac_manager()
    enforcer = PermissionEnforcer(
        metadata_store=None,
        rebac_manager=mgr,
        router=router,
        enable_boundary_cache=False,
        enable_hotspot_tracking=False,
    )
    # Inject boundary cache if provided (bypass PermissionCacheCoordinator)
    if boundary_cache is not None:
        enforcer._boundary_cache = boundary_cache
        enforcer._cache._boundary_cache = boundary_cache
    return enforcer


def _ctx(user: str = "alice", zone_id: str = "default") -> OperationContext:
    return OperationContext(user=user, groups=[], zone_id=zone_id)


# ===========================================================================
# 1. TestCheckRebacAdaptive
# ===========================================================================


class TestCheckRebacAdaptive:
    """Verify the depth-based routing: depth <= 3 -> sequential, depth > 3 -> batched."""

    def test_root_path_uses_sequential(self):
        """Root '/' has depth 0 -> sequential."""
        mgr = _make_rebac_manager()
        enforcer = _make_enforcer(rebac_manager=mgr)

        with (
            patch.object(enforcer, "_check_rebac_sequential", return_value=True) as seq,
            patch.object(enforcer, "_check_rebac_batched", return_value=True) as bat,
        ):
            enforcer._check_rebac("/", Permission.READ, _ctx())
            seq.assert_called_once()
            bat.assert_not_called()

    def test_depth_1_uses_sequential(self):
        """'/workspace' has depth 1 -> sequential."""
        mgr = _make_rebac_manager()
        enforcer = _make_enforcer(rebac_manager=mgr)

        with (
            patch.object(enforcer, "_check_rebac_sequential", return_value=True) as seq,
            patch.object(enforcer, "_check_rebac_batched", return_value=True) as bat,
        ):
            enforcer._check_rebac("/workspace", Permission.READ, _ctx())
            seq.assert_called_once()
            bat.assert_not_called()

    def test_depth_2_uses_sequential(self):
        """'/workspace/file.txt' has depth 2 -> sequential."""
        mgr = _make_rebac_manager()
        enforcer = _make_enforcer(rebac_manager=mgr)

        with (
            patch.object(enforcer, "_check_rebac_sequential", return_value=True) as seq,
            patch.object(enforcer, "_check_rebac_batched", return_value=True) as bat,
        ):
            enforcer._check_rebac("/workspace/file.txt", Permission.READ, _ctx())
            seq.assert_called_once()
            bat.assert_not_called()

    def test_depth_3_uses_sequential(self):
        """'/workspace/projects/file.txt' has depth 3 -> sequential."""
        mgr = _make_rebac_manager()
        enforcer = _make_enforcer(rebac_manager=mgr)

        with (
            patch.object(enforcer, "_check_rebac_sequential", return_value=True) as seq,
            patch.object(enforcer, "_check_rebac_batched", return_value=True) as bat,
        ):
            enforcer._check_rebac("/workspace/projects/file.txt", Permission.READ, _ctx())
            seq.assert_called_once()
            bat.assert_not_called()

    def test_depth_4_uses_batched(self):
        """'/workspace/projects/src/file.txt' has depth 4 -> batched."""
        mgr = _make_rebac_manager()
        enforcer = _make_enforcer(rebac_manager=mgr)

        with (
            patch.object(enforcer, "_check_rebac_sequential", return_value=True) as seq,
            patch.object(enforcer, "_check_rebac_batched", return_value=True) as bat,
        ):
            enforcer._check_rebac("/workspace/projects/src/file.txt", Permission.READ, _ctx())
            bat.assert_called_once()
            seq.assert_not_called()


# ===========================================================================
# 2. TestCheckRebacSequential
# ===========================================================================


class TestCheckRebacSequential:
    """Sequential path for shallow paths (depth <= 3)."""

    def test_direct_permission_granted(self):
        """Direct rebac_check on the path itself returns True."""
        mgr = _make_rebac_manager()
        mgr.rebac_check.return_value = True
        enforcer = _make_enforcer(rebac_manager=mgr)

        result = enforcer._check_rebac_sequential(
            subject=("user", "alice"),
            permission_name="read",
            object_type="file",
            object_id="/workspace/file.txt",
            zone_id="default",
        )
        assert result is True
        # The first call should be the direct check
        first_call = mgr.rebac_check.call_args_list[0]
        assert first_call.kwargs["subject"] == ("user", "alice")
        assert first_call.kwargs["permission"] == "read"
        assert first_call.kwargs["object"] == ("file", "/workspace/file.txt")

    def test_traverse_implied_by_read(self):
        """TRAVERSE is granted when the subject has READ on the same object."""
        mgr = _make_rebac_manager()

        def _side_effect(*, subject, permission, object, zone_id):  # noqa: A002
            # Deny traverse, allow read
            return permission == "read" and object == ("file", "/workspace")

        mgr.rebac_check.side_effect = _side_effect
        enforcer = _make_enforcer(rebac_manager=mgr)

        result = enforcer._check_rebac_sequential(
            subject=("user", "alice"),
            permission_name="traverse",
            object_type="file",
            object_id="/workspace",
            zone_id="default",
        )
        assert result is True

    def test_traverse_implied_by_write(self):
        """TRAVERSE is granted when the subject has WRITE on the same object."""
        mgr = _make_rebac_manager()

        def _side_effect(*, subject, permission, object, zone_id):  # noqa: A002
            return permission == "write" and object == ("file", "/workspace")

        mgr.rebac_check.side_effect = _side_effect
        enforcer = _make_enforcer(rebac_manager=mgr)

        result = enforcer._check_rebac_sequential(
            subject=("user", "alice"),
            permission_name="traverse",
            object_type="file",
            object_id="/workspace",
            zone_id="default",
        )
        assert result is True

    def test_parent_directory_grants_inherited_permission(self):
        """Permission granted on parent directory is inherited by the child."""
        mgr = _make_rebac_manager()

        def _side_effect(*, subject, permission, object, zone_id):  # noqa: A002
            # Only grant at the parent level
            return object == ("file", "/workspace")

        mgr.rebac_check.side_effect = _side_effect
        enforcer = _make_enforcer(rebac_manager=mgr)

        result = enforcer._check_rebac_sequential(
            subject=("user", "alice"),
            permission_name="read",
            object_type="file",
            object_id="/workspace/file.txt",
            zone_id="default",
        )
        assert result is True

    def test_boundary_cache_hit_returns_early(self):
        """Boundary cache hit skips the parent walk entirely."""
        mgr = _make_rebac_manager()

        def _side_effect(*, subject, permission, object, zone_id):  # noqa: A002
            # Grant at boundary path
            return object == ("file", "/workspace")

        mgr.rebac_check.side_effect = _side_effect

        boundary_cache = MagicMock()
        boundary_cache.get_boundary.return_value = "/workspace"
        enforcer = _make_enforcer(rebac_manager=mgr, boundary_cache=boundary_cache)

        result = enforcer._check_rebac_sequential(
            subject=("user", "alice"),
            permission_name="read",
            object_type="file",
            object_id="/workspace/file.txt",
            zone_id="default",
        )
        assert result is True
        boundary_cache.get_boundary.assert_called_once_with(
            "default", "user", "alice", "read", "/workspace/file.txt"
        )
        # Sequential first does a direct check (miss), then the boundary check (hit).
        # No parent walk occurs beyond that, so exactly 2 rebac_check calls.
        assert mgr.rebac_check.call_count == 2


# ===========================================================================
# 3. TestCheckRebacBatched
# ===========================================================================


class TestCheckRebacBatched:
    """Batch path for deep paths (depth > 3)."""

    def test_direct_permission_found_in_batch_results(self):
        """Direct permission on the target path is resolved via bulk call."""
        mgr = _make_rebac_manager()
        subject = ("user", "alice")
        target = ("file", "/a/b/c/d.txt")

        # Build expected check list -- direct check is first
        direct_check = (subject, "read", target)
        mgr.rebac_check_bulk.return_value = {direct_check: True}

        enforcer = _make_enforcer(rebac_manager=mgr)

        result = enforcer._check_rebac_batched(
            subject=subject,
            permission_name="read",
            object_type="file",
            object_id="/a/b/c/d.txt",
            zone_id="default",
        )
        assert result is True
        mgr.rebac_check_bulk.assert_called_once()
        # Verify the direct check tuple was in the batch
        checks_arg = mgr.rebac_check_bulk.call_args[0][0]
        assert direct_check in checks_arg

    def test_permission_inherited_from_grandparent(self):
        """Permission granted on /a (grandparent+) is found in batch."""
        mgr = _make_rebac_manager()
        subject = ("user", "alice")

        # The grandparent grants read
        grandparent_check = (subject, "read", ("file", "/a"))

        def _bulk_side_effect(checks, *, zone_id):
            return {c: (c == grandparent_check) for c in checks}

        mgr.rebac_check_bulk.side_effect = _bulk_side_effect
        enforcer = _make_enforcer(rebac_manager=mgr)

        result = enforcer._check_rebac_batched(
            subject=subject,
            permission_name="read",
            object_type="file",
            object_id="/a/b/c/d.txt",
            zone_id="default",
        )
        assert result is True

    def test_traverse_implied_by_read_on_ancestor(self):
        """TRAVERSE request resolves via READ on an ancestor in the batch."""
        mgr = _make_rebac_manager()
        subject = ("user", "alice")

        # Grant read on /a/b (an ancestor)
        ancestor_read = (subject, "read", ("file", "/a/b"))

        def _bulk_side_effect(checks, *, zone_id):
            return {c: (c == ancestor_read) for c in checks}

        mgr.rebac_check_bulk.side_effect = _bulk_side_effect
        enforcer = _make_enforcer(rebac_manager=mgr)

        result = enforcer._check_rebac_batched(
            subject=subject,
            permission_name="traverse",
            object_type="file",
            object_id="/a/b/c/d.txt",
            zone_id="default",
        )
        assert result is True

    def test_boundary_cache_populated_on_batch_hit(self):
        """When batch finds an ancestor grant, boundary cache is populated."""
        mgr = _make_rebac_manager()
        subject = ("user", "alice")

        ancestor_check = (subject, "read", ("file", "/a/b"))

        def _bulk_side_effect(checks, *, zone_id):
            return {c: (c == ancestor_check) for c in checks}

        mgr.rebac_check_bulk.side_effect = _bulk_side_effect

        boundary_cache = MagicMock()
        boundary_cache.get_boundary.return_value = None  # No cache hit

        enforcer = _make_enforcer(rebac_manager=mgr, boundary_cache=boundary_cache)

        result = enforcer._check_rebac_batched(
            subject=subject,
            permission_name="read",
            object_type="file",
            object_id="/a/b/c/d.txt",
            zone_id="default",
        )
        assert result is True
        # Boundary cache should have been written
        boundary_cache.set_boundary.assert_called_once_with(
            "default", "user", "alice", "read", "/a/b/c/d.txt", "/a/b"
        )

    def test_all_checks_denied_returns_false(self):
        """When every check in the batch is denied, return False."""
        mgr = _make_rebac_manager()
        subject = ("user", "alice")

        def _bulk_side_effect(checks, *, zone_id):
            return dict.fromkeys(checks, False)

        mgr.rebac_check_bulk.side_effect = _bulk_side_effect
        enforcer = _make_enforcer(rebac_manager=mgr)

        result = enforcer._check_rebac_batched(
            subject=subject,
            permission_name="read",
            object_type="file",
            object_id="/a/b/c/d.txt",
            zone_id="default",
        )
        assert result is False


# ===========================================================================
# 4. TestConsistency
# ===========================================================================


class TestConsistency:
    """Same result from sequential and batched code paths."""

    def test_depth_3_grant_at_parent_passes_both_paths(self):
        """A permission at /a/b should grant access to /a/b/c/file.txt via both paths.

        We call both internal methods directly and verify they agree.
        """
        subject = ("user", "alice")
        object_type = "file"
        object_id = "/a/b/c/file.txt"
        zone_id = "default"
        permission_name = "read"
        granting_path = "/a/b"

        # ---------- sequential ----------
        mgr_seq = _make_rebac_manager()

        def _seq_side_effect(*, subject, permission, object, zone_id):  # noqa: A002
            return object == (object_type, granting_path) and permission == permission_name

        mgr_seq.rebac_check.side_effect = _seq_side_effect
        enforcer_seq = _make_enforcer(rebac_manager=mgr_seq)

        result_seq = enforcer_seq._check_rebac_sequential(
            subject=subject,
            permission_name=permission_name,
            object_type=object_type,
            object_id=object_id,
            zone_id=zone_id,
        )

        # ---------- batched ----------
        mgr_bat = _make_rebac_manager()
        grant_check = (subject, permission_name, (object_type, granting_path))

        def _bulk_side_effect(checks, *, zone_id):
            return {c: (c == grant_check) for c in checks}

        mgr_bat.rebac_check_bulk.side_effect = _bulk_side_effect
        enforcer_bat = _make_enforcer(rebac_manager=mgr_bat)

        result_bat = enforcer_bat._check_rebac_batched(
            subject=subject,
            permission_name=permission_name,
            object_type=object_type,
            object_id=object_id,
            zone_id=zone_id,
        )

        # Both must agree
        assert result_seq is True
        assert result_bat is True
        assert result_seq == result_bat
