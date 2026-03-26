"""Unit tests for ReactiveSubscriptionManager (Issue #1167).

Tests the Subscription dataclass, ReactiveSubscriptionManager class,
find_affected_connections, cleanup sweep, and stats.
"""

import time

import pytest

from nexus.lib.path_utils import path_matches_pattern
from nexus.services.event_bus.subscriptions import (
    ReactiveSubscriptionManager,
    Subscription,
)
from nexus.services.event_bus.types import FileEvent
from nexus.storage.read_set import ReadSet, ReadSetRegistry

# ---------------------------------------------------------------------------
# TestSubscription
# ---------------------------------------------------------------------------


class TestSubscription:
    """Tests for the frozen Subscription dataclass."""

    def test_create_read_set_subscription(self) -> None:
        """Frozen dataclass is created correctly."""
        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
            event_types=frozenset({"file_write"}),
        )
        assert sub.subscription_id == "sub1"
        assert sub.connection_id == "conn1"
        assert sub.zone_id == "zone1"
        assert sub.query_id == "q1"
        assert sub.event_types == frozenset({"file_write"})
        assert sub.created_at > 0

    def test_immutability(self) -> None:
        """Verify cannot mutate frozen fields."""
        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        with pytest.raises(AttributeError):
            sub.subscription_id = "changed"


# ---------------------------------------------------------------------------
# TestPathMatchesPattern
# ---------------------------------------------------------------------------


class TestPathMatchesPattern:
    """Tests for the extracted path_matches_pattern function."""

    def test_simple_glob(self) -> None:
        assert path_matches_pattern("/workspace/main.py", "/workspace/*.py")

    def test_double_star(self) -> None:
        assert path_matches_pattern("/workspace/src/main.py", "/workspace/**/*.py")

    def test_no_match(self) -> None:
        assert not path_matches_pattern("/inbox/msg.txt", "/workspace/*.py")

    def test_question_mark(self) -> None:
        assert path_matches_pattern("/a/b.py", "/a/?.py")

    def test_fnmatch_simple(self) -> None:
        assert path_matches_pattern("/a/b.py", "/a/b.py")


# ---------------------------------------------------------------------------
# TestReactiveSubscriptionManager
# ---------------------------------------------------------------------------


