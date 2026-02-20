"""Backward-compat shim — canonical: ``nexus.system_services.workspace.overlay_resolver``.

Issue #2132: Organized into domain subdirectory.
"""

import importlib
import warnings
from typing import Any

_MOVED = {
    "OverlayResolver": "nexus.system_services.workspace.overlay_resolver",
    "OverlayStats": "nexus.system_services.workspace.overlay_resolver",
    "WHITEOUT_HASH": "nexus.system_services.workspace.overlay_resolver",
}


def __getattr__(name: str) -> Any:
    if name in _MOVED:
        warnings.warn(
            f"Importing {name} from {__name__} is deprecated. "
            f"Use 'from {_MOVED[name]} import {name}' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        mod = importlib.import_module(_MOVED[name])
        attr = getattr(mod, name)
        globals()[name] = attr  # Cache: warn once per process
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(_MOVED) + list(globals())
