"""Tests for ReBACService.list_accessible_zones() (Issue #3147 decision 12A).

Tests the zone discovery helper that extracts zone IDs from ReBAC tuples.
This is a security-critical function — incorrect zone extraction means
users see results from unauthorized zones.
"""

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pyroaring")


from nexus.bricks.rebac.rebac_service import ReBACService


def _make_tuple(
    subject_type: str,
    subject_id: str,
    relation: str,
    object_type: str,
    object_id: str,
    zone_id: str | None = None,
) -> dict:
    """Create a ReBAC tuple dict matching the list_tuples return format."""
    return {
        "tuple_id": f"{subject_type}:{subject_id}#{relation}@{object_type}:{object_id}",
        "subject_type": subject_type,
        "subject_id": subject_id,
        "relation": relation,
        "object_type": object_type,
        "object_id": object_id,
        "created_at": "2025-01-01T00:00:00Z",
        "expires_at": None,
        "zone_id": zone_id,
    }


class TestListAccessibleZones:
    @pytest.mark.asyncio
    async def test_basic_zone_membership(self) -> None:
        """User with member relation on two zones."""
        svc = ReBACService.__new__(ReBACService)
        svc.rebac_list_tuples = AsyncMock(
            return_value=[
                _make_tuple("user", "alice", "member", "zone", "zone_a"),
                _make_tuple("user", "alice", "member", "zone", "zone_b"),
            ]
        )

        zones = await svc.list_accessible_zones(subject=("user", "alice"))
        assert zones == ["zone_a", "zone_b"]

    @pytest.mark.asyncio
    async def test_mixed_relations(self) -> None:
        """User with different relations (owner, viewer) should all be included."""
        svc = ReBACService.__new__(ReBACService)
        svc.rebac_list_tuples = AsyncMock(
            return_value=[
                _make_tuple("user", "alice", "owner", "zone", "zone_owned"),
                _make_tuple("user", "alice", "viewer", "zone", "zone_viewed"),
                _make_tuple("user", "alice", "admin", "zone", "zone_admin"),
            ]
        )

        zones = await svc.list_accessible_zones(subject=("user", "alice"))
        assert set(zones) == {"zone_owned", "zone_viewed", "zone_admin"}

    @pytest.mark.asyncio
    async def test_filters_non_zone_objects(self) -> None:
        """Tuples with object_type != 'zone' should be excluded."""
        svc = ReBACService.__new__(ReBACService)
        svc.rebac_list_tuples = AsyncMock(
            return_value=[
                _make_tuple("user", "alice", "member", "zone", "zone_a"),
                _make_tuple("user", "alice", "viewer", "file", "/secret.txt"),
                _make_tuple("user", "alice", "member", "directory", "/shared/"),
            ]
        )

        zones = await svc.list_accessible_zones(subject=("user", "alice"))
        assert zones == ["zone_a"]

    @pytest.mark.asyncio
    async def test_deduplicates_zones(self) -> None:
        """Same zone with multiple relations should appear only once."""
        svc = ReBACService.__new__(ReBACService)
        svc.rebac_list_tuples = AsyncMock(
            return_value=[
                _make_tuple("user", "alice", "member", "zone", "zone_a"),
                _make_tuple("user", "alice", "owner", "zone", "zone_a"),
            ]
        )

        zones = await svc.list_accessible_zones(subject=("user", "alice"))
        assert zones == ["zone_a"]

    @pytest.mark.asyncio
    async def test_no_zone_membership(self) -> None:
        """User with no zone tuples should return empty list."""
        svc = ReBACService.__new__(ReBACService)
        svc.rebac_list_tuples = AsyncMock(return_value=[])

        zones = await svc.list_accessible_zones(subject=("user", "alice"))
        assert zones == []

    @pytest.mark.asyncio
    async def test_agent_subject(self) -> None:
        """Agent subjects should work the same as user subjects."""
        svc = ReBACService.__new__(ReBACService)
        svc.rebac_list_tuples = AsyncMock(
            return_value=[
                _make_tuple("agent", "bot_1", "member", "zone", "zone_a"),
            ]
        )

        zones = await svc.list_accessible_zones(subject=("agent", "bot_1"))

        assert zones == ["zone_a"]
        # Verify correct subject was passed to list_tuples
        svc.rebac_list_tuples.assert_called_once_with(
            subject=("agent", "bot_1"),
            relation_in=["member", "owner", "admin", "viewer"],
        )

    @pytest.mark.asyncio
    async def test_empty_object_id_filtered(self) -> None:
        """Tuples with empty object_id should be excluded."""
        svc = ReBACService.__new__(ReBACService)
        svc.rebac_list_tuples = AsyncMock(
            return_value=[
                _make_tuple("user", "alice", "member", "zone", "zone_a"),
                _make_tuple("user", "alice", "member", "zone", ""),
            ]
        )

        zones = await svc.list_accessible_zones(subject=("user", "alice"))
        assert zones == ["zone_a"]

    @pytest.mark.asyncio
    async def test_passes_correct_relations(self) -> None:
        """Should query with member, owner, admin, viewer relations."""
        svc = ReBACService.__new__(ReBACService)
        svc.rebac_list_tuples = AsyncMock(return_value=[])

        await svc.list_accessible_zones(subject=("user", "alice"))

        svc.rebac_list_tuples.assert_called_once_with(
            subject=("user", "alice"),
            relation_in=["member", "owner", "admin", "viewer"],
        )
