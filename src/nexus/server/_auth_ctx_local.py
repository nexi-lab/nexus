"""Per-request auth-context thread-local for the python_ffi dispatch path.

The Rust tonic ``Call`` handler already builds an ``auth_dict`` from the
gRPC metadata (api-key fast path or OIDC ``authenticate_sync``).  The
legacy Python servicer path consumed it by passing ``auth_dict`` as an
explicit arg into ``dispatch_method`` → ``handle_*``.  The Rust thin-
dispatch path (``Kernel::dispatch_rust_call``) only carries
``(svc_name, method, payload)`` — auth context falls off.

This module bridges the gap with a plain ``contextvars.ContextVar``:

  * ``set_auth(auth_dict)`` is called by the tonic Call handler (via
    PyO3) BEFORE ``dispatch_rust_call``.
  * ``clear_auth()`` runs after dispatch returns.
  * ``get_auth()`` is consulted by ``services::python_ffi::PyFfiRouter``
    when it forwards into a Python service that takes a ``context``
    kwarg, so admin handlers / @rpc_expose'd methods that need
    ``OperationContext`` get the right one.

ContextVar (not threading.local) so async paths share the value
correctly across coroutine reschedules — same ergonomics the legacy
servicer path enjoys via FastAPI request scope.
"""

from __future__ import annotations

import contextvars
from typing import Any

_auth_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "nexus_auth_dict",
    default=None,
)


def set_auth(auth_dict: dict[str, Any] | None) -> Any:
    """Store ``auth_dict`` for the current request scope.

    Returns the ``Token`` so callers can ``reset(token)`` symmetrically;
    the typical usage is ``set_auth + clear_auth`` rather than
    token-tracking, so the return value is informational.
    """
    return _auth_var.set(auth_dict)


def get_auth() -> dict[str, Any] | None:
    """Return the auth dict set by the active request scope, or None.

    Called by the python_ffi router to construct a fresh
    ``OperationContext`` per dispatch.  Returns None when no auth
    context has been set (e.g., direct in-process calls that bypass
    the gRPC entry point).
    """
    return _auth_var.get()


def clear_auth() -> None:
    """Reset the auth dict for the current scope (matched ``set_auth``)."""
    _auth_var.set(None)
