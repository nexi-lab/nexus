"""Unit tests for RPC proxy dispatch (rpc_proxy.py + method_registry.py).

Tests __getattr__-based dispatch, MethodSpec registry configuration,
deprecated method handling, and edge cases.

Issue #1289: Protocol + RPC Proxy pattern.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.remote.method_registry import METHOD_REGISTRY, MethodSpec
from nexus.remote.rpc_proxy import _INTERNAL_ATTRS, RPCProxyBase

# ============================================================
# Test Fixtures — Concrete proxy subclass with mock transport
# ============================================================


class MockRPCProxy(RPCProxyBase):
    """Concrete proxy subclass that records _call_rpc invocations."""

    def __init__(self) -> None:
        self.rpc_calls: list[tuple[str, dict[str, Any] | None, float | None]] = []
        self.rpc_return: Any = {"ok": True}

    def _call_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        self.rpc_calls.append((method, params, read_timeout))
        return self.rpc_return


@pytest.fixture
def proxy() -> MockRPCProxy:
    return MockRPCProxy()


# ============================================================
# MethodSpec Tests
# ============================================================


class TestMethodSpec:
    """Test MethodSpec dataclass."""

    def test_default_values(self) -> None:
        spec = MethodSpec()
        assert spec.rpc_name is None
        assert spec.response_key is None
        assert spec.custom_timeout is None
        assert spec.deprecated_message is None

    def test_frozen(self) -> None:
        spec = MethodSpec(response_key="files")
        with pytest.raises(AttributeError):
            spec.response_key = "other"  # type: ignore[misc]

    def test_response_key(self) -> None:
        spec = MethodSpec(response_key="files")
        assert spec.response_key == "files"

    def test_custom_timeout(self) -> None:
        spec = MethodSpec(custom_timeout=300.0)
        assert spec.custom_timeout == 300.0

    def test_deprecated_message(self) -> None:
        spec = MethodSpec(deprecated_message="Use new_method() instead")
        assert spec.deprecated_message == "Use new_method() instead"


class TestMethodRegistry:
    """Test METHOD_REGISTRY configuration."""

    def test_list_has_response_key(self) -> None:
        assert METHOD_REGISTRY["list"].response_key == "files"

    def test_glob_has_response_key(self) -> None:
        assert METHOD_REGISTRY["glob"].response_key == "matches"

    def test_grep_has_response_key(self) -> None:
        assert METHOD_REGISTRY["grep"].response_key == "results"

    def test_deprecated_methods_have_messages(self) -> None:
        deprecated = [
            "chmod",
            "chown",
            "chgrp",
            "grant_user",
            "grant_group",
            "deny_user",
            "revoke_acl",
            "get_acl",
        ]
        for name in deprecated:
            assert name in METHOD_REGISTRY, f"Missing deprecated method: {name}"
            spec = METHOD_REGISTRY[name]
            assert spec.deprecated_message is not None, f"{name} missing deprecated_message"

    def test_all_entries_are_methodspec(self) -> None:
        for name, spec in METHOD_REGISTRY.items():
            assert isinstance(spec, MethodSpec), f"{name} is not MethodSpec"


# ============================================================
# RPCProxyBase __getattr__ Tests
# ============================================================


class TestGetAttrDispatch:
    """Test __getattr__ dynamic method dispatch."""

    def test_trivial_method_dispatches(self, proxy: MockRPCProxy) -> None:
        """Methods not in registry use default pass-through."""
        proxy.rpc_return = {"status": "ok"}
        result = proxy.mkdir("/test/dir")
        assert result == {"status": "ok"}
        assert len(proxy.rpc_calls) == 1
        call_method, _, call_timeout = proxy.rpc_calls[0]
        assert call_method == "mkdir"
        assert call_timeout is None

    def test_registry_method_with_response_key(self, proxy: MockRPCProxy) -> None:
        """Methods with response_key extract from result dict."""
        proxy.rpc_return = {"files": ["a.txt", "b.txt"], "total": 2}
        result = proxy.list("/workspace")
        assert result == ["a.txt", "b.txt"]

    def test_registry_method_response_key_missing(self, proxy: MockRPCProxy) -> None:
        """When response_key is not in result, return full result."""
        proxy.rpc_return = {"unexpected": "data"}
        result = proxy.list("/workspace")
        assert result == {"unexpected": "data"}

    def test_deprecated_method_raises(self, proxy: MockRPCProxy) -> None:
        """Deprecated methods raise NotImplementedError."""
        with pytest.raises(NotImplementedError, match="rebac_create"):
            proxy.chmod("/test", 0o755)
        assert len(proxy.rpc_calls) == 0  # No RPC call made

    def test_unknown_private_attr_raises(self, proxy: MockRPCProxy) -> None:
        """Private attributes raise AttributeError."""
        with pytest.raises(AttributeError):
            _ = proxy._unknown_private

    def test_dunder_attr_raises(self, proxy: MockRPCProxy) -> None:
        """Dunder attributes raise AttributeError."""
        with pytest.raises(AttributeError):
            _ = proxy.__unknown__

    def test_internal_attrs_raise(self, proxy: MockRPCProxy) -> None:
        """Known internal attrs raise AttributeError."""
        with pytest.raises(AttributeError):
            _ = proxy.server_url

    def test_proxy_method_has_correct_name(self, proxy: MockRPCProxy) -> None:
        """Proxy methods preserve method name for debugging."""
        method = proxy.__getattr__("mkdir")
        assert method.__name__ == "mkdir"
        assert "MockRPCProxy.mkdir" in method.__qualname__

    def test_positional_args_mapped_to_kwargs(self, proxy: MockRPCProxy) -> None:
        """Positional args are mapped using ABC signature introspection."""
        # 'exists' has param_names: ['path'] from NexusFilesystem ABC
        proxy.rpc_return = {"exists": True}
        proxy.exists("/test/file.txt")
        _, params, _ = proxy.rpc_calls[0]
        assert params == {"path": "/test/file.txt"}

    def test_context_param_stripped(self, proxy: MockRPCProxy) -> None:
        """Context params are removed before RPC call."""
        proxy.rpc_return = {"ok": True}
        # Use a method that takes context param
        proxy.mkdir("/test", context={"user": "test"})
        _, call_params, _ = proxy.rpc_calls[0]
        assert "context" not in (call_params or {})

    def test_kwargs_passed_through(self, proxy: MockRPCProxy) -> None:
        """Keyword arguments are passed through to RPC."""
        proxy.rpc_return = {"ok": True}
        proxy.mkdir("/test", recursive=True)
        _, params, _ = proxy.rpc_calls[0]
        assert params is not None
        assert params.get("recursive") is True


class TestGetParamNames:
    """Test ABC parameter name introspection."""

    def test_known_method_returns_params(self) -> None:
        """Known NexusFilesystem methods return parameter names."""
        names = RPCProxyBase._get_param_names("read")
        assert "path" in names
        assert "self" not in names

    def test_unknown_method_returns_empty(self) -> None:
        """Unknown methods return empty list."""
        names = RPCProxyBase._get_param_names("nonexistent_method_xyz")
        assert names == []

    def test_caching_works(self) -> None:
        """Results are cached in _param_name_cache."""
        # Clear cache first
        RPCProxyBase._param_name_cache.pop("read", None)
        names1 = RPCProxyBase._get_param_names("read")
        names2 = RPCProxyBase._get_param_names("read")
        assert names1 is names2  # Same object (cached)


class TestInternalAttrs:
    """Test _INTERNAL_ATTRS configuration."""

    def test_contains_python_internals(self) -> None:
        assert "__class__" in _INTERNAL_ATTRS
        assert "__dict__" in _INTERNAL_ATTRS

    def test_contains_instance_attrs(self) -> None:
        assert "server_url" in _INTERNAL_ATTRS
        assert "api_key" in _INTERNAL_ATTRS
        assert "session" in _INTERNAL_ATTRS
        assert "_client" in _INTERNAL_ATTRS

    def test_is_frozenset(self) -> None:
        assert isinstance(_INTERNAL_ATTRS, frozenset)
