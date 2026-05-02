"""Assertion helpers for Nexus tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nexus.contracts.exceptions import MissingDependencyError

__all__ = [
    "assert_event_payload",
    "assert_metadata_contains",
    "assert_missing_dependency_error",
    "assert_permission_decision",
]


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def assert_missing_dependency_error(
    error: MissingDependencyError,
    *,
    backend: str | None = None,
    count: int | None = None,
    missing_names: tuple[str, ...] = (),
    install_hints: tuple[str, ...] = (),
) -> None:
    """Assert stable details on a `MissingDependencyError`."""

    if backend is not None:
        assert error.backend == backend, f"expected backend {backend!r}, got {error.backend!r}"
    if count is not None:
        assert len(error.missing) == count, (
            f"expected {count} missing deps, got {len(error.missing)}"
        )

    reasons = [reason for _, reason in error.missing]
    rendered = "\n".join(reasons)
    names = {getattr(dep, "module", None) or getattr(dep, "name", None) for dep, _ in error.missing}

    for name in missing_names:
        assert name in names, f"expected missing dependency {name!r} in {names!r}"
    for hint in install_hints:
        assert hint in rendered, f"expected install hint {hint!r} in {rendered!r}"


def assert_event_payload(
    event: Any,
    *,
    event_type: str,
    path: str | None = None,
    zone_id: str | None = None,
) -> None:
    """Assert common event payload fields on dicts or objects."""

    actual_type = _value(event, "event_type", _value(event, "type"))
    assert actual_type == event_type, f"expected event type {event_type!r}, got {actual_type!r}"
    if path is not None:
        assert _value(event, "path") == path
    if zone_id is not None:
        assert _value(event, "zone_id") == zone_id


def assert_metadata_contains(metadata: Any, expected: Mapping[str, Any]) -> None:
    """Assert that metadata contains an expected subset."""

    for key, value in expected.items():
        assert _value(metadata, key) == value, (
            f"expected metadata {key!r} to be {value!r}, got {_value(metadata, key)!r}"
        )


def assert_permission_decision(decision: Any, *, allowed: bool) -> None:
    """Assert a permission decision bool or object has the expected state."""

    actual = decision if isinstance(decision, bool) else _value(decision, "allowed")
    assert actual is allowed, f"permission decision expected allowed={allowed!r}, got {actual!r}"
