"""Tests for nexus.lib.zone_helpers (Issue #2138).

TDD Red Phase: These tests expose bugs in the current implementation:
1. remove_user_from_zone calls rebac_delete with wrong kwargs (TypeError)
2. get_user_zones accesses private _connection() instead of Protocol methods
3. user_belongs_to_zone accesses private _connection() instead of Protocol methods
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Mock ReBACManager that ONLY implements ReBACManagerProtocol methods
# No _connection(), no kwarg-accepting rebac_delete()
# ---------------------------------------------------------------------------
@dataclass
class WriteResult:
    """Minimal WriteResult for testing."""

    tuple_id: str
    created: bool = True


@dataclass
class MockReBACManager:
    """Mock that strictly follows ReBACManagerProtocol.

    Does NOT have _connection() or other private methods.
    rebac_delete only accepts tuple_id (per Protocol).
    """

    # Internal state for tracking tuples
    _tuples: list[dict[str, Any]] = field(default_factory=list)
    _tuple_counter: int = 0

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
        consistency: Any = None,
    ) -> bool:
        """Check if a relationship exists."""
        for t in self._tuples:
            if (
                t["subject"] == subject
                and t["relation"] == permission
                and t["object"] == object
                and (zone_id is None or t.get("zone_id") == zone_id)
            ):
                return True
        return False

    def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,
        subject_zone_id: str | None = None,
        object_zone_id: str | None = None,
    ) -> WriteResult:
        """Write a relationship tuple."""
        self._tuple_counter += 1
        tid = f"tuple_{self._tuple_counter}"
        self._tuples.append(
            {
                "tuple_id": tid,
                "subject": subject[:2],
                "relation": relation,
                "object": object,
                "zone_id": zone_id,
            }
        )
        return WriteResult(tuple_id=tid)

    def rebac_delete(self, tuple_id: str | WriteResult) -> bool:
        """Delete by tuple_id ONLY (per Protocol)."""
        if isinstance(tuple_id, WriteResult):
            tuple_id = tuple_id.tuple_id
        for i, t in enumerate(self._tuples):
            if t["tuple_id"] == tuple_id:
                self._tuples.pop(i)
                return True
        return False

    def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
        consistency: Any = None,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]:
        return {(s, p, o): self.rebac_check(s, p, o, zone_id=zone_id) for s, p, o in checks}

    def rebac_list_objects(
        self,
        subject: tuple[str, str],
        permission: str,
        object_type: str = "file",
        zone_id: str | None = None,
        path_prefix: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[tuple[str, str]]:
        """List objects accessible by subject."""
        results: list[tuple[str, str]] = []
        for t in self._tuples:
            if (
                t["subject"] == subject
                and t["relation"] == permission
                and t["object"][0] == object_type
                and (zone_id is None or t.get("zone_id") == zone_id)
            ):
                results.append(t["object"])
        return results[offset : offset + limit]

    def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
        **_kw: Any,
    ) -> list[dict[str, Any]]:
        """List tuples matching criteria."""
        results: list[dict[str, Any]] = []
        for t in self._tuples:
            if subject is not None and t["subject"] != subject:
                continue
            if relation is not None and t["relation"] != relation:
                continue
            if object is not None and t["object"] != object:
                continue
            if relation_in is not None and t["relation"] not in relation_in:
                continue
            results.append(
                {
                    "tuple_id": t["tuple_id"],
                    "subject_type": t["subject"][0],
                    "subject_id": t["subject"][1],
                    "relation": t["relation"],
                    "object_type": t["object"][0],
                    "object_id": t["object"][1],
                    "zone_id": t.get("zone_id"),
                }
            )
        return results

    def get_zone_revision(self, zone_id: str | None, conn: Any | None = None) -> int:
        return 0

    def invalidate_zone_graph_cache(self, zone_id: str | None = None) -> None:
        pass

    def close(self) -> None:
        pass


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def rebac() -> MockReBACManager:
    """Fresh mock ReBACManager."""
    return MockReBACManager()


@pytest.fixture()
def rebac_with_membership(rebac: MockReBACManager) -> MockReBACManager:
    """ReBACManager with pre-existing zone memberships."""
    # alice is owner of zone "acme"
    rebac.rebac_write(
        subject=("user", "alice"),
        relation="member",
        object=("group", "zone-acme-owners"),
        zone_id="acme",
    )
    # alice is also member of zone "acme"
    rebac.rebac_write(
        subject=("user", "alice"),
        relation="member",
        object=("group", "zone-acme"),
        zone_id="acme",
    )
    # bob is member of zone "acme"
    rebac.rebac_write(
        subject=("user", "bob"),
        relation="member",
        object=("group", "zone-acme"),
        zone_id="acme",
    )
    # alice is member of zone "beta"
    rebac.rebac_write(
        subject=("user", "alice"),
        relation="member",
        object=("group", "zone-beta"),
        zone_id="beta",
    )
    return rebac


# ===========================================================================
# Pure helper tests (no Protocol dependency)
# ===========================================================================


class TestZoneGroupHelpers:
    """Test zone group naming functions — pure logic, no ReBAC needed."""

    def test_zone_group_id(self) -> None:
        from nexus.lib.zone_helpers import zone_group_id

        assert zone_group_id("acme") == "zone-acme"
        assert zone_group_id("test-zone") == "zone-test-zone"

    def test_parse_zone_from_group(self) -> None:
        from nexus.lib.zone_helpers import parse_zone_from_group

        assert parse_zone_from_group("zone-acme") == "acme"
        assert parse_zone_from_group("engineering") is None
        assert parse_zone_from_group("zone-") == ""

    def test_is_zone_group(self) -> None:
        from nexus.lib.zone_helpers import is_zone_group

        assert is_zone_group("zone-acme") is True
        assert is_zone_group("engineering") is False


# ===========================================================================
# Protocol-based tests (use MockReBACManager)
# ===========================================================================


class TestIsZoneOwner:
    """Test is_zone_owner — uses rebac_check (Protocol-compliant)."""

    def test_owner_returns_true(self, rebac_with_membership: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import is_zone_owner

        assert is_zone_owner(rebac_with_membership, "alice", "acme") is True

    def test_non_owner_returns_false(self, rebac_with_membership: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import is_zone_owner

        assert is_zone_owner(rebac_with_membership, "bob", "acme") is False


class TestIsZoneAdmin:
    """Test is_zone_admin — uses rebac_check (Protocol-compliant)."""

    def test_owner_is_admin(self, rebac_with_membership: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import is_zone_admin

        assert is_zone_admin(rebac_with_membership, "alice", "acme") is True

    def test_member_is_not_admin(self, rebac_with_membership: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import is_zone_admin

        assert is_zone_admin(rebac_with_membership, "bob", "acme") is False


class TestAddUserToZone:
    """Test add_user_to_zone — uses rebac_write (Protocol-compliant)."""

    def test_add_member(self, rebac: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import add_user_to_zone

        result = add_user_to_zone(rebac, "charlie", "acme", role="member")
        # Should return a tuple_id (str or WriteResult)
        assert result is not None

    def test_invalid_role_raises(self, rebac: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import add_user_to_zone

        with pytest.raises(ValueError, match="Invalid role"):
            add_user_to_zone(rebac, "charlie", "acme", role="superuser")

    def test_non_owner_cannot_add_owner(self, rebac_with_membership: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import add_user_to_zone

        with pytest.raises(PermissionError):
            add_user_to_zone(
                rebac_with_membership,
                "charlie",
                "acme",
                role="owner",
                caller_user_id="bob",
            )


class TestRemoveUserFromZone:
    """Test remove_user_from_zone.

    BUG: Current implementation calls rebac_delete(subject=..., relation=...,
    object=..., zone_id=...) but Protocol signature is rebac_delete(tuple_id).
    This should raise TypeError with a Protocol-compliant mock.
    """

    def test_remove_specific_role(self, rebac_with_membership: MockReBACManager) -> None:
        """Removing a user's membership should work via Protocol methods."""
        from nexus.lib.zone_helpers import remove_user_from_zone

        # Bob is member of acme — removing should succeed
        remove_user_from_zone(rebac_with_membership, "bob", "acme", role="member")

        # Verify bob is no longer a member
        assert not rebac_with_membership.rebac_check(
            subject=("user", "bob"),
            permission="member",
            object=("group", "zone-acme"),
            zone_id="acme",
        )

    def test_remove_all_roles(self, rebac_with_membership: MockReBACManager) -> None:
        """Removing all roles should work."""
        from nexus.lib.zone_helpers import remove_user_from_zone

        # Alice has owner + member in acme — remove all
        remove_user_from_zone(rebac_with_membership, "alice", "acme", role=None)

        # Verify alice has no membership in acme
        assert not rebac_with_membership.rebac_check(
            subject=("user", "alice"),
            permission="member",
            object=("group", "zone-acme"),
            zone_id="acme",
        )
        assert not rebac_with_membership.rebac_check(
            subject=("user", "alice"),
            permission="member",
            object=("group", "zone-acme-owners"),
            zone_id="acme",
        )


