"""Unit tests for zone admin helper functions (#819)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.rebac.types import WriteResult
from nexus.server.auth.user_helpers import (
    add_user_to_zone,
    can_invite_to_zone,
    is_zone_admin,
    is_zone_owner,
    zone_group_id,
)


def _write_result(tuple_id: str) -> WriteResult:
    """Helper to create WriteResult for mocks."""
    return WriteResult(tuple_id=tuple_id, revision=1, consistency_token="t", written_at_ms=0.0)


@pytest.fixture
def mock_rebac_manager() -> MagicMock:
    """Create a mock ReBAC manager."""
    return MagicMock()


class TestZoneAdminHelpers:
    """Test zone admin helper functions."""

    def test_is_zone_owner_true(self, mock_rebac_manager: Any) -> None:
        """Test is_zone_owner returns True when user is owner."""
        # Setup: User is member of zone-acme-owners
        mock_rebac_manager.rebac_check.return_value = True

        result = is_zone_owner(mock_rebac_manager, "alice", "acme")

        assert result is True
        mock_rebac_manager.rebac_check.assert_called_once_with(
            subject=("user", "alice"),
            permission="member",
            object=("group", "zone-acme-owners"),
            zone_id="acme",
        )

    def test_is_zone_owner_false(self, mock_rebac_manager: Any) -> None:
        """Test is_zone_owner returns False when user is not owner."""
        # Setup: User is not member of zone-acme-owners
        mock_rebac_manager.rebac_check.return_value = False

        result = is_zone_owner(mock_rebac_manager, "alice", "acme")

        assert result is False

    def test_is_zone_admin_via_owner(self, mock_rebac_manager: Any) -> None:
        """Test is_zone_admin returns True for owner."""
        # Setup: User is owner (which implies admin)
        mock_rebac_manager.rebac_check.return_value = True

        result = is_zone_admin(mock_rebac_manager, "alice", "acme")

        assert result is True
        # Should check owner group
        mock_rebac_manager.rebac_check.assert_called_with(
            subject=("user", "alice"),
            permission="member",
            object=("group", "zone-acme-owners"),
            zone_id="acme",
        )

    def test_is_zone_admin_via_admin_group(self, mock_rebac_manager: Any) -> None:
        """Test is_zone_admin returns True for admin (not owner)."""

        # Setup: User is admin but not owner
        def mock_check(**kwargs: Any) -> bool:
            if kwargs["object"][1] == "zone-acme-owners":
                return False  # Not owner
            elif kwargs["object"][1] == "zone-acme-admins":
                return True  # Is admin
            return False

        mock_rebac_manager.rebac_check.side_effect = mock_check

        result = is_zone_admin(mock_rebac_manager, "alice", "acme")

        assert result is True

    def test_is_zone_admin_false(self, mock_rebac_manager: Any) -> None:
        """Test is_zone_admin returns False for regular member."""
        # Setup: User is neither owner nor admin
        mock_rebac_manager.rebac_check.return_value = False

        result = is_zone_admin(mock_rebac_manager, "alice", "acme")

        assert result is False

    def test_can_invite_to_zone(self, mock_rebac_manager: Any) -> None:
        """Test can_invite_to_zone delegates to is_zone_admin."""

        # Setup: User is admin
        def mock_check(**kwargs: Any) -> bool:
            return kwargs["object"][1] == "zone-acme-admins"

        mock_rebac_manager.rebac_check.side_effect = mock_check

        result = can_invite_to_zone(mock_rebac_manager, "alice", "acme")

        assert result is True


class TestAddUserToZone:
    """Test add_user_to_zone with permission checks."""

    def test_add_member_without_caller(self, mock_rebac_manager: Any) -> None:
        """Test adding member without permission check (backward compat)."""
        mock_rebac_manager.rebac_write.return_value = _write_result("tuple-123")

        result = add_user_to_zone(mock_rebac_manager, "bob", "acme", "member")

        assert result == "tuple-123"
        mock_rebac_manager.rebac_write.assert_called_once_with(
            subject=("user", "bob"),
            relation="member",
            object=("group", "zone-acme"),
            zone_id="acme",
        )

    def test_add_admin_as_admin(self, mock_rebac_manager: Any) -> None:
        """Test admin can add another admin."""

        # Setup: Alice is admin
        def mock_check(**kwargs: Any) -> bool:
            return kwargs["object"][1] == "zone-acme-admins"

        mock_rebac_manager.rebac_check.side_effect = mock_check
        mock_rebac_manager.rebac_write.return_value = _write_result("tuple-456")

        result = add_user_to_zone(
            mock_rebac_manager, "bob", "acme", "admin", caller_user_id="alice"
        )

        assert result == "tuple-456"
        mock_rebac_manager.rebac_write.assert_called_once_with(
            subject=("user", "bob"),
            relation="member",
            object=("group", "zone-acme-admins"),
            zone_id="acme",
        )

    def test_add_owner_as_owner(self, mock_rebac_manager: Any) -> None:
        """Test owner can add another owner."""
        # Setup: Alice is owner
        mock_rebac_manager.rebac_check.return_value = True
        mock_rebac_manager.rebac_write.return_value = _write_result("tuple-789")

        result = add_user_to_zone(
            mock_rebac_manager, "bob", "acme", "owner", caller_user_id="alice"
        )

        assert result == "tuple-789"
        mock_rebac_manager.rebac_write.assert_called_once_with(
            subject=("user", "bob"),
            relation="member",
            object=("group", "zone-acme-owners"),
            zone_id="acme",
        )

    def test_add_owner_as_non_owner_fails(self, mock_rebac_manager: Any) -> None:
        """Test non-owner cannot add owner."""

        # Setup: Alice is admin but not owner
        def mock_check(**kwargs: Any) -> bool:
            if kwargs["object"][1] == "zone-acme-owners":
                return False  # Not owner
            elif kwargs["object"][1] == "zone-acme-admins":
                return True  # Is admin
            return False

        mock_rebac_manager.rebac_check.side_effect = mock_check

        with pytest.raises(PermissionError, match="Only zone owners can add other owners"):
            add_user_to_zone(mock_rebac_manager, "bob", "acme", "owner", caller_user_id="alice")

    def test_add_member_as_non_admin_fails(self, mock_rebac_manager: Any) -> None:
        """Test non-admin cannot invite users."""
        # Setup: Alice is regular member (not admin/owner)
        mock_rebac_manager.rebac_check.return_value = False

        with pytest.raises(PermissionError, match="Only zone admins/owners can invite"):
            add_user_to_zone(mock_rebac_manager, "bob", "acme", "member", caller_user_id="alice")

    def test_invalid_role_raises_value_error(self, mock_rebac_manager: Any) -> None:
        """Test invalid role raises ValueError."""
        # Setup: Alice is owner
        mock_rebac_manager.rebac_check.return_value = True

        with pytest.raises(ValueError, match="Invalid role 'superuser'"):
            add_user_to_zone(
                mock_rebac_manager,
                "bob",
                "acme",
                "superuser",
                caller_user_id="alice",  # type: ignore
            )


class TestZoneGroupNaming:
    """Test zone group naming functions."""

    def test_zone_group_id(self) -> None:
        """Test zone group ID generation."""
        assert zone_group_id("acme") == "zone-acme"
        assert zone_group_id("tech-corp") == "zone-tech-corp"

    def test_owner_group_naming(self) -> None:
        """Test owner group naming convention."""
        base_id = zone_group_id("acme")
        owner_id = f"{base_id}-owners"
        assert owner_id == "zone-acme-owners"

    def test_admin_group_naming(self) -> None:
        """Test admin group naming convention."""
        base_id = zone_group_id("acme")
        admin_id = f"{base_id}-admins"
        assert admin_id == "zone-acme-admins"
