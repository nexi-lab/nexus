"""Tests for WiredServices dataclass and enlist_wired_services (Issue #2133, #1381, #1452, #1708)."""

import asyncio
import dataclasses
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.core.config import WiredServices
from nexus.factory.service_routing import enlist_wired_services
from tests.helpers.test_context import TEST_CONTEXT


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
    def nx(self) -> Any:
        """Minimal NexusFS with mocked pillars."""
        from nexus.core.config import KernelServices, ParseConfig
        from nexus.core.nexus_fs import NexusFS

        mock_metadata = MagicMock()
        mock_metadata.list = MagicMock(return_value=[])

        nx = NexusFS(
            metadata_store=mock_metadata,
            kernel_services=KernelServices(),
            parsing=ParseConfig(auto_parse=False),
        )
        nx._default_context = TEST_CONTEXT
        return nx

    @pytest.fixture()
    def coordinator(self, nx: Any) -> Any:
        """Create coordinator with BLM=None (Issue #1708)."""
        from nexus.system_services.lifecycle.service_lifecycle_coordinator import (
            ServiceLifecycleCoordinator,
        )

        return ServiceLifecycleCoordinator(nx._service_registry, None, nx._dispatch)

    def test_enlist_from_dataclass(self, nx: Any, coordinator: Any) -> None:
        mock_svc = MagicMock()
        ws = WiredServices(rebac_service=mock_svc, mount_service=mock_svc)
        asyncio.run(enlist_wired_services(coordinator, ws))
        assert nx.service("rebac")._service_instance is mock_svc
        assert nx.service("mount")._service_instance is mock_svc
        assert nx.service("mcp") is None

    def test_enlist_from_dict(self, nx: Any, coordinator: Any) -> None:
        mock_svc = MagicMock()
        asyncio.run(
            enlist_wired_services(
                coordinator, {"rebac_service": mock_svc, "mount_service": mock_svc}
            )
        )
        assert nx.service("rebac")._service_instance is mock_svc
        assert nx.service("mount")._service_instance is mock_svc
        assert nx.service("mcp") is None
