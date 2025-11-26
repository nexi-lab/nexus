"""Unit tests for Google Drive connector bug fixes.

This test suite covers critical bug fixes in the gdrive_connector:

1. mkdir() bug fix: Ensures path components are created one at a time,
   not as compound paths with slashes in the folder name.

2. is_directory() bug fix: Ensures the method correctly identifies files
   vs directories by querying Google Drive API with proper folder MIME type.
"""

import contextlib
from unittest.mock import Mock, patch

import pytest

from nexus.backends.gdrive_connector import GoogleDriveConnectorBackend
from nexus.core.exceptions import BackendError


class TestGDriveMkdirFix:
    """Test suite for mkdir() bug fix.

    Bug: mkdir was passing full paths like "workspace/folder/file.png" to
    _get_or_create_folder, which would create a single folder with slashes
    in the name instead of navigating through the hierarchy.

    Fix: Navigate through path components one at a time.
    """

    @pytest.fixture
    def mock_service(self):
        """Mock Google Drive service."""
        service = Mock()
        service.files = Mock(return_value=Mock())
        return service

    @pytest.fixture
    def connector(self):
        """Create a GoogleDriveConnectorBackend instance with mocked credentials."""
        # Create backend without needing actual credentials
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:",
            user_email="test@example.com",
            root_folder="test_root",
            use_shared_drives=False,
        )
        return backend

    def test_mkdir_single_folder(self, connector, mock_service):
        """Test mkdir creates a single folder correctly."""
        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
            patch.object(connector, "is_directory", return_value=False),
            patch.object(
                connector, "_get_or_create_folder", return_value="folder_id"
            ) as mock_create,
        ):
            mock_context = Mock()
            mock_context.user_id = "test_user"
            mock_context.tenant_id = "default"

            connector.mkdir("test_folder", parents=True, exist_ok=False, context=mock_context)

            # Verify _get_or_create_folder was called once with just the folder name
            mock_create.assert_called_once()
            args = mock_create.call_args[0]
            assert args[1] == "test_folder", (
                "Folder name should be 'test_folder', not a path with slashes"
            )

    def test_mkdir_nested_path_creates_hierarchy(self, connector, mock_service):
        """Test mkdir with nested path creates folders one at a time.

        This is the core fix: ensure "workspace/data/file.png" creates:
        1. "workspace" folder
        2. "data" folder inside "workspace"
        3. "file.png" folder inside "data"

        NOT a single folder named "workspace/data/file.png"
        """
        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
            patch.object(connector, "is_directory", return_value=False),
            patch.object(
                connector,
                "_get_or_create_folder",
                side_effect=["folder1_id", "folder2_id", "folder3_id"],
            ) as mock_create,
        ):
            mock_context = Mock()
            mock_context.user_id = "test_user"
            mock_context.tenant_id = "default"

            connector.mkdir(
                "workspace/data/images", parents=True, exist_ok=False, context=mock_context
            )

            # Verify _get_or_create_folder was called 3 times, once for each component
            assert mock_create.call_count == 3, "Should create 3 folders for 3 path components"

            # Verify each call used a single folder name (no slashes)
            calls = mock_create.call_args_list
            assert calls[0][0][1] == "workspace", "First call should be for 'workspace'"
            assert calls[1][0][1] == "data", "Second call should be for 'data'"
            assert calls[2][0][1] == "images", "Third call should be for 'images'"

            # Verify parent_id chaining (each folder is created inside the previous one)
            assert calls[0][0][2] == "root_id", "First folder should be created in root"
            assert calls[1][0][2] == "folder1_id", "Second folder should be created in first folder"
            assert calls[2][0][2] == "folder2_id", "Third folder should be created in second folder"

    def test_mkdir_with_empty_components(self, connector, mock_service):
        """Test mkdir handles paths with leading/trailing slashes correctly."""
        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
            patch.object(connector, "is_directory", return_value=False),
            patch.object(
                connector, "_get_or_create_folder", return_value="folder_id"
            ) as mock_create,
        ):
            mock_context = Mock()
            mock_context.user_id = "test_user"
            mock_context.tenant_id = "default"

            # Path with leading slash
            connector.mkdir("/test_folder/", parents=True, exist_ok=False, context=mock_context)

            # Should only create one folder (empty components are skipped)
            assert mock_create.call_count == 1
            assert mock_create.call_args[0][1] == "test_folder"


