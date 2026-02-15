"""RPC proxy base class for dynamic method dispatch.

Provides __getattr__-based dispatch that eliminates the need to hand-write
trivial RPC wrapper methods. Methods are dispatched using the MethodSpec
registry for configurable transforms, with a default pass-through for
methods not in the registry.

Issue #1289: Protocol + RPC Proxy pattern.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from nexus.remote.method_registry import METHOD_REGISTRY, MethodSpec

logger = logging.getLogger(__name__)

# Set of attribute names that should NOT be intercepted by __getattr__.
# These are either Python internals or attributes managed by the class itself.
_INTERNAL_ATTRS = frozenset(
    {
        # Python internals
        "__class__",
        "__dict__",
        "__doc__",
        "__module__",
        "__weakref__",
        # Pickle / copy
        "__getstate__",
        "__setstate__",
        "__reduce__",
        "__reduce_ex__",
        "__copy__",
        "__deepcopy__",
        # Class inspection
        "__abstractmethods__",
        "__subclasshook__",
        # Instance attributes set in __init__
        "server_url",
        "api_key",
        "timeout",
        "connect_timeout",
        "session",
        "_client",
        "_zone_id",
        "_agent_id",
        "_negative_bloom",
        "_negative_cache_capacity",
        "_negative_cache_fp_rate",
        "_memory_api",
        "_semantic_search",
        "_initialized",
        "_llm_service_instance",
        "max_retries",
    }
)


class RPCProxyBase:
    """Mixin that provides __getattr__-based RPC method dispatch.

    When a method is accessed that isn't defined on the class, __getattr__
    looks it up in METHOD_REGISTRY and returns a dynamically-generated
    wrapper that:
    1. Maps positional args to keyword args using ABC signature introspection
    2. Strips context params (handled server-side via auth headers)
    3. Calls _call_rpc with the appropriate method name and timeout
    4. Applies response_key extraction if configured
    5. Returns the result

    Methods with complex logic (negative cache, content encoding, etc.)
    should be defined as explicit overrides on the subclass.
    """

    # Cache for ABC method parameter names (class-level)
    _param_name_cache: dict[str, list[str]] = {}

    def _call_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        """Make RPC call to server. Implemented by subclass."""
        raise NotImplementedError

    @classmethod
    def _get_param_names(cls, method_name: str) -> list[str]:
        """Get parameter names for a method from the NexusFilesystem ABC.

        Uses inspect.signature to extract parameter names, caching results
        for performance. Falls back to empty list for non-ABC methods.
        """
        if method_name not in cls._param_name_cache:
            # Lazy import to avoid circular dependency
            from nexus.core.filesystem import NexusFilesystem

            abc_method = getattr(NexusFilesystem, method_name, None)
            if abc_method and callable(abc_method):
                try:
                    sig = inspect.signature(abc_method)
                    names = [p for p in sig.parameters if p != "self"]
                    cls._param_name_cache[method_name] = names
                except (ValueError, TypeError):
                    cls._param_name_cache[method_name] = []
            else:
                cls._param_name_cache[method_name] = []
        return cls._param_name_cache[method_name]

    def __getattr__(self, name: str) -> Any:
        """Dynamic dispatch for RPC methods not explicitly defined.

        Raises:
            AttributeError: For truly unknown attributes (Python internals,
                private attributes starting with '_').
        """
        # Don't intercept private/dunder attributes or known instance attrs
        if name.startswith("_") or name in _INTERNAL_ATTRS:
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

        spec = METHOD_REGISTRY.get(name)

        # Deprecated methods
        if spec is not None and spec.deprecated_message is not None:

            def _deprecated(*_args: Any, **_kwargs: Any) -> None:
                raise NotImplementedError(spec.deprecated_message)

            return _deprecated

        # Build proxy method
        def _proxy(*args: Any, **kwargs: Any) -> Any:
            return self._dispatch_rpc(name, spec, args, kwargs)

        # Preserve method name for debugging
        _proxy.__name__ = name
        _proxy.__qualname__ = f"{type(self).__name__}.{name}"
        return _proxy

    def _dispatch_rpc(
        self,
        name: str,
        spec: MethodSpec | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Execute the RPC dispatch logic for a proxy method.

        Args:
            name: Method name
            spec: Optional MethodSpec with dispatch configuration
            args: Positional arguments from the caller
            kwargs: Keyword arguments from the caller

        Returns:
            RPC call result, optionally transformed by spec
        """
        # Map positional args to keyword args using ABC signature
        param_names = self._get_param_names(name)
        params: dict[str, Any] = {}
        for i, arg in enumerate(args):
            if i < len(param_names):
                params[param_names[i]] = arg
        params.update(kwargs)

        # Remove context param (handled server-side via auth headers)
        params.pop("context", None)
        params.pop("_context", None)

        # Determine RPC method name and timeout
        rpc_name = spec.rpc_name if spec and spec.rpc_name else name
        read_timeout = spec.custom_timeout if spec else None

        # Call RPC
        result = self._call_rpc(rpc_name, params or None, read_timeout=read_timeout)

        # Apply response_key extraction
        if spec and spec.response_key and isinstance(result, dict):
            return result.get(spec.response_key, result)

        return result
