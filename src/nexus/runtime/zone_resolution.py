from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

_EXPLICIT_ZONE_ATTRS = ("zone", "zone_id", "target_zone_id")
_PATH_ATTRS = ("path", "src", "dst", "old_path", "new_path", "prefix")


def zone_from_path(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    if not value.startswith("/zone/"):
        return None
    remainder = value[len("/zone/") :]
    zone = remainder.split("/", 1)[0]
    if not zone or zone == ROOT_ZONE_ID:
        return None
    return zone


def zone_from_params(params: Any) -> str | None:
    if params is None:
        return None
    for attr in _EXPLICIT_ZONE_ATTRS:
        zone = _read_attr(params, attr)
        if isinstance(zone, str) and zone and zone != ROOT_ZONE_ID:
            return zone
    for value in _iter_path_values(params):
        zone = zone_from_path(value)
        if zone is not None:
            return zone
    return None


def target_zone_for_context(context: Any, params: Any | None) -> str | None:
    zone = zone_from_params(params)
    if zone is not None:
        return zone
    context_zone = getattr(context, "zone_id", None)
    if isinstance(context_zone, str) and context_zone and context_zone != ROOT_ZONE_ID:
        return context_zone
    return None


def _read_attr(value: Any, attr: str) -> Any:
    if isinstance(value, dict):
        return value.get(attr)
    return getattr(value, attr, None)


def _iter_path_values(params: Any) -> Iterable[str]:
    for attr in _PATH_ATTRS:
        value = _read_attr(params, attr)
        yield from _paths_from_value(value)
    files = _read_attr(params, "files")
    yield from _paths_from_value(files)
    operations = _read_attr(params, "operations")
    yield from _paths_from_value(operations)


def _paths_from_value(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for key in _PATH_ATTRS:
            nested = value.get(key)
            if isinstance(nested, str):
                yield nested
        return
    if isinstance(value, tuple) and value and isinstance(value[0], str):
        yield value[0]
        return
    if isinstance(value, list | tuple):
        for item in value:
            yield from _paths_from_value(item)
        return
    for attr in _PATH_ATTRS:
        nested = getattr(value, attr, None)
        if isinstance(nested, str):
            yield nested