class TestGDriveIsDirectoryFix:
    """Test suite for is_directory() bug fix.

    Bug: is_directory was calling _resolve_path_to_folder_id which returns
    a tuple (parent_id, filename), not a single folder_id. Since tuples are
    never None, the method always returned True, making FUSE think every
    file was a directory.

    Fix: Query Google Drive API directly for items with folder MIME type.
    """

    @pytest.fixture
    def mock_service(self):
        """Mock Google Drive service."""
        service = Mock()
        return service

    @pytest.fixture
    def connector(self):
        """Create a GoogleDriveConnectorBackend instance with mocked credentials."""
        # Create backend without needing actual credentials
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:",
            user_email="test@example.com",
            root_folder="test_root",
            use_shared_drives=False,
        )
        return backend

    def test_is_directory_returns_false_for_file(self, connector, mock_service):
        """Test is_directory returns False for a file (not a folder)."""
        # Mock the Google Drive API response: no folders found
        mock_files_list = Mock()
        mock_files_list.list = Mock(return_value=Mock(execute=Mock(return_value={"files": []})))
        mock_service.files = Mock(return_value=mock_files_list)

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
        ):
            result = connector.is_directory("test_file.png")

            # Should return False since no folder with that name was found
            assert result is False, "PNG file should not be identified as a directory"

    def test_is_directory_returns_true_for_folder(self, connector, mock_service):
        """Test is_directory returns True for an actual folder."""
        # Mock the Google Drive API response: folder found
        mock_files_list = Mock()
        mock_files_list.list = Mock(
            return_value=Mock(execute=Mock(return_value={"files": [{"id": "folder_id"}]}))
        )
        mock_service.files = Mock(return_value=mock_files_list)

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
        ):
            result = connector.is_directory("test_folder")

            # Should return True since a folder was found
            assert result is True, "Folder should be identified as a directory"

    def test_is_directory_root_always_true(self, connector):
        """Test is_directory returns True for root path."""
        # Root should always be a directory without querying API
        assert connector.is_directory("") is True
        assert connector.is_directory("/") is True

    def test_is_directory_nested_file_path(self, connector, mock_service):
        """Test is_directory correctly handles nested paths like workspace/data/file.png."""
        call_count = [0]

        def mock_list_execute():
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: looking for "workspace" folder
                return {"files": [{"id": "workspace_id"}]}
            elif call_count[0] == 2:
                # Second call: looking for "data" folder
                return {"files": [{"id": "data_id"}]}
            else:
                # Third call: looking for "file.png" as a folder (should not exist)
                return {"files": []}

        mock_files_list = Mock()
        mock_files_list.list = Mock(return_value=Mock(execute=mock_list_execute))
        mock_service.files = Mock(return_value=mock_files_list)

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
        ):
            result = connector.is_directory("workspace/data/file.png")

            # Should return False since file.png is not a folder
            assert result is False, "Nested file path should not be identified as directory"

    def test_is_directory_does_not_create_folders(self, connector, mock_service):
        """Test is_directory is read-only and doesn't create missing folders.

        This is critical: is_directory should only CHECK, not create.
        If parent folders don't exist, it should return False.
        """
        # Mock: parent folder "workspace" doesn't exist
        mock_files_list = Mock()
        mock_files_list.list = Mock(return_value=Mock(execute=Mock(return_value={"files": []})))
        mock_service.files = Mock(return_value=mock_files_list)

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
            patch.object(connector, "_get_or_create_folder") as mock_create,
        ):
            result = connector.is_directory("workspace/data")

            # Should return False since parent doesn't exist
            assert result is False, "Should return False when parent folder doesn't exist"

            # CRITICAL: _get_or_create_folder should NEVER be called by is_directory
            mock_create.assert_not_called()

    def test_is_directory_queries_folder_mime_type(self, connector, mock_service):
        """Test is_directory specifically queries for folder MIME type.

        This ensures we're not returning True for files that happen to have
        the same name as a folder.
        """
        mock_files_list = Mock()
        mock_files_list.list = Mock(return_value=Mock(execute=Mock(return_value={"files": []})))
        mock_service.files = Mock(return_value=mock_files_list)

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
        ):
            connector.is_directory("test_item")

            # Verify the query includes folder MIME type filter
            call_args = mock_files_list.list.call_args[1]
            query = call_args.get("q", "")
            assert "mimeType='application/vnd.google-apps.folder'" in query, (
                "Query should specifically filter for folder MIME type"
            )


