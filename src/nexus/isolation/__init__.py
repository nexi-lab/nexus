"""Sub-interpreter / process isolation for untrusted Backend implementations.

WARNING: This provides FAULT ISOLATION (state / crash isolation), NOT security
sandboxing.  For untrusted-code security, use Docker / E2B sandbox providers
(see ``src/nexus/sandbox/``).

Public API
----------
- ``IsolatedBackend``            — Backend wrapper (transparent decorator)
- ``IsolationConfig``            — Immutable configuration dataclass
- ``create_isolated_backend()``  — Convenience factory

Errors
------
- ``IsolationError``             — base exception
- ``IsolationStartupError``      — import / init failure in worker
- ``IsolationCallError``         — method raised exception in worker
- ``IsolationTimeoutError``      — call exceeded deadline
- ``IsolationPoolError``         — pool shut down or unhealthy
"""

from __future__ import annotations

from typing import Any

from nexus.isolation.backend import IsolatedBackend
from nexus.isolation.config import IsolationConfig
from nexus.isolation.errors import (
    IsolationCallError,
    IsolationError,
    IsolationPoolError,
    IsolationStartupError,
    IsolationTimeoutError,
)

__all__ = [
    "IsolatedBackend",
    "IsolationConfig",
    "IsolationCallError",
    "IsolationError",
    "IsolationPoolError",
    "IsolationStartupError",
    "IsolationTimeoutError",
    "create_isolated_backend",
]


def create_isolated_backend(
    module: str,
    cls: str,
    *,
    pool_size: int = 2,
    call_timeout: float = 30.0,
    force_process: bool = False,
    **backend_kwargs: Any,
) -> IsolatedBackend:
    """Convenience factory for creating an ``IsolatedBackend``.

    Parameters
    ----------
    module:
        Dotted import path of the backend module.
    cls:
        Class name inside *module*.
    pool_size:
        Number of workers in the executor pool.
    call_timeout:
        Per-call timeout in seconds.
    force_process:
        Force ``ProcessPoolExecutor`` even on Python 3.14+.
    **backend_kwargs:
        Keyword arguments forwarded to the backend constructor.

    Returns
    -------
    IsolatedBackend
        Ready-to-use backend wrapper.
    """
    config = IsolationConfig(
        backend_module=module,
        backend_class=cls,
        backend_kwargs=backend_kwargs,
        pool_size=pool_size,
        call_timeout=call_timeout,
        force_process=force_process,
    )
    return IsolatedBackend(config)
