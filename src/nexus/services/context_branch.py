"""Backward-compat shim — canonical: ``nexus.system_services.workspace.context_branch``.

Issue #2132: Organized into domain subdirectory.
"""

import importlib
import warnings
from typing import Any

_MOVED = {
    "ContextBranchService": "nexus.system_services.workspace.context_branch",
    "BranchInfo": "nexus.system_services.workspace.context_branch",
    "MergeResult": "nexus.system_services.workspace.context_branch",
    "ExploreResult": "nexus.system_services.workspace.context_branch",
    "DEFAULT_BRANCH": "nexus.system_services.workspace.context_branch",
    "PROTECTED_BRANCHES": "nexus.system_services.workspace.context_branch",
    "_BASE_BACKOFF_MS": "nexus.system_services.workspace.context_branch",
    "_MAX_RETRIES": "nexus.system_services.workspace.context_branch",
    "_VALID_STRATEGIES": "nexus.system_services.workspace.context_branch",
    "_slugify": "nexus.system_services.workspace.context_branch",
    "_branch_from_model": "nexus.system_services.workspace.context_branch",
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
