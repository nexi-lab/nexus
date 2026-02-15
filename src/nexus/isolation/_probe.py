"""Probe sub-interpreter compatibility for a backend module.

A sub-interpreter import can fail when the module (or one of its transitive
dependencies) contains C extensions that have not been updated for PEP 630
multi-phase initialisation.  This module tests the import *before* creating
the full pool so that we can auto-fallback to ``ProcessPoolExecutor``.
"""

from __future__ import annotations

import sys


def _try_import(module_path: str) -> bool:
    """Import *module_path* inside the current interpreter.

    Used as the worker function submitted to a throw-away pool.
    Returns ``True`` on success, raises on failure.
    """
    import importlib as _imp

    _imp.import_module(module_path)
    return True


def probe_subinterpreter_compat(module_path: str) -> bool:
    """Test whether *module_path* can be imported in a sub-interpreter.

    On Python < 3.14 (where sub-interpreters are not used), always returns
    ``True`` because the fallback ``ProcessPoolExecutor`` can import anything.

    On 3.14+ we spawn a single ``InterpreterPoolExecutor`` worker and attempt
    the import.  If it raises we return ``False`` — the caller should fall
    back to process isolation.

    Parameters
    ----------
    module_path:
        Dotted module name (e.g. ``"nexus.backends.gdrive_connector"``).

    Returns
    -------
    bool
        ``True`` if the module is sub-interpreter safe (or sub-interpreters
        are unavailable), ``False`` if the import fails.
    """
    if sys.version_info < (3, 14):
        return True

    try:
        from concurrent.futures import InterpreterPoolExecutor  # type: ignore[attr-defined]

        pool = InterpreterPoolExecutor(max_workers=1)
    except ImportError:
        return True

    try:
        future = pool.submit(_try_import, module_path)
        future.result(timeout=10)
        return True
    except Exception:
        return False
    finally:
        pool.shutdown(wait=False)
