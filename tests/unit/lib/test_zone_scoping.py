"""Tests for zone-scoping helpers (Issue #3063).

Comprehensive coverage for the shared zone_scoping module used by both
HTTP/RPC and gRPC layers.
"""

from types import SimpleNamespace

import pytest

from nexus.lib.zone_scoping import (
    ZONE_PATH_ATTRS,
    ZONE_PATH_LIST_ATTRS,
    ZoneScopingError,
    scope_params_for_zone,
    scope_single_path,
)

# ============================================================================
# scope_single_path
# ============================================================================


class TestScopeSinglePath:
    """Unit tests for scope_single_path()."""

    # --- Happy path: normal paths get prefixed ---

    @pytest.mark.parametrize(
        "path, expected",
        [
            ("/documents/file.txt", "/zone/tenant-1/documents/file.txt"),
            ("/a/b/c", "/zone/tenant-1/a/b/c"),
            ("/", "/zone/tenant-1/"),
        ],
    )
    def test_absolute_path_gets_prefixed(self, path: str, expected: str) -> None:
        assert scope_single_path(path, "/zone/tenant-1", "tenant-1") == expected

    def test_relative_path_gets_prefixed(self) -> None:
        assert (
            scope_single_path("documents/file.txt", "/zone/tenant-1", "tenant-1")
            == "/zone/tenant-1/documents/file.txt"
        )

    # --- Already zone-prefixed: matching zone passes through ---

    def test_matching_zone_prefix_passes_through(self) -> None:
        path = "/zone/tenant-1/documents/file.txt"
        assert scope_single_path(path, "/zone/tenant-1", "tenant-1") == path

    def test_matching_zone_prefix_root_path(self) -> None:
        path = "/zone/my-zone/"
        assert scope_single_path(path, "/zone/my-zone", "my-zone") == path

    # --- Already zone-prefixed: mismatching zone is REJECTED ---

    def test_mismatched_zone_prefix_raises(self) -> None:
        with pytest.raises(ZoneScopingError, match="does not match caller zone"):
            scope_single_path("/zone/other-zone/secret.txt", "/zone/tenant-1", "tenant-1")

    def test_mismatched_zone_prefix_different_zones(self) -> None:
        with pytest.raises(ZoneScopingError):
            scope_single_path("/zone/zone-B/data", "/zone/zone-A", "zone-A")

    # --- Tenant-prefixed paths pass through unchanged ---

    def test_tenant_prefix_passes_through(self) -> None:
        path = "/tenant:foo/bar"
        assert scope_single_path(path, "/zone/tenant-1", "tenant-1") == path

    # --- Edge cases ---

    def test_empty_path_relative(self) -> None:
        # Empty string is a relative path
        assert scope_single_path("", "/zone/t1", "t1") == "/zone/t1/"

    def test_zone_prefix_with_no_subpath(self) -> None:
        # /zone/tenant-1 (no trailing slash or subpath)
        path = "/zone/tenant-1"
        # The embedded zone is extracted as "tenant-1" — matches
        assert scope_single_path(path, "/zone/tenant-1", "tenant-1") == path

    def test_zone_prefix_with_only_slash(self) -> None:
        path = "/zone/tenant-1/"
        assert scope_single_path(path, "/zone/tenant-1", "tenant-1") == path

    def test_zone_prefix_case_sensitive(self) -> None:
        """Zone IDs are case-sensitive."""
        with pytest.raises(ZoneScopingError):
            scope_single_path("/zone/Tenant-1/file.txt", "/zone/tenant-1", "tenant-1")


# ============================================================================
# scope_params_for_zone
# ============================================================================


