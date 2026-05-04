"""Tests for reusable testkit assertions."""

from __future__ import annotations

import pytest

from nexus.contracts.exceptions import (
    AccessDeniedError,
    AuthenticationError,
    BackendError,
    MissingDependencyError,
)
from nexus.services.event_bus.types import FileEvent, FileEventType


def test_assert_metadata_contains_accepts_dict() -> None:
    from tests.testkit.assertions import assert_metadata_contains

    assert_metadata_contains({"path": "/a.txt", "size": 3}, path="/a.txt", size=3)


def test_assert_metadata_contains_accepts_object() -> None:
    from tests.testkit.assertions import assert_metadata_contains

    class Metadata:
        path = "/a.txt"
        size = 3

    assert_metadata_contains(Metadata(), path="/a.txt", size=3)


def test_assert_metadata_contains_reports_mismatch() -> None:
    from tests.testkit.assertions import assert_metadata_contains

    with pytest.raises(AssertionError, match="metadata.size"):
        assert_metadata_contains({"size": 3}, size=4)


def test_assert_permission_denied_accepts_response_shape() -> None:
    from tests.testkit.assertions import assert_permission_denied

    assert_permission_denied({"status_code": 403, "detail": "permission denied"})


def test_assert_permission_denied_accepts_authentication_error_status_code() -> None:
    from tests.testkit.assertions import assert_permission_denied

    assert_permission_denied(AuthenticationError("Token expired"))


def test_assert_permission_denied_accepts_access_denied_error_status_code() -> None:
    from tests.testkit.assertions import assert_permission_denied

    assert_permission_denied(AccessDeniedError("Admin privileges required"))


def test_assert_permission_denied_rejects_loose_permission_text() -> None:
    from tests.testkit.assertions import assert_permission_denied

    with pytest.raises(AssertionError, match="permission denied"):
        assert_permission_denied("permission granted")


def test_assert_dependency_failure_accepts_missing_dependency_error() -> None:
    from tests.testkit.assertions import assert_dependency_failure

    err = MissingDependencyError(
        backend="gcs",
        missing=[
            (
                "google-cloud-storage",
                "python 'google-cloud-storage': install with: pip install google-cloud-storage",
            )
        ],
    )

    assert_dependency_failure(err, "google-cloud-storage")


def test_assert_dependency_failure_rejects_backend_name_without_missing_dependency() -> None:
    from tests.testkit.assertions import assert_dependency_failure

    err = MissingDependencyError(
        backend="gcs",
        missing=[
            (
                "google-cloud-storage",
                "python 'google-cloud-storage': install with: pip install google-cloud-storage",
            )
        ],
    )

    with pytest.raises(AssertionError, match="dependency failure"):
        assert_dependency_failure(err, "gcs")


def test_assert_event_matches_checks_selected_fields() -> None:
    from tests.testkit.assertions import assert_event_matches

    assert_event_matches(
        {"path": "/x.txt", "event_type": "file_write", "zone_id": "root"},
        path="/x.txt",
        event_type="file_write",
        zone_id="root",
    )


def test_assert_event_matches_checks_canonical_file_event_type() -> None:
    from tests.testkit.assertions import assert_event_matches

    event = FileEvent(type=FileEventType.FILE_WRITE, path="/x.txt", zone_id="root")

    assert_event_matches(
        event,
        path="/x.txt",
        event_type="file_write",
        zone_id="root",
    )


def test_assert_event_matches_checks_canonical_file_event_dict_type() -> None:
    from tests.testkit.assertions import assert_event_matches

    event = FileEvent(type=FileEventType.FILE_WRITE, path="/x.txt", zone_id="root")

    assert_event_matches(
        event.to_dict(),
        path="/x.txt",
        event_type="file_write",
        zone_id="root",
    )


def test_assert_event_matches_reports_selected_field() -> None:
    from tests.testkit.assertions import assert_event_matches

    with pytest.raises(AssertionError, match="event.path"):
        assert_event_matches({"path": "/x.txt"}, path="/y.txt")


def test_assert_dependency_failure_rejects_wrong_error() -> None:
    from tests.testkit.assertions import assert_dependency_failure

    with pytest.raises(AssertionError, match="dependency failure"):
        assert_dependency_failure(BackendError("different", backend="local"), "gcs")


def test_assertion_helpers_exported_from_testkit_package() -> None:
    from tests.testkit import (
        assert_dependency_failure,
        assert_event_matches,
        assert_metadata_contains,
        assert_permission_denied,
    )

    assert callable(assert_dependency_failure)
    assert callable(assert_event_matches)
    assert callable(assert_metadata_contains)
    assert callable(assert_permission_denied)
