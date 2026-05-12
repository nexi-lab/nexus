from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

_EXPLICIT_ZONE_ATTRS = ("zone", "zone_id", "target_zone_id")
_PATH_ATTRS = ("path", "src", "dst", "old_path", "new_path", "prefix")
_CONTAINER_ATTRS = ("files", "operations")


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
    seen: set[int] = set()
    for attr in _PATH_ATTRS:
        value = _read_attr(params, attr)
        yield from _paths_from_value(value, seen)
    for attr in _CONTAINER_ATTRS:
        value = _read_attr(params, attr)
        yield from _paths_from_value(value, seen)


def _paths_from_value(value: Any, seen: set[int]) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if value is None or isinstance(value, bytes | bytearray | memoryview):
        return
    if isinstance(value, dict):
        if _already_seen(value, seen):
            return
        for key in _PATH_ATTRS:
            yield from _paths_from_value(value.get(key), seen)
        for key in _CONTAINER_ATTRS:
            yield from _paths_from_value(value.get(key), seen)
        return
    if isinstance(value, list | tuple):
        if _already_seen(value, seen):
            return
        for item in value:
            yield from _paths_from_value(item, seen)
        return
    if _already_seen(value, seen):
        return
    for attr in _PATH_ATTRS:
        nested = getattr(value, attr, None)
        yield from _paths_from_value(nested, seen)
    for attr in _CONTAINER_ATTRS:
        nested = getattr(value, attr, None)
        yield from _paths_from_value(nested, seen)


def _already_seen(value: Any, seen: set[int]) -> bool:
    value_id = id(value)
    if value_id in seen:
        return True
    seen.add(value_id)
    return False
