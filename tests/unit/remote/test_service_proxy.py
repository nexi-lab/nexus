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

    @pytest.mark.parametrize(
        ("method", "arguments"),
        [
            ("semantic_search", {"query": "needle"}),
            ("semantic_search_index", {"path": "/docs"}),
            ("semantic_search_stats", {}),
        ],
    )
    def test_search_keeps_only_the_zone_and_respects_an_explicit_selector(self, method, arguments):
        from types import SimpleNamespace

        from nexus.remote.service_proxy import RemoteServiceProxy

        calls, call_rpc = _make_recorder()
        configured = SimpleNamespace(zone_id="configured", is_admin=True, user_id="ambient")
        proxy = RemoteServiceProxy(call_rpc, default_context=configured)
        context = SimpleNamespace(zone_id="workspace", is_admin=True, user_id="forged")
        getattr(proxy, method)(**arguments)
        assert calls[-1] == (method, {**arguments, "zone_id": "configured"})
        getattr(proxy, method)(**arguments, context=context)
        assert calls[-1] == (method, {**arguments, "zone_id": "workspace"})
        getattr(proxy, method)(**arguments, zone_id=None, _context=context)
        assert calls[-1] == (method, {**arguments, "zone_id": "workspace"})
        getattr(proxy, method)(**arguments, zone_id="explicit", context=context)
        assert calls[-1] == (method, {**arguments, "zone_id": "explicit"})

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
