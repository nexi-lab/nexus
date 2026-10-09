"""Zone routing and host-reported Search capabilities."""

from unittest.mock import MagicMock

import pytest

from nexus.bricks.search.zone_registry import (
    ZoneSearchCapabilities,
    ZoneSearchRegistry,
)


class TestZoneSearchCapabilities:
    def test_keyword_only_zone(self) -> None:
        caps = ZoneSearchCapabilities(
            zone_id="phone_1",
            device_tier="phone",
            search_modes=("keyword",),
        )
        assert caps.supports_keyword
        assert not caps.supports_semantic

    def test_frozen(self) -> None:
        caps = ZoneSearchCapabilities(zone_id="z", search_modes=("keyword",))
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
        assert registry.get_capabilities("zone_a") is None

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

    @pytest.mark.parametrize("remote", [False, True])
    def test_replacing_a_route_clears_its_previous_capabilities(self, remote: bool) -> None:
        registry = ZoneSearchRegistry()
        register = registry.register_remote if remote else registry.register
        register("zone", MagicMock(), ZoneSearchCapabilities("zone", search_modes=("keyword",)))
        register("zone", MagicMock())
        assert registry.get_capabilities("zone") is None


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
    async def test_discovery_failure_clears_a_previous_capability_snapshot(self) -> None:
        from unittest.mock import AsyncMock

        registry = ZoneSearchRegistry()
        registry.register_remote(
            "remote", MagicMock(), ZoneSearchCapabilities("remote", search_modes=("keyword",))
        )
        client = AsyncMock()
        client.get_search_capabilities.side_effect = RuntimeError("host unavailable")
        with pytest.raises(RuntimeError, match="host unavailable"):
            await registry.discover_remote_capabilities("remote", client)
        assert registry.get_capabilities("remote") is None

    @pytest.mark.asyncio
    async def test_discovery_rejects_a_different_zone(self) -> None:
        from unittest.mock import AsyncMock

        registry = ZoneSearchRegistry()
        client = AsyncMock()
        client.get_search_capabilities.return_value = {"zone_id": "other"}
        with pytest.raises(ValueError, match="different zone"):
            await registry.discover_remote_capabilities("remote", client)
        assert registry.get_capabilities("remote") is None

    @pytest.mark.asyncio
    async def test_incomplete_discovery_does_not_invent_capabilities(self) -> None:
        from unittest.mock import AsyncMock

        registry = ZoneSearchRegistry()
        client = AsyncMock()
        client.get_search_capabilities.return_value = {"zone_id": "remote"}
        with pytest.raises(KeyError):
            await registry.discover_remote_capabilities("remote", client)
        assert registry.get_capabilities("remote") is None