class TestReactiveSubscriptionManager:
    """Tests for register/unregister operations."""

    @pytest.fixture
    def registry(self) -> ReadSetRegistry:
        return ReadSetRegistry()

    @pytest.fixture
    def manager(self, registry: ReadSetRegistry) -> ReactiveSubscriptionManager:
        return ReactiveSubscriptionManager(registry=registry)

    @pytest.mark.asyncio
    async def test_register_read_set_subscription(
        self, manager: ReactiveSubscriptionManager, registry: ReadSetRegistry
    ) -> None:
        """Register subscription stores in registry + indexes."""
        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("file", "/inbox/a.txt", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub, read_set=rs)

        assert "sub1" in manager._subscriptions
        assert "conn1" in manager._connection_index
        assert "sub1" in manager._connection_index["conn1"]
        assert registry.get_read_set("q1") is not None

    @pytest.mark.asyncio
    async def test_register_duplicate_updates(
        self, manager: ReactiveSubscriptionManager, registry: ReadSetRegistry
    ) -> None:
        """Re-registration replaces existing subscription."""
        rs1 = ReadSet(query_id="q1", zone_id="zone1")
        rs1.record_read("file", "/old/a.txt", revision=10)

        sub_v1 = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub_v1, read_set=rs1)

        rs2 = ReadSet(query_id="q2", zone_id="zone1")
        rs2.record_read("file", "/new/a.txt", revision=10)

        sub_v2 = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q2",
        )
        await manager.register(sub_v2, read_set=rs2)

        assert manager._subscriptions["sub1"].query_id == "q2"
        stats = manager.get_stats()
        assert stats["total_subscriptions"] == 1

    @pytest.mark.asyncio
    async def test_register_requires_query_id(self, manager: ReactiveSubscriptionManager) -> None:
        """Subscription without query_id raises ValueError."""
        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
        )
        with pytest.raises(ValueError, match="query_id"):
            await manager.register(sub)

    @pytest.mark.asyncio
    async def test_register_requires_read_set(self, manager: ReactiveSubscriptionManager) -> None:
        """Subscription without ReadSet raises ValueError."""
        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        with pytest.raises(ValueError, match="ReadSet"):
            await manager.register(sub)

    @pytest.mark.asyncio
    async def test_unregister_read_set(
        self, manager: ReactiveSubscriptionManager, registry: ReadSetRegistry
    ) -> None:
        """Unregistering sub removes from all indexes."""
        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("file", "/inbox/a.txt", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub, read_set=rs)
        result = await manager.unregister("sub1")

        assert result is True
        assert "sub1" not in manager._subscriptions
        assert "conn1" not in manager._connection_index
        assert registry.get_read_set("q1") is None

    @pytest.mark.asyncio
    async def test_unregister_nonexistent(self, manager: ReactiveSubscriptionManager) -> None:
        """Unregistering non-existent subscription returns False."""
        result = await manager.unregister("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_unregister_connection(self, manager: ReactiveSubscriptionManager) -> None:
        """Removes all subscriptions for a connection."""
        rs1 = ReadSet(query_id="q1", zone_id="zone1")
        rs1.record_read("file", "/inbox/a.txt", revision=10)
        rs2 = ReadSet(query_id="q2", zone_id="zone1")
        rs2.record_read("file", "/docs/b.txt", revision=10)
        rs3 = ReadSet(query_id="q3", zone_id="zone1")
        rs3.record_read("file", "/inbox/c.txt", revision=10)

        sub1 = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        sub2 = Subscription(
            subscription_id="sub2",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q2",
        )
        sub3 = Subscription(
            subscription_id="sub3",
            connection_id="conn2",
            zone_id="zone1",
            query_id="q3",
        )
        await manager.register(sub1, read_set=rs1)
        await manager.register(sub2, read_set=rs2)
        await manager.register(sub3, read_set=rs3)

        count = await manager.unregister_connection("conn1")

        assert count == 2
        assert "sub1" not in manager._subscriptions
        assert "sub2" not in manager._subscriptions
        assert "sub3" in manager._subscriptions

    @pytest.mark.asyncio
    async def test_unregister_connection_nonexistent(
        self, manager: ReactiveSubscriptionManager
    ) -> None:
        """Unregistering unknown connection returns 0."""
        count = await manager.unregister_connection("ghost")
        assert count == 0


# ---------------------------------------------------------------------------
# TestFindAffectedConnections
# ---------------------------------------------------------------------------


class TestFindAffectedConnections:
    """Tests for find_affected_connections event matching."""

    @pytest.fixture
    def registry(self) -> ReadSetRegistry:
        return ReadSetRegistry()

    @pytest.fixture
    def manager(self, registry: ReadSetRegistry) -> ReactiveSubscriptionManager:
        return ReactiveSubscriptionManager(registry=registry)

    def _make_event(
        self,
        path: str = "/inbox/a.txt",
        zone_id: str = "zone1",
        event_type: str = "file_write",
        version: int = 20,
    ) -> FileEvent:
        return FileEvent(
            type=event_type,
            path=path,
            zone_id=zone_id,
            version=version,
        )

    @pytest.mark.asyncio
    async def test_read_set_direct_path_match(self, manager: ReactiveSubscriptionManager) -> None:
        """O(1) lookup finds connection via direct path match."""
        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("file", "/inbox/a.txt", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub, read_set=rs)

        event = self._make_event(path="/inbox/a.txt", version=20)
        result = manager.find_affected_connections(event)

        assert result == {"conn1"}

    @pytest.mark.asyncio
    async def test_read_set_directory_containment(
        self, manager: ReactiveSubscriptionManager
    ) -> None:
        """Directory containment match finds connection."""
        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("directory", "/inbox/", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub, read_set=rs)

        event = self._make_event(path="/inbox/new_file.txt", version=20)
        result = manager.find_affected_connections(event)

        assert result == {"conn1"}

    @pytest.mark.asyncio
    async def test_read_set_no_match(self, manager: ReactiveSubscriptionManager) -> None:
        """Unrelated path does not match."""
        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("file", "/inbox/a.txt", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub, read_set=rs)

        event = self._make_event(path="/docs/readme.md", version=20)
        result = manager.find_affected_connections(event)

        assert result == set()

    @pytest.mark.asyncio
    async def test_event_type_filter_read_set(self, manager: ReactiveSubscriptionManager) -> None:
        """Event type filter works for subscriptions."""
        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("file", "/inbox/a.txt", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
            event_types=frozenset({"file_delete"}),
        )
        await manager.register(sub, read_set=rs)

        write_event = self._make_event(path="/inbox/a.txt", event_type="file_write")
        delete_event = self._make_event(path="/inbox/a.txt", event_type="file_delete")

        assert manager.find_affected_connections(write_event) == set()
        assert manager.find_affected_connections(delete_event) == {"conn1"}

    @pytest.mark.asyncio
    async def test_zone_isolation(self, manager: ReactiveSubscriptionManager) -> None:
        """Different zones don't cross-talk."""
        rs1 = ReadSet(query_id="q1", zone_id="zone1")
        rs1.record_read("file", "/inbox/a.txt", revision=10)
        rs2 = ReadSet(query_id="q2", zone_id="zone2")
        rs2.record_read("file", "/inbox/a.txt", revision=10)

        sub1 = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        sub2 = Subscription(
            subscription_id="sub2",
            connection_id="conn2",
            zone_id="zone2",
            query_id="q2",
        )
        await manager.register(sub1, read_set=rs1)
        await manager.register(sub2, read_set=rs2)

        event_z1 = self._make_event(zone_id="zone1")
        result = manager.find_affected_connections(event_z1)

        # Only conn1 is in zone1; conn2 is in zone2 and should not appear
        assert result == {"conn1"}

    @pytest.mark.asyncio
    async def test_multiple_subs_same_connection_dedup(
        self, manager: ReactiveSubscriptionManager
    ) -> None:
        """Connection with multiple subs only appears once."""
        rs1 = ReadSet(query_id="q1", zone_id="zone1")
        rs1.record_read("file", "/inbox/a.txt", revision=10)

        rs2 = ReadSet(query_id="q2", zone_id="zone1")
        rs2.record_read("file", "/inbox/a.txt", revision=10)

        sub1 = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        sub2 = Subscription(
            subscription_id="sub2",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q2",
        )
        await manager.register(sub1, read_set=rs1)
        await manager.register(sub2, read_set=rs2)

        event = self._make_event(path="/inbox/a.txt", version=20)
        result = manager.find_affected_connections(event)

        assert result == {"conn1"}  # Deduplicated

    @pytest.mark.asyncio
    async def test_no_subscriptions(self, manager: ReactiveSubscriptionManager) -> None:
        """Empty manager returns empty set."""
        event = self._make_event()
        result = manager.find_affected_connections(event)
        assert result == set()


# ---------------------------------------------------------------------------
# TestFindAffectedSubscriptions (#1170)
# ---------------------------------------------------------------------------


class TestFindAffectedSubscriptions:
    """Tests for find_affected_subscriptions — subscription-level grouping."""

    @pytest.fixture
    def registry(self) -> ReadSetRegistry:
        return ReadSetRegistry()

    @pytest.fixture
    def manager(self, registry: ReadSetRegistry) -> ReactiveSubscriptionManager:
        return ReactiveSubscriptionManager(registry=registry)

    def _make_event(
        self,
        path: str = "/inbox/a.txt",
        zone_id: str = "zone1",
        event_type: str = "file_write",
        version: int = 20,
    ) -> FileEvent:
        return FileEvent(
            type=event_type,
            path=path,
            zone_id=zone_id,
            version=version,
        )

    @pytest.mark.asyncio
    async def test_single_subscription(self, manager: ReactiveSubscriptionManager) -> None:
        """Single matching sub returns one connection with one sub."""
        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("file", "/inbox/msg.txt", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub, read_set=rs)

        event = self._make_event(path="/inbox/msg.txt")
        result = manager.find_affected_subscriptions(event)

        assert set(result.keys()) == {"conn1"}
        assert len(result["conn1"]) == 1
        assert result["conn1"][0].subscription_id == "sub1"

    @pytest.mark.asyncio
    async def test_multiple_subs_same_connection(
        self, manager: ReactiveSubscriptionManager
    ) -> None:
        """Two subs on same connection both returned in one group."""
        rs1 = ReadSet(query_id="q1", zone_id="zone1")
        rs1.record_read("file", "/inbox/a.txt", revision=10)
        rs2 = ReadSet(query_id="q2", zone_id="zone1")
        rs2.record_read("file", "/inbox/a.txt", revision=10)

        sub1 = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        sub2 = Subscription(
            subscription_id="sub2",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q2",
        )
        await manager.register(sub1, read_set=rs1)
        await manager.register(sub2, read_set=rs2)

        event = self._make_event(path="/inbox/a.txt")
        result = manager.find_affected_subscriptions(event)

        assert set(result.keys()) == {"conn1"}
        sub_ids = {s.subscription_id for s in result["conn1"]}
        assert sub_ids == {"sub1", "sub2"}

    @pytest.mark.asyncio
    async def test_multiple_connections(self, manager: ReactiveSubscriptionManager) -> None:
        """Subs on different connections are grouped separately."""
        rs1 = ReadSet(query_id="q1", zone_id="zone1")
        rs1.record_read("file", "/inbox/a.txt", revision=10)
        rs2 = ReadSet(query_id="q2", zone_id="zone1")
        rs2.record_read("file", "/inbox/a.txt", revision=10)

        sub1 = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        sub2 = Subscription(
            subscription_id="sub2",
            connection_id="conn2",
            zone_id="zone1",
            query_id="q2",
        )
        await manager.register(sub1, read_set=rs1)
        await manager.register(sub2, read_set=rs2)

        event = self._make_event()
        result = manager.find_affected_subscriptions(event)

        assert set(result.keys()) == {"conn1", "conn2"}
        assert len(result["conn1"]) == 1
        assert len(result["conn2"]) == 1

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self, manager: ReactiveSubscriptionManager) -> None:
        """Unmatching event returns empty dict."""
        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("file", "/docs/readme.md", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub, read_set=rs)

        event = self._make_event(path="/inbox/a.txt")
        result = manager.find_affected_subscriptions(event)

        assert result == {}

    @pytest.mark.asyncio
    async def test_event_type_filter(self, manager: ReactiveSubscriptionManager) -> None:
        """Event type filter excludes non-matching subs from result."""
        rs1 = ReadSet(query_id="q1", zone_id="zone1")
        rs1.record_read("file", "/inbox/a.txt", revision=10)
        rs2 = ReadSet(query_id="q2", zone_id="zone1")
        rs2.record_read("file", "/inbox/a.txt", revision=10)

        sub_match = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
            event_types=frozenset({"file_delete"}),
        )
        sub_no_match = Subscription(
            subscription_id="sub2",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q2",
            event_types=frozenset({"file_write"}),
        )
        await manager.register(sub_match, read_set=rs1)
        await manager.register(sub_no_match, read_set=rs2)

        event = self._make_event(event_type="file_delete")
        result = manager.find_affected_subscriptions(event)

        assert set(result.keys()) == {"conn1"}
        assert len(result["conn1"]) == 1
        assert result["conn1"][0].subscription_id == "sub1"

    @pytest.mark.asyncio
    async def test_zone_isolation(self, manager: ReactiveSubscriptionManager) -> None:
        """Only subs in the event's zone are returned."""
        rs1 = ReadSet(query_id="q1", zone_id="zone1")
        rs1.record_read("file", "/inbox/a.txt", revision=10)
        rs2 = ReadSet(query_id="q2", zone_id="zone2")
        rs2.record_read("file", "/inbox/a.txt", revision=10)

        sub1 = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        sub2 = Subscription(
            subscription_id="sub2",
            connection_id="conn2",
            zone_id="zone2",
            query_id="q2",
        )
        await manager.register(sub1, read_set=rs1)
        await manager.register(sub2, read_set=rs2)

        event = self._make_event(zone_id="zone1")
        result = manager.find_affected_subscriptions(event)

        assert set(result.keys()) == {"conn1"}

    @pytest.mark.asyncio
    async def test_no_subscriptions_returns_empty(
        self, manager: ReactiveSubscriptionManager
    ) -> None:
        """Empty manager returns empty dict."""
        event = self._make_event()
        result = manager.find_affected_subscriptions(event)
        assert result == {}

    @pytest.mark.asyncio
    async def test_tracks_performance(self, manager: ReactiveSubscriptionManager) -> None:
        """Lookup count incremented by find_affected_subscriptions."""
        event = self._make_event()
        initial_count = manager._lookup_count

        manager.find_affected_subscriptions(event)
        manager.find_affected_subscriptions(event)

        assert manager._lookup_count == initial_count + 2


