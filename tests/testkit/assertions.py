"""Reusable assertions for Nexus tests."""

from __future__ import annotations

from typing import Any

from nexus.contracts.exceptions import MissingDependencyError

_PERMISSION_DENIED_MARKERS = (
    "permission denied",
    "access denied",
    "forbidden",
    "unauthorized",
)


def _read_field(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _read_first_field(value: Any, *keys: str) -> Any:
    for key in keys:
        actual = _read_field(value, key)
        if actual is not None:
            return actual
    return None


def _is_permission_denied_text(value: Any) -> bool:
    text = str(value).lower()
    return any(marker in text for marker in _PERMISSION_DENIED_MARKERS)


def _missing_entry_matches_dependency(entry: Any, dependency_name: str) -> bool:
    dep = entry
    reason = ""
    if isinstance(entry, tuple | list) and entry:
        dep = entry[0]
        if len(entry) > 1:
            reason = str(entry[1])

    candidates = []
    if isinstance(dep, str):
        candidates.append(dep)
    for attr in ("name", "module", "package", "install_target"):
        candidate = getattr(dep, attr, None)
        if candidate is not None:
            candidates.append(str(candidate))

    quoted_in_reason = (
        f"'{dependency_name}'" in reason
        or f'"{dependency_name}"' in reason
        or f"`{dependency_name}`" in reason
    )
    return dependency_name in candidates or quoted_in_reason


def assert_metadata_contains(metadata: Any, **expected: Any) -> None:
    """Assert selected metadata fields match expected values."""
    for key, expected_value in expected.items():
        actual = _read_field(metadata, key)
        assert actual == expected_value, (
            f"metadata.{key}: expected {expected_value!r}, got {actual!r}"
        )


def assert_permission_denied(value: Any) -> None:
    """Assert an exception or response-like value represents permission denial."""
    status = _read_first_field(value, "status_code", "status")
    if status in {401, 403}:
        return

    if isinstance(value, dict):
        detail = str(value.get("detail") or value.get("message") or "")
        assert _is_permission_denied_text(detail), (
            f"expected permission denied response, got {value!r}"
        )
        return

    assert _is_permission_denied_text(value), f"expected permission denied error, got {value!r}"


def assert_dependency_failure(value: Any, dependency_name: str) -> None:
    """Assert an error/response identifies a missing dependency."""
    if isinstance(value, MissingDependencyError):
        assert any(
            _missing_entry_matches_dependency(entry, dependency_name) for entry in value.missing
        ), f"dependency failure did not mention {dependency_name!r}: {value!r}"
        return

    text = str(value)
    assert "dependency" in text.lower() and dependency_name in text, (
        f"expected dependency failure for {dependency_name!r}, got {value!r}"
    )


def assert_event_matches(
    event: Any,
    *,
    path: str | None = None,
    event_type: str | None = None,
    zone_id: str | None = None,
) -> None:
    """Assert selected event fields match expected values."""
    expected = {
        "path": path,
        ("type", "event_type"): event_type,
        "zone_id": zone_id,
    }
    for keys, expected_value in expected.items():
        if expected_value is None:
            continue
        key_options = (keys,) if isinstance(keys, str) else keys
        actual = _read_first_field(event, *key_options)
        key = key_options[0]
        assert actual == expected_value, f"event.{key}: expected {expected_value!r}, got {actual!r}"


__all__ = [
    "assert_dependency_failure",
    "assert_event_matches",
    "assert_metadata_contains",
    "assert_permission_denied",
]