class TestGetUserZones:
    """Test get_user_zones.

    BUG: Current implementation accesses rebac_manager._connection() (private).
    Should use Protocol methods instead.
    """

    def test_returns_user_zones(self, rebac_with_membership: MockReBACManager) -> None:
        """Should return all zone IDs the user belongs to."""
        from nexus.lib.zone_helpers import get_user_zones

        zones = get_user_zones(rebac_with_membership, "alice")
        assert sorted(zones) == ["acme", "beta"]

    def test_returns_empty_for_unknown_user(self, rebac: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import get_user_zones

        zones = get_user_zones(rebac, "nobody")
        assert zones == []

    def test_single_zone_membership(self, rebac_with_membership: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import get_user_zones

        zones = get_user_zones(rebac_with_membership, "bob")
        assert zones == ["acme"]


class TestUserBelongsToZone:
    """Test user_belongs_to_zone.

    BUG: Current implementation accesses rebac_manager._connection() (private).
    Should use rebac_check() instead.
    """

    def test_member_belongs(self, rebac_with_membership: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import user_belongs_to_zone

        assert user_belongs_to_zone(rebac_with_membership, "bob", "acme") is True

    def test_non_member_does_not_belong(self, rebac_with_membership: MockReBACManager) -> None:
        from nexus.lib.zone_helpers import user_belongs_to_zone

        assert user_belongs_to_zone(rebac_with_membership, "bob", "beta") is False

    def test_owner_belongs(self, rebac_with_membership: MockReBACManager) -> None:
        """Owner should be considered a member of the zone."""
        from nexus.lib.zone_helpers import user_belongs_to_zone

        assert user_belongs_to_zone(rebac_with_membership, "alice", "acme") is True
