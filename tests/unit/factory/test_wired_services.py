"""Tests for enlist_wired_services with plain dict (Issue #2133, #1381, #1452, #1708)."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.factory.service_routing import enlist_wired_services


class TestEnlistWiredServices:
    """Test enlist_wired_services accepts dict (#1708)."""

    @pytest.fixture()
    def nx(self, tmp_path: Any) -> Any:
        """Minimal NexusFS via factory boot path."""
        from tests.conftest import make_test_nexus

        return make_test_nexus(tmp_path)

    @pytest.fixture()
    def registry(self, nx: Any) -> Any:
        """Return NexusFS itself as the service coordinator.

        NexusFS.sys_setattr("/__sys__/services/X") dispatches to kernel,
        so NexusFS is the coordinator for enlist_wired_services.
        Clears any wired-service keys that the factory boot path already
        registered, so tests can call enlist_wired_services() without
        hitting 'already registered' errors.
        """
        from nexus.factory.service_routing import _CANONICAL_NAMES

        for canonical in _CANONICAL_NAMES.values():
            try:
                nx._kernel.service_unregister(canonical)
            except (KeyError, Exception):
                pass
        return nx

    def test_enlist_from_dict(self, nx: Any, registry: Any) -> None:
        mock_svc = MagicMock()
        # Factory boot may have pre-registered these; clear them for a clean test.
        for key in ("rebac", "mount"):
            try:
                nx._kernel.service_unregister(key)
            except (KeyError, Exception):
                pass
        enlist_wired_services(registry, {"rebac_service": mock_svc, "mount_service": mock_svc})
        assert nx.service("rebac") is mock_svc
        assert nx.service("mount") is mock_svc
        assert nx.service("mcp") is None
