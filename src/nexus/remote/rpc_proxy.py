"""RPC proxy base class for dynamic method dispatch.

Provides __getattr__-based dispatch that eliminates the need to hand-write
trivial RPC wrapper methods. Methods are dispatched using the MethodSpec
registry for configurable transforms, with a default pass-through for
methods not in the registry.

Issue #1289: Protocol + RPC Proxy pattern.
"""

import inspect
import logging
from typing import Any

from nexus.remote.method_registry import METHOD_REGISTRY, MethodSpec

logger = logging.getLogger(__name__)

# Public attribute names that should NOT be intercepted by __getattr__.
# Private/dunder attrs are already excluded by the ``name.startswith("_")``
# check in __getattr__, so only public instance attrs need listing here.
_INTERNAL_ATTRS = frozenset(
    {
        "server_url",
        "api_key",
        "timeout",
        "connect_timeout",
        "session",
        "max_retries",
        "close",  # local lifecycle method — must not be proxied as an RPC call
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

    def close(self) -> None:
        """Local lifecycle method — close any underlying transport/session.

        Subclasses should override to release resources.  The base
        implementation is a no-op so that callers can safely call close()
        without knowing whether the proxy has resources to release.
        ``close`` must not be proxied as a remote RPC call (it is listed in
        _INTERNAL_ATTRS for that reason).
        """

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
        """Get parameter names for a method from known service classes.

        Uses inspect.signature to extract parameter names, caching results
        for performance. Falls back to empty list for unknown methods.
        """
        if method_name not in cls._param_name_cache:
            # Lazy import to avoid circular dependency
            from nexus.contracts.filesystem.filesystem_abc import NexusFilesystem

            # Try NexusFilesystem first, then NexusFS, then RPC params
            method = getattr(NexusFilesystem, method_name, None)
            if method is None:
                try:
                    import dataclasses as _dc

                    from nexus.server._rpc_params_generated import METHOD_PARAMS

                    params_cls = METHOD_PARAMS.get(method_name)
                    if params_cls and _dc.is_dataclass(params_cls):
                        names = [f.name for f in _dc.fields(params_cls)]
                        cls._param_name_cache[method_name] = names
                        return names
                except (ImportError, AttributeError):
                    pass

            if method and callable(method):
                try:
                    sig = inspect.signature(method)
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
