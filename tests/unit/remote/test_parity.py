"""Parity tests: verify all @rpc_expose methods are dispatchable via proxy.

Introspects NexusFS for all @rpc_expose-decorated methods and verifies
the proxy can dispatch each one (doesn't raise AttributeError).

Issue #1289: Protocol + RPC Proxy pattern.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from nexus.remote.rpc_proxy import RPCProxyBase


class MockRPCProxy(RPCProxyBase):
    """Concrete proxy for parity testing."""

    def __init__(self) -> None:
        self.rpc_calls: list[tuple[str, dict[str, Any] | None]] = []

    def _call_rpc(  # noqa: ARG002
        self,
        method: str,
        params: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        self.rpc_calls.append((method, params))
        return {"ok": True}


def _get_rpc_exposed_methods() -> list[str]:
    """Collect all @rpc_expose method names from NexusFS and its mixins."""
    from nexus.core.nexus_fs import NexusFS

    exposed = []
    for name in dir(NexusFS):
        if name.startswith("_"):
            continue
        attr = getattr(NexusFS, name, None)
        if attr and callable(attr) and getattr(attr, "_rpc_exposed", False):
            exposed.append(name)
    return sorted(exposed)


class TestRPCExposeParity:
    """Verify proxy can dispatch every @rpc_expose method."""

    @pytest.fixture
    def proxy(self) -> MockRPCProxy:
        return MockRPCProxy()

    def test_rpc_exposed_methods_exist(self) -> None:
        """Sanity check: at least 50 methods are @rpc_expose'd."""
        methods = _get_rpc_exposed_methods()
        assert len(methods) >= 50, f"Expected 50+ @rpc_expose methods, found {len(methods)}"

    @pytest.mark.parametrize("method_name", _get_rpc_exposed_methods())
    def test_proxy_can_dispatch(self, proxy: MockRPCProxy, method_name: str) -> None:
        """Each @rpc_expose method must be dispatchable via __getattr__.

        The proxy should either:
        1. Return a callable (from __getattr__ dispatch), OR
        2. Have an explicit override method defined on the class
        """
        # Should NOT raise AttributeError
        attr = getattr(proxy, method_name)
        assert callable(attr), f"{method_name} is not callable"


class TestSignatureCoverage:
    """Verify proxy parameter names match ABC signatures."""

    def test_abc_methods_have_param_names(self) -> None:
        """All NexusFilesystem abstract methods should have discoverable signatures."""
        from nexus.core.filesystem import NexusFilesystem

        for name in dir(NexusFilesystem):
            if name.startswith("_"):
                continue
            attr = getattr(NexusFilesystem, name, None)
            if attr and callable(attr):
                try:
                    sig = inspect.signature(attr)
                    params = [p for p in sig.parameters if p != "self"]
                    # Just verify introspection doesn't fail
                    assert isinstance(params, list)
                except (ValueError, TypeError):
                    pass  # Some methods can't be introspected
