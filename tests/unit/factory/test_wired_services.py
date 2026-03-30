"""Tests for enlist_wired_services with plain dict (Issue #2133, #1381, #1452, #1708)."""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.factory.service_routing import enlist_wired_services


class TestEnlistWiredServices:
    """Test enlist_wired_services accepts dict (#1708)."""

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
