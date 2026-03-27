"""Tests for metastore-first listing (Issue #3266, Decision #10A).

Unit tests for _list_from_metastore_or_api: directory_entries hit, miss
(fallback to API), error handling, and non-eligible backends.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from nexus.bricks.search.search_service import SearchService


def _make_search_service(
    directory_entries: dict[str, list[tuple[str, str]]] | None = None,
    parallel_results: list[str] | None = None,
) -> Any:
    """Create a mock search service with _list_from_metastore_or_api and _query_directory_entries."""
    svc = MagicMock(spec=SearchService)

    # Wire up the real methods
    svc._list_from_metastore_or_api = SearchService._list_from_metastore_or_api.__get__(svc)
    svc._query_directory_entries = SearchService._query_directory_entries.__get__(svc)

    # Mock _list_dir_parallel
    svc._list_dir_parallel = MagicMock(return_value=parallel_results or [])

    # Mock _query_directory_entries to return from the dict instead of hitting DB
    entries = directory_entries or {}

    def _mock_query(path: str) -> list[str] | None:
        parent = path.rstrip("/")
        if parent in entries:
            rows = entries[parent]
            if not rows:
                return None
            results = []
            for name, entry_type in rows:
                full_path = f"{parent}/{name}"
                if entry_type == "dir":
                    full_path += "/"
                results.append(full_path)
            return results
        return None

    svc._query_directory_entries = MagicMock(side_effect=_mock_query)

    return svc


def _make_route(
    backend_name: str = "gmail",
    sync_eligible: bool = True,
    use_metadata_listing: bool = True,
    backend_path: str = "",
) -> SimpleNamespace:
    from nexus.contracts.backend_features import BackendFeature

    caps = frozenset()
    if sync_eligible:
        caps = frozenset({BackendFeature.SYNC_ELIGIBLE})

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
        """When directory_entries has data, returns them without API call."""
        svc = _make_search_service(
            directory_entries={
                "/mnt/gmail/INBOX": [
                    ("msg1.yaml", "file"),
                    ("msg2.yaml", "file"),
                ],
            },
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
        assert "/mnt/gmail/INBOX/msg1.yaml" in result
        assert "/mnt/gmail/INBOX/msg2.yaml" in result
        svc._list_dir_parallel.assert_not_called()

    def test_returns_directories_with_trailing_slash(self) -> None:
        """Directory entries get trailing slash."""
        svc = _make_search_service(
            directory_entries={
                "/mnt/gmail": [
                    ("INBOX", "dir"),
                    ("SENT", "dir"),
                ],
            },
        )
        route = _make_route()
        ctx = _make_context()

        result = svc._list_from_metastore_or_api(
            path="/mnt/gmail",
            route=route,
            list_context=ctx,
            recursive=False,
        )

        assert "/mnt/gmail/INBOX/" in result
        assert "/mnt/gmail/SENT/" in result


# ============================================================================
# Metastore miss (cache-miss fallback) tests
# ============================================================================


class TestMetastoreMiss:
    def test_falls_back_to_api_on_empty_metastore(self) -> None:
        """When directory_entries has no data, falls back to live API."""
        svc = _make_search_service(
            directory_entries={},
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

    def test_falls_back_to_api_on_query_error(self) -> None:
        """When DB query fails, falls back to live API."""
        svc = _make_search_service(
            parallel_results=["/mnt/gmail/INBOX/msg1.yaml"],
        )
        svc._query_directory_entries = MagicMock(side_effect=RuntimeError("DB down"))
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
            directory_entries={
                "/mnt/local": [("file.txt", "file")],
            },
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
