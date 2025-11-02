"""Tests for NexusFS version management operations."""

from unittest.mock import Mock

import pytest

from nexus.core.exceptions import NexusFileNotFoundError
from nexus.core.nexus_fs_versions import NexusFSVersionsMixin


class TestNexusFSVersions:
    """Tests for NexusFSVersionsMixin."""

    @pytest.fixture
    def mock_fs(self):
        """Create a mock filesystem with version support."""
        from nexus.core.permissions_enhanced import EnhancedOperationContext

        fs = Mock(spec=NexusFSVersionsMixin)
        fs.metadata = Mock()
        fs.router = Mock()
        fs.tenant_id = None
        fs.agent_id = None
        fs.is_admin = False
        fs._validate_path = Mock(side_effect=lambda p, allow_root=False: p)
        fs._check_permission = Mock()
        fs._get_routing_params = Mock(
            return_value=(None, None, False)
        )  # Returns (tenant_id, agent_id, is_admin)
        fs._get_created_by = Mock(return_value="test-user")  # Mock created_by tracking
        fs._default_context = EnhancedOperationContext(
            user="test",
            groups=[],
            tenant_id=None,
            agent_id=None,
            is_admin=False,
            is_system=False,
        )

        # Bind the actual methods
        fs.get_version = lambda path, version, context=None: NexusFSVersionsMixin.get_version(
            fs, path, version, context
        )
        fs.list_versions = lambda path: NexusFSVersionsMixin.list_versions(fs, path)
        fs.rollback = lambda path, version, context=None: NexusFSVersionsMixin.rollback(
            fs, path, version, context
        )
        fs.diff_versions = (
            lambda path, v1, v2, mode="metadata", context=None: NexusFSVersionsMixin.diff_versions(
                fs, path, v1, v2, mode, context
            )
        )

        return fs

    def test_get_version_success(self, mock_fs):
        """Test getting a specific version."""
        # Setup mocks
        version_meta = Mock()
        version_meta.etag = "abc123"
        mock_fs.metadata.get_version = Mock(return_value=version_meta)

        mock_route = Mock()
        mock_backend = Mock()
        mock_backend.read_content = Mock(return_value=b"version 2 content")
        mock_route.backend = mock_backend
        mock_fs.router.route = Mock(return_value=mock_route)

        # Call method
        content = mock_fs.get_version("/test.txt", 2)

        # Verify
        assert content == b"version 2 content"
        mock_fs.metadata.get_version.assert_called_once_with("/test.txt", 2)
        mock_backend.read_content.assert_called_once_with("abc123")

    def test_get_version_not_found(self, mock_fs):
        """Test getting a version that doesn't exist."""
        mock_fs.metadata.get_version = Mock(return_value=None)

        with pytest.raises(NexusFileNotFoundError, match="/test.txt \\(version 5\\)"):
            mock_fs.get_version("/test.txt", 5)

    def test_get_version_no_content(self, mock_fs):
        """Test getting a version with no content hash."""
        version_meta = Mock()
        version_meta.etag = None
        mock_fs.metadata.get_version = Mock(return_value=version_meta)

        with pytest.raises(NexusFileNotFoundError, match="has no content"):
            mock_fs.get_version("/test.txt", 2)

    def test_list_versions(self, mock_fs):
        """Test listing file versions."""
        versions = [
            {"version": 3, "size": 100, "created_at": "2024-01-03"},
            {"version": 2, "size": 90, "created_at": "2024-01-02"},
            {"version": 1, "size": 80, "created_at": "2024-01-01"},
        ]
        mock_fs.metadata.list_versions = Mock(return_value=versions)

        result = mock_fs.list_versions("/test.txt")

        assert result == versions
        mock_fs.metadata.list_versions.assert_called_once_with("/test.txt")

    def test_rollback_success(self, mock_fs):
        """Test rolling back to a previous version."""
        # Setup mocks
        mock_route = Mock()
        mock_route.readonly = False
        mock_fs.router.route = Mock(return_value=mock_route)
        mock_fs.metadata.rollback = Mock()
        mock_fs.metadata._cache_enabled = False

        # Call method
        mock_fs.rollback("/test.txt", 2)

        # Verify
        mock_fs._check_permission.assert_called_once()
        mock_fs.metadata.rollback.assert_called_once_with("/test.txt", 2, created_by="test-user")

    def test_rollback_readonly_path(self, mock_fs):
        """Test that rollback fails on readonly path."""
        mock_route = Mock()
        mock_route.readonly = True
        mock_fs.router.route = Mock(return_value=mock_route)

        with pytest.raises(PermissionError, match="Cannot rollback read-only path"):
            mock_fs.rollback("/test.txt", 2)

    def test_rollback_invalidates_cache(self, mock_fs):
        """Test that rollback invalidates cache."""
        # Setup mocks with cache enabled
        mock_route = Mock()
        mock_route.readonly = False
        mock_fs.router.route = Mock(return_value=mock_route)
        mock_fs.metadata.rollback = Mock()
        mock_fs.metadata._cache_enabled = True
        mock_fs.metadata._cache = Mock()

        # Call method
        mock_fs.rollback("/test.txt", 2)

        # Verify cache was invalidated
        mock_fs.metadata._cache.invalidate_path.assert_called_once_with("/test.txt")

    def test_diff_versions_metadata_mode(self, mock_fs):
        """Test diffing versions in metadata mode."""
        meta_diff = {
            "version_1": 1,
            "version_2": 3,
            "size_v1": 100,
            "size_v2": 150,
            "content_changed": True,
        }
        mock_fs.metadata.get_version_diff = Mock(return_value=meta_diff)

        result = mock_fs.diff_versions("/test.txt", 1, 3, mode="metadata")

        assert result == meta_diff
        mock_fs.metadata.get_version_diff.assert_called_once_with("/test.txt", 1, 3)

    def test_diff_versions_content_mode_no_changes(self, mock_fs):
        """Test content diff when there are no changes."""
        meta_diff = {"content_changed": False}
        mock_fs.metadata.get_version_diff = Mock(return_value=meta_diff)

        result = mock_fs.diff_versions("/test.txt", 1, 2, mode="content")

        assert result == "(no content changes)"

    def test_diff_versions_content_mode_with_changes(self, mock_fs):
        """Test content diff with actual changes."""
        meta_diff = {"content_changed": True}
        mock_fs.metadata.get_version_diff = Mock(return_value=meta_diff)

        # Mock get_version to return different content
        version_meta_v1 = Mock(etag="hash1")
        version_meta_v2 = Mock(etag="hash2")
        mock_fs.metadata.get_version = Mock(side_effect=[version_meta_v1, version_meta_v2])

        mock_route = Mock()
        mock_backend = Mock()
        mock_backend.read_content = Mock(
            side_effect=[b"line 1\nline 2\nline 3", b"line 1\nline 2 modified\nline 3"]
        )
        mock_route.backend = mock_backend
        mock_fs.router.route = Mock(return_value=mock_route)

        result = mock_fs.diff_versions("/test.txt", 1, 2, mode="content")

        assert isinstance(result, str)
        assert "---" in result
        assert "+++" in result
        # Should show the modified line
        assert "line 2" in result or "modified" in result

    def test_diff_versions_invalid_mode(self, mock_fs):
        """Test that invalid mode raises ValueError."""
        with pytest.raises(ValueError, match="Invalid mode: invalid"):
            mock_fs.diff_versions("/test.txt", 1, 2, mode="invalid")

    def test_get_version_validates_path(self, mock_fs):
        """Test that get_version validates the path."""
        mock_fs._validate_path = Mock(return_value="/validated/path.txt")

        version_meta = Mock(etag="abc123")
        mock_fs.metadata.get_version = Mock(return_value=version_meta)

        mock_route = Mock()
        mock_backend = Mock()
        mock_backend.read_content = Mock(return_value=b"content")
        mock_route.backend = mock_backend
        mock_fs.router.route = Mock(return_value=mock_route)

        mock_fs.get_version("/path.txt", 1)

        mock_fs._validate_path.assert_called_once_with("/path.txt")

    def test_list_versions_validates_path(self, mock_fs):
        """Test that list_versions validates the path."""
        mock_fs._validate_path = Mock(return_value="/validated/path.txt")
        mock_fs.metadata.list_versions = Mock(return_value=[])

        mock_fs.list_versions("/path.txt")

        mock_fs._validate_path.assert_called_once_with("/path.txt")

    def test_rollback_checks_permission(self, mock_fs):
        """Test that rollback checks write permission."""
        from nexus.core.permissions import Permission

        mock_route = Mock(readonly=False)
        mock_fs.router.route = Mock(return_value=mock_route)
        mock_fs.metadata.rollback = Mock()
        mock_fs.metadata._cache_enabled = False

        mock_fs.rollback("/test.txt", 2, context=None)

        # Verify permission check was called
        mock_fs._check_permission.assert_called_once()
        args = mock_fs._check_permission.call_args[0]
        assert args[0] == "/test.txt"
        assert args[1] == Permission.WRITE
