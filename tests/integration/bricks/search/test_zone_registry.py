"""Tests for ZoneSearchRegistry and ZoneSearchCapabilities (Issue #3147 Phase 2).

Tests zone registration, capability detection, daemon lookup, and
the from_daemon_stats factory method.
"""

from unittest.mock import MagicMock

import pytest

from nexus.bricks.search.zone_registry import (
    ZoneSearchCapabilities,
    ZoneSearchRegistry,
)


class TestZoneSearchCapabilities:
    def test_default_capabilities(self) -> None:
        caps = ZoneSearchCapabilities(zone_id="zone_a")
        assert caps.supports_semantic
        assert caps.supports_keyword
        assert caps.device_tier == "server"

    def test_keyword_only_zone(self) -> None:
        caps = ZoneSearchCapabilities(
            zone_id="phone_1",
            device_tier="phone",
            search_modes=("keyword",),
        )
        assert caps.supports_keyword
        assert not caps.supports_semantic

    def test_from_daemon_stats_with_db(self) -> None:
        daemon = MagicMock()
        daemon.get_stats.return_value = {
            "db_pool_size": 10,
            "zoekt_available": True,
            "embedding_dimensions": 384,
        }
        caps = ZoneSearchCapabilities.from_daemon_stats("zone_a", daemon)
        assert "semantic" in caps.search_modes
        assert "hybrid" in caps.search_modes
        assert "keyword" in caps.search_modes

    def test_from_daemon_stats_no_db(self) -> None:
        daemon = MagicMock()
        daemon.get_stats.return_value = {
            "db_pool_size": 0,
            "zoekt_available": False,
        }
        caps = ZoneSearchCapabilities.from_daemon_stats("phone_1", daemon)
        assert caps.search_modes == ("keyword",)
        assert not caps.supports_semantic

    def test_from_daemon_stats_no_stats_method(self) -> None:
        daemon = MagicMock(spec=[])  # No get_stats
        caps = ZoneSearchCapabilities.from_daemon_stats("zone_x", daemon)
        assert caps.search_modes == ("keyword",)

    def test_frozen(self) -> None:
        caps = ZoneSearchCapabilities(zone_id="z")
        with pytest.raises(AttributeError):
            caps.zone_id = "other"  # noqa: B003


class TestZoneSearchRegistry:
    def test_register_and_get(self) -> None:
        registry = ZoneSearchRegistry()
        daemon = MagicMock()
        daemon.get_stats.return_value = {"db_pool_size": 10}
        registry.register("zone_a", daemon)
        assert registry.get_daemon("zone_a") is daemon
        assert registry.has_zone("zone_a")

    def test_get_falls_back_to_default(self) -> None:
        default = MagicMock()
        registry = ZoneSearchRegistry(default_daemon=default)
        assert registry.get_daemon("unknown_zone") is default

    def test_get_returns_none_without_default(self) -> None:
        registry = ZoneSearchRegistry()
        assert registry.get_daemon("unknown") is None

    def test_unregister(self) -> None:
        registry = ZoneSearchRegistry()
        daemon = MagicMock()
        daemon.get_stats.return_value = {"db_pool_size": 10}
        registry.register("zone_a", daemon)
        registry.unregister("zone_a")
        assert not registry.has_zone("zone_a")
        assert registry.get_capabilities("zone_a") is None

    def test_list_zones(self) -> None:
        registry = ZoneSearchRegistry()
        d1, d2 = MagicMock(), MagicMock()
        d1.get_stats.return_value = {"db_pool_size": 0}
        d2.get_stats.return_value = {"db_pool_size": 10}
        registry.register("zone_a", d1)
        registry.register("zone_b", d2)
        assert set(registry.list_zones()) == {"zone_a", "zone_b"}

    def test_explicit_capabilities(self) -> None:
        registry = ZoneSearchRegistry()
        daemon = MagicMock()
        caps = ZoneSearchCapabilities(
            zone_id="phone",
            device_tier="phone",
            search_modes=("keyword",),
        )
        registry.register("phone", daemon, capabilities=caps)
        assert registry.get_capabilities("phone") is caps
        phone_caps = registry.get_capabilities("phone")
        assert phone_caps is not None
        assert not phone_caps.supports_semantic

    def test_default_daemon_setter(self) -> None:
        registry = ZoneSearchRegistry()
        assert registry.default_daemon is None
        daemon = MagicMock()
        registry.default_daemon = daemon
        assert registry.default_daemon is daemon
        # Should be used as fallback
        assert registry.get_daemon("any_zone") is daemon


class TestRemoteCapabilityDiscovery:
    @pytest.mark.asyncio
    async def test_discover_remote_success(self) -> None:
        """Successful RPC should populate capabilities."""
        from unittest.mock import AsyncMock

        registry = ZoneSearchRegistry()
        client = AsyncMock()
        client.get_search_capabilities = AsyncMock(
            return_value={
                "zone_id": "remote_z",
                "device_tier": "server",
                "search_modes": ["keyword", "semantic", "hybrid"],
                "embedding_model": "all-MiniLM-L6-v2",
                "embedding_dimensions": 384,
                "has_graph": True,
            }
        )

        caps = await registry.discover_remote_capabilities("remote_z", client)
        assert caps.zone_id == "remote_z"
        assert caps.supports_semantic
        assert caps.has_graph
        assert caps.embedding_dimensions == 384
        # Should be stored in registry
        assert registry.get_capabilities("remote_z") is caps

    @pytest.mark.asyncio
    async def test_discover_remote_fallback_on_error(self) -> None:
        """Failed RPC should fall back to keyword-only."""
        from unittest.mock import AsyncMock

        registry = ZoneSearchRegistry()
        client = AsyncMock()
        client.get_search_capabilities = AsyncMock(
            side_effect=RuntimeError("RPC not supported"),
        )

        caps = await registry.discover_remote_capabilities("old_node", client)
        assert caps.zone_id == "old_node"
        assert caps.search_modes == ("keyword",)
        assert not caps.supports_semantic
        assert caps.device_tier == "unknown"
