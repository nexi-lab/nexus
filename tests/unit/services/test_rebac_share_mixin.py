"""Unit tests for ReBACShareMixin.

Tests the sharing, privacy, consent, and visibility methods extracted
from ReBACService into the share mixin.

Issue #2132: Previously 0% test coverage.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")


from nexus.bricks.rebac.share_mixin import ReBACShareMixin


def _pandas_available() -> bool:
    """Check whether pandas can be imported without error."""
    try:
        import pandas  # noqa: F401

        return True
    except (ImportError, ValueError):
        return False


# =============================================================================
# Test harness: concrete class that uses the mixin
# =============================================================================


class _StubReBACService(ReBACShareMixin):
    """Minimal host class providing the attributes ReBACShareMixin expects."""

    def __init__(
        self,
        *,
        manager: Any = None,
        expand_result: list | None = None,
        create_result: dict | None = None,
        delete_result: bool = True,
        list_tuples_result: list | None = None,
    ):
        self._rebac_manager = manager
        self._expand_result = expand_result or []
        self._create_result = create_result or {"tuple_id": "t1", "revision": "r1"}
        self._delete_result = delete_result
        self._list_tuples_result = list_tuples_result or []

    def _require_manager(self) -> Any:
        if self._rebac_manager is None:
            raise RuntimeError("ReBACManager not configured")
        return self._rebac_manager

    def _check_share_permission(self, resource: Any, context: Any = None) -> None:
        """No-op for most tests; overridden in permission-check tests."""

    def rebac_expand_sync(self, permission: str, object: tuple[str, str]) -> list[tuple[str, str]]:
        return list(self._expand_result)

    def rebac_create_sync(self, **kwargs: Any) -> dict[str, Any]:
        self._last_create_kwargs = kwargs
        return dict(self._create_result)

    def rebac_delete_sync(self, tuple_id: str) -> bool:
        self._last_delete_id = tuple_id
        return self._delete_result

    def rebac_list_tuples_sync(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._last_list_kwargs = kwargs
        return list(self._list_tuples_result)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_manager():
    """Create a mock ReBACManager."""
    mgr = MagicMock()
    write_result = SimpleNamespace(
        tuple_id="tid-001",
        revision="rev-001",
        consistency_token="ct-001",
    )
    mgr.rebac_write.return_value = write_result
    mgr.rebac_check.return_value = True
    return mgr


@pytest.fixture
def svc(mock_manager):
    """Create a _StubReBACService with a mock manager."""
    return _StubReBACService(manager=mock_manager)


@pytest.fixture
def svc_no_manager():
    """Create a _StubReBACService without a manager."""
    return _StubReBACService(manager=None)


# =============================================================================
# Privacy & Consent tests
# =============================================================================


class TestPrivacyAndConsent:
    """Tests for privacy and consent methods."""

    def test_expand_with_privacy_no_filtering(self, svc):
        """When respect_consent=False, all subjects are returned."""
        svc._expand_result = [("user", "alice"), ("user", "bob")]

        result = svc.rebac_expand_with_privacy_sync(
            permission="read",
            object=("file", "/doc.txt"),
            respect_consent=False,
        )
        assert result == [("user", "alice"), ("user", "bob")]

    def test_expand_with_privacy_filters_by_consent(self, svc, mock_manager):
        """When respect_consent=True, only discoverable subjects are returned."""
        svc._expand_result = [("user", "alice"), ("user", "bob")]
        # Only alice is discoverable
        mock_manager.rebac_check.side_effect = lambda subject, permission, object: (
            object == ("user", "alice")
        )

        result = svc.rebac_expand_with_privacy_sync(
            permission="read",
            object=("file", "/doc.txt"),
            respect_consent=True,
            requester=("user", "carol"),
        )
        assert result == [("user", "alice")]

    def test_expand_with_privacy_no_requester_skips_filtering(self, svc):
        """When requester is None, filtering is skipped even if respect_consent=True."""
        svc._expand_result = [("user", "alice")]
        result = svc.rebac_expand_with_privacy_sync(
            permission="read",
            object=("file", "/doc.txt"),
            respect_consent=True,
            requester=None,
        )
        assert result == [("user", "alice")]

    def test_grant_consent_creates_tuple(self, svc):
        """grant_consent_sync should create a consent_granted relation."""
        result = svc.grant_consent_sync(
            from_subject=("user", "alice"),
            to_subject=("user", "bob"),
        )
        assert "tuple_id" in result
        assert svc._last_create_kwargs["subject"] == ("user", "bob")
        assert svc._last_create_kwargs["relation"] == "consent_granted"
        assert svc._last_create_kwargs["object"] == ("user", "alice")

    def test_grant_consent_with_expiry(self, svc):
        """grant_consent_sync should pass expires_at through."""
        expiry = datetime(2025, 12, 31, tzinfo=UTC)
        svc.grant_consent_sync(
            from_subject=("user", "alice"),
            to_subject=("user", "bob"),
            expires_at=expiry,
        )
        assert svc._last_create_kwargs["expires_at"] == expiry

    def test_revoke_consent_deletes_tuple(self, svc):
        """revoke_consent_sync should delete the consent tuple."""
        svc._list_tuples_result = [{"tuple_id": "consent-tuple-1"}]
        result = svc.revoke_consent_sync(
            from_subject=("user", "alice"),
            to_subject=("user", "bob"),
        )
        assert result is True
        assert svc._last_delete_id == "consent-tuple-1"

    def test_revoke_consent_returns_false_when_no_tuple(self, svc):
        """revoke_consent_sync should return False if no consent tuple exists."""
        svc._list_tuples_result = []
        result = svc.revoke_consent_sync(
            from_subject=("user", "alice"),
            to_subject=("user", "bob"),
        )
        assert result is False


# =============================================================================
# Public / Private tests
# =============================================================================


class TestMakePublicPrivate:
    """Tests for make_public_sync() and make_private_sync()."""

    def test_make_public_creates_wildcard_tuple(self, svc):
        """make_public_sync should create a public_discoverable tuple with wildcard subject."""
        result = svc.make_public_sync(resource=("file", "/public-doc.txt"))
        assert "tuple_id" in result
        assert svc._last_create_kwargs["subject"] == ("*", "*")
        assert svc._last_create_kwargs["relation"] == "public_discoverable"
        assert svc._last_create_kwargs["object"] == ("file", "/public-doc.txt")

    def test_make_public_with_zone(self, svc):
        """make_public_sync should pass zone_id through."""
        svc.make_public_sync(resource=("file", "/doc.txt"), zone_id="zone-42")
        assert svc._last_create_kwargs["zone_id"] == "zone-42"

    def test_make_private_deletes_public_tuple(self, svc):
        """make_private_sync should delete the public_discoverable tuple."""
        svc._list_tuples_result = [{"tuple_id": "public-tuple-1"}]
        result = svc.make_private_sync(resource=("file", "/doc.txt"))
        assert result is True
        assert svc._last_delete_id == "public-tuple-1"

    def test_make_private_returns_false_when_not_public(self, svc):
        """make_private_sync should return False if resource is not public."""
        svc._list_tuples_result = []
        result = svc.make_private_sync(resource=("file", "/doc.txt"))
        assert result is False


# =============================================================================
# share_with_user_sync() tests
# =============================================================================


class TestShareWithUser:
    """Tests for share_with_user_sync()."""

    def test_share_with_user_viewer(self, svc, mock_manager):
        """Sharing with relation='viewer' should write a 'shared-viewer' tuple."""
        result = svc.share_with_user_sync(
            resource=("file", "/shared.txt"),
            user_id="alice",
            relation="viewer",
        )
        assert result["tuple_id"] == "tid-001"
        assert result["revision"] == "rev-001"
        assert result["consistency_token"] == "ct-001"

        mock_manager.rebac_write.assert_called_once()
        call_kwargs = mock_manager.rebac_write.call_args
        assert call_kwargs.kwargs["subject"] == ("user", "alice")
        assert call_kwargs.kwargs["relation"] == "shared-viewer"
        assert call_kwargs.kwargs["object"] == ("file", "/shared.txt")

    def test_share_with_user_editor(self, svc, mock_manager):
        """Sharing with relation='editor' should write a 'shared-editor' tuple."""
        svc.share_with_user_sync(
            resource=("file", "/shared.txt"),
            user_id="bob",
            relation="editor",
        )
        call_kwargs = mock_manager.rebac_write.call_args
        assert call_kwargs.kwargs["relation"] == "shared-editor"

    def test_share_with_user_owner(self, svc, mock_manager):
        """Sharing with relation='owner' should write a 'shared-owner' tuple."""
        svc.share_with_user_sync(
            resource=("file", "/shared.txt"),
            user_id="carol",
            relation="owner",
        )
        call_kwargs = mock_manager.rebac_write.call_args
        assert call_kwargs.kwargs["relation"] == "shared-owner"

    def test_share_with_user_invalid_relation(self, svc):
        """An invalid relation should raise ValueError."""
        with pytest.raises(ValueError, match="relation must be"):
            svc.share_with_user_sync(
                resource=("file", "/shared.txt"),
                user_id="alice",
                relation="admin",
            )

    def test_share_with_user_passes_zone_ids(self, svc, mock_manager):
        """Zone IDs should be passed through to rebac_write."""
        svc.share_with_user_sync(
            resource=("file", "/shared.txt"),
            user_id="alice",
            zone_id="zone-a",
            user_zone_id="zone-b",
        )
        call_kwargs = mock_manager.rebac_write.call_args
        assert call_kwargs.kwargs["zone_id"] == "zone-a"
        assert call_kwargs.kwargs["subject_zone_id"] == "zone-b"

    def test_share_with_user_string_expiry(self, svc, mock_manager):
        """A string expires_at should be parsed to datetime."""
        svc.share_with_user_sync(
            resource=("file", "/shared.txt"),
            user_id="alice",
            expires_at="2025-12-31T00:00:00Z",
        )
        call_kwargs = mock_manager.rebac_write.call_args
        expires_dt = call_kwargs.kwargs["expires_at"]
        assert isinstance(expires_dt, datetime)
        assert expires_dt.year == 2025

    def test_share_with_user_datetime_expiry(self, svc, mock_manager):
        """A datetime expires_at should be passed through directly."""
        expiry = datetime(2025, 6, 15, tzinfo=UTC)
        svc.share_with_user_sync(
            resource=("file", "/shared.txt"),
            user_id="alice",
            expires_at=expiry,
        )
        call_kwargs = mock_manager.rebac_write.call_args
        assert call_kwargs.kwargs["expires_at"] == expiry

    def test_share_with_user_requires_manager(self, svc_no_manager):
        """share_with_user_sync should raise if no manager is configured."""
        with pytest.raises(RuntimeError, match="not configured"):
            svc_no_manager.share_with_user_sync(
                resource=("file", "/x.txt"),
                user_id="alice",
            )


# =============================================================================
# share_with_group_sync() tests
# =============================================================================


class TestShareWithGroup:
    """Tests for share_with_group_sync()."""

    def test_share_with_group_viewer(self, svc, mock_manager):
        """Sharing with a group should use ('group', group_id, 'member') subject."""
        result = svc.share_with_group_sync(
            resource=("file", "/shared.txt"),
            group_id="engineering",
            relation="viewer",
        )
        assert result["tuple_id"] == "tid-001"

        call_kwargs = mock_manager.rebac_write.call_args
        assert call_kwargs.kwargs["subject"] == ("group", "engineering", "member")
        assert call_kwargs.kwargs["relation"] == "shared-viewer"

    def test_share_with_group_invalid_relation(self, svc):
        """An invalid relation should raise ValueError."""
        with pytest.raises(ValueError, match="relation must be"):
            svc.share_with_group_sync(
                resource=("file", "/shared.txt"),
                group_id="engineering",
                relation="superadmin",
            )

    def test_share_with_group_passes_zone_ids(self, svc, mock_manager):
        """Zone and group zone IDs should be passed through."""
        svc.share_with_group_sync(
            resource=("file", "/shared.txt"),
            group_id="eng",
            zone_id="z1",
            group_zone_id="z2",
        )
        call_kwargs = mock_manager.rebac_write.call_args
        assert call_kwargs.kwargs["zone_id"] == "z1"
        assert call_kwargs.kwargs["subject_zone_id"] == "z2"


# =============================================================================
# revoke_share_sync() tests
# =============================================================================


class TestRevokeShare:
    """Tests for revoke_share_sync() and revoke_share_by_id_sync()."""

    def test_revoke_share_deletes_matching_tuple(self, svc):
        """revoke_share_sync should find and delete the share tuple."""
        svc._list_tuples_result = [{"tuple_id": "share-tuple-42"}]
        result = svc.revoke_share_sync(
            resource=("file", "/shared.txt"),
            user_id="alice",
        )
        assert result is True
        assert svc._last_delete_id == "share-tuple-42"

        # Verify it searched for the right tuples
        assert svc._last_list_kwargs["subject"] == ("user", "alice")
        assert set(svc._last_list_kwargs["relation_in"]) == {
            "shared-viewer",
            "shared-editor",
            "shared-owner",
        }

    def test_revoke_share_returns_false_when_no_tuple(self, svc):
        """revoke_share_sync should return False if no share exists."""
        svc._list_tuples_result = []
        result = svc.revoke_share_sync(
            resource=("file", "/shared.txt"),
            user_id="alice",
        )
        assert result is False

    def test_revoke_share_by_id(self, svc):
        """revoke_share_by_id_sync should directly delete by tuple ID."""
        result = svc.revoke_share_by_id_sync("share-tuple-99")
        assert result is True
        assert svc._last_delete_id == "share-tuple-99"


# =============================================================================
# list_outgoing_shares_sync() tests
# =============================================================================


class TestListOutgoingShares:
    """Tests for list_outgoing_shares_sync()."""

    def test_returns_transformed_shares(self, svc, mock_manager):
        """list_outgoing_shares_sync should transform tuples into share dicts."""
        svc._list_tuples_result = [
            {
                "tuple_id": "t1",
                "object_type": "file",
                "object_id": "/doc.txt",
                "subject_id": "alice",
                "relation": "shared-viewer",
                "created_at": "2025-01-01",
                "expires_at": None,
            },
        ]
        mock_manager._iterator_cache = MagicMock()
        mock_manager._iterator_cache.get_or_create.return_value = (
            "cursor-1",
            [
                {
                    "share_id": "t1",
                    "resource_type": "file",
                    "resource_id": "/doc.txt",
                    "recipient_id": "alice",
                    "permission_level": "viewer",
                    "created_at": "2025-01-01",
                    "expires_at": None,
                }
            ],
            1,
        )

        result = svc.list_outgoing_shares_sync(
            resource=("file", "/doc.txt"),
        )
        assert "items" in result
        assert "total_count" in result
        assert result["total_count"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["share_id"] == "t1"
        assert result["items"][0]["permission_level"] == "viewer"


# =============================================================================
# list_incoming_shares_sync() tests
# =============================================================================


class TestListIncomingShares:
    """Tests for list_incoming_shares_sync()."""

    def test_returns_transformed_shares(self, svc, mock_manager):
        """list_incoming_shares_sync should transform tuples into share dicts."""
        mock_manager._iterator_cache = MagicMock()
        mock_manager._iterator_cache.get_or_create.return_value = (
            "cursor-2",
            [
                {
                    "share_id": "t2",
                    "resource_type": "file",
                    "resource_id": "/doc.txt",
                    "owner_zone_id": "zone-1",
                    "permission_level": "editor",
                    "created_at": "2025-01-01",
                    "expires_at": None,
                }
            ],
            1,
        )

        result = svc.list_incoming_shares_sync(user_id="alice")
        assert "items" in result
        assert result["total_count"] == 1
        assert result["items"][0]["permission_level"] == "editor"


# =============================================================================
# Dynamic Viewer tests
# =============================================================================


class TestApplyDynamicViewerFilter:
    """Tests for apply_dynamic_viewer_filter_sync().

    These tests require pandas; skipped if pandas is not importable
    (e.g. numpy binary incompatibility on some Python versions).
    """

    def test_unsupported_format_raises(self, svc):
        """Only 'csv' format is supported (checked before pandas import)."""
        with pytest.raises(ValueError, match="Unsupported file format"):
            svc.apply_dynamic_viewer_filter_sync(
                data="col1,col2\n1,2",
                column_config={},
                file_format="json",
            )

    @pytest.mark.skipif(not _pandas_available(), reason="pandas not importable in this environment")
    def test_hidden_columns_removed(self, svc):
        """Hidden columns should not appear in output."""
        csv_data = "name,salary,department\nalice,100000,eng\nbob,90000,sales"
        result = svc.apply_dynamic_viewer_filter_sync(
            data=csv_data,
            column_config={"hidden_columns": ["salary"]},
        )
        assert "salary" not in result["columns_shown"]
        assert "name" in result["columns_shown"]
        assert "department" in result["columns_shown"]

    @pytest.mark.skipif(not _pandas_available(), reason="pandas not importable in this environment")
    def test_aggregation_columns(self, svc):
        """Aggregated columns should appear in aggregation results."""
        csv_data = "name,salary\nalice,100000\nbob,90000"
        result = svc.apply_dynamic_viewer_filter_sync(
            data=csv_data,
            column_config={
                "aggregations": {"salary": "mean"},
            },
        )
        assert "salary" in result["aggregations"]
        assert result["aggregations"]["salary"]["mean"] == 95000.0
        assert "mean(salary)" in result["aggregated_columns"]

    @pytest.mark.skipif(not _pandas_available(), reason="pandas not importable in this environment")
    def test_visible_columns_explicit(self, svc):
        """Explicit visible_columns should control output."""
        csv_data = "a,b,c\n1,2,3"
        result = svc.apply_dynamic_viewer_filter_sync(
            data=csv_data,
            column_config={"visible_columns": ["a", "c"]},
        )
        assert result["columns_shown"] == ["a", "c"]
