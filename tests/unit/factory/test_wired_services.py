"""Tests for WiredServices dataclass and enlist_wired_services (Issue #2133, #1381, #1452, #1708)."""

import asyncio
import dataclasses
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.core.config import WiredServices
from nexus.factory.service_routing import enlist_wired_services


class TestWiredServicesDataclass:
    """Test WiredServices frozen dataclass behavior."""

    def test_all_fields_default_to_none(self) -> None:
        ws = WiredServices()
        for field in dataclasses.fields(ws):
            assert getattr(ws, field.name) is None

    def test_frozen_prevents_mutation(self) -> None:
        ws = WiredServices()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ws.rebac_service = "test"

    def test_construction_with_values(self) -> None:
        mock_svc = MagicMock()
        ws = WiredServices(rebac_service=mock_svc, gateway=mock_svc)
        assert ws.rebac_service is mock_svc
        assert ws.gateway is mock_svc
        assert ws.mcp_service is None

    def test_replace_creates_new_instance(self) -> None:
        ws1 = WiredServices(rebac_service="a")
        ws2 = dataclasses.replace(ws1, rebac_service="b")
        assert ws1.rebac_service == "a"
        assert ws2.rebac_service == "b"

    def test_field_count(self) -> None:
        """WiredServices should have 20 service fields."""
        assert len(dataclasses.fields(WiredServices)) == 20


class TestEnlistWiredServices:
    """Test enlist_wired_services accepts both WiredServices and dict (#1708)."""

    @pytest.fixture()
    async def nx(self, tmp_path: Any) -> Any:
        """Minimal NexusFS via factory boot path."""
        from tests.conftest import make_test_nexus

        return await make_test_nexus(tmp_path)

    @pytest.fixture()
    def registry(self, nx: Any) -> Any:
        """Return the ServiceRegistry (now has lifecycle methods, Issue #1814).

        Clears any wired-service keys that the factory boot path already
        registered, so tests can call enlist_wired_services() without
        hitting 'already registered' errors.
        """
        from nexus.factory.service_routing import _CANONICAL_NAMES

        reg = nx._service_registry
        for canonical in _CANONICAL_NAMES.values():
            reg.unregister(canonical)
        return reg

    def test_enlist_from_dataclass(self, nx: Any, registry: Any) -> None:
        mock_svc = MagicMock()
        # Factory boot may have pre-registered these; clear them for a clean test.
        for key in ("rebac", "mount"):
            try:
                registry.unregister(key)
            except KeyError:
                pass
        ws = WiredServices(rebac_service=mock_svc, mount_service=mock_svc)
        asyncio.run(enlist_wired_services(registry, ws))
        assert nx.service("rebac")._service_instance is mock_svc
        assert nx.service("mount")._service_instance is mock_svc
        assert nx.service("mcp") is None

    def test_enlist_from_dict(self, nx: Any, registry: Any) -> None:
        mock_svc = MagicMock()
        # Factory boot may have pre-registered these; clear them for a clean test.
        for key in ("rebac", "mount"):
            try:
                registry.unregister(key)
            except KeyError:
                pass
        asyncio.run(
            enlist_wired_services(registry, {"rebac_service": mock_svc, "mount_service": mock_svc})
        )
        assert nx.service("rebac")._service_instance is mock_svc
        assert nx.service("mount")._service_instance is mock_svc
        assert nx.service("mcp") is None
