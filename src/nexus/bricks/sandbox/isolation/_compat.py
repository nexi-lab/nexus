"""Runtime compatibility — pool creation with version detection."""

import concurrent.futures
import sys
from concurrent.futures import Executor, ProcessPoolExecutor
from typing import cast

SUPPORTS_SUBINTERPRETERS: bool = sys.version_info >= (3, 14)


def create_isolation_pool(pool_size: int, *, force_process: bool = False) -> Executor:
    """Create the best available isolation pool.

    On Python 3.14+ returns an ``InterpreterPoolExecutor`` (sub-interpreters
    share the process address-space but have isolated ``sys.modules`` and globals).
    On earlier versions, or when *force_process* is ``True``, falls back to
    ``ProcessPoolExecutor`` which provides process-level isolation.

    Parameters
    ----------
    pool_size:
        Maximum number of concurrent workers.
    force_process:
        Always use ``ProcessPoolExecutor`` even when sub-interpreters are
        available.  Useful when the target module contains C extensions
        that are not sub-interpreter safe.
    """
    if SUPPORTS_SUBINTERPRETERS and not force_process:
        cls = getattr(concurrent.futures, "InterpreterPoolExecutor")  # noqa: B009
        return cast(Executor, cls(max_workers=pool_size))
    return ProcessPoolExecutor(max_workers=pool_size)
