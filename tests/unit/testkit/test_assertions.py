from __future__ import annotations

from types import SimpleNamespace

import pytest
from testkit.assertions import (
    assert_event_payload,
    assert_metadata_contains,
    assert_missing_dependency_error,
    assert_permission_decision,
)

from nexus.backends.base.runtime_deps import BinaryDep, PythonDep
from nexus.contracts.exceptions import MissingDependencyError


def _missing_error() -> MissingDependencyError:
    return MissingDependencyError(
        backend="stub_backend",
        missing=[
            (
                PythonDep("missing_module", extras=("gcs",)),
                "python 'missing_module': install with: pip install nexus-fs[gcs]",
            ),
            (
                BinaryDep("missing_bin", "brew install missing-bin"),
                "binary 'missing_bin': not on PATH - install with: brew install missing-bin",
            ),
        ],
    )


def test_assert_missing_dependency_error_accepts_expected_details() -> None:
    assert_missing_dependency_error(
        _missing_error(),
        backend="stub_backend",
        count=2,
        missing_names=("missing_module", "missing_bin"),
        install_hints=("pip install nexus-fs[gcs]", "brew install missing-bin"),
    )


def test_assert_missing_dependency_error_rejects_wrong_backend() -> None:
    with pytest.raises(AssertionError, match="expected backend"):
        assert_missing_dependency_error(_missing_error(), backend="other")


def test_assert_event_payload_supports_dicts() -> None:
    assert_event_payload(
        {"event_type": "file_write", "path": "/docs/a.txt", "zone_id": "zone-a"},
        event_type="file_write",
        path="/docs/a.txt",
        zone_id="zone-a",
    )


def test_assert_event_payload_supports_objects() -> None:
    event = SimpleNamespace(type="file_delete", path="/docs/b.txt", zone_id="zone-b")

    assert_event_payload(
        event,
        event_type="file_delete",
        path="/docs/b.txt",
        zone_id="zone-b",
    )


def test_assert_metadata_contains_checks_subset() -> None:
    assert_metadata_contains(
        {"path": "/docs/a.txt", "content_type": "text/plain", "size": 12},
        {"path": "/docs/a.txt", "size": 12},
    )


def test_assert_metadata_contains_rejects_missing_none_value_from_mapping() -> None:
    with pytest.raises(AssertionError, match="missing"):
        assert_metadata_contains({"path": "/docs/a.txt"}, {"nullable": None})


def test_assert_metadata_contains_rejects_missing_none_value_from_object() -> None:
    with pytest.raises(AssertionError, match="missing"):
        assert_metadata_contains(SimpleNamespace(path="/docs/a.txt"), {"nullable": None})


def test_assert_permission_decision_supports_bool_and_objects() -> None:
    assert_permission_decision(True, allowed=True)
    assert_permission_decision(SimpleNamespace(allowed=False), allowed=False)


def test_assert_permission_decision_rejects_wrong_state() -> None:
    with pytest.raises(AssertionError, match="permission decision"):
        assert_permission_decision(False, allowed=True)