class TestGDriveIntegrationScenarios:
    """Integration test scenarios for the bug fixes."""

    @pytest.fixture
    def connector(self):
        """Create a GoogleDriveConnectorBackend instance with mocked credentials."""
        # Create backend without needing actual credentials
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:",
            user_email="test@example.com",
            root_folder="test_root",
            use_shared_drives=False,
        )
        return backend

    def test_scenario_fuse_file_creation(self, connector):
        """Simulate FUSE layer creating a PNG file.

        This was the original bug scenario:
        1. FUSE calls create() with path="/test_drive/image.png"
        2. create() calls write() with empty content
        3. write() checks is_directory() to ensure parent exists
        4. is_directory() should return False for the file path
        """
        mock_service = Mock()
        mock_files_list = Mock()
        mock_files_list.list = Mock(return_value=Mock(execute=Mock(return_value={"files": []})))
        mock_service.files = Mock(return_value=mock_files_list)

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
        ):
            # Simulate checking if "image.png" is a directory (should be False)
            is_dir = connector.is_directory("test_drive/image.png")

            assert is_dir is False, "PNG file should not be detected as directory in FUSE scenario"

    def test_scenario_mkdir_then_file_creation(self, connector):
        """Test creating a directory hierarchy then checking file status."""
        mock_service = Mock()

        # Track what folders exist
        existing_folders = set()

        def mock_list_execute():
            # Return the query to determine what's being looked for
            query = mock_files_list.list.call_args[1].get("q", "")

            # Extract folder name from query
            if "name='" in query:
                folder_name = query.split("name='")[1].split("'")[0]
                if folder_name in existing_folders:
                    return {"files": [{"id": f"{folder_name}_id"}]}
            return {"files": []}

        mock_files_list = Mock()
        mock_files_list.list = Mock(return_value=Mock(execute=mock_list_execute))
        mock_service.files = Mock(return_value=mock_files_list)

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
            patch.object(
                connector,
                "_get_or_create_folder",
                side_effect=lambda s, name, parent, ctx: existing_folders.add(name) or f"{name}_id",
            ),
        ):
            mock_context = Mock()
            mock_context.user_id = "test_user"
            mock_context.tenant_id = "default"

            # Create directory hierarchy
            connector.mkdir("workspace/data", parents=True, exist_ok=False, context=mock_context)

            # Now check if a file in that hierarchy is detected as a directory
            existing_folders.add("workspace")
            existing_folders.add("data")

            is_dir = connector.is_directory("workspace/data/file.png")

            assert is_dir is False, "File should not be detected as directory even after mkdir"


