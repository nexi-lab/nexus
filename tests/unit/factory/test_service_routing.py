"""Tests for factory service routing tables (Issue #1381)."""

from unittest.mock import MagicMock

from nexus.factory.service_routing import (
    SERVICE_ALIASES,
    SERVICE_METHODS,
    resolve_service_attr,
)


class TestResolveServiceAttr:
    """Test resolve_service_attr two-phase lookup."""

    def test_standard_forwarding(self) -> None:
        """SERVICE_METHODS: same method name on service."""
        obj = MagicMock()
        obj.__dict__["_workspace_rpc_service"] = svc = MagicMock()
        svc.workspace_snapshot = lambda: "snap"

        result = resolve_service_attr(obj, "workspace_snapshot")
        assert result is svc.workspace_snapshot

    def test_alias_forwarding(self) -> None:
        """SERVICE_ALIASES: method name differs on service."""
        obj = MagicMock()
        obj.__dict__["oauth_service"] = svc = MagicMock()
        svc.list_providers = lambda: ["google"]

        result = resolve_service_attr(obj, "oauth_list_providers")
        assert result is svc.list_providers

    def test_unknown_attr_returns_none(self) -> None:
        obj = MagicMock()
        assert resolve_service_attr(obj, "totally_unknown_method") is None

    def test_service_not_wired_returns_none(self) -> None:
        """Known method but service attr is None / missing → None."""
        obj = MagicMock()
        obj.__dict__.clear()
        assert resolve_service_attr(obj, "workspace_snapshot") is None

    def test_alias_takes_precedence(self) -> None:
        """If a name is in both ALIASES and METHODS, alias wins."""
        # "get_namespace" is in both — alias should be checked first
        obj = MagicMock()
        obj.__dict__["rebac_service"] = svc = MagicMock()
        svc.get_namespace_sync = lambda: "aliased"

        result = resolve_service_attr(obj, "get_namespace")
        assert result is svc.get_namespace_sync

    def test_glob_not_in_service_methods(self) -> None:
        """glob removed from SERVICE_METHODS — callers use search_service directly."""
        assert "glob" not in SERVICE_METHODS
        assert "glob_batch" not in SERVICE_METHODS
        obj = MagicMock()
        obj.__dict__["search_service"] = MagicMock()
        assert resolve_service_attr(obj, "glob") is None

    def test_grep_not_in_service_methods(self) -> None:
        """grep removed from SERVICE_METHODS — callers use search_service directly."""
        assert "grep" not in SERVICE_METHODS
        obj = MagicMock()
        obj.__dict__["search_service"] = MagicMock()
        assert resolve_service_attr(obj, "grep") is None


class TestRoutingTableCompleteness:
    """Sanity checks for the routing tables."""

    def test_service_methods_non_empty(self) -> None:
        assert len(SERVICE_METHODS) >= 54

    def test_service_aliases_non_empty(self) -> None:
        assert len(SERVICE_ALIASES) >= 53
