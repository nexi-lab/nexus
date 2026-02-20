"""Backward-compat shim — canonical: ``nexus.system_services.lifecycle.sessions``.

Issue #2132: Organized into domain subdirectory.
"""

import importlib
import warnings
from typing import Any

_MOVED = {
    "create_session": "nexus.system_services.lifecycle.sessions",
    "get_session": "nexus.system_services.lifecycle.sessions",
    "update_session_activity": "nexus.system_services.lifecycle.sessions",
    "delete_session_resources": "nexus.system_services.lifecycle.sessions",
    "delete_session": "nexus.system_services.lifecycle.sessions",
    "cleanup_expired_sessions": "nexus.system_services.lifecycle.sessions",
    "list_user_sessions": "nexus.system_services.lifecycle.sessions",
    "cleanup_inactive_sessions": "nexus.system_services.lifecycle.sessions",
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
