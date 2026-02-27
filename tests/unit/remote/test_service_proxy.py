"""Unit tests for RemoteServiceProxy.

Issue #1171: Service-layer RPC proxy for REMOTE profile.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_recorder() -> tuple[list, callable]:
    """Return (calls_list, call_rpc) where call_rpc records invocations."""
    calls: list[tuple[str, dict | None]] = []

    def call_rpc(method: str, params: dict | None = None, **kw) -> dict:
        calls.append((method, params))
        return {"ok": True, "method": method}

    return calls, call_rpc


# ---------------------------------------------------------------------------
# RemoteServiceProxy tests
# ---------------------------------------------------------------------------


class TestRemoteServiceProxy:
    """Tests for the universal RPC proxy."""

    def test_basic_forwarding(self):
        """Method call is forwarded to call_rpc with correct name and kwargs."""
        from nexus.remote.service_proxy import RemoteServiceProxy

        calls, call_rpc = _make_recorder()
        proxy = RemoteServiceProxy(call_rpc, service_name="test")

        result = proxy.workspace_snapshot(workspace_path="/ws", description="snap")

        assert len(calls) == 1
        method, params = calls[0]
        assert method == "workspace_snapshot"
        assert params["workspace_path"] == "/ws"
        assert params["description"] == "snap"
        assert result == {"ok": True, "method": "workspace_snapshot"}

    def test_no_args_sends_none_params(self):
        """Method with no arguments sends params=None."""
        from nexus.remote.service_proxy import RemoteServiceProxy

        calls, call_rpc = _make_recorder()
        proxy = RemoteServiceProxy(call_rpc)

        proxy.list_agents()

        assert len(calls) == 1
        _, params = calls[0]
        assert params is None

    def test_context_stripped(self):
        """context and _context kwargs are stripped (server handles auth)."""
        from nexus.remote.service_proxy import RemoteServiceProxy

        calls, call_rpc = _make_recorder()
        proxy = RemoteServiceProxy(call_rpc)

        proxy.sandbox_create(name="box", context={"user_id": "u1"}, _context="x")

        _, params = calls[0]
        assert "context" not in params
        assert "_context" not in params
        assert params["name"] == "box"

    def test_private_attr_raises(self):
        """Private attributes (underscore-prefixed) raise AttributeError."""
        from nexus.remote.service_proxy import RemoteServiceProxy

        proxy = RemoteServiceProxy(lambda m, p: None)

        with pytest.raises(AttributeError):
            proxy._internal_method()

    def test_dunder_attr_raises(self):
        """Dunder attributes raise AttributeError (Python internals)."""
        from nexus.remote.service_proxy import RemoteServiceProxy

        proxy = RemoteServiceProxy(lambda m, p: None)
        dunder = "__nonexistent_dunder__"

        with pytest.raises(AttributeError):
            getattr(proxy, dunder)

    def test_repr(self):
        """repr shows service name."""
        from nexus.remote.service_proxy import RemoteServiceProxy

        proxy = RemoteServiceProxy(lambda m, p: None, service_name="universal")
        assert "universal" in repr(proxy)

    def test_same_proxy_different_methods(self):
        """Same proxy instance can forward different method names."""
        from nexus.remote.service_proxy import RemoteServiceProxy

        calls, call_rpc = _make_recorder()
        proxy = RemoteServiceProxy(call_rpc)

        proxy.workspace_snapshot(workspace_path="/ws")
        proxy.register_agent(agent_id="a1")
        proxy.lock(path="/file")

        assert [c[0] for c in calls] == [
            "workspace_snapshot",
            "register_agent",
            "lock",
        ]


# ---------------------------------------------------------------------------
# _boot_remote_services tests
# ---------------------------------------------------------------------------


class TestBootRemoteServices:
    """Tests for the factory wiring helper."""

    def test_wires_all_service_slots(self):
        """_boot_remote_services fills all wired service slots with proxy."""
        from unittest.mock import MagicMock

        from nexus.factory._remote import _WIRED_FIELDS, _boot_remote_services
        from nexus.remote.service_proxy import RemoteServiceProxy

        # Create a mock NexusFS with _bind_wired_services
        nfs = MagicMock()
        nfs._bind_wired_services = MagicMock()

        _, call_rpc = _make_recorder()
        _boot_remote_services(nfs, call_rpc)

        # _bind_wired_services was called with a dict covering all fields
        nfs._bind_wired_services.assert_called_once()
        wired_dict = nfs._bind_wired_services.call_args[0][0]

        assert isinstance(wired_dict, dict)
        for field in _WIRED_FIELDS:
            assert field in wired_dict
            assert isinstance(wired_dict[field], RemoteServiceProxy)

        # version_service also set
        assert isinstance(nfs.version_service, RemoteServiceProxy)

    def test_all_slots_are_same_proxy_instance(self):
        """All slots share one proxy instance (universal pass-through)."""
        from unittest.mock import MagicMock

        from nexus.factory._remote import _boot_remote_services

        nfs = MagicMock()
        nfs._bind_wired_services = MagicMock()

        _, call_rpc = _make_recorder()
        _boot_remote_services(nfs, call_rpc)

        wired_dict = nfs._bind_wired_services.call_args[0][0]
        proxies = list(wired_dict.values())

        # All values should be the same object
        assert all(p is proxies[0] for p in proxies)


# ---------------------------------------------------------------------------
# _SERVICE_METHODS event entries
# ---------------------------------------------------------------------------


class TestServiceMethodsEventEntries:
    """Verify event/locking methods are in the dispatch table."""

    def test_event_methods_registered(self):
        """lock, unlock, extend_lock, wait_for_changes are in _SERVICE_METHODS."""
        from nexus.core.nexus_fs import NexusFS

        for method in ("lock", "unlock", "extend_lock", "wait_for_changes"):
            assert method in NexusFS._SERVICE_METHODS, f"{method} missing from _SERVICE_METHODS"
            assert NexusFS._SERVICE_METHODS[method] == "events_service"