class TestGDriveGetOrCreateFolder:
    """Test suite for _get_or_create_folder() helper method."""

    @pytest.fixture
    def mock_service(self):
        """Mock Google Drive service."""
        service = Mock()
        return service

    @pytest.fixture
    def connector(self):
        """Create a GoogleDriveConnectorBackend instance with mocked credentials."""
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:",
            user_email="test@example.com",
            root_folder="test_root",
            use_shared_drives=False,
        )
        return backend

    def test_get_or_create_folder_existing_folder(self, connector, mock_service):
        """Test _get_or_create_folder returns existing folder ID."""
        mock_context = Mock()
        mock_context.user_id = "test_user"
        mock_context.tenant_id = "default"

        # Mock that folder exists
        mock_list_result = Mock()
        mock_list_result.execute.return_value = {"files": [{"id": "existing_folder_id"}]}

        mock_files = Mock()
        mock_files.list.return_value = mock_list_result
        mock_service.files.return_value = mock_files

        result = connector._get_or_create_folder(
            mock_service, "existing_folder", "parent_id", mock_context
        )

        assert result == "existing_folder_id"
        # Should not create a new folder
        mock_files.create.assert_not_called()

    def test_get_or_create_folder_creates_new_folder(self, connector, mock_service):
        """Test _get_or_create_folder creates folder when it doesn't exist."""
        mock_context = Mock()
        mock_context.user_id = "test_user"
        mock_context.tenant_id = "default"

        # Mock that folder doesn't exist
        mock_list_result = Mock()
        mock_list_result.execute.return_value = {"files": []}

        # Mock folder creation
        mock_create_result = Mock()
        mock_create_result.execute.return_value = {"id": "new_folder_id"}

        mock_files = Mock()
        mock_files.list.return_value = mock_list_result
        mock_files.create.return_value = mock_create_result
        mock_service.files.return_value = mock_files

        result = connector._get_or_create_folder(
            mock_service, "new_folder", "parent_id", mock_context
        )

        assert result == "new_folder_id"
        # Should create a new folder
        mock_files.create.assert_called_once()


class TestGDriveErrorHandling:
    """Test suite for error handling scenarios."""

    @pytest.fixture
    def connector(self):
        """Create a GoogleDriveConnectorBackend instance with mocked credentials."""
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:",
            user_email="test@example.com",
            root_folder="test_root",
            use_shared_drives=False,
        )
        return backend

    def test_operation_without_context(self, connector):
        """Test operations fail gracefully without context."""
        with pytest.raises((ValueError, BackendError)):
            connector.write_content(b"test", context=None)

    def test_mkdir_with_invalid_path(self, connector):
        """Test mkdir handles invalid paths."""
        mock_context = Mock()
        mock_context.user_id = "test_user"
        mock_context.tenant_id = "default"

        mock_service = Mock()

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
            patch.object(connector, "is_directory", return_value=False),
            contextlib.suppress(ValueError, BackendError, FileNotFoundError),
        ):
            # Empty path should be handled
            connector.mkdir("", parents=True, exist_ok=False, context=mock_context)

    def test_is_directory_handles_api_errors(self, connector):
        """Test is_directory returns False on API errors."""
        mock_service = Mock()
        mock_service.files.side_effect = Exception("API Error")

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(connector, "_get_or_create_root_folder", return_value="root_id"),
        ):
            # Should return False instead of raising
            result = connector.is_directory("some/path")
            assert result is False