# ---------------------------------------------------------------------------
# TestCleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    """Tests for cleanup_sweep and connection cleanup."""

    @pytest.fixture
    def registry(self) -> ReadSetRegistry:
        return ReadSetRegistry()

    @pytest.fixture
    def manager(self, registry: ReadSetRegistry) -> ReactiveSubscriptionManager:
        return ReactiveSubscriptionManager(registry=registry)

    @pytest.mark.asyncio
    async def test_cleanup_sweep_removes_expired(
        self, manager: ReactiveSubscriptionManager
    ) -> None:
        """Expired read sets are cleaned up."""
        # Create a read set that expires immediately
        rs = ReadSet(
            query_id="q1",
            zone_id="zone1",
            expires_at=time.time() - 1,  # Already expired
        )
        rs.record_read("file", "/inbox/a.txt", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub, read_set=rs)
        assert "sub1" in manager._subscriptions

        count = await manager.cleanup_sweep()

        assert count == 1
        assert "sub1" not in manager._subscriptions

    @pytest.mark.asyncio
    async def test_cleanup_sweep_preserves_active(
        self, manager: ReactiveSubscriptionManager
    ) -> None:
        """Active subscriptions are not removed by cleanup."""
        rs = ReadSet(
            query_id="q1",
            zone_id="zone1",
            expires_at=time.time() + 3600,  # 1 hour from now
        )
        rs.record_read("file", "/inbox/a.txt", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub, read_set=rs)

        count = await manager.cleanup_sweep()

        assert count == 0
        assert "sub1" in manager._subscriptions

    @pytest.mark.asyncio
    async def test_unregister_connection_cleans_all(
        self, manager: ReactiveSubscriptionManager, registry: ReadSetRegistry
    ) -> None:
        """Disconnect cleanup removes all subscriptions + registry entries."""
        rs1 = ReadSet(query_id="q1", zone_id="zone1")
        rs1.record_read("file", "/inbox/a.txt", revision=10)
        rs2 = ReadSet(query_id="q2", zone_id="zone1")
        rs2.record_read("file", "/docs/b.txt", revision=10)

        sub_rs = Subscription(
            subscription_id="sub_rs",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        sub_rs2 = Subscription(
            subscription_id="sub_rs2",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q2",
        )
        await manager.register(sub_rs, read_set=rs1)
        await manager.register(sub_rs2, read_set=rs2)

        count = await manager.unregister_connection("conn1")

        assert count == 2
        assert len(manager._subscriptions) == 0
        assert registry.get_read_set("q1") is None


# ---------------------------------------------------------------------------
# TestStats
# ---------------------------------------------------------------------------


class TestStats:
    """Tests for get_stats."""

    @pytest.fixture
    def registry(self) -> ReadSetRegistry:
        return ReadSetRegistry()

    @pytest.fixture
    def manager(self, registry: ReadSetRegistry) -> ReactiveSubscriptionManager:
        return ReactiveSubscriptionManager(registry=registry)

    @pytest.mark.asyncio
    async def test_stats_include_counts(self, manager: ReactiveSubscriptionManager) -> None:
        """Stats show subscription counts."""
        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("file", "/inbox/a.txt", revision=10)

        sub_rs = Subscription(
            subscription_id="sub_rs",
            connection_id="conn1",
            zone_id="zone1",
            query_id="q1",
        )
        await manager.register(sub_rs, read_set=rs)

        stats = manager.get_stats()

        assert stats["total_subscriptions"] == 1
        assert stats["connections_tracked"] == 1

    @pytest.mark.asyncio
    async def test_stats_include_registry(self, manager: ReactiveSubscriptionManager) -> None:
        """Stats include registry stats."""
        stats = manager.get_stats()

        assert "registry" in stats
        assert "read_sets_count" in stats["registry"]
        assert "paths_indexed" in stats["registry"]
        assert "hit_rate_percent" in stats["registry"]

    @pytest.mark.asyncio
    async def test_stats_track_lookup_performance(
        self, manager: ReactiveSubscriptionManager
    ) -> None:
        """Stats track lookup count and avg time."""
        event = FileEvent(
            type="file_write",
            path="/inbox/a.txt",
            zone_id="zone1",
            version=10,
        )
        manager.find_affected_connections(event)
        manager.find_affected_connections(event)

        stats = manager.get_stats()

        assert stats["lookup_count"] == 2
        assert stats["avg_lookup_ms"] >= 0
