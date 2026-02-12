"""Reusable conformance helpers for kernel protocol tests (Issue #1383).

Provides ``assert_protocol_conformance`` which verifies that an implementation
class exposes all methods declared in a Protocol with compatible parameter
names and counts.
"""

from __future__ import annotations

import inspect
from typing import Any


def assert_protocol_conformance(
    impl_class: type,
    protocol_class: type,
    *,
    ignore_methods: frozenset[str] = frozenset(),
) -> None:
    """Verify *impl_class* has all public methods from *protocol_class*.

    Checks method existence and parameter name compatibility.  Does NOT
    enforce async/sync parity — sync implementations are expected to be
    wrapped before production use.

    Args:
        impl_class: Concrete class to check.
        protocol_class: ``@runtime_checkable`` Protocol to check against.
        ignore_methods: Method names to skip (e.g. for known divergences).

    Raises:
        AssertionError: On any conformance violation.
    """
    protocol_methods = _public_methods(protocol_class)

    for name, proto_method in protocol_methods.items():
        if name in ignore_methods:
            continue

        impl_method = getattr(impl_class, name, None)
        assert impl_method is not None, (
            f"{impl_class.__name__} is missing method '{name}' "
            f"required by {protocol_class.__name__}"
        )
        assert callable(impl_method), f"{impl_class.__name__}.{name} is not callable"

        # Verify protocol params are a subset of impl params (impl may have
        # additional optional parameters that the protocol doesn't require).
        proto_params = _param_names(proto_method)
        impl_params = _param_names(impl_method)

        missing = [p for p in proto_params if p not in impl_params]
        assert not missing, (
            f"{impl_class.__name__}.{name} missing protocol params: {missing}\n"
            f"  Protocol: {proto_params}\n"
            f"  Impl:     {impl_params}"
        )


def _public_methods(cls: type) -> dict[str, Any]:
    """Return {name: method} for all public, non-dunder methods on *cls*."""
    result: dict[str, Any] = {}
    for name in dir(cls):
        if name.startswith("_"):
            continue
        attr = getattr(cls, name, None)
        if attr is not None and callable(attr):
            result[name] = attr
    return result


def _param_names(method: Any) -> list[str]:
    """Return parameter names excluding 'self'."""
    try:
        sig = inspect.signature(method)
    except (ValueError, TypeError):
        return []
    return [n for n in sig.parameters if n != "self"]
