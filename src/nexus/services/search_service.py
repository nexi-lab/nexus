"""Backward-compat shim — canonical: ``nexus.services.search.search_service``.

Issue #2132: Organized into domain subdirectory.
"""

import importlib
import warnings
from typing import Any

_MOVED = {
    "SearchService": "nexus.services.search.search_service",
    "DEFAULT_IGNORE_PATTERNS": "nexus.services.search.search_service",
    "LIST_PARALLEL_WORKERS": "nexus.services.search.search_service",
    "LIST_PARALLEL_MAX_DEPTH": "nexus.services.search.search_service",
    "_filter_ignored_paths": "nexus.services.search.search_service",
    "_should_ignore_path": "nexus.services.search.search_service",
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
