"""Worker functions executed inside sub-interpreters / child processes.

Every function in this module MUST be a top-level (module-scope) function so
that ``pickle`` can resolve it by qualified name.  Each worker (interpreter or
process) owns exactly one ``Backend`` instance that is lazily created on the
first call and reused for subsequent calls.
"""

from __future__ import annotations

import contextlib
import importlib
from typing import Any

# ── Per-worker globals (one Backend instance per interpreter / process) ──────
_BACKEND_INSTANCE: Any = None
_BACKEND_SPEC: tuple[str, str, tuple[tuple[str, Any], ...]] | None = (
    None  # (module, class, kwargs_items)
)


def _ensure_backend(
    module_path: str,
    class_name: str,
    init_kwargs: dict[str, Any],
) -> Any:
    """Lazily create (or replace) the worker-local Backend instance.

    The *spec* triple ``(module, class, sorted-kwargs)`` is used to detect
    configuration changes — if the caller asks for a different backend the
    old instance is disconnected and a new one is created.
    """
    global _BACKEND_INSTANCE, _BACKEND_SPEC  # noqa: PLW0603

    spec = (module_path, class_name, tuple(sorted(init_kwargs.items())))
    if _BACKEND_INSTANCE is not None and spec == _BACKEND_SPEC:
        return _BACKEND_INSTANCE

    # Disconnect old instance (if any) before replacing
    if _BACKEND_INSTANCE is not None:
        with contextlib.suppress(Exception):
            _BACKEND_INSTANCE.disconnect()
        _BACKEND_INSTANCE = None
        _BACKEND_SPEC = None

    mod = importlib.import_module(module_path)
    klass = getattr(mod, class_name)
    instance = klass(**init_kwargs)
    try:
        instance.connect()
    except Exception:
        with contextlib.suppress(Exception):
            instance.disconnect()
        raise
    _BACKEND_INSTANCE = instance
    _BACKEND_SPEC = spec
    return _BACKEND_INSTANCE


def worker_call(
    module_path: str,
    class_name: str,
    init_kwargs: dict[str, Any],
    method_name: str,
    method_args: tuple[Any, ...],
    method_kwargs: dict[str, Any],
) -> Any:
    """Execute a Backend method in the isolated worker context.

    Called via ``executor.submit(worker_call, ...)``.  Lazily creates the
    backend on first invocation, then reuses it for all subsequent calls.
    """
    backend = _ensure_backend(module_path, class_name, init_kwargs)
    return getattr(backend, method_name)(*method_args, **method_kwargs)


def worker_get_property(
    module_path: str,
    class_name: str,
    init_kwargs: dict[str, Any],
    prop_name: str,
) -> Any:
    """Read a Backend property in the isolated worker context."""
    backend = _ensure_backend(module_path, class_name, init_kwargs)
    return getattr(backend, prop_name)


def worker_shutdown() -> None:
    """Disconnect and release the worker-local Backend instance."""
    global _BACKEND_INSTANCE, _BACKEND_SPEC  # noqa: PLW0603

    if _BACKEND_INSTANCE is not None:
        with contextlib.suppress(Exception):
            _BACKEND_INSTANCE.disconnect()
    _BACKEND_INSTANCE = None
    _BACKEND_SPEC = None
