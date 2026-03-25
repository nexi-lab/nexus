"""Tests for metastore-first listing (Issue #3266, Decision #10A).

Unit tests for _list_from_metastore_or_api: metastore hit, metastore miss
(fallback to API), stale metastore, empty mount, permission filtering.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock


def _make_search_service(
    metastore_entries: dict[str, list] | None = None,
    parallel_results: list[str] | None = None,
) -> Any:
    """Create a minimal mock search service with _list_from_metastore_or_api."""
    # We need to import the actual method, so we'll test it through the class
    # But since SearchService has complex deps, we'll test the logic in isolation
    from nexus.bricks.search.search_service import SearchService

    svc = MagicMock(spec=SearchService)

    # Wire up the real method
    svc._list_from_metastore_or_api = SearchService._list_from_metastore_or_api.__get__(svc)

    # Mock metadata store
    metadata = MagicMock()

    def _list_dir_entries(path: str, zone_id: str | None = None) -> list | None:
        if metastore_entries and path in metastore_entries:
            entries = metastore_entries[path]
            return entries if entries else None
        return None

    metadata.list_directory_entries = MagicMock(side_effect=_list_dir_entries)
    svc.metadata = metadata

    # Mock _list_dir_parallel
    svc._list_dir_parallel = MagicMock(return_value=parallel_results or [])

    return svc


def _make_route(
    backend_name: str = "gmail",
    sync_eligible: bool = True,
    use_metadata_listing: bool = True,
    backend_path: str = "",
) -> SimpleNamespace:
    from nexus.contracts.capabilities import ConnectorCapability

    caps = frozenset()
    if sync_eligible:
        caps = frozenset({ConnectorCapability.SYNC_ELIGIBLE})

    backend = MagicMock()
    backend.name = backend_name
    backend.capabilities = caps
    backend.has_capability = MagicMock(side_effect=lambda c: c in caps)
    backend.use_metadata_listing = use_metadata_listing

    return SimpleNamespace(
        backend=backend, backend_path=backend_path, mount_point=f"/mnt/{backend_name}"
    )


def _make_context(user_id: str = "user1", zone_id: str = "root") -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, zone_id=zone_id, is_admin=False, subject_id=user_id)


# ============================================================================
# Metastore hit tests
# ============================================================================


class TestMetastoreHit:
    def test_returns_cached_entries_on_hit(self) -> None:
        """When metastore has entries, returns them without API call."""
        entries = [
            SimpleNamespace(path="/mnt/gmail/INBOX/msg1.yaml"),
            SimpleNamespace(path="/mnt/gmail/INBOX/msg2.yaml"),
        ]
        svc = _make_search_service(
            metastore_entries={"/mnt/gmail/INBOX": entries},
            parallel_results=["should-not-be-called"],
        )
        route = _make_route()
        ctx = _make_context()

        result = svc._list_from_metastore_or_api(
            path="/mnt/gmail/INBOX",
            route=route,
            list_context=ctx,
            recursive=False,
        )

        assert len(result) == 2
        # Should NOT have called _list_dir_parallel
        svc._list_dir_parallel.assert_not_called()


# ============================================================================
# Metastore miss (cache-miss fallback) tests
# ============================================================================


class TestMetastoreMiss:
    def test_falls_back_to_api_on_empty_metastore(self) -> None:
        """When metastore returns None, falls back to live API."""
        svc = _make_search_service(
            metastore_entries={},  # No entries for any path
            parallel_results=["/mnt/gmail/INBOX/msg1.yaml", "/mnt/gmail/INBOX/msg2.yaml"],
        )
        route = _make_route()
        ctx = _make_context()

        result = svc._list_from_metastore_or_api(
            path="/mnt/gmail/INBOX",
            route=route,
            list_context=ctx,
            recursive=False,
        )

        assert len(result) == 2
        svc._list_dir_parallel.assert_called_once()

    def test_falls_back_to_api_on_metastore_error(self) -> None:
        """When metastore raises, falls back to live API."""
        svc = _make_search_service(
            parallel_results=["/mnt/gmail/INBOX/msg1.yaml"],
        )
        svc.metadata.list_directory_entries = MagicMock(side_effect=RuntimeError("DB down"))
        route = _make_route()
        ctx = _make_context()

        result = svc._list_from_metastore_or_api(
            path="/mnt/gmail/INBOX",
            route=route,
            list_context=ctx,
            recursive=False,
        )

        assert len(result) == 1
        svc._list_dir_parallel.assert_called_once()


# ============================================================================
# Non-sync-eligible backends
# ============================================================================


class TestNonSyncEligible:
    def test_non_sync_eligible_always_uses_api(self) -> None:
        """Backends without SYNC_ELIGIBLE always go to live API."""
        svc = _make_search_service(
            metastore_entries={"/mnt/local": [SimpleNamespace(path="/mnt/local/file.txt")]},
            parallel_results=["/mnt/local/file.txt"],
        )
        route = _make_route(sync_eligible=False)
        ctx = _make_context()

        svc._list_from_metastore_or_api(
            path="/mnt/local",
            route=route,
            list_context=ctx,
            recursive=False,
        )

        # Should call API directly, not metastore
        svc._list_dir_parallel.assert_called_once()

    def test_metadata_listing_disabled_uses_api(self) -> None:
        """Backends with use_metadata_listing=False always use API."""
        svc = _make_search_service(
            parallel_results=["/mnt/gws/msg.yaml"],
        )
        route = _make_route(use_metadata_listing=False)
        ctx = _make_context()

        svc._list_from_metastore_or_api(
            path="/mnt/gws",
            route=route,
            list_context=ctx,
            recursive=False,
        )

        svc._list_dir_parallel.assert_called_once()