class TestGDriveWriteContent:
    """Test suite for write_content() method."""

    @pytest.fixture
    def mock_service(self):
        """Mock Google Drive service."""
        service = Mock()
        return service

    @pytest.fixture
    def connector(self):
        """Create a GoogleDriveConnectorBackend instance."""
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:",
            user_email="test@example.com",
            root_folder="test_root",
            use_shared_drives=False,
        )
        return backend

    def test_write_content_new_file(self, connector, mock_service):
        """Test write_content creates a new file."""
        test_content = b"Hello, Google Drive!"
        mock_context = Mock()
        mock_context.user_id = "test_user"
        mock_context.tenant_id = "default"
        mock_context.backend_path = "workspace/test.txt"

        # Mock file doesn't exist
        mock_list_result = Mock()
        mock_list_result.execute.return_value = {"files": []}

        # Mock file creation
        mock_create_result = Mock()
        mock_create_result.execute.return_value = {"id": "new_file_id"}

        mock_files = Mock()
        mock_files.list.return_value = mock_list_result
        mock_files.create.return_value = mock_create_result
        mock_service.files.return_value = mock_files

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(
                connector, "_resolve_path_to_folder_id", return_value=("parent_id", "test.txt")
            ),
        ):
            result = connector.write_content(test_content, context=mock_context)

            # Should return SHA-256 hash
            assert len(result) == 64
            mock_files.create.assert_called_once()

    def test_write_content_update_existing_file(self, connector, mock_service):
        """Test write_content updates an existing file."""
        test_content = b"Updated content"
        mock_context = Mock()
        mock_context.user_id = "test_user"
        mock_context.tenant_id = "default"
        mock_context.backend_path = "workspace/existing.txt"

        # Mock file exists
        mock_list_result = Mock()
        mock_list_result.execute.return_value = {"files": [{"id": "existing_file_id"}]}

        # Mock file update
        mock_update_result = Mock()
        mock_update_result.execute.return_value = {"id": "existing_file_id"}

        mock_files = Mock()
        mock_files.list.return_value = mock_list_result
        mock_files.update.return_value = mock_update_result
        mock_service.files.return_value = mock_files

        with (
            patch.object(connector, "_get_drive_service", return_value=mock_service),
            patch.object(
                connector, "_resolve_path_to_folder_id", return_value=("parent_id", "existing.txt")
            ),
        ):
            result = connector.write_content(test_content, context=mock_context)

            # Should return SHA-256 hash
            assert len(result) == 64
            mock_files.update.assert_called_once()

    def test_write_content_without_context(self, connector):
        """Test write_content fails without context."""
        with pytest.raises(BackendError) as exc_info:
            connector.write_content(b"test", context=None)

        assert "backend_path" in str(exc_info.value)


class TestGDriveReadWriteDeleteRequireComplexMocking:
    """
    Tests for read_content(), delete_content(), and list_dir() are challenging
    for unit tests because they use MediaIoBaseDownload and complex Google API patterns.

    These methods are better tested through:
    1. Integration tests with real Google Drive API
    2. Manual verification (as was done for the FUSE bug fixes)

    The current test suite focuses on the critical bug fixes (mkdir and is_directory)
    which provide 30% coverage and verify the most important functionality.
    """

    def test_read_content_requires_context(self):
        """Test read_content fails without context (basic validation)."""
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:", user_email="test@example.com", root_folder="test_root"
        )

        with pytest.raises(BackendError) as exc_info:
            backend.read_content("hash", context=None)

        assert "backend_path" in str(exc_info.value)

    def test_delete_content_requires_context(self):
        """Test delete_content fails without context (basic validation)."""
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:", user_email="test@example.com", root_folder="test_root"
        )

        with pytest.raises(BackendError) as exc_info:
            backend.delete_content("hash", context=None)

        assert "backend_path" in str(exc_info.value)


class TestGDriveProperties:
    """Test suite for backend properties."""

    def test_name_property(self):
        """Test name property returns correct backend name."""
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:", user_email="test@example.com", root_folder="test_root"
        )
        assert backend.name == "gdrive"

    def test_user_scoped_property(self):
        """Test user_scoped property returns True."""
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:", user_email="test@example.com", root_folder="test_root"
        )
        assert backend.user_scoped is True


