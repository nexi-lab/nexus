"""Tests for enlist_wired_services (Issue #2133, #1381, #1452, #1708).

WiredServices dataclass deleted — Tier 2b now returns plain dict.
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock

from nexus.factory.service_routing import enlist_wired_services


class TestEnlistWiredServices:
    """Test enlist_wired_services accepts dict (#1708)."""

    async def _make_nx(self, tmp_path: Any) -> Any:
        from tests.conftest import make_test_nexus

        return await make_test_nexus(tmp_path)

    def test_enlist_from_dict(self, tmp_path: Any) -> None:
        nx = asyncio.run(self._make_nx(tmp_path))
        registry = nx._service_registry

        # Clear pre-registered keys for clean test
        from nexus.factory.service_routing import _CANONICAL_NAMES

        for canonical in _CANONICAL_NAMES.values():
            registry.unregister(canonical)

        mock_svc = MagicMock()
        asyncio.run(
            enlist_wired_services(registry, {"rebac_service": mock_svc, "mount_service": mock_svc})
        )
        assert nx.service("rebac")._service_instance is mock_svc
        assert nx.service("mount")._service_instance is mock_svc
        assert nx.service("mcp") is None
