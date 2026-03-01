"""Tests for WiredServices dataclass and _boot_wired_services typing (Issue #2133)."""

import dataclasses
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.core.config import WiredServices


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
        """WiredServices should have 22 service fields."""
        assert len(dataclasses.fields(WiredServices)) == 22


class TestNexusFSBindWiredServices:
    """Test NexusFS._bind_wired_services accepts both WiredServices and dict."""

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
        return nx

    def test_bind_wired_services_dataclass(self, nx: Any) -> None:
        mock_svc = MagicMock()
        ws = WiredServices(rebac_service=mock_svc, mount_service=mock_svc)
        nx._bind_wired_services(ws)
        assert nx.rebac_service is mock_svc
        assert nx.mount_service is mock_svc
        assert nx.mcp_service is None

    def test_bind_wired_services_dict(self, nx: Any) -> None:
        mock_svc = MagicMock()
        nx._bind_wired_services({"rebac_service": mock_svc, "mount_service": mock_svc})
        assert nx.rebac_service is mock_svc
        assert nx.mount_service is mock_svc
        assert nx.mcp_service is None