class TestGDriveSharedDrives:
    """Test suite for shared drives functionality."""

    @pytest.fixture
    def connector_shared(self):
        """Create a GoogleDriveConnectorBackend with shared drives enabled."""
        backend = GoogleDriveConnectorBackend(
            token_manager_db=":memory:",
            user_email="test@example.com",
            root_folder="test_root",
            use_shared_drives=True,
            shared_drive_id="shared_drive_123",
        )
        return backend

    def test_is_directory_with_shared_drives(self, connector_shared):
        """Test is_directory works with shared drives."""
        mock_service = Mock()

        # Mock folder exists in shared drive
        mock_files_list = Mock()
        mock_files_list.list.return_value = Mock(
            execute=Mock(return_value={"files": [{"id": "folder_id"}]})
        )
        mock_service.files.return_value = mock_files_list

        with (
            patch.object(connector_shared, "_get_drive_service", return_value=mock_service),
            patch.object(connector_shared, "_get_or_create_root_folder", return_value="root_id"),
        ):
            result = connector_shared.is_directory("shared_folder")

            # Verify shared drives parameters were used
            assert result is True
            call_args = mock_files_list.list.call_args
            assert call_args[1].get("includeItemsFromAllDrives") is True
            assert call_args[1].get("supportsAllDrives") is True


