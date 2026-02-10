"""Tests for server path unscoping utilities.

Tests path translation from internal storage formats to user-friendly paths.
Covers both legacy (/tenant:) and current (/zone/) path formats.

Related: Issue #1202 - list('/') returns paths with /tenant: prefix
"""

from __future__ import annotations

from nexus.server.path_utils import (
    unscope_internal_dict,
    unscope_internal_path,
    unscope_internal_paths,
)


class TestUnscopeInternalPath:
    """Test unscope_internal_path for all path format variants."""

    # --- Legacy /tenant: format ---

    def test_tenant_prefix_with_connector(self) -> None:
        """Strip /tenant:default/ prefix from connector path."""
        assert (
            unscope_internal_path("/tenant:default/connector/gcs_demo/auto-test.txt")
            == "/connector/gcs_demo/auto-test.txt"
        )

    def test_tenant_prefix_with_user_and_workspace(self) -> None:
        """Strip /tenant:default/user:admin/ prefix from workspace path."""
        assert (
            unscope_internal_path("/tenant:default/user:admin/workspace/file.txt")
            == "/workspace/file.txt"
        )

    def test_tenant_prefix_only(self) -> None:
        """Strip /tenant:default with nothing after -> root."""
        assert unscope_internal_path("/tenant:default") == "/"

    def test_tenant_prefix_with_trailing_slash(self) -> None:
        """Strip /tenant:default/ with trailing slash only -> root."""
        assert unscope_internal_path("/tenant:default/") == "/"

    def test_tenant_prefix_custom_zone(self) -> None:
        """Strip /tenant:mycompany/ prefix."""
        assert (
            unscope_internal_path("/tenant:mycompany/workspace/project/data.csv")
            == "/workspace/project/data.csv"
        )

    def test_tenant_prefix_with_user_deep_path(self) -> None:
        """Strip prefix from deeply nested path."""
        assert (
            unscope_internal_path(
                "/tenant:acme/user:alice/workspace/ws_personal_abc/project/src/main.py"
            )
            == "/workspace/ws_personal_abc/project/src/main.py"
        )

    # --- Current /zone/ format ---

    def test_zone_prefix_with_user_workspace(self) -> None:
        """Strip /zone/default/user:admin/ prefix."""
        assert (
            unscope_internal_path("/zone/default/user:admin/workspace/file.txt")
            == "/workspace/file.txt"
        )

    def test_zone_prefix_without_user(self) -> None:
        """Strip /zone/myzone/ prefix (zone-level resource, no user)."""
        assert (
            unscope_internal_path("/zone/myzone/connector/s3/data.csv")
            == "/connector/s3/data.csv"
        )

    def test_zone_prefix_only(self) -> None:
        """Strip /zone/default with nothing meaningful after -> root."""
        assert unscope_internal_path("/zone/default") == "/"

    def test_zone_prefix_with_user_only(self) -> None:
        """Strip /zone/default/user:admin with nothing after -> root."""
        assert unscope_internal_path("/zone/default/user:admin") == "/"

    def test_zone_prefix_with_skill(self) -> None:
        """Strip zone+user prefix from skill path."""
        assert (
            unscope_internal_path("/zone/acme/user:bob/skill/my-skill/main.py")
            == "/skill/my-skill/main.py"
        )

    # --- Paths without internal prefix (should be unchanged) ---

    def test_no_prefix_workspace(self) -> None:
        """Paths without internal prefix are unchanged."""
        assert unscope_internal_path("/workspace/file.txt") == "/workspace/file.txt"

    def test_no_prefix_skills(self) -> None:
        """Global namespace paths are unchanged."""
        assert (
            unscope_internal_path("/skills/my-skill/main.py")
            == "/skills/my-skill/main.py"
        )

    def test_no_prefix_system(self) -> None:
        """System paths are unchanged."""
        assert unscope_internal_path("/system/config.yaml") == "/system/config.yaml"

    def test_no_prefix_memory(self) -> None:
        """Memory paths are unchanged."""
        assert (
            unscope_internal_path("/memory/by-user/alice/facts")
            == "/memory/by-user/alice/facts"
        )

    def test_no_prefix_connector(self) -> None:
        """Already-clean connector paths are unchanged."""
        assert (
            unscope_internal_path("/connector/gcs_demo/file.txt")
            == "/connector/gcs_demo/file.txt"
        )

    # --- Edge cases ---

    def test_root_path(self) -> None:
        """Root path is unchanged."""
        assert unscope_internal_path("/") == "/"

    def test_empty_string(self) -> None:
        """Empty string returns root."""
        assert unscope_internal_path("") == "/"

    def test_path_with_zone_in_name(self) -> None:
        """Path containing 'zone' as a file/dir name is NOT stripped."""
        # /workspace/zone/data.txt should NOT be treated as a zone prefix
        assert (
            unscope_internal_path("/workspace/zone/data.txt")
            == "/workspace/zone/data.txt"
        )

    def test_path_starting_with_zone_word_not_prefix(self) -> None:
        """Path starting with 'zone' but as a namespace (not /zone/) is unchanged."""
        # /zones/... is a different format from /zone/ (ScopedFilesystem uses /zones/)
        assert (
            unscope_internal_path("/zones/team_12/users/user_1/workspace/file.txt")
            == "/zones/team_12/users/user_1/workspace/file.txt"
        )

    def test_tenant_colon_in_middle_of_path(self) -> None:
        """Path with tenant: in a non-root position is NOT stripped."""
        assert (
            unscope_internal_path("/workspace/tenant:default/file.txt")
            == "/workspace/tenant:default/file.txt"
        )


