"""API versioning decorators for Nexus SDK.

Provides @stable_api and @experimental decorators that mark public API
surfaces with stability metadata. These are zero-runtime-cost decorators
(no wrapping, no overhead) — they only attach introspectable attributes.

Usage:
    @stable_api(since="1.0")
    def my_stable_function():
        ...

    @experimental
    def my_experimental_function():
        ...

    # Inspect at CI time
    surface = collect_api_surface(my_module)
    export_api_surface(my_module, Path("api-surface.json"))
"""

import inspect
import json
import types
from pathlib import Path
from typing import Any, TypeVar

F = TypeVar("F")


def stable_api(since: str) -> Any:
    """Mark a function/class as stable public API.

    Zero runtime cost — sets metadata attributes only, no wrapping.

    Args:
        since: Version string when this API became stable (e.g. "1.0")

    Returns:
        The original object with __stable_api__ and __stable_since__ attributes

    Example:
        @stable_api(since="1.0")
        class MyProtocol(ABC):
            ...
    """
    if not since or not isinstance(since, str):
        raise ValueError("since must be a non-empty version string")

    def decorator(obj: F) -> F:
        obj.__stable_api__ = True  # type: ignore[attr-defined]
        obj.__stable_since__ = since  # type: ignore[attr-defined]
        return obj

    return decorator


def experimental(obj: F) -> F:
    """Mark a function/class as experimental API.

    Experimental APIs may change or be removed without notice.
    Zero runtime cost — sets metadata attribute only, no wrapping.

    Args:
        obj: The function or class to mark

    Returns:
        The original object with __experimental__ attribute

    Example:
        @experimental
        def new_feature():
            ...
    """
    obj.__experimental__ = True  # type: ignore[attr-defined]
    return obj


def is_stable_api(obj: Any) -> bool:
    """Check if an object is marked as stable API."""
    return getattr(obj, "__stable_api__", False) is True


def is_experimental(obj: Any) -> bool:
    """Check if an object is marked as experimental."""
    return getattr(obj, "__experimental__", False) is True


def get_stable_since(obj: Any) -> str | None:
    """Get the version when an API became stable."""
    return getattr(obj, "__stable_since__", None)


def collect_api_surface(module: types.ModuleType) -> list[dict[str, Any]]:
    """Collect all decorated API items from a module.

    Recursively inspects module members for @stable_api and @experimental
    markers. Returns a sorted list of API descriptors.

    Args:
        module: Python module to inspect

    Returns:
        List of API surface entries, each a dict with:
            - name: Fully qualified name
            - kind: "function", "class", or "method"
            - stability: "stable" or "experimental"
            - since: Version string (stable only)
    """
    surface: list[dict[str, Any]] = []
    module_name = module.__name__

    for attr_name in sorted(dir(module)):
        if attr_name.startswith("_"):
            continue

        obj = getattr(module, attr_name)
        qualified = f"{module_name}.{attr_name}"

        # Skip imported objects from other modules
        if hasattr(obj, "__module__") and obj.__module__ != module_name:
            continue

        entry = _inspect_object(qualified, obj)
        if entry:
            surface.append(entry)

        # Inspect class methods
        if inspect.isclass(obj) and obj.__module__ == module_name:
            for method_name in sorted(dir(obj)):
                if method_name.startswith("_"):
                    continue
                method = getattr(obj, method_name, None)
                if method is None:
                    continue
                method_entry = _inspect_object(
                    f"{qualified}.{method_name}", method
                )
                if method_entry:
                    method_entry["kind"] = "method"
                    surface.append(method_entry)

    return surface


def _inspect_object(name: str, obj: Any) -> dict[str, Any] | None:
    """Inspect a single object for API markers.

    Args:
        name: Fully qualified name
        obj: Object to inspect

    Returns:
        API entry dict or None if not decorated
    """
    if is_stable_api(obj):
        kind = "class" if inspect.isclass(obj) else "function"
        return {
            "name": name,
            "kind": kind,
            "stability": "stable",
            "since": get_stable_since(obj),
        }

    if is_experimental(obj):
        kind = "class" if inspect.isclass(obj) else "function"
        return {
            "name": name,
            "kind": kind,
            "stability": "experimental",
        }

    return None


def export_api_surface(module: types.ModuleType, path: Path) -> int:
    """Export the API surface of a module to a JSON file.

    Used by CI to detect breaking changes between versions.

    Args:
        module: Module to inspect
        path: Output file path (.json)

    Returns:
        Number of API entries exported
    """
    surface = collect_api_surface(module)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(
            {
                "module": module.__name__,
                "entries": surface,
                "count": len(surface),
            },
            f,
            indent=2,
        )
    return len(surface)


def load_api_surface(path: Path) -> list[dict[str, Any]]:
    """Load a previously exported API surface for comparison.

    Args:
        path: Path to the JSON surface file

    Returns:
        List of API entries

    Raises:
        FileNotFoundError: If the surface file doesn't exist
    """
    with open(path) as f:
        data = json.load(f)
    entries: list[dict[str, Any]] = data.get("entries", [])
    return entries


def diff_api_surfaces(
    old: list[dict[str, Any]], new: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Compare two API surfaces and detect breaking changes.

    A breaking change is when a stable API entry in the old surface
    is missing from the new surface.

    Args:
        old: Previous API surface entries
        new: Current API surface entries

    Returns:
        Dict with keys:
            - removed_stable: Stable APIs removed (BREAKING)
            - removed_experimental: Experimental APIs removed (OK)
            - added: New APIs added
    """
    old_by_name = {e["name"]: e for e in old}
    new_by_name = {e["name"]: e for e in new}

    removed_stable = [
        e
        for name, e in old_by_name.items()
        if name not in new_by_name and e.get("stability") == "stable"
    ]

    removed_experimental = [
        e
        for name, e in old_by_name.items()
        if name not in new_by_name and e.get("stability") == "experimental"
    ]

    added = [e for name, e in new_by_name.items() if name not in old_by_name]

    return {
        "removed_stable": removed_stable,
        "removed_experimental": removed_experimental,
        "added": added,
    }