class TestScopeParamsForZone:
    """Unit tests for scope_params_for_zone()."""

    # --- Root zone: no scoping applied ---

    def test_root_zone_skips_scoping(self) -> None:
        params = SimpleNamespace(path="/documents/file.txt")
        scope_params_for_zone(params, "root")
        assert params.path == "/documents/file.txt"

    # --- Single path attributes ---

    def test_scopes_path_attribute(self) -> None:
        params = SimpleNamespace(path="/file.txt")
        scope_params_for_zone(params, "zone-A")
        assert params.path == "/zone/zone-A/file.txt"

    def test_scopes_old_path_and_new_path(self) -> None:
        params = SimpleNamespace(old_path="/src.txt", new_path="/dst.txt")
        scope_params_for_zone(params, "zone-A")
        assert params.old_path == "/zone/zone-A/src.txt"
        assert params.new_path == "/zone/zone-A/dst.txt"

    def test_skips_non_string_attributes(self) -> None:
        params = SimpleNamespace(path=None, old_path=123)
        scope_params_for_zone(params, "zone-A")
        assert params.path is None
        assert params.old_path == 123

    def test_skips_missing_attributes(self) -> None:
        """Params without path attributes should not error."""
        params = SimpleNamespace(method="read")
        scope_params_for_zone(params, "zone-A")
        assert params.method == "read"

    # --- List path attributes ---

    def test_scopes_list_paths(self) -> None:
        params = SimpleNamespace(paths=["/a.txt", "/b.txt"])
        scope_params_for_zone(params, "zone-A")
        assert params.paths == ["/zone/zone-A/a.txt", "/zone/zone-A/b.txt"]

    def test_scopes_list_patterns(self) -> None:
        params = SimpleNamespace(patterns=["/docs/*.md"])
        scope_params_for_zone(params, "zone-A")
        assert params.patterns == ["/zone/zone-A/docs/*.md"]

    def test_skips_non_string_items_in_list(self) -> None:
        params = SimpleNamespace(paths=["/a.txt", 42, None, "/b.txt"])
        scope_params_for_zone(params, "zone-A")
        # Non-string items are filtered out
        assert params.paths == ["/zone/zone-A/a.txt", "/zone/zone-A/b.txt"]

    def test_skips_non_list_collection_attributes(self) -> None:
        params = SimpleNamespace(paths="not-a-list")
        scope_params_for_zone(params, "zone-A")
        assert params.paths == "not-a-list"  # Unchanged

    def test_empty_list_stays_empty(self) -> None:
        params = SimpleNamespace(paths=[])
        scope_params_for_zone(params, "zone-A")
        assert params.paths == []

    # --- Zone validation in params ---

    def test_rejects_mismatched_zone_in_path(self) -> None:
        params = SimpleNamespace(path="/zone/zone-B/secret.txt")
        with pytest.raises(ZoneScopingError, match="does not match"):
            scope_params_for_zone(params, "zone-A")

    def test_rejects_mismatched_zone_in_list(self) -> None:
        params = SimpleNamespace(paths=["/zone/zone-B/a.txt"])
        with pytest.raises(ZoneScopingError, match="does not match"):
            scope_params_for_zone(params, "zone-A")

    def test_accepts_matching_zone_in_path(self) -> None:
        params = SimpleNamespace(path="/zone/zone-A/file.txt")
        scope_params_for_zone(params, "zone-A")
        assert params.path == "/zone/zone-A/file.txt"

    def test_accepts_matching_zone_in_list(self) -> None:
        params = SimpleNamespace(paths=["/zone/zone-A/a.txt", "/zone/zone-A/b.txt"])
        scope_params_for_zone(params, "zone-A")
        assert params.paths == ["/zone/zone-A/a.txt", "/zone/zone-A/b.txt"]

    # --- Mixed attributes ---

    def test_scopes_all_attribute_types_together(self) -> None:
        params = SimpleNamespace(
            path="/file.txt",
            old_path="/old.txt",
            new_path="/new.txt",
            paths=["/a.txt", "/b.txt"],
            patterns=["/docs/*.md"],
        )
        scope_params_for_zone(params, "zone-X")
        assert params.path == "/zone/zone-X/file.txt"
        assert params.old_path == "/zone/zone-X/old.txt"
        assert params.new_path == "/zone/zone-X/new.txt"
        assert params.paths == ["/zone/zone-X/a.txt", "/zone/zone-X/b.txt"]
        assert params.patterns == ["/zone/zone-X/docs/*.md"]

    def test_tenant_prefix_in_list(self) -> None:
        params = SimpleNamespace(paths=["/tenant:foo/bar", "/a.txt"])
        scope_params_for_zone(params, "zone-A")
        assert params.paths == ["/tenant:foo/bar", "/zone/zone-A/a.txt"]

    # --- Attribute constants match RPC expectations ---

    def test_zone_path_attrs_tuple(self) -> None:
        assert "path" in ZONE_PATH_ATTRS
        assert "old_path" in ZONE_PATH_ATTRS
        assert "new_path" in ZONE_PATH_ATTRS

    def test_zone_path_list_attrs_tuple(self) -> None:
        assert "paths" in ZONE_PATH_LIST_ATTRS
        assert "patterns" in ZONE_PATH_LIST_ATTRS