class TestUnscopeInternalPaths:
    """Test unscope_internal_paths (list version)."""

    def test_mixed_paths(self) -> None:
        """Strip prefixes from a mixed list of paths."""
        paths = [
            "/tenant:default/connector/gcs_demo/auto-test.txt",
            "/tenant:default/user:admin/workspace/file.txt",
            "/skills/my-skill/main.py",
            "/zone/default/user:admin/memory/facts.json",
        ]
        expected = [
            "/connector/gcs_demo/auto-test.txt",
            "/workspace/file.txt",
            "/skills/my-skill/main.py",
            "/memory/facts.json",
        ]
        assert unscope_internal_paths(paths) == expected

    def test_empty_list(self) -> None:
        """Empty list returns empty list."""
        assert unscope_internal_paths([]) == []

    def test_all_unprefixed(self) -> None:
        """List with no prefixed paths is unchanged."""
        paths = ["/workspace/a.txt", "/workspace/b.txt"]
        assert unscope_internal_paths(paths) == paths


class TestUnscopInternalDict:
    """Test unscope_internal_dict."""

    def test_dict_with_path_key(self) -> None:
        """Strip prefix from path key in dict."""
        d = {
            "path": "/tenant:default/user:admin/workspace/file.txt",
            "size": 100,
            "etag": "abc123",
        }
        result = unscope_internal_dict(d, ["path"])
        assert result["path"] == "/workspace/file.txt"
        assert result["size"] == 100
        assert result["etag"] == "abc123"

    def test_dict_with_multiple_path_keys(self) -> None:
        """Strip prefix from multiple path keys."""
        d = {
            "path": "/zone/default/user:admin/workspace/file.txt",
            "virtual_path": "/zone/default/user:admin/workspace/file.txt",
            "size": 200,
        }
        result = unscope_internal_dict(d, ["path", "virtual_path"])
        assert result["path"] == "/workspace/file.txt"
        assert result["virtual_path"] == "/workspace/file.txt"
        assert result["size"] == 200

    def test_dict_without_path_key(self) -> None:
        """Dict without matching keys is unchanged."""
        d = {"size": 100, "etag": "abc"}
        result = unscope_internal_dict(d, ["path"])
        assert result == d

    def test_original_dict_not_mutated(self) -> None:
        """Original dict is not mutated (immutability)."""
        d = {"path": "/tenant:default/workspace/file.txt", "size": 100}
        result = unscope_internal_dict(d, ["path"])
        assert d["path"] == "/tenant:default/workspace/file.txt"  # Original unchanged
        assert result["path"] == "/workspace/file.txt"  # Copy changed

    def test_non_string_path_value_unchanged(self) -> None:
        """Non-string values in path keys are left unchanged."""
        d = {"path": 42, "size": 100}
        result = unscope_internal_dict(d, ["path"])
        assert result["path"] == 42
