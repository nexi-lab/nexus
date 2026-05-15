"""Tests for SearchService glob prefix decoupling (Issue #1572).

Verifies that _should_prepend_recursive_wildcard() and _get_namespace_prefixes()
correctly determine when to add **/ to glob patterns, using dynamic mount point
data from the router instead of hardcoded prefixes.
"""

from unittest.mock import Mock

import pytest

from nexus.bricks.search.search_service import SearchService


def _make_service(router: object | None = None) -> SearchService:
    """Create a minimal SearchService with a mock metadata store."""
    metadata = Mock()
    return SearchService(
        metadata_store=metadata,
        router=router,
        enforce_permissions=False,
    )


# ---------------------------------------------------------------------------
# _get_namespace_prefixes
# ---------------------------------------------------------------------------


class TestGetNamespacePrefixes:
    """Tests for _get_namespace_prefixes()."""

    def test_no_router_returns_fallback(self):
        svc = _make_service(router=None)
        prefixes = svc._get_namespace_prefixes()
        assert prefixes == ("workspace/", "shared/", "external/", "system/", "archives/")

    def test_router_with_default_mounts(self):
        router = Mock()
        router.get_mount_points.return_value = ["/external", "/shared", "/workspace"]
        svc = _make_service(router=router)

        prefixes = svc._get_namespace_prefixes()
        assert set(prefixes) == {"workspace/", "shared/", "external/"}

    def test_router_with_custom_mounts(self):
        router = Mock()
        router.get_mount_points.return_value = [
            "/external",
            "/federation",
            "/shared",
            "/tenant-a",
            "/workspace",
        ]
        svc = _make_service(router=router)

        prefixes = svc._get_namespace_prefixes()
        assert "federation/" in prefixes
        assert "tenant-a/" in prefixes

    def test_router_without_get_mount_points_attr_returns_fallback(self):
        router = Mock(spec=[])  # no get_mount_points attribute
        svc = _make_service(router=router)

        prefixes = svc._get_namespace_prefixes()
        assert prefixes == ("workspace/", "shared/", "external/", "system/", "archives/")


# ---------------------------------------------------------------------------
# _should_prepend_recursive_wildcard
# ---------------------------------------------------------------------------


class TestShouldPrependRecursiveWildcard:
    """Tests for _should_prepend_recursive_wildcard()."""

    @pytest.fixture
    def svc(self):
        """SearchService with default (no router) fallback prefixes."""
        return _make_service(router=None)

    @pytest.mark.parametrize(
        "pattern, expected",
        [
            # Unknown prefix -> needs **/
            ("models/file.py", True),
            ("src/utils/helper.py", True),
            ("deep/nested/path/file.txt", True),
            # Known namespaces -> no **/
            ("workspace/file.py", False),
            ("shared/zone1/file.py", False),
            ("external/s3/file.py", False),
            ("system/config.json", False),
            ("archives/old/file.py", False),
            # Already recursive -> no **/
            ("**/*.py", False),
            ("foo/**/bar.py", False),
            # Absolute path -> no **/
            ("/absolute/path.py", False),
            # Single-level (no slash) -> no **/
            ("file.py", False),
            ("*.txt", False),
        ],
        ids=[
            "unknown-prefix-models",
            "unknown-prefix-src",
            "unknown-prefix-deep",
            "known-workspace",
            "known-shared",
            "known-external",
            "known-system",
            "known-archives",
            "already-recursive-star",
            "already-recursive-mid",
            "absolute-path",
            "single-level-file",
            "single-level-glob",
        ],
    )
    def test_default_prefixes(self, svc: SearchService, pattern: str, expected: bool):
        assert svc._should_prepend_recursive_wildcard(pattern) is expected


class TestShouldPrependWithCustomRouter:
    """Tests with a router that has custom federation mount points."""

    @pytest.fixture
    def svc(self):
        router = Mock()
        router.get_mount_points.return_value = [
            "/external",
            "/federation",
            "/shared",
            "/workspace",
        ]
        return _make_service(router=router)

    def test_federation_prefix_not_prepended(self, svc: SearchService):
        assert svc._should_prepend_recursive_wildcard("federation/file.py") is False

    def test_unknown_prefix_still_prepended(self, svc: SearchService):
        assert svc._should_prepend_recursive_wildcard("unknown/file.py") is True

    def test_system_not_in_router_gets_prepended(self, svc: SearchService):
        """system/ is in the fallback but not in this router's mount points."""
        assert svc._should_prepend_recursive_wildcard("system/config.json") is True