class TestOAuthProviderRegistration:
    """Test suite for OAuth provider registration feature.

    Tests the _register_oauth_provider() method that automatically registers
    OAuth providers with the TokenManager during backend initialization.
    """

    @pytest.fixture
    def mock_token_manager(self):
        """Create a mock TokenManager."""
        manager = Mock()
        manager.register_provider = Mock()
        return manager

    def test_oauth_provider_registration_success(self, mock_token_manager):
        """Test successful OAuth provider registration with valid credentials."""
        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id_123",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret_456",
                },
            ),
            patch("nexus.server.auth.token_manager.TokenManager") as MockTM,
            patch("nexus.server.auth.oauth_factory.OAuthProviderFactory") as MockFactory,
        ):
            MockTM.return_value = mock_token_manager
            mock_oauth_provider = Mock()
            mock_factory = Mock()
            mock_factory.create_provider.return_value = mock_oauth_provider
            MockFactory.return_value = mock_factory

            # Create backend - should automatically register OAuth provider
            _backend = GoogleDriveConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@example.com",
                root_folder="test_root",
                provider="google-drive",
            )

            # Verify TokenManager was created
            MockTM.assert_called_once_with(db_path=":memory:")

            # Verify factory was used to create provider
            mock_factory.create_provider.assert_called_once_with(name="google-drive")

            # Verify provider was registered with TokenManager
            mock_token_manager.register_provider.assert_called_once_with(
                "google-drive", mock_oauth_provider
            )

    def test_oauth_provider_registration_missing_credentials(self, mock_token_manager):
        """Test OAuth provider registration skips gracefully when credentials missing."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("nexus.server.auth.token_manager.TokenManager") as MockTM,
            patch("nexus.server.auth.oauth_factory.OAuthProviderFactory") as MockFactory,
        ):
            MockTM.return_value = mock_token_manager
            mock_factory = Mock()
            mock_factory.create_provider.side_effect = ValueError("Provider not found")
            MockFactory.return_value = mock_factory

            # Create backend without OAuth credentials in environment
            _backend = GoogleDriveConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@example.com",
                root_folder="test_root",
                provider="google-drive",
            )

            # Verify provider was never registered
            mock_token_manager.register_provider.assert_not_called()

    def test_oauth_provider_registration_partial_credentials(self, mock_token_manager):
        """Test OAuth registration skips when only client_id is provided."""
        with (
            patch.dict(
                "os.environ",
                {"NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id_123"},
                clear=True,
            ),
            patch("nexus.server.auth.token_manager.TokenManager") as MockTM,
            patch("nexus.server.auth.oauth_factory.OAuthProviderFactory") as MockFactory,
        ):
            MockTM.return_value = mock_token_manager
            mock_factory = Mock()
            mock_factory.create_provider.side_effect = ValueError("Provider not found")
            MockFactory.return_value = mock_factory

            # Create backend with only client_id (missing client_secret)
            _backend = GoogleDriveConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@example.com",
                root_folder="test_root",
                provider="google-drive",
            )

            # Verify provider was not registered
            mock_token_manager.register_provider.assert_not_called()

    def test_oauth_provider_registration_handles_errors(self, mock_token_manager, capsys):
        """Test OAuth provider registration handles errors gracefully."""
        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id_123",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret_456",
                },
            ),
            patch("nexus.server.auth.token_manager.TokenManager") as MockTM,
            patch("nexus.server.auth.oauth_factory.OAuthProviderFactory") as MockFactory,
        ):
            MockTM.return_value = mock_token_manager
            mock_factory = Mock()
            mock_factory.create_provider.side_effect = Exception("OAuth initialization failed")
            MockFactory.return_value = mock_factory

            # Create backend - should handle the error without crashing
            _backend = GoogleDriveConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@example.com",
                root_folder="test_root",
                provider="google-drive",
            )

            # Verify error was logged (captured in stdout)
            captured = capsys.readouterr()
            assert "Failed to register OAuth provider" in captured.out or "✗" in captured.out

            # Verify provider was not registered
            mock_token_manager.register_provider.assert_not_called()

    def test_oauth_provider_registration_with_database_url(self, mock_token_manager):
        """Test OAuth provider registration with database URL instead of file path."""
        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id_123",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret_456",
                },
            ),
            patch("nexus.server.auth.token_manager.TokenManager") as MockTM,
            patch("nexus.server.auth.oauth_factory.OAuthProviderFactory") as MockFactory,
        ):
            MockTM.return_value = mock_token_manager
            mock_oauth_provider = Mock()
            mock_factory = Mock()
            mock_factory.create_provider.return_value = mock_oauth_provider
            MockFactory.return_value = mock_factory

            # Create backend with PostgreSQL URL
            _backend = GoogleDriveConnectorBackend(
                token_manager_db="postgresql://postgres:nexus@localhost:5432/nexus",
                user_email="test@example.com",
                root_folder="test_root",
                provider="google-drive",
            )

            # Verify TokenManager was created with db_url instead of db_path
            MockTM.assert_called_once_with(
                db_url="postgresql://postgres:nexus@localhost:5432/nexus"
            )

            # Verify provider was still registered
            mock_token_manager.register_provider.assert_called_once_with(
                "google-drive", mock_oauth_provider
            )

    def test_oauth_provider_registration_non_google_provider(self, mock_token_manager):
        """Test OAuth provider registration skips for non-Google providers."""
        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id_123",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret_456",
                },
            ),
            patch("nexus.server.auth.token_manager.TokenManager") as MockTM,
            patch("nexus.server.auth.google_oauth.GoogleOAuthProvider") as MockProvider,
        ):
            MockTM.return_value = mock_token_manager

            # Create backend with a non-google provider (not yet implemented)
            _backend = GoogleDriveConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@example.com",
                root_folder="test_root",
                provider="microsoft",  # Not supported yet
            )

            # Verify GoogleOAuthProvider was never created
            MockProvider.assert_not_called()

            # Verify provider was never registered
            mock_token_manager.register_provider.assert_not_called()

    def test_oauth_provider_registration_print_statements(self, mock_token_manager, capsys):
        """Test OAuth provider registration produces correct debug output."""
        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id_123",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret_456",
                },
            ),
            patch("nexus.server.auth.token_manager.TokenManager") as MockTM,
            patch("nexus.server.auth.oauth_factory.OAuthProviderFactory") as MockFactory,
        ):
            MockTM.return_value = mock_token_manager
            mock_oauth_provider = Mock()
            mock_factory = Mock()
            mock_factory.create_provider.return_value = mock_oauth_provider
            MockFactory.return_value = mock_factory

            # Create backend
            _backend = GoogleDriveConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@example.com",
                root_folder="test_root",
                provider="google-drive",
            )

            # Verify debug output was printed
            captured = capsys.readouterr()
            assert "[GDRIVE-INIT]" in captured.out
            assert (
                "✓ Registered OAuth provider" in captured.out
                or "Registered OAuth provider" in captured.out
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
